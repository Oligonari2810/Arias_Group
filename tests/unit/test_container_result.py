"""Tests for app._container_result — normalised container-fit descriptor.

Signature: _container_result(key, units, pallets, weight) -> dict.
Keys: type_key, recommended, units, pallets_capacity_per_unit,
weight_capacity_per_unit_kg, pallet_occupancy, weight_occupancy, score.

CONTAINERS (app.py:486-490):
  '20'   -> pallets=10,  kg=21500
  '40'   -> pallets=20,  kg=26500
  '40HC' -> pallets=24,  kg=26500
"""
from app import _container_result


def test_container_20_single_unit_full():
    # 10 pallets, 21500 kg → exactly full for 20ft
    r = _container_result('20', units=1, pallets=10, weight=21500)
    assert r['type_key'] == '20'
    assert r['recommended'] == "20'"
    assert r['pallet_occupancy'] == 1.0
    assert r['weight_occupancy'] == 1.0
    assert r['score'] == 2.0


def test_container_40hc_partial_load():
    r = _container_result('40HC', units=1, pallets=12, weight=13250)
    assert r['recommended'] == '40HC'
    assert r['pallet_occupancy'] == 0.5   # 12/24
    assert r['weight_occupancy'] == 0.5   # 13250/26500
    assert r['score'] == 1.0


def test_container_zero_units_yields_zero_occupancy():
    # units=0 should not crash and should yield zero ratios
    r = _container_result('40', units=0, pallets=0, weight=0)
    assert r['pallet_occupancy'] == 0
    assert r['weight_occupancy'] == 0
    assert r['score'] == 0.0


def test_container_result_structure_contains_all_keys():
    r = _container_result('40', units=2, pallets=15, weight=20000)
    required = {
        'type_key', 'recommended', 'units',
        'pallets_capacity_per_unit', 'weight_capacity_per_unit_kg',
        'pallet_occupancy', 'weight_occupancy', 'score',
    }
    assert required.issubset(r.keys())


def test_container_result_multi_unit_occupancy_is_per_unit():
    # 2 units carrying 15 pallets total → 7.5 pallets/unit, 7.5/20 = 0.375
    r = _container_result('40', units=2, pallets=15, weight=20000)
    assert r['units'] == 2
    assert r['pallet_occupancy'] == round(7.5 / 20, 3)
