"""Migrate Arias_Group SQLite data to PostgreSQL.

Usage:
    python scripts/migrate_sqlite_to_postgres.py \\
        --sqlite ./fassa_ops.db \\
        --postgres 'postgresql+psycopg://arias:arias@localhost:5434/arias_dev' \\
        [--dry-run] [--truncate]

- Idempotent: re-running skips rows whose PK already exists (ON CONFLICT DO
  NOTHING). Safe to run repeatedly.
- Preserves IDs.  After inserts, advances each IDENTITY sequence to
  MAX(id)+1 so subsequent app-driven inserts don't collide.
- Converts types: REAL→Numeric, TEXT-ISO→TIMESTAMPTZ, TEXT-JSON→JSONB.
- Reports per-table: rows read / inserted / skipped.
- --dry-run: prints the plan without writing.
- --truncate: DESTRUCTIVE. Only for dev. Wipes Postgres tables first.

Enums (project_stage_enum, go_no_go_enum, incoterm_enum,
offer_status_enum, user_role_enum) expect specific values. Rows with
unknown values on those columns are logged and coerced to safe defaults
(see _coerce_enum).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


# ---------------------------------------------------------------------------
# Ordered list of tables respecting FK dependencies.

TABLES_IN_ORDER = [
    'clients',
    'users',
    'systems',
    'products',
    'system_components',
    'projects',
    'project_quotes',
    'stage_events',
    'shipping_routes',
    'customs_rates',
    'fx_rates',
    'pending_offers',
    'order_lines',
    'audit_log',
    'doc_sequences',
    'family_defaults',
    'price_history',
    'app_settings',
]


# ---------------------------------------------------------------------------
# Type converters — SQLite type → Postgres-friendly Python value.

def _iso_to_tstz(value: Any) -> datetime | None:
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso_to_date(value: Any):
    dt = _iso_to_tstz(value)
    return dt.date() if dt else None


def _jsonb(value: Any):
    """Accept a Python primitive, a JSON string, or a list/dict."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            json.loads(value)
            return value         # already valid JSON
        except json.JSONDecodeError:
            return json.dumps(value)   # stuff opaque text in as JSON string
    return json.dumps(value)


def _decimal(value: Any):
    if value is None or value == '':
        return None
    return Decimal(str(value))


_PROJECT_STAGES = {
    'CLIENTE', 'OPORTUNIDAD', 'FILTRO GO / NO-GO', 'PRE-CÁLCULO RÁPIDO',
    'CÁLCULO DETALLADO', 'OFERTA V1/V2', 'VALIDACIÓN TÉCNICA',
    'VALIDACIÓN CLIENTE', 'CIERRE', 'CONTRATO + CONDICIONES',
    'PREPAGO VALIDADO', 'ORDEN BLOQUEADA', 'CHECK INTERNO',
    'LOGÍSTICA VALIDADA', 'BOOKING NAVIERA', 'PEDIDO A FASSA',
    'CONFIRMACIÓN FÁBRICA', 'READY DATE', 'EXPEDICIÓN (BL)',
    'TRACKING + CONTROL ETA', 'ADUANA',
    'LIQUIDACIÓN ADUANERA + COSTES FINALES',
    'INSPECCIÓN / CONTROL DAÑOS', 'ENTREGA', 'POSTVENTA',
    'RECOMPRA / REFERIDOS / ESCALA',
}
_GO_NO_GO = {'PENDING', 'GO', 'NO_GO'}
_INCOTERMS = {'EXW', 'FOB', 'CIF', 'DAP', 'CPT', 'DDP'}
_OFFER_STATUS = {'pending', 'sent', 'accepted', 'rejected', 'expired'}
_USER_ROLES = {'admin', 'viewer', 'sales', 'warehouse', 'accountant'}


def _coerce_enum(value, allowed: set[str], default: str, warn_label: str, warnings: list[str]):
    if value is None:
        return None
    if value in allowed:
        return value
    warnings.append(f'{warn_label}: unknown value {value!r}, coerced to {default!r}')
    return default


