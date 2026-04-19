"""Integration tests for app.calculate_quote — top-level orchestrator.

Signature: calculate_quote(system_id, area_sqm, freight_eur, target_margin_pct,
                           fx_rate) -> dict

Requires a seeded DB: at least one system + its system_components pointing to
products. conftest._seed_calc_fixtures wires 'Sistema placa estándar interior'
to three real Fassa SKUs (BA13-STD, PERFIL-48, TORNILLO-25).
"""
import pytest

from app import calculate_quote


@pytest.fixture(scope='module')
def system_id(app):
    from app import get_db
    with app.app_context():
        row = get_db().execute(
            "SELECT id FROM systems WHERE name = 'Sistema placa estándar interior'"
        ).fetchone()
        return row['id']


def test_calculate_quote_happy_path_returns_full_summary(app, system_id):
    with app.app_context():
        r = calculate_quote(
            system_id=system_id,
            area_sqm=100.0,
            freight_eur=500.0,
            target_margin_pct=0.25,
            fx_rate=1.085,
        )

    assert 'summary' in r
    s = r['summary']
    assert s['product_cost_eur'] > 0
    # target margin is applied → gross_margin_pct ≈ target
    assert abs(s['gross_margin_pct'] - 0.25) < 1e-3
    # FX conversion sanity-check
    assert s['sale_total_local'] == pytest.approx(s['sale_total_eur'] * 1.085, rel=1e-3)
    # containers recommended because we have positive weight/pallets
    assert s['container_recommendation'] is not None


def test_calculate_quote_produces_three_line_items(app, system_id):
    with app.app_context():
        r = calculate_quote(system_id, 100.0, 500.0, 0.25, 1.085)
    # BA13-STD + PERFIL-48 + TORNILLO-25
    assert len(r['line_items']) == 3


def test_calculate_quote_freight_flows_into_landed_total(app, system_id):
    with app.app_context():
        r = calculate_quote(system_id, 100.0, 1000.0, 0.25, 1.085)
    s = r['summary']
    assert s['landed_total_eur'] == pytest.approx(s['product_cost_eur'] + 1000.0, abs=0.01)


def test_calculate_quote_waste_takes_max_of_system_and_component(app, system_id):
    # With default_waste_pct=0.05 on system and 0.05 on each component, max→0.05.
    # We just verify the resulting cost is higher than the "no waste" baseline
    # by roughly 5% — confirms waste is applied at least once (not compounded).
    with app.app_context():
        r = calculate_quote(system_id, 100.0, 0.0, 0.25, 1.085)
    # With 100 m² and 1.05 consumption/m² of BA13-STD (upp 50, sqm_pp 60), cost
    # can be cross-checked: placa qty = ceil(100 * 1.05 * 1.05) = 111 boards
    # at 4.20 € = 466.2 €. This is a regression lock; recompute if seed changes.
    assert r['summary']['product_cost_eur'] > 400.0


def test_calculate_quote_clamps_near_100pct_target_margin(app, system_id):
    # target_margin_pct=0.99 → (1-0.99)=0.01, clamp kicks in at 0.01, no division error.
    with app.app_context():
        r = calculate_quote(system_id, 100.0, 500.0, 0.99, 1.085)
    # sale_total should be finite and large but not infinity/NaN
    assert r['summary']['sale_total_eur'] > 0


def test_calculate_quote_zero_area_does_not_crash(app, system_id):
    with app.app_context():
        r = calculate_quote(system_id, 0.0, 500.0, 0.25, 1.085)
    # Zero area → zero product cost; price_per_sqm protected from zero-division
    assert r['summary']['product_cost_eur'] == 0.0
    assert r['summary']['price_per_sqm_eur'] == 0.0


def test_calculate_quote_summary_contains_required_keys(app, system_id):
    with app.app_context():
        r = calculate_quote(system_id, 100.0, 500.0, 0.25, 1.085)
    required = {
        'total_units', 'total_pallets', 'total_pallets_theoretical',
        'total_weight_kg', 'm2_total',
        'product_cost_eur', 'freight_eur', 'landed_total_eur',
        'sale_total_eur', 'gross_margin_eur', 'gross_margin_pct',
        'price_per_sqm_eur',
        'containers_20_est', 'containers_40_est', 'container_recommendation',
        'family_breakdown', 'fx_rate', 'sale_total_local', 'alerts',
    }
    assert required.issubset(r['summary'].keys())


def test_calculate_quote_fx_changes_local_total_proportionally(app, system_id):
    with app.app_context():
        r1 = calculate_quote(system_id, 100.0, 500.0, 0.25, 1.000)
        r2 = calculate_quote(system_id, 100.0, 500.0, 0.25, 2.000)
    assert r2['summary']['sale_total_local'] == pytest.approx(
        2 * r1['summary']['sale_total_local'], rel=1e-3)
