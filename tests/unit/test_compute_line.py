"""Tests for app.compute_line — per-line calc with weight, pallets, cost, alerts.

Signature: compute_line(prod: dict | sqlite3.Row, qty: float) -> dict.
Returns a dict with keys: ok, sku, name, family, unit, qty_input, units,
price_unit_eur, m2_total, weight_total_kg, pallets_theoretical,
pallets_logistic, cost_exw_eur, alerts.
"""
import math
import sqlite3

import pytest

from app import compute_line


# --- Happy path ---------------------------------------------------------

def test_compute_line_happy_path_placa(product_factory):
    prod = product_factory()  # default: price 4.20, kg 8.5, upp 50, sqm_pp 60
    result = compute_line(prod, qty=100)

    assert result['ok'] is True
    assert result['sku'] == 'TEST-001'
    assert result['family'] == 'PLACAS'
    # 100 boards × (60/50) sqm each = 120 sqm
    assert result['m2_total'] == 120.0
    # 100 × 8.5 = 850 kg
    assert result['weight_total_kg'] == 850.0
    # 100/50 = 2.0 pallets exactly
    assert result['pallets_theoretical'] == 2.0
    assert result['pallets_logistic'] == 2
    # 100 × 4.20 = 420 €
    assert result['cost_exw_eur'] == 420.0
    assert result['alerts'] == []


def test_compute_line_pallets_logistic_is_ceil(product_factory):
    prod = product_factory()  # 50 upp
    # 75 boards → 1.5 pallets theoretical → 2 pallets logistic
    result = compute_line(prod, qty=75)
    assert result['pallets_theoretical'] == 1.5
    assert result['pallets_logistic'] == 2


# --- Alertas ------------------------------------------------------------

def test_compute_line_missing_price_triggers_alert(product_factory):
    prod = product_factory(unit_price_eur=0)
    result = compute_line(prod, qty=10)
    assert any('falta precio unitario' in a for a in result['alerts'])


def test_compute_line_placas_missing_upp_triggers_alert(product_factory):
    prod = product_factory(units_per_pallet=0)
    result = compute_line(prod, qty=10)
    assert any('falta unidades/palé' in a for a in result['alerts'])


def test_compute_line_placas_missing_kg_unit_triggers_alert(product_factory):
    prod = product_factory(kg_per_unit=0)
    result = compute_line(prod, qty=10)
    assert any('falta peso unitario' in a for a in result['alerts'])


def test_compute_line_tornillos_missing_kg_has_softer_alert(product_factory):
    prod = product_factory(
        category='tornillos', unit='ud',
        kg_per_unit=0, units_per_pallet=0, sqm_per_pallet=0,
    )
    result = compute_line(prod, qty=500)
    assert any('peso total = 0' in a for a in result['alerts'])
    assert result['weight_total_kg'] == 0.0


# --- Edge cases ---------------------------------------------------------

def test_compute_line_zero_qty_yields_zero_totals(product_factory):
    result = compute_line(product_factory(), qty=0)
    assert result['weight_total_kg'] == 0.0
    assert result['cost_exw_eur'] == 0.0
    assert result['pallets_theoretical'] == 0.0
    assert result['pallets_logistic'] == 0


def test_compute_line_accepts_sqlite_row():
    """prod is often a sqlite3.Row in production — compute_line must cast it.

    Uses a standalone in-memory SQLite (no Flask context needed); this verifies
    compute_line handles the Row type regardless of which connection created it.
    """
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE p (sku TEXT, name TEXT, category TEXT, unit TEXT, '
                 'unit_price_eur REAL, kg_per_unit REAL, units_per_pallet REAL, '
                 'sqm_per_pallet REAL)')
    conn.execute("INSERT INTO p VALUES ('X', 'N', 'placas', 'board', 1.0, 1.0, 10, 10)")
    row = conn.execute('SELECT * FROM p').fetchone()
    result = compute_line(row, qty=20)
    assert result['ok'] is True
    assert result['cost_exw_eur'] == 20.0


def test_compute_line_unit_m2_sets_m2_total_directly(product_factory):
    prod = product_factory(unit='m2', sqm_per_pallet=0, units_per_pallet=0)
    result = compute_line(prod, qty=35)
    # unit in ('m2', 'm²') → m2_total = qty
    assert result['m2_total'] == 35.0


def test_compute_line_unit_ml_does_not_compute_m2(product_factory):
    prod = product_factory(unit='ml', sqm_per_pallet=0, units_per_pallet=0)
    result = compute_line(prod, qty=100)
    assert result['m2_total'] == 0.0


def test_compute_line_units_rounding_per_unit_type(product_factory):
    # unit='board' rounds up via math.ceil
    prod = product_factory()
    result = compute_line(prod, qty=12.3)
    assert result['units'] == math.ceil(12.3) == 13


def test_compute_line_units_m2_keeps_decimals(product_factory):
    prod = product_factory(unit='m2', sqm_per_pallet=0, units_per_pallet=0)
    # round(12.345, 2) in CPython 3.9+ yields 12.35 (banker's rounding rounds
    # .5 to nearest even; here the representable float is slightly >12.345 so
    # it rounds up regardless).
    result = compute_line(prod, qty=12.345)
    assert result['units'] == 12.35
