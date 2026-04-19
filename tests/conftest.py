"""Test fixtures for Arias_Group calc engine tests.

Flask app must be importable without triggering a hard SECRET_KEY error and
without pointing to the production SQLite file. We set env vars *before*
importing `app`.
"""
import os
import tempfile

os.environ.setdefault('FLASK_DEBUG', '1')
os.environ.setdefault('SECRET_KEY', 'test-secret-key')

_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix='arias_test_', suffix='.db')
os.environ['FASSA_DB_PATH'] = _TMP_DB_PATH

import pytest

from app import app as flask_app, init_db, seed_db, get_db, now_iso


def _seed_calc_fixtures(db):
    """Insert products + system_components so calculate_quote has lines to compute.

    System 'Sistema placa estándar interior' is seeded by seed_db(); we attach
    three components (placa + perfil + tornillos) using realistic Fassa SKUs.
    """
    existing = db.execute(
        "SELECT COUNT(*) AS c FROM system_components WHERE system_id = "
        "(SELECT id FROM systems WHERE name = 'Sistema placa estándar interior')"
    ).fetchone()['c']
    if existing > 0:
        return

    products = [
        ('BA13-STD', 'Placa BA13 estándar 1200x2500', 'placas', 'board',
         4.20, 9.5, 50, 60, 0.0800),
        ('PERFIL-48', 'Perfil Montante 48mm 3m', 'perfiles', 'ud',
         2.10, 1.2, 120, 0.0, 0.0500),
        ('TORNILLO-25', 'Tornillo autoperforante 3.5x25', 'tornillos', 'ud',
         0.03, 0.005, 0, 0.0, 0.0500),
    ]
    for sku, name, cat, unit, price, kg, upp, sqm_pp, _waste in products:
        db.execute(
            '''INSERT OR IGNORE INTO products
            (sku, name, category, source_catalog, unit, unit_price_eur,
             kg_per_unit, units_per_pallet, sqm_per_pallet)
            VALUES (?,?,?,?,?,?,?,?,?)''',
            (sku, name, cat, 'TEST', unit, price, kg, upp, sqm_pp),
        )

    system_id = db.execute(
        "SELECT id FROM systems WHERE name = 'Sistema placa estándar interior'"
    ).fetchone()['id']

    components = [
        ('BA13-STD',     1.05, 0.05),
        ('PERFIL-48',    2.50, 0.05),
        ('TORNILLO-25', 12.00, 0.05),
    ]
    for sku, consumption, waste in components:
        prod_id = db.execute('SELECT id FROM products WHERE sku = ?', (sku,)).fetchone()['id']
        db.execute(
            '''INSERT OR IGNORE INTO system_components
            (system_id, product_id, consumption_per_sqm, waste_pct)
            VALUES (?,?,?,?)''',
            (system_id, prod_id, consumption, waste),
        )
    db.commit()


@pytest.fixture(scope='session')
def app():
    flask_app.config['TESTING'] = True
    flask_app.config['DATABASE'] = _TMP_DB_PATH
    with flask_app.app_context():
        init_db()
        seed_db()
        _seed_calc_fixtures(get_db())
    yield flask_app
    os.close(_TMP_DB_FD)
    try:
        os.unlink(_TMP_DB_PATH)
    except OSError:
        pass


@pytest.fixture
def db(app):
    with app.app_context():
        yield get_db()


@pytest.fixture
def product_factory():
    """Produce a dict shaped like what compute_line expects.

    Defaults model a realistic BA13 placa (category='placas'): the 8.5 kg/unit,
    50 units/pallet and 60 sqm/pallet come from a real Fassa board spec sheet,
    so derived figures in assertions have a verifiable source.
    """
    def _make(**overrides):
        base = {
            'sku': 'TEST-001',
            'name': 'Test Product',
            'category': 'placas',
            'unit': 'board',
            'unit_price_eur': 4.20,
            'kg_per_unit': 8.5,
            'units_per_pallet': 50,
            'sqm_per_pallet': 60,
        }
        base.update(overrides)
        return base
    return _make
