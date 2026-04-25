"""Test fixtures for Arias_Group.

Supports two backends:

  * **Postgres (preferred, SPEC-002c)** — when TEST_DATABASE_URL is set, the
    fixture drops `public`, runs `alembic upgrade head`, then seeds.  The
    Flask app picks up the Postgres adapter via DATABASE_URL.
  * **SQLite fallback** — when TEST_DATABASE_URL is absent, a tempfile DB is
    created and `init_db()` + `seed_db()` run against it.  This preserves
    the original SPEC-001 test environment for devs without Docker.

All env vars that `app.py` reads are set BEFORE importing `app` — otherwise
the SECRET_KEY guard aborts the import.
"""
import os
import tempfile

# --- Environment setup (MUST happen before `from app import ...`) --------

os.environ['SECRET_KEY'] = os.environ.get('SECRET_KEY') or 'test-secret-key'
os.environ['FLASK_DEBUG'] = os.environ.get('FLASK_DEBUG') or '1'

_USE_POSTGRES = bool(os.environ.get('TEST_DATABASE_URL'))

if _USE_POSTGRES:
    # Align DATABASE_URL so app.get_db() picks the Postgres adapter.
    os.environ['DATABASE_URL'] = os.environ['TEST_DATABASE_URL']
    _TMP_DB_FD, _TMP_DB_PATH = None, None
else:
    _TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix='arias_test_', suffix='.db')
    os.environ['FASSA_DB_PATH'] = _TMP_DB_PATH
    # Ensure the Postgres adapter stays dormant in SQLite mode.
    os.environ.pop('DATABASE_URL', None)

import pytest

from app import app as flask_app, init_db, seed_db, get_db, now_iso


def _reset_postgres_schema():
    """Wipe and re-apply the Alembic schema against TEST_DATABASE_URL."""
    from sqlalchemy import text
    from alembic import command
    from alembic.config import Config
    from db import get_engine
    from db.engine import reset_engine_cache

    url = os.environ['TEST_DATABASE_URL']
    reset_engine_cache()
    eng = get_engine(url)
    with eng.begin() as conn:
        conn.execute(text('DROP SCHEMA public CASCADE'))
        conn.execute(text('CREATE SCHEMA public'))

    cfg = Config('alembic.ini')
    cfg.set_main_option('sqlalchemy.url', url)
    command.upgrade(cfg, 'head')


def _seed_calc_fixtures(db):
    """Insert products + system_components so calculate_quote has lines to compute.

    Uses portable INSERT ... ON CONFLICT DO NOTHING so it works on both SQLite
    3.24+ and Postgres without adapter translation.
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
            '''INSERT INTO products
            (sku, name, category, source_catalog, unit, unit_price_eur,
             kg_per_unit, units_per_pallet, sqm_per_pallet)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT (sku) DO NOTHING''',
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
            '''INSERT INTO system_components
            (system_id, product_id, consumption_per_sqm, waste_pct)
            VALUES (?,?,?,?)
            ON CONFLICT (system_id, product_id) DO NOTHING''',
            (system_id, prod_id, consumption, waste),
        )
    db.commit()


@pytest.fixture(scope='session')
def app():
    flask_app.config['TESTING'] = True

    if _USE_POSTGRES:
        _reset_postgres_schema()
        with flask_app.app_context():
            seed_db()
            _seed_calc_fixtures(get_db())
    else:
        flask_app.config['DATABASE'] = _TMP_DB_PATH
        with flask_app.app_context():
            init_db()
            seed_db()
            _seed_calc_fixtures(get_db())

    yield flask_app

    if not _USE_POSTGRES:
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
