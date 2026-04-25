"""End-to-end test of scripts/migrate_sqlite_to_postgres.py.

Builds a small SQLite fixture mirroring the legacy schema, runs the
migrator against a freshly-migrated Postgres test DB, and asserts that
rows land with correct types and that the id sequence is advanced past
MAX(id).
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text


pytestmark = pytest.mark.integration


def _skip_if_no_testdb():
    if not os.environ.get('TEST_DATABASE_URL'):
        pytest.skip('TEST_DATABASE_URL not set')


@pytest.fixture
def legacy_sqlite(tmp_path):
    """Create a tiny SQLite file matching app.py init_db shape."""
    path = tmp_path / 'legacy.db'
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, company TEXT, rnc TEXT, email TEXT, phone TEXT,
            address TEXT, country TEXT DEFAULT 'República Dominicana',
            score INTEGER DEFAULT 50, created_at TEXT NOT NULL
        );
        CREATE TABLE products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT UNIQUE NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL,
            subfamily TEXT, source_catalog TEXT NOT NULL, unit TEXT NOT NULL,
            unit_price_eur REAL NOT NULL, kg_per_unit REAL,
            units_per_pallet REAL, sqm_per_pallet REAL, notes TEXT,
            content_per_unit TEXT,
            pack_size TEXT, pvp_eur_unit REAL, precio_arias_eur_unit REAL,
            discount_pct REAL DEFAULT 50,
            length_mm INTEGER, width_mm INTEGER, thickness_mm REAL,
            kg_per_ml REAL, box_units INTEGER, peso_saco_kg REAL,
            dispo_tarancon TEXT, tariff_origen TEXT, color TEXT, norma_text TEXT
        );
        CREATE TABLE systems (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
            description TEXT, default_waste_pct REAL DEFAULT 0.08
        );
        CREATE TABLE system_components (
            id INTEGER PRIMARY KEY AUTOINCREMENT, system_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL, consumption_per_sqm REAL NOT NULL,
            waste_pct REAL DEFAULT 0.0
        );
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER NOT NULL,
            name TEXT NOT NULL, project_type TEXT, location TEXT,
            area_sqm REAL DEFAULT 0, stage TEXT NOT NULL DEFAULT 'OPORTUNIDAD',
            go_no_go TEXT DEFAULT 'PENDING', incoterm TEXT DEFAULT 'EXW',
            fx_rate REAL DEFAULT 1.0, target_margin_pct REAL DEFAULT 0.30,
            freight_eur REAL DEFAULT 0, customs_pct REAL DEFAULT 0,
            logistics_notes TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE project_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
            system_id INTEGER, version_label TEXT NOT NULL, area_sqm REAL NOT NULL,
            fx_rate REAL NOT NULL, freight_eur REAL NOT NULL, customs_pct REAL NOT NULL,
            target_margin_pct REAL NOT NULL, result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE stage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
            from_stage TEXT, to_stage TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE shipping_routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, origin_port TEXT NOT NULL,
            destination_port TEXT NOT NULL, carrier TEXT, transit_days INTEGER,
            container_20_eur REAL, container_40_eur REAL, container_40hc_eur REAL,
            insurance_pct REAL DEFAULT 0.005, valid_from TEXT, valid_until TEXT,
            notes TEXT
        );
        CREATE TABLE customs_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, country TEXT NOT NULL,
            hs_code TEXT NOT NULL, category TEXT, dai_pct REAL DEFAULT 0.0,
            itbis_pct REAL DEFAULT 0.18, other_pct REAL DEFAULT 0.0, notes TEXT
        );
        CREATE TABLE fx_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            base_currency TEXT NOT NULL DEFAULT 'EUR', target_currency TEXT NOT NULL,
            rate REAL NOT NULL, updated_at TEXT NOT NULL, source TEXT DEFAULT 'Manual'
        );
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'viewer',
            full_name TEXT, email TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE pending_offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, offer_number TEXT NOT NULL,
            client_name TEXT NOT NULL, project_name TEXT NOT NULL,
            waste_pct REAL DEFAULT 0.05, margin_pct REAL DEFAULT 0.33,
            fx_rate REAL DEFAULT 1.18, lines_json TEXT NOT NULL,
            total_product_eur REAL DEFAULT 0, total_logistic_eur REAL DEFAULT 0,
            total_final_eur REAL DEFAULT 0, status TEXT DEFAULT 'pending',
            incoterm TEXT DEFAULT 'EXW', route_id INTEGER,
            container_count INTEGER DEFAULT 0, validity_days INTEGER DEFAULT 30,
            client_id INTEGER, created_at TEXT NOT NULL,
            updated_at TEXT, raw_hash TEXT
        );
        CREATE TABLE order_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT, offer_id INTEGER NOT NULL,
            sku TEXT NOT NULL, name TEXT, family TEXT, unit TEXT,
            qty_input REAL NOT NULL, qty_logistic REAL, price_unit_eur REAL,
            cost_exw_eur REAL, m2_total REAL DEFAULT 0, weight_total_kg REAL DEFAULT 0,
            pallets_theoretical REAL DEFAULT 0, pallets_logistic INTEGER DEFAULT 0,
            alerts_text TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, offer_id INTEGER,
            action TEXT NOT NULL, detail TEXT, username TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE doc_sequences (
            id INTEGER PRIMARY KEY AUTOINCREMENT, prefix TEXT UNIQUE NOT NULL,
            last_number INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE family_defaults (
            category TEXT PRIMARY KEY, discount_pct REAL NOT NULL DEFAULT 50,
            display_order INTEGER DEFAULT 99, notes TEXT
        );
        CREATE TABLE price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
            field TEXT NOT NULL, old_value REAL, new_value REAL,
            user_id INTEGER, username TEXT, changed_at TEXT NOT NULL, notes TEXT
        );
        CREATE TABLE app_settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT
        );
    """)
    # Seed small but realistic rows.
    now = '2026-04-19T12:00:00+00:00'
    conn.execute('INSERT INTO clients (id, name, company, country, score, created_at) '
                 'VALUES (?, ?, ?, ?, ?, ?)',
                 (1, 'Acme', 'Acme SA', 'República Dominicana', 80, now))
    conn.execute('INSERT INTO systems (id, name, description, default_waste_pct) '
                 'VALUES (?, ?, ?, ?)',
                 (1, 'Test System', 'desc', 0.05))
    conn.execute('INSERT INTO products (id, sku, name, category, source_catalog, unit, '
                 'unit_price_eur, kg_per_unit, units_per_pallet, sqm_per_pallet) '
                 'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                 (1, 'BA13', 'Placa BA13', 'placas', 'TEST', 'board', 4.20, 8.5, 50, 60))
    conn.execute('INSERT INTO system_components (id, system_id, product_id, '
                 'consumption_per_sqm, waste_pct) VALUES (?, ?, ?, ?, ?)',
                 (1, 1, 1, 1.05, 0.05))
    conn.execute('INSERT INTO projects (id, client_id, name, stage, go_no_go, incoterm, '
                 'fx_rate, target_margin_pct, freight_eur, customs_pct, created_at) '
                 'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                 (1, 1, 'Proyecto X', 'CÁLCULO DETALLADO', 'GO', 'EXW',
                  1.085, 0.30, 500, 0.18, now))
    conn.execute('INSERT INTO pending_offers (id, offer_number, client_name, project_name, '
                 'lines_json, status, incoterm, created_at) '
                 'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                 (1, 'OF-001', 'Acme', 'Proyecto X',
                  '[{"sku":"BA13","qty":10}]', 'pending', 'EXW', now))
    conn.execute('INSERT INTO order_lines (id, offer_id, sku, qty_input, price_unit_eur, '
                 'created_at) VALUES (?, ?, ?, ?, ?, ?)',
                 (1, 1, 'BA13', 10, 4.20, now))
    conn.execute("INSERT INTO app_settings (key, value, updated_at) "
                 "VALUES ('fx_eur_usd', '1.085', ?)", (now,))
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def clean_postgres():
    _skip_if_no_testdb()
    from alembic import command
    from alembic.config import Config
    from db import get_engine

    url = os.environ['TEST_DATABASE_URL']
    eng = get_engine(url)
    with eng.begin() as conn:
        conn.execute(text('DROP SCHEMA public CASCADE'))
        conn.execute(text('CREATE SCHEMA public'))

    cfg = Config('alembic.ini')
    cfg.set_main_option('sqlalchemy.url', url)
    os.environ['DATABASE_URL'] = url
    command.upgrade(cfg, 'head')
    return eng, url


