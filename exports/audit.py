"""Audita la calidad de datos para export a Odoo/NetSuite.

No modifica la DB. Reporta issues por tabla con severidad (error/warn/info).
ERROR = bloquea el import en el ERP destino.
WARN  = pasa el import pero queda incompleto.
INFO  = observación (ej. campo ausente pero opcional).

Uso:
    python -m exports.audit                  # reporte humano
    python -m exports.audit --json           # salida JSON (para CI/scripts)
    FASSA_DB_PATH=ruta.db python -m exports.audit
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path


# ISO-3166 alpha-2 mínimo para los países Caribe+Europa que toca esta app.
# Si aparece un país fuera de este set, la auditoría marca warn (necesita mapeo manual).
COUNTRY_TO_ISO2 = {
    'república dominicana': 'DO', 'republica dominicana': 'DO', 'dominicana': 'DO',
    'dominican republic': 'DO', 'rd': 'DO', 'do': 'DO',
    'haití': 'HT', 'haiti': 'HT', 'ht': 'HT',
    'puerto rico': 'PR', 'pr': 'PR',
    'jamaica': 'JM', 'jm': 'JM',
    'cuba': 'CU', 'cu': 'CU',
    'españa': 'ES', 'spain': 'ES', 'es': 'ES',
    'estados unidos': 'US', 'usa': 'US', 'united states': 'US', 'us': 'US',
    'méxico': 'MX', 'mexico': 'MX', 'mx': 'MX',
    'colombia': 'CO', 'co': 'CO',
    'panamá': 'PA', 'panama': 'PA', 'pa': 'PA',
}

VALID_OFFER_STATES = {'pending', 'approved', 'rejected', 'draft', 'sent', 'sale', 'done', 'cancel'}
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _connect() -> sqlite3.Connection:
    db_path = os.environ.get('FASSA_DB_PATH') or str(Path(__file__).resolve().parent.parent / 'fassa_ops.db')
    if not Path(db_path).exists():
        sys.exit(f'DB no existe: {db_path}')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _cols(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f'PRAGMA table_info({table})').fetchall()}


def _g(row: sqlite3.Row, key: str, default=None):
    """Acceso tolerante: la columna puede no existir en DBs con schema viejo."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def audit_clients(db: sqlite3.Connection) -> list[dict]:
    issues = []
    rows = db.execute('SELECT * FROM clients').fetchall()
    for r in rows:
        cid = r['id']
        name = _g(r, 'name') or ''
        if not name.strip():
            issues.append({'severity': 'error', 'table': 'clients', 'id': cid, 'field': 'name', 'msg': 'vacío (Odoo: required)'})
        if not (_g(r, 'rnc') or '').strip():
            issues.append({'severity': 'warn', 'table': 'clients', 'id': cid, 'field': 'rnc', 'msg': 'VAT faltante (Odoo: res.partner.vat)'})
        email = (_g(r, 'email') or '').strip()
        if email and not EMAIL_RE.match(email):
            issues.append({'severity': 'warn', 'table': 'clients', 'id': cid, 'field': 'email', 'msg': f'formato inválido: {email!r}'})
        country = (_g(r, 'country') or '').strip().lower()
        if not country:
            issues.append({'severity': 'warn', 'table': 'clients', 'id': cid, 'field': 'country', 'msg': 'país vacío (Odoo: country_id)'})
        elif country not in COUNTRY_TO_ISO2:
            issues.append({'severity': 'warn', 'table': 'clients', 'id': cid, 'field': 'country', 'msg': f'no mapeable a ISO-3166: {country!r}'})
    return issues


def audit_products(db: sqlite3.Connection) -> list[dict]:
    issues = []
    rows = db.execute('SELECT * FROM products').fetchall()
    skus_seen: dict[str, int] = {}
    for r in rows:
        pid = r['id']
        sku = (_g(r, 'sku') or '').strip()
        if not sku:
            issues.append({'severity': 'error', 'table': 'products', 'id': pid, 'field': 'sku', 'msg': 'SKU vacío (Odoo: default_code)'})
        elif sku in skus_seen:
            issues.append({'severity': 'error', 'table': 'products', 'id': pid, 'field': 'sku', 'msg': f'duplicado con id={skus_seen[sku]}'})
        else:
            skus_seen[sku] = pid
        price = _g(r, 'unit_price_eur')
        if price is None or price <= 0:
            issues.append({'severity': 'error', 'table': 'products', 'id': pid, 'field': 'unit_price_eur', 'msg': f'precio inválido: {price} (Odoo: list_price > 0)'})
        if not (_g(r, 'category') or '').strip():
            issues.append({'severity': 'warn', 'table': 'products', 'id': pid, 'field': 'category', 'msg': 'sin categoría (Odoo: categ_id)'})
        if not (_g(r, 'unit') or '').strip():
            issues.append({'severity': 'warn', 'table': 'products', 'id': pid, 'field': 'unit', 'msg': 'sin UoM (Odoo: product_uom)'})
    return issues


def audit_projects(db: sqlite3.Connection) -> list[dict]:
    issues = []
    rows = db.execute('SELECT p.*, c.id AS client_exists FROM projects p LEFT JOIN clients c ON c.id = p.client_id').fetchall()
    for r in rows:
        pid = r['id']
        if _g(r, 'client_exists') is None:
            issues.append({'severity': 'error', 'table': 'projects', 'id': pid, 'field': 'client_id', 'msg': f"client_id={_g(r, 'client_id')} orphan"})
        if not (_g(r, 'name') or '').strip():
            issues.append({'severity': 'error', 'table': 'projects', 'id': pid, 'field': 'name', 'msg': 'vacío'})
        area = _g(r, 'area_sqm') or 0
        if area <= 0:
            issues.append({'severity': 'info', 'table': 'projects', 'id': pid, 'field': 'area_sqm', 'msg': 'area_sqm=0 (no calculable)'})
    return issues