# ---------------------------------------------------------------------------
# Per-table column specs.  Each entry maps postgres column -> (sqlite column,
# converter function or None).  None = copy verbatim.

@dataclass
class TableSpec:
    name: str
    columns: list[tuple[str, str, Callable[[Any], Any] | None]] = field(default_factory=list)
    enum_fixups: list[tuple[str, set[str], str, str]] = field(default_factory=list)
    # column -> (allowed_values, default, label)


def _specs(warnings: list[str]) -> dict[str, TableSpec]:
    def enum_conv(column: str, allowed: set[str], default: str, label: str):
        def conv(value):
            return _coerce_enum(value, allowed, default, label, warnings)
        return conv

    return {
        'clients': TableSpec('clients', [
            ('id', 'id', None),
            ('name', 'name', None),
            ('company', 'company', None),
            ('rnc', 'rnc', None),
            ('email', 'email', None),
            ('phone', 'phone', None),
            ('address', 'address', None),
            ('country', 'country', None),
            ('score', 'score', None),
            ('created_at', 'created_at', _iso_to_tstz),
        ]),
        'products': TableSpec('products', [
            ('id', 'id', None),
            ('sku', 'sku', None),
            ('name', 'name', None),
            ('category', 'category', None),
            ('subfamily', 'subfamily', None),
            ('source_catalog', 'source_catalog', None),
            ('unit', 'unit', None),
            ('unit_price_eur', 'unit_price_eur', _decimal),
            ('kg_per_unit', 'kg_per_unit', _decimal),
            ('units_per_pallet', 'units_per_pallet', _decimal),
            ('sqm_per_pallet', 'sqm_per_pallet', _decimal),
            ('notes', 'notes', None),
            ('pvp_per_m2', 'pvp_per_m2', _decimal),
            ('precio_arias_m2', 'precio_arias_m2', _decimal),
            ('content_per_unit', 'content_per_unit', None),
            ('pack_size', 'pack_size', None),
            ('pvp_eur_unit', 'pvp_eur_unit', _decimal),
            ('precio_arias_eur_unit', 'precio_arias_eur_unit', _decimal),
            ('discount_pct', 'discount_pct', _decimal),
        ]),
        'systems': TableSpec('systems', [
            ('id', 'id', None),
            ('name', 'name', None),
            ('description', 'description', None),
            ('default_waste_pct', 'default_waste_pct', _decimal),
        ]),
        'system_components': TableSpec('system_components', [
            ('id', 'id', None),
            ('system_id', 'system_id', None),
            ('product_id', 'product_id', None),
            ('consumption_per_sqm', 'consumption_per_sqm', _decimal),
            ('waste_pct', 'waste_pct', _decimal),
        ]),
        'projects': TableSpec('projects', [
            ('id', 'id', None),
            ('client_id', 'client_id', None),
            ('name', 'name', None),
            ('project_type', 'project_type', None),
            ('location', 'location', None),
            ('area_sqm', 'area_sqm', _decimal),
            ('stage', 'stage', enum_conv('stage', _PROJECT_STAGES, 'OPORTUNIDAD', 'projects.stage')),
            ('go_no_go', 'go_no_go', enum_conv('go_no_go', _GO_NO_GO, 'PENDING', 'projects.go_no_go')),
            ('incoterm', 'incoterm', enum_conv('incoterm', _INCOTERMS, 'EXW', 'projects.incoterm')),
            ('fx_rate', 'fx_rate', _decimal),
            ('target_margin_pct', 'target_margin_pct', _decimal),
            ('freight_eur', 'freight_eur', _decimal),
            ('customs_pct', 'customs_pct', _decimal),
            ('logistics_notes', 'logistics_notes', None),
            ('created_at', 'created_at', _iso_to_tstz),
        ]),
        'project_quotes': TableSpec('project_quotes', [
            ('id', 'id', None),
            ('project_id', 'project_id', None),
            ('system_id', 'system_id', None),
            ('version_label', 'version_label', None),
            ('area_sqm', 'area_sqm', _decimal),
            ('fx_rate', 'fx_rate', _decimal),
            ('freight_eur', 'freight_eur', _decimal),
            ('customs_pct', 'customs_pct', _decimal),
            ('target_margin_pct', 'target_margin_pct', _decimal),
            ('result_json', 'result_json', _jsonb),
            ('created_at', 'created_at', _iso_to_tstz),
        ]),
        'stage_events': TableSpec('stage_events', [
            ('id', 'id', None),
            ('project_id', 'project_id', None),
            ('from_stage', 'from_stage',
             enum_conv('from_stage', _PROJECT_STAGES, 'OPORTUNIDAD', 'stage_events.from_stage')),
            ('to_stage', 'to_stage',
             enum_conv('to_stage', _PROJECT_STAGES, 'OPORTUNIDAD', 'stage_events.to_stage')),
            ('note', 'note', None),
            ('created_at', 'created_at', _iso_to_tstz),
        ]),
        'shipping_routes': TableSpec('shipping_routes', [
            ('id', 'id', None),
            ('origin_port', 'origin_port', None),
            ('destination_port', 'destination_port', None),
            ('carrier', 'carrier', None),
            ('transit_days', 'transit_days', None),
            ('container_20_eur', 'container_20_eur', _decimal),
            ('container_40_eur', 'container_40_eur', _decimal),
            ('container_40hc_eur', 'container_40hc_eur', _decimal),
            ('insurance_pct', 'insurance_pct', _decimal),
            ('valid_from', 'valid_from', _iso_to_date),
            ('valid_until', 'valid_until', _iso_to_date),
            ('notes', 'notes', None),
        ]),
        'customs_rates': TableSpec('customs_rates', [
            ('id', 'id', None),
            ('country', 'country', None),
            ('hs_code', 'hs_code', None),
            ('category', 'category', None),
            ('dai_pct', 'dai_pct', _decimal),
            ('itbis_pct', 'itbis_pct', _decimal),
            ('other_pct', 'other_pct', _decimal),
            ('notes', 'notes', None),
        ]),
        'fx_rates': TableSpec('fx_rates', [
            ('id', 'id', None),
            ('base_currency', 'base_currency', None),
            ('target_currency', 'target_currency', None),
            ('rate', 'rate', _decimal),
            ('updated_at', 'updated_at', _iso_to_tstz),
            ('source', 'source', None),
        ]),
        'users': TableSpec('users', [
            ('id', 'id', None),
            ('username', 'username', None),
            ('password_hash', 'password_hash', None),
            ('role', 'role', enum_conv('role', _USER_ROLES, 'viewer', 'users.role')),
            ('full_name', 'full_name', None),
            ('email', 'email', None),
            ('created_at', 'created_at', _iso_to_tstz),
        ]),
        'pending_offers': TableSpec('pending_offers', [
            ('id', 'id', None),
            ('offer_number', 'offer_number', None),
            ('client_name', 'client_name', None),
            ('project_name', 'project_name', None),
            ('waste_pct', 'waste_pct', _decimal),
            ('margin_pct', 'margin_pct', _decimal),
            ('fx_rate', 'fx_rate', _decimal),
            ('lines_json', 'lines_json', _jsonb),
            ('total_product_eur', 'total_product_eur', _decimal),
            ('total_logistic_eur', 'total_logistic_eur', _decimal),
            ('total_final_eur', 'total_final_eur', _decimal),
            ('status', 'status', enum_conv('status', _OFFER_STATUS, 'pending', 'pending_offers.status')),
            ('incoterm', 'incoterm', enum_conv('incoterm', _INCOTERMS, 'EXW', 'pending_offers.incoterm')),
            ('route_id', 'route_id', None),
            ('container_count', 'container_count', None),
            ('validity_days', 'validity_days', None),
            ('client_id', 'client_id', None),
            ('raw_hash', 'raw_hash', None),
            ('created_at', 'created_at', _iso_to_tstz),
            ('updated_at', 'updated_at', _iso_to_tstz),
        ]),
        'order_lines': TableSpec('order_lines', [
            ('id', 'id', None),
            ('offer_id', 'offer_id', None),
            ('sku', 'sku', None),
            ('name', 'name', None),
            ('family', 'family', None),
            ('unit', 'unit', None),
            ('qty_input', 'qty_input', _decimal),
            ('qty_logistic', 'qty_logistic', _decimal),
            ('price_unit_eur', 'price_unit_eur', _decimal),
            ('cost_exw_eur', 'cost_exw_eur', _decimal),
            ('m2_total', 'm2_total', _decimal),
            ('weight_total_kg', 'weight_total_kg', _decimal),
            ('pallets_theoretical', 'pallets_theoretical', _decimal),
            ('pallets_logistic', 'pallets_logistic', None),
            ('alerts_text', 'alerts_text', None),
            ('created_at', 'created_at', _iso_to_tstz),
        ]),
        'audit_log': TableSpec('audit_log', [
            ('id', 'id', None),
            ('offer_id', 'offer_id', None),
            ('action', 'action', None),
            ('detail', 'detail', _jsonb),
            ('username', 'username', None),
            ('created_at', 'created_at', _iso_to_tstz),
        ]),
        'doc_sequences': TableSpec('doc_sequences', [
            ('id', 'id', None),
            ('prefix', 'prefix', None),
            ('last_number', 'last_number', None),
        ]),
        'family_defaults': TableSpec('family_defaults', [
            ('category', 'category', None),
            ('discount_pct', 'discount_pct', _decimal),
            ('display_order', 'display_order', None),
            ('notes', 'notes', None),
        ]),
        'price_history': TableSpec('price_history', [
            ('id', 'id', None),
            ('product_id', 'product_id', None),
            ('field', 'field', None),
            ('old_value', 'old_value', _decimal),
            ('new_value', 'new_value', _decimal),
            ('user_id', 'user_id', None),
            ('username', 'username', None),
            ('changed_at', 'changed_at', _iso_to_tstz),
            ('notes', 'notes', None),
        ]),
        'app_settings': TableSpec('app_settings', [
            ('key', 'key', None),
            ('value', 'value', _jsonb),
            ('updated_at', 'updated_at', _iso_to_tstz),
        ]),
    }