def test_migrator_dry_run_writes_nothing(legacy_sqlite, clean_postgres):
    eng, url = clean_postgres
    from scripts.migrate_sqlite_to_postgres import migrate
    rc = migrate(legacy_sqlite, url, dry_run=True, truncate=False)
    assert rc == 0
    with eng.connect() as conn:
        clients_count = conn.execute(text('SELECT COUNT(*) FROM clients')).scalar()
    assert clients_count == 0


def test_migrator_copies_rows(legacy_sqlite, clean_postgres):
    eng, url = clean_postgres
    from scripts.migrate_sqlite_to_postgres import migrate
    rc = migrate(legacy_sqlite, url, dry_run=False, truncate=False)
    assert rc == 0

    with eng.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM clients')).scalar() == 1
        assert conn.execute(text('SELECT COUNT(*) FROM products')).scalar() == 1
        assert conn.execute(text('SELECT COUNT(*) FROM projects')).scalar() == 1
        assert conn.execute(text('SELECT COUNT(*) FROM pending_offers')).scalar() == 1
        assert conn.execute(text('SELECT COUNT(*) FROM order_lines')).scalar() == 1
        # JSONB was parsed
        lines = conn.execute(text("SELECT lines_json FROM pending_offers")).scalar()
        assert isinstance(lines, list)
        assert lines[0]['sku'] == 'BA13'


