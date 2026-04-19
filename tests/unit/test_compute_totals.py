"""Tests for app.compute_totals — aggregation over compute_line results."""
from app import compute_totals


def _line(**kw):
    """Build a minimal line matching compute_line output shape."""
    base = {
        'ok': True,
        'cost_exw_eur': 0.0,
        'weight_total_kg': 0.0,
        'm2_total': 0.0,
        'pallets_theoretical': 0.0,
        'pallets_logistic': 0,
        'family': 'PLACAS',
    }
    base.update(kw)
    return base


def test_empty_lines_yield_zero_totals_and_none_container():
    t = compute_totals([])
    assert t['cost_exw_eur'] == 0.0
    assert t['weight_total_kg'] == 0.0
    assert t['m2_total'] == 0.0
    assert t['pallets_theoretical'] == 0.0
    assert t['pallets_logistic'] == 0
    assert t['family_breakdown'] == {}
    assert t['containers'] is None


def test_compute_totals_sums_cost_weight_m2():
    lines = [
        _line(cost_exw_eur=100, weight_total_kg=50, m2_total=10),
        _line(cost_exw_eur=200, weight_total_kg=75, m2_total=20),
    ]
    t = compute_totals(lines)
    assert t['cost_exw_eur'] == 300.0
    assert t['weight_total_kg'] == 125.0
    assert t['m2_total'] == 30.0


def test_compute_totals_counts_family_breakdown():
    lines = [
        _line(family='PLACAS'),
        _line(family='PLACAS'),
        _line(family='PERFILES'),
    ]
    t = compute_totals(lines)
    assert t['family_breakdown'] == {'PLACAS': 2, 'PERFILES': 1}


def test_compute_totals_excludes_lines_with_ok_false():
    lines = [
        _line(ok=True, cost_exw_eur=100),
        _line(ok=False, cost_exw_eur=9999),
    ]
    t = compute_totals(lines)
    assert t['cost_exw_eur'] == 100.0


def test_pallets_logistic_is_ceil_of_sum_not_sum_of_ceils():
    # Two lines with 0.6 logistic each → sum 1.2 → ceil = 2 (not 1 + 1 = 2).
    # Shows the difference between theoretical (sum of floats) and logistic (ceil).
    lines = [
        _line(pallets_theoretical=0.6, pallets_logistic=1),
        _line(pallets_theoretical=0.6, pallets_logistic=1),
    ]
    t = compute_totals(lines)
    assert t['pallets_theoretical'] == 1.2
    # pallets_logistic in totals = ceil(sum of per-line logistic) = ceil(2) = 2
    assert t['pallets_logistic'] == 2


def test_compute_totals_invokes_container_estimation():
    lines = [
        _line(weight_total_kg=18000, pallets_logistic=8, family='PLACAS'),
    ]
    t = compute_totals(lines)
    assert t['containers'] is not None
    assert t['containers']['type_key'] == '20'


def test_compute_totals_unknown_family_key():
    lines = [_line(family=None)]
    t = compute_totals(lines)
    assert 'DESCONOCIDA' in t['family_breakdown']


def test_compute_totals_missing_fields_defaulted_to_zero():
    # A line missing keys should not crash _num coercion.
    lines = [{'ok': True, 'family': 'PLACAS'}]
    t = compute_totals(lines)
    assert t['cost_exw_eur'] == 0.0
    assert t['weight_total_kg'] == 0.0