# ---------------------------------------------------------------------------

def _sqlite_columns(sqlite_conn, table: str) -> set[str]:
    rows = sqlite_conn.execute(f'PRAGMA table_info({table})').fetchall()
    return {r[1] for r in rows}


def _load_rows(sqlite_conn, table: str) -> list[dict]:
    rows = sqlite_conn.execute(f'SELECT * FROM {table}').fetchall()
    return [dict(r) for r in rows]


def _convert_row(row: dict, spec: TableSpec, actual_sqlite_cols: set[str]) -> dict:
    """Return a dict keyed by Postgres column name, values converted."""
    out: dict[str, Any] = {}
    for pg_col, sqlite_col, conv in spec.columns:
        if sqlite_col not in actual_sqlite_cols:
            continue   # optional column missing in this SQLite snapshot
        value = row.get(sqlite_col)
        if conv is not None:
            value = conv(value)
        out[pg_col] = value
    return out


def _primary_key(table: str) -> str:
    # family_defaults uses category; app_settings uses key. Everything else uses id.
    if table == 'family_defaults':
        return 'category'
    if table == 'app_settings':
        return 'key'
    return 'id'


def _insert_rows(pg_conn, table: str, rows: list[dict], dry_run: bool) -> tuple[int, int]:
    if not rows:
        return 0, 0

    columns = list(rows[0].keys())
    placeholders = ', '.join(f':{c}' for c in columns)
    col_list = ', '.join(columns)
    pk = _primary_key(table)
    sql = (
        f'INSERT INTO {table} ({col_list}) '
        f'VALUES ({placeholders}) '
        f'ON CONFLICT ({pk}) DO NOTHING'
    )

    if dry_run:
        print(f'  DRY-RUN → {table}: would attempt {len(rows)} inserts')
        return len(rows), 0

    inserted = 0
    skipped = 0
    for row in rows:
        try:
            res = pg_conn.execute(text(sql), row)
            if res.rowcount == 0:
                skipped += 1
            else:
                inserted += 1
        except IntegrityError as exc:
            # FK violations etc.  Log and skip; caller can inspect.
            skipped += 1
            print(f'  {table} id={row.get(pk)!r}: {exc.orig}')
    return inserted, skipped


