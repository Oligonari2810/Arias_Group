"""Introspection tests: verify the Postgres schema matches SPEC-002 §5.

Runs against the TEST_DATABASE_URL after `alembic upgrade head` has been
applied.  Skipped when TEST_DATABASE_URL is unset.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import text


pytestmark = pytest.mark.integration


def _skip_if_no_testdb():
    if not os.environ.get('TEST_DATABASE_URL'):
        pytest.skip('TEST_DATABASE_URL not set; start docker compose or configure env')


@pytest.fixture(scope='module')
def migrated_engine():
    """Engine against TEST_DATABASE_URL with alembic upgrade head applied."""
    _skip_if_no_testdb()
    from alembic import command
    from alembic.config import Config
    from db import get_engine
    url = os.environ['TEST_DATABASE_URL']

    cfg = Config('alembic.ini')
    cfg.set_main_option('sqlalchemy.url', url)
    # Wipe any prior state first to keep the test hermetic.
    eng = get_engine(url)
    with eng.begin() as conn:
        conn.execute(text('DROP SCHEMA public CASCADE'))
        conn.execute(text('CREATE SCHEMA public'))
    os.environ['DATABASE_URL'] = url   # alembic/env.py reads DATABASE_URL
    command.upgrade(cfg, 'head')
    return eng


def test_all_expected_tables_exist(migrated_engine):
    expected = {
        'clients', 'products', 'systems', 'system_components', 'projects',
        'project_quotes', 'stage_events', 'shipping_routes', 'customs_rates',
        'fx_rates', 'users', 'pending_offers', 'order_lines', 'audit_log',
        'doc_sequences', 'family_defaults', 'price_history',
        'app_settings',
    }
    with migrated_engine.connect() as conn:
        actual = set(conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE'"
        )).scalars().all())
    missing = expected - actual
    assert not missing, f'Tables missing from schema: {sorted(missing)}'


def test_project_stage_enum_has_26_values(migrated_engine):
    with migrated_engine.connect() as conn:
        values = conn.execute(text(
            "SELECT enumlabel FROM pg_enum e "
            "JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = 'project_stage_enum' "
            "ORDER BY e.enumsortorder"
        )).scalars().all()
    assert len(values) == 26, f'Expected 26 stages, got {len(values)}: {values}'
    # First and last as a sanity check on ordering.
    assert values[0] == 'CLIENTE'
    assert values[-1] == 'RECOMPRA / REFERIDOS / ESCALA'


def test_core_enums_exist(migrated_engine):
    expected_enums = {
        'project_stage_enum', 'go_no_go_enum', 'incoterm_enum',
        'offer_status_enum', 'user_role_enum',
    }
    with migrated_engine.connect() as conn:
        present = set(conn.execute(text(
            "SELECT typname FROM pg_type WHERE typtype='e'"
        )).scalars().all())
    missing = expected_enums - present
    assert not missing, f'Enums missing: {sorted(missing)}'


def test_monetary_columns_are_numeric(migrated_engine):
    cases = [
        ('products', 'unit_price_eur'),
        ('projects', 'freight_eur'),
        ('pending_offers', 'total_final_eur'),
        ('order_lines', 'cost_exw_eur'),
    ]
    with migrated_engine.connect() as conn:
        for table, col in cases:
            dtype = conn.execute(text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name=:t AND column_name=:c"
            ), {'t': table, 'c': col}).scalar()
            assert dtype == 'numeric', f'{table}.{col} has type {dtype}, expected numeric'


def test_timestamp_columns_are_timestamptz(migrated_engine):
    cases = [
        ('clients', 'created_at'),
        ('projects', 'created_at'),
        ('audit_log', 'created_at'),
        ('fx_rates', 'updated_at'),
    ]
    with migrated_engine.connect() as conn:
        for table, col in cases:
            dtype = conn.execute(text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name=:t AND column_name=:c"
            ), {'t': table, 'c': col}).scalar()
            assert dtype == 'timestamp with time zone', (
                f'{table}.{col} has type {dtype}, expected timestamptz'
            )


def test_json_columns_are_jsonb(migrated_engine):
    cases = [
        ('project_quotes', 'result_json'),
        ('pending_offers', 'lines_json'),
        ('audit_log', 'detail'),
        ('app_settings', 'value'),
    ]
    with migrated_engine.connect() as conn:
        for table, col in cases:
            dtype = conn.execute(text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name=:t AND column_name=:c"
            ), {'t': table, 'c': col}).scalar()
            assert dtype == 'jsonb', f'{table}.{col} has type {dtype}, expected jsonb'


def test_foreign_keys_exist(migrated_engine):
    # (table, column, references_table)
    cases = [
        ('system_components', 'system_id', 'systems'),
        ('system_components', 'product_id', 'products'),
        ('projects', 'client_id', 'clients'),
        ('project_quotes', 'project_id', 'projects'),
        ('stage_events', 'project_id', 'projects'),
        ('order_lines', 'offer_id', 'pending_offers'),
        ('pending_offers', 'route_id', 'shipping_routes'),
        ('pending_offers', 'client_id', 'clients'),
        ('price_history', 'product_id', 'products'),
    ]
    with migrated_engine.connect() as conn:
        actual = {
            (r[0], r[1], r[2]) for r in conn.execute(text(
                "SELECT tc.table_name, kcu.column_name, ccu.table_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON tc.constraint_name = ccu.constraint_name "
                "WHERE tc.constraint_type = 'FOREIGN KEY' "
                "AND tc.table_schema = 'public'"
            )).all()
        }
    for case in cases:
        assert case in actual, f'FK missing: {case} not in {len(actual)} FKs'


def test_expected_indexes_exist(migrated_engine):
    expected = {
        'idx_products_category_subfamily',
        'idx_products_sku_lower',
        'idx_stage_events_project_created',
        'idx_shipping_routes_pair_valid',
        'idx_fx_rates_latest',
        'idx_pending_offers_status_created',
        'idx_pending_offers_client_project',
        'idx_pending_offers_raw_hash',
        'idx_order_lines_offer',
        'idx_audit_log_created',
        'idx_audit_log_offer_created',
        'idx_price_history_product_changed',
        'idx_clients_rnc',
    }
    with migrated_engine.connect() as conn:
        present = set(conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE schemaname='public'"
        )).scalars().all())
    missing = expected - present
    assert not missing, f'Indexes missing: {sorted(missing)}'


def test_score_check_constraint_on_clients(migrated_engine):
    with migrated_engine.begin() as conn:
        from sqlalchemy.exc import IntegrityError
        import pytest as _pt
        conn.execute(text("INSERT INTO clients (name, score) VALUES ('ok', 50)"))
        with _pt.raises(IntegrityError):
            conn.execute(text("INSERT INTO clients (name, score) VALUES ('bad', 999)"))