def test_migrator_preserves_ids_and_advances_sequence(legacy_sqlite, clean_postgres):
    eng, url = clean_postgres
    from scripts.migrate_sqlite_to_postgres import migrate
    migrate(legacy_sqlite, url, dry_run=False, truncate=False)

    with eng.connect() as conn:
        max_id = conn.execute(text('SELECT MAX(id) FROM clients')).scalar()
    assert max_id == 1

    # Sequence must point past the max, so a fresh insert takes id=2.
    with eng.begin() as conn:
        new_id = conn.execute(text(
            "INSERT INTO clients (name, score) VALUES ('SeqCheck', 50) RETURNING id"
        )).scalar()
    assert new_id == 2


def test_migrator_is_idempotent_on_second_run(legacy_sqlite, clean_postgres):
    eng, url = clean_postgres
    from scripts.migrate_sqlite_to_postgres import migrate
    migrate(legacy_sqlite, url, dry_run=False, truncate=False)
    # Second run must not raise and must not duplicate rows.
    migrate(legacy_sqlite, url, dry_run=False, truncate=False)
    with eng.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM clients')).scalar() == 1
        assert conn.execute(text('SELECT COUNT(*) FROM projects')).scalar() == 1


def test_migrator_converts_timestamps_to_timezone_aware(legacy_sqlite, clean_postgres):
    eng, url = clean_postgres
    from scripts.migrate_sqlite_to_postgres import migrate
    migrate(legacy_sqlite, url, dry_run=False, truncate=False)
    with eng.connect() as conn:
        created_at = conn.execute(text(
            "SELECT created_at FROM clients WHERE id=1"
        )).scalar()
    assert isinstance(created_at, datetime)
    assert created_at.tzinfo is not None


def test_migrator_converts_prices_to_decimal(legacy_sqlite, clean_postgres):
    eng, url = clean_postgres
    from scripts.migrate_sqlite_to_postgres import migrate
    migrate(legacy_sqlite, url, dry_run=False, truncate=False)
    with eng.connect() as conn:
        price = conn.execute(text(
            "SELECT unit_price_eur FROM products WHERE id=1"
        )).scalar()
    assert isinstance(price, Decimal)
    assert price == Decimal('4.2000')