def _reset_sequence(pg_conn, table: str) -> None:
    """Set table id's identity sequence to MAX(id)+1 so new inserts don't collide."""
    if _primary_key(table) != 'id':
        return
    # Skip if table has no rows.
    max_id = pg_conn.execute(text(f'SELECT COALESCE(MAX(id), 0) FROM {table}')).scalar()
    if max_id is None:
        return
    # Find the underlying sequence for IDENTITY columns.
    seq_row = pg_conn.execute(text(
        "SELECT pg_get_serial_sequence(:t, 'id')"
    ), {'t': table}).scalar()
    if seq_row:
        pg_conn.execute(text(f"SELECT setval('{seq_row}', :v, true)"), {'v': max(int(max_id), 1)})


# ---------------------------------------------------------------------------

def migrate(sqlite_path: str, postgres_url: str, *, dry_run: bool, truncate: bool) -> int:
    from db import get_engine

    print(f'Source : {sqlite_path}')
    print(f'Target : {postgres_url}')
    print(f'Dry-run: {dry_run} | Truncate first: {truncate}\n')

    warnings: list[str] = []
    specs = _specs(warnings)

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row
    engine = get_engine(postgres_url)

    summary: list[tuple[str, int, int, int]] = []   # table, read, inserted, skipped

    with engine.begin() as pg_conn:
        if truncate:
            if dry_run:
                print('DRY-RUN → would TRUNCATE tables in reverse order\n')
            else:
                print('TRUNCATE-ing tables (dev only) ...')
                for table in reversed(TABLES_IN_ORDER):
                    pg_conn.execute(text(f'TRUNCATE TABLE {table} RESTART IDENTITY CASCADE'))
                print('done.\n')

        for table in TABLES_IN_ORDER:
            if table not in specs:
                print(f'  {table}: no spec, skipping')
                continue
            spec = specs[table]
            try:
                cols = _sqlite_columns(sqlite_conn, table)
            except sqlite3.OperationalError:
                print(f'  {table}: not present in SQLite, skipping')
                summary.append((table, 0, 0, 0))
                continue
            raw = _load_rows(sqlite_conn, table)
            converted = [_convert_row(r, spec, cols) for r in raw]
            inserted, skipped = _insert_rows(pg_conn, table, converted, dry_run)
            if not dry_run:
                _reset_sequence(pg_conn, table)
            print(f'  {table:<22} read={len(raw):>4}  inserted={inserted:>4}  skipped={skipped:>4}')
            summary.append((table, len(raw), inserted, skipped))

    sqlite_conn.close()

    print('\n--- Summary ---')
    total_read = sum(r for _, r, _, _ in summary)
    total_inserted = sum(i for _, _, i, _ in summary)
    total_skipped = sum(s for _, _, _, s in summary)
    print(f'Rows read     : {total_read}')
    print(f'Rows inserted : {total_inserted}')
    print(f'Rows skipped  : {total_skipped}')

    if warnings:
        print('\nWarnings:')
        for w in warnings[:20]:
            print(f'  * {w}')
        if len(warnings) > 20:
            print(f'  ... and {len(warnings) - 20} more')

    return 0 if not warnings else 0   # non-zero exit reserved for hard failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sqlite', required=True, help='Path to source SQLite file')
    parser.add_argument('--postgres', required=True, help='Postgres URL (SQLAlchemy format)')
    parser.add_argument('--dry-run', action='store_true', help='Print plan, do not write')
    parser.add_argument('--truncate', action='store_true',
                        help='DESTRUCTIVE: wipe Postgres tables before migrating')
    args = parser.parse_args(argv)

    return migrate(args.sqlite, args.postgres, dry_run=args.dry_run, truncate=args.truncate)


if __name__ == '__main__':
    sys.exit(main())
