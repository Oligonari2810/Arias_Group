"""Tests for app.estimate_containers — container optimiser.

Signature: estimate_containers(pallets_logistic, weight_kg, family_breakdown)
  -> dict | None

Priority rules encoded in app.py:604-613:
  * only {'PLACAS'}                 → try order ['20', '40', '40HC']
  * 'PERFILES' in families          → try order ['40HC', '40']  (no 20ft)
  * otherwise                        → try order ['40HC', '40', '20']
If cargo fits in a single container in priority order, return it. Otherwise
pick the container type that needs fewest units, tie-breaking by higher score.
"""
from app import estimate_containers


def test_zero_pallets_and_weight_returns_none():
    assert estimate_containers(0, 0, None) is None


def test_small_placas_only_fits_in_20ft():
    r = estimate_containers(pallets_logistic=8, weight_kg=18000,
                            family_breakdown={'PLACAS': 3})
    assert r is not None
    assert r['type_key'] == '20'
    assert r['units'] == 1


def test_perfiles_never_uses_20ft():
    # Small cargo that would fit in a 20ft, but PERFILES is present so the
    # optimiser is forbidden from picking '20'.
    r = estimate_containers(pallets_logistic=8, weight_kg=18000,
                            family_breakdown={'PLACAS': 2, 'PERFILES': 1})
    assert r is not None
    assert r['type_key'] in ('40', '40HC')


def test_mixed_families_prefers_40hc_first():
    r = estimate_containers(pallets_logistic=20, weight_kg=25000,
                            family_breakdown={'PLACAS': 2, 'TORNILLOS': 1})
    assert r is not None
    # 20 pallets ≤ 24 (40HC) and 25000 ≤ 26500 → fits in a single 40HC.
    assert r['type_key'] == '40HC'
    assert r['units'] == 1


def test_exact_fit_40ft():
    r = estimate_containers(pallets_logistic=20, weight_kg=26500,
                            family_breakdown={'PLACAS': 5})
    assert r is not None
    # 40HC also fits (24 >= 20, 26500 == 26500), and is first in mixed order
    # but with only PLACAS the order is ['20','40','40HC']; 20ft rejects by pallets,
    # 40ft accepts (20 pallets == 20, 26500 == 26500).
    assert r['type_key'] == '40'


def test_very_large_cargo_returns_multi_unit_best_fit():
    # 100 pallets is way beyond any single container.
    r = estimate_containers(pallets_logistic=100, weight_kg=100000,
                            family_breakdown={'PLACAS': 10})
    assert r is not None
    assert r['units'] >= 2
    # Should favour '40HC' with PLACAS present? Actually only PLACAS → order is
    # ['20','40','40HC'] and the best-by-score branch picks the one needing
    # fewest units. 100/24 ≈ 5, 100/20 = 5, 100/10 = 10 → '40HC' or '40' wins.
    assert r['type_key'] in ('40', '40HC')


def test_family_breakdown_none_defaults_to_mixed_rules():
    # None should behave like 'mixto' → order ['40HC', '40', '20']
    r = estimate_containers(pallets_logistic=5, weight_kg=5000,
                            family_breakdown=None)
    assert r is not None
    assert r['type_key'] == '40HC'


def test_pallets_only_no_weight():
    r = estimate_containers(pallets_logistic=5, weight_kg=0,
                            family_breakdown={'PLACAS': 1})
    assert r is not None
    assert r['type_key'] == '20'


def test_weight_only_no_pallets():
    r = estimate_containers(pallets_logistic=0, weight_kg=15000,
                            family_breakdown={'PLACAS': 1})
    assert r is not None
    # 0 pallets ≤ 10 and 15000 ≤ 21500 → 20ft
    assert r['type_key'] == '20'


def test_result_contains_recommendation_metadata():
    r = estimate_containers(pallets_logistic=8, weight_kg=18000,
                            family_breakdown={'PLACAS': 1})
    assert set(r.keys()) >= {
        'type_key', 'recommended', 'units',
        'pallet_occupancy', 'weight_occupancy', 'score',
    }


def test_at_boundary_pallet_capacity_20ft():
    # Exactly at 20ft capacity boundary: 10 pallets + weight within limit
    r = estimate_containers(pallets_logistic=10, weight_kg=21500,
                            family_breakdown={'PLACAS': 1})
    assert r is not None
    assert r['type_key'] == '20'
    assert r['units'] == 1


def test_just_over_20ft_weight_bumps_to_next_tier():
    # 10 pallets (fits 20ft by pallets) but 22000 kg (> 21500) should not fit
    # 20ft; falls to 40ft.
    r = estimate_containers(pallets_logistic=10, weight_kg=22000,
                            family_breakdown={'PLACAS': 1})
    assert r is not None
    assert r['type_key'] != '20'
