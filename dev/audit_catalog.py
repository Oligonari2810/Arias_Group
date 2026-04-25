#!/usr/bin/env python3
"""Auditoría de calidad del catálogo (products).

Uso:
    .venv/bin/python dev/audit_catalog.py [path_a_arias.db]

Si no se pasa ruta, intenta ./instance/arias.db.

Reglas evaluadas:
    1) Coherencia descuento Arias: precio_arias_eur_unit ≈ pvp_eur_unit × 0,475
    2) kg_per_unit > 0 y no marcado como [peso estimado]
    3) m²/palé NULL/0 para familias no planares (cintas, tornillos, pastas)
    4) units_per_pallet plausible por familia
    5) PVP fuera de distribución dentro de la familia (posibles typos)
    6) Unidades inconsistentes (ud / Ud / unit / etc.)
    7) Duplicados de SKU o nombre
    8) Cache pvp_per_m2 / precio_arias_m2 desfasados respecto a unit price

El script NO modifica la DB. Solo imprime un informe.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_DB = Path('instance/arias.db')

# Tolerancia para igualdades flotantes (céntimos / kg / m²).
EPS_EUR = 0.01
EPS_RATIO = 0.001
EPS_M2 = 0.001

NON_PLANAR_FAMILIES = {'CINTAS', 'TORNILLOS', 'PASTAS', 'ACCESORIOS', 'TRAMPILLAS'}
EXPECTED_UNIT_TOKENS = {'ud', 'kg', 'rollo', 'saco', 'bolsa', 'caja', 'm', 'm²', 'l'}


def load_products(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT * FROM products ORDER BY category, sku').fetchall()
    conn.close()
    return rows


def fmt_money(v):
    return f'{v:.2f}€' if v is not None else '—'


def section(title: str, items: list, width: int = 80) -> None:
    bar = '─' * width
    print(f'\n{bar}\n{title}  ({len(items)})\n{bar}')
    if not items:
        print('  ✓ ninguna anomalía')
        return
    for line in items:
        print(f'  {line}')


def audit(db_path: Path) -> int:
    rows = load_products(db_path)
    print(f'\n📋 Catálogo: {db_path}\n   Total productos: {len(rows)}')
    by_family = defaultdict(list)
    for r in rows:
        by_family[r['category']].append(r)
    print('   Por familia:', ', '.join(f'{k}={len(v)}' for k, v in sorted(by_family.items())))

    # ── Regla 1: descuento Arias ─────────────────────────────────────
    desc_dev = []
    for r in rows:
        pvp = r['pvp_eur_unit'] or 0
        arias = r['precio_arias_eur_unit'] or 0
        if pvp <= 0:
            continue
        # base = pvp × (1 - desc/100) × (1 - extra/100). Default 50+5 → 0,475.
        d = (r['discount_pct'] or 50) / 100
        e = (r['discount_extra_pct'] or 0) / 100
        expected = round(pvp * (1 - d) * (1 - e), 2)
        if abs(arias - expected) > EPS_EUR:
            desc_dev.append(
                f'{r["sku"]:<14} {r["category"]:<12} PVP={fmt_money(pvp)} '
                f'Arias={fmt_money(arias)} esperado={fmt_money(expected)} '
                f'desv={arias - expected:+.2f}€  (desc={r["discount_pct"]} extra={r["discount_extra_pct"]})'
            )
    section('🔴 1. Descuento Arias desviado de PVP × (1-desc) × (1-extra)', desc_dev)

    # ── Regla 2: kg/ud ───────────────────────────────────────────────
    kg_zero = []
    kg_estimated = []
    for r in rows:
        kg = r['kg_per_unit'] or 0
        notes = (r['notes'] or '').lower()
        if kg <= 0:
            kg_zero.append(f'{r["sku"]:<14} {r["category"]:<12} {r["name"][:50]}')
        elif 'peso estimado' in notes or '[estimado]' in notes:
            kg_estimated.append(
                f'{r["sku"]:<14} {r["category"]:<12} kg={kg:>7.3f}  {r["name"][:45]}'
            )
    section('🔴 2a. kg_per_unit = 0 (logística no puede imputar coste)', kg_zero)
    section('🟡 2b. kg_per_unit marcado [peso estimado] (pendiente confirmar Fassa)', kg_estimated)

    # ── Regla 3: m²/palé en familias no planares ─────────────────────
    m2_in_non_planar = []
    for r in rows:
        m2 = r['sqm_per_pallet'] or 0
        if r['category'] in NON_PLANAR_FAMILIES and m2 > EPS_M2:
            m2_in_non_planar.append(
                f'{r["sku"]:<14} {r["category"]:<12} m²/palé={m2:.3f}  {r["name"][:40]}'
            )
    section('🔴 3. m²/palé en familia no planar (debe ser NULL/0)', m2_in_non_planar)

    # ── Regla 4: units_per_pallet implausible ────────────────────────
    upp_zero = []
    upp_too_high = []
    upp_too_low = []
    for r in rows:
        upp = r['units_per_pallet'] or 0
        if r['category'] in ('PLACAS', 'PERFILES', 'PASTAS') and upp <= 0:
            upp_zero.append(f'{r["sku"]:<14} {r["category"]:<12} upp=0  {r["name"][:40]}')
        # Heurísticas grossas: tornillos/cintas suelen ser cajas, palé real ronda 100-1500.
        if r['category'] in ('TORNILLOS', 'CINTAS') and 0 < upp < 50:
            upp_too_low.append(
                f'{r["sku"]:<14} {r["category"]:<12} upp={upp:>5.0f}  ← probable uds/caja en lugar de uds/palé'
            )
        if r['category'] in ('PLACAS',) and upp > 100:
            upp_too_high.append(
                f'{r["sku"]:<14} {r["category"]:<12} upp={upp:>5.0f}  ← muy alto para palé de placas'
            )
    section('🔴 4a. units_per_pallet = 0 en familia que debería tener palé fijo', upp_zero)
    section('🟡 4b. units_per_pallet sospechosamente bajo (¿uds/caja?)', upp_too_low)
    section('🟡 4c. units_per_pallet sospechosamente alto', upp_too_high)

    # ── Regla 5: PVP outliers por familia ────────────────────────────
    outliers = []
    for fam, items in by_family.items():
        prices = [(r['pvp_eur_unit'] or 0) for r in items if (r['pvp_eur_unit'] or 0) > 0]
        if len(prices) < 5:
            continue
        median = statistics.median(prices)
        for r in items:
            p = r['pvp_eur_unit'] or 0
            if p <= 0:
                continue
            ratio = p / median if median else 0
            if ratio > 10 or ratio < 0.1:
                outliers.append(
                    f'{r["sku"]:<14} {fam:<12} PVP={fmt_money(p)} '
                    f'(mediana familia={fmt_money(median)}, ratio={ratio:.2f}×)'
                )
    section('🟡 5. PVP fuera de distribución (posible typo)', outliers)

    # ── Regla 6: unidades inconsistentes ─────────────────────────────
    unit_counts = defaultdict(list)
    for r in rows:
        u = (r['unit'] or '').strip()
        unit_counts[u].append(r['sku'])
    rare_units = []
    for u, skus in unit_counts.items():
        norm = u.lower()
        if not u:
            rare_units.append(f'unit vacía: {len(skus)} SKUs ({", ".join(skus[:3])}…)')
        elif u != norm and any(other.lower() == norm for other in unit_counts if other != u):
            rare_units.append(f'unit con mayúsculas {u!r}: {len(skus)} SKUs (existe también {norm!r})')
    section('🟡 6. Unidades inconsistentes', rare_units)

    # ── Regla 7: duplicados ──────────────────────────────────────────
    name_counts = defaultdict(list)
    for r in rows:
        name_counts[r['name'].strip().lower()].append(r['sku'])
    dup_names = [
        f'{n!r}: SKUs {skus}' for n, skus in name_counts.items() if len(skus) > 1
    ]
    section('🔴 7. Nombres duplicados', dup_names)

    # ── Regla 8: cache columns desfasados ────────────────────────────
    cache_drift = []
    for r in rows:
        pvp = r['pvp_eur_unit'] or 0
        upp = r['units_per_pallet'] or 0
        sqm = r['sqm_per_pallet'] or 0
        cached = r['pvp_per_m2'] or 0
        if pvp > 0 and upp > 0 and sqm > 0 and cached > 0:
            expected = pvp * upp / sqm
            if abs(expected - cached) / max(expected, 1e-9) > 0.02:
                cache_drift.append(
                    f'{r["sku"]:<14} {r["category"]:<12} '
                    f'pvp_per_m2 cache={cached:.2f} esperado={expected:.2f} '
                    f'desv={cached - expected:+.2f}€/m²'
                )
    section('🟡 8. Cache pvp_per_m2 desfasado respecto a PVP × upp / m²/palé', cache_drift)

    # ── Resumen final ────────────────────────────────────────────────
    print('\n' + '═' * 80)
    print('RESUMEN — anomalías por gravedad:')
    critical = len(desc_dev) + len(kg_zero) + len(m2_in_non_planar) + len(upp_zero) + len(dup_names)
    warning = len(kg_estimated) + len(upp_too_low) + len(upp_too_high) + len(outliers) + len(rare_units) + len(cache_drift)
    print(f'  🔴 Críticas  (corregir): {critical}')
    print(f'  🟡 Warnings (revisar):   {warning}')
    print('═' * 80)
    return 0 if critical == 0 else 1


def main():
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    if not db.exists():
        print(f'❌ DB no encontrada: {db}\n'
              f'   Copia /Users/olivergonzalezarias/Arias_Group/instance/arias.db a {db}')
        sys.exit(2)
    sys.exit(audit(db))


if __name__ == '__main__':
    main()