def audit_offers(db: sqlite3.Connection) -> list[dict]:
    issues = []
    rows = db.execute('SELECT * FROM pending_offers').fetchall()
    for r in rows:
        oid = r['id']
        status = (_g(r, 'status') or '').strip()
        if status not in VALID_OFFER_STATES:
            issues.append({'severity': 'warn', 'table': 'pending_offers', 'id': oid, 'field': 'status', 'msg': f'status no-canónico: {status!r}'})
        total = _g(r, 'total_final_eur') or 0
        if total <= 0:
            issues.append({'severity': 'warn', 'table': 'pending_offers', 'id': oid, 'field': 'total_final_eur', 'msg': f'total={total} (Odoo: sale.order.amount_total)'})
        try:
            lines = json.loads(_g(r, 'lines_json') or '[]')
            if not isinstance(lines, list) or not lines:
                issues.append({'severity': 'error', 'table': 'pending_offers', 'id': oid, 'field': 'lines_json', 'msg': 'sin líneas'})
        except json.JSONDecodeError as e:
            issues.append({'severity': 'error', 'table': 'pending_offers', 'id': oid, 'field': 'lines_json', 'msg': f'JSON inválido: {e}'})
        if not (_g(r, 'client_name') or '').strip():
            issues.append({'severity': 'error', 'table': 'pending_offers', 'id': oid, 'field': 'client_name', 'msg': 'cliente vacío'})
    return issues


def audit_order_lines(db: sqlite3.Connection) -> list[dict]:
    issues = []
    rows = db.execute(
        'SELECT ol.*, p.id AS product_exists, o.id AS offer_exists '
        'FROM order_lines ol '
        'LEFT JOIN products p ON p.sku = ol.sku '
        'LEFT JOIN pending_offers o ON o.id = ol.offer_id'
    ).fetchall()
    for r in rows:
        lid = r['id']
        if _g(r, 'offer_exists') is None:
            issues.append({'severity': 'error', 'table': 'order_lines', 'id': lid, 'field': 'offer_id', 'msg': f"offer_id={_g(r, 'offer_id')} orphan"})
        if _g(r, 'product_exists') is None:
            issues.append({'severity': 'warn', 'table': 'order_lines', 'id': lid, 'field': 'sku', 'msg': f"SKU {_g(r, 'sku')!r} no existe en products"})
        qty = _g(r, 'qty_input') or 0
        if qty <= 0:
            issues.append({'severity': 'error', 'table': 'order_lines', 'id': lid, 'field': 'qty_input', 'msg': f'qty={qty} (Odoo: product_uom_qty > 0)'})
    return issues


def _has_table(db: sqlite3.Connection, table: str) -> bool:
    return bool(db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def run_audit(db: sqlite3.Connection) -> dict:
    all_issues = []
    for table, fn in [
        ('clients', audit_clients),
        ('products', audit_products),
        ('projects', audit_projects),
        ('pending_offers', audit_offers),
        ('order_lines', audit_order_lines),
    ]:
        if _has_table(db, table):
            all_issues += fn(db)
        else:
            all_issues.append({'severity': 'warn', 'table': table, 'id': 0, 'field': '*', 'msg': 'tabla no existe en esta DB'})
    counts = {
        'error': sum(1 for i in all_issues if i['severity'] == 'error'),
        'warn':  sum(1 for i in all_issues if i['severity'] == 'warn'),
        'info':  sum(1 for i in all_issues if i['severity'] == 'info'),
    }
    totals = {}
    for t in ('clients', 'products', 'projects', 'pending_offers', 'order_lines'):
        totals[t] = db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] if _has_table(db, t) else None
    return {'totals': totals, 'counts': counts, 'issues': all_issues}


def print_human(report: dict) -> None:
    t = report['totals']
    c = report['counts']
    print('── AUDIT DE CALIDAD PARA EXPORT ODOO/NETSUITE ' + '─' * 30)
    print(f"Filas:  clients={t['clients']}  products={t['products']}  projects={t['projects']}  "
          f"offers={t['pending_offers']}  order_lines={t['order_lines']}")
    print(f"Issues: {c['error']} errores  {c['warn']} warnings  {c['info']} info")
    print('─' * 75)
    if not report['issues']:
        print('✓ DB limpia. Lista para exportar.')
        return
    current_table = None
    for i in sorted(report['issues'], key=lambda x: (x['table'], x['severity'], x['id'])):
        if i['table'] != current_table:
            current_table = i['table']
            print(f'\n[{current_table}]')
        marker = {'error': '✗', 'warn': '!', 'info': '·'}[i['severity']]
        print(f"  {marker} id={i['id']:<4} {i['field']:<22} {i['msg']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', action='store_true', help='salida JSON en vez de texto')
    args = ap.parse_args()
    with _connect() as db:
        report = run_audit(db)
    if args.json:
        json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
        print()
    else:
        print_human(report)
    # Exit code: 1 si hay errors, 0 si solo warns/info.
    return 1 if report['counts']['error'] > 0 else 0


if __name__ == '__main__':
    sys.exit(main())
