from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Carga .env si existe (dev local). En producción las env vars ya están
# inyectadas por el runtime (Render, Docker, etc.) y override=False respeta
# esos valores sin pisarlos.
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for, make_response, session, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
import bcrypt
import openpyxl
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, PageBreak, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
import io

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('FASSA_DB_PATH', BASE_DIR / 'fassa_ops.db'))

STAGES = [
    'CLIENTE',
    'OPORTUNIDAD',
    'FILTRO GO / NO-GO',
    'PRE-CÁLCULO RÁPIDO',
    'CÁLCULO DETALLADO',
    'OFERTA V1/V2',
    'VALIDACIÓN TÉCNICA',
    'VALIDACIÓN CLIENTE',
    'CIERRE',
    'CONTRATO + CONDICIONES',
    'PREPAGO VALIDADO',
    'ORDEN BLOQUEADA',
    'CHECK INTERNO',
    'LOGÍSTICA VALIDADA',
    'BOOKING NAVIERA',
    'PEDIDO A FASSA',
    'CONFIRMACIÓN FÁBRICA',
    'READY DATE',
    'EXPEDICIÓN (BL)',
    'TRACKING + CONTROL ETA',
    'ADUANA',
    'LIQUIDACIÓN ADUANERA + COSTES FINALES',
    'INSPECCIÓN / CONTROL DAÑOS',
    'ENTREGA',
    'POSTVENTA',
    'RECOMPRA / REFERIDOS / ESCALA',
]

app = Flask(__name__)
_secret = os.environ.get('SECRET_KEY')
_debug = os.environ.get('FLASK_DEBUG', '0') == '1'
if not _secret:
    if not _debug:
        raise RuntimeError('SECRET_KEY environment variable is required when FLASK_DEBUG != 1')
    _secret = 'dev-secret-key-fassa-2026'
app.config['SECRET_KEY'] = _secret
app.config['DATABASE'] = str(DB_PATH)
# Session cookie hardening — Secure flag solo en producción (HTTPS).
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = not _debug
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)
app.config['WTF_CSRF_TIME_LIMIT'] = None  # token vive lo que la sesión
csrf = CSRFProtect(app)
BOT_API_TOKEN = os.environ.get('BOT_API_TOKEN')


def _safe_next_url(target: str | None) -> str | None:
    """Permite solo paths internos relativos. Bloquea open redirect a otros hosts."""
    if not target:
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return None
    if not target.startswith('/') or target.startswith('//'):
        return None
    return target


# Identificadores SQLite seguros para migraciones de columnas en init_db().
_SAFE_IDENTIFIER_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')
_SAFE_COLUMN_TYPES = frozenset({
    'TEXT', 'INTEGER', 'REAL', 'BLOB', 'NUMERIC',
    'REAL DEFAULT 50', 'REAL DEFAULT 5', 'INTEGER DEFAULT 99',
    'INTEGER DEFAULT 30',
})


def _safe_add_column(db: sqlite3.Connection, table: str, col: str, typ: str) -> None:
    """ALTER TABLE seguro: valida identifier y tipo contra allowlist."""
    if not _SAFE_IDENTIFIER_RE.match(table):
        raise ValueError(f"Identifier inseguro de tabla: {table!r}")
    if not _SAFE_IDENTIFIER_RE.match(col):
        raise ValueError(f"Identifier inseguro de columna: {col!r}")
    if typ not in _SAFE_COLUMN_TYPES:
        raise ValueError(f"Tipo de columna no permitido: {typ!r}")
    db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_current_fx_eur_usd() -> float:
    db = get_db()
    row = db.execute(
        "SELECT rate FROM fx_rates WHERE base_currency='EUR' AND target_currency='USD' "
        "ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    if row:
        return float(row['rate'])
    row = db.execute("SELECT value FROM app_settings WHERE key='fx_eur_usd'").fetchone()
    return float(row['value']) if row else 1.18


def eur_to_usd(amount_eur: float, fx_rate: float) -> float:
    return round(amount_eur * fx_rate, 2)


def bot_token_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not BOT_API_TOKEN:
            abort(503, description='BOT_API_TOKEN no configurado en el servidor')
        token = request.headers.get('X-Bot-Token') or request.args.get('bot_token')
        if token != BOT_API_TOKEN:
            abort(401, description='Token inválido')
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(401, description='Autenticación requerida')
        if getattr(current_user, 'role', None) != 'admin':
            abort(403, description='Requiere rol admin')
        return fn(*args, **kwargs)
    return wrapper

# ── Login Manager ──────────────────────────────────────────────────────
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Inicia sesión para acceder.'


class User(UserMixin):
    def __init__(self, id_, username, role):
        self.id = id_
        self.username = username
        self.role = role


@login_manager.user_loader
def load_user(user_id):
    db = get_db()
    row = db.execute('SELECT * FROM users WHERE id = ?', (int(user_id),)).fetchone()
    if row:
        return User(row['id'], row['username'], row['role'])
    return None


def get_db():
    """Return the request-scoped DB handle.

    When DATABASE_URL is set, returns a psycopg-backed adapter that mimics
    sqlite3.Connection (see db/adapter.py); otherwise falls back to a plain
    SQLite connection for backward compatibility (SPEC-002c cutover window).
    """
    if 'db' not in g:
        from db import adapter
        if adapter.is_configured():
            g.db = adapter.connect()
        else:
            g.db = sqlite3.connect(app.config['DATABASE'])
            g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_: Any) -> None:
    db = g.pop('db', None)
    if db is not None:
        db.close()


def using_postgres() -> bool:
    """True when the current get_db() would return a Postgres adapter."""
    from db import adapter
    return adapter.is_configured()


def init_db() -> None:
    if using_postgres():
        # On Postgres, Alembic owns the schema. Skip the SQLite DDL script.
        return
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            company TEXT,
            rnc TEXT,
            email TEXT,
            phone TEXT,
            address TEXT,
            country TEXT DEFAULT 'República Dominicana',
            score INTEGER DEFAULT 50,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            subfamily TEXT,
            source_catalog TEXT NOT NULL,
            unit TEXT NOT NULL,
            unit_price_eur REAL NOT NULL,
            kg_per_unit REAL,
            units_per_pallet REAL,
            sqm_per_pallet REAL,
            notes TEXT,
            pvp_per_m2 REAL,
            precio_arias_m2 REAL,
            content_per_unit TEXT,
            pack_size TEXT,
            pvp_eur_unit REAL,
            precio_arias_eur_unit REAL,
            discount_pct REAL DEFAULT 50,
            discount_extra_pct REAL
        );
        CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
        CREATE INDEX IF NOT EXISTS idx_products_subfamily ON products(subfamily);

        CREATE TABLE IF NOT EXISTS systems (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT,
            default_waste_pct REAL DEFAULT 0.08
        );

        CREATE TABLE IF NOT EXISTS system_components (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            consumption_per_sqm REAL NOT NULL,
            waste_pct REAL DEFAULT 0.0,
            FOREIGN KEY(system_id) REFERENCES systems(id),
            FOREIGN KEY(product_id) REFERENCES products(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uniq_system_components_pair
          ON system_components(system_id, product_id);

        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            project_type TEXT,
            location TEXT,
            area_sqm REAL DEFAULT 0,
            stage TEXT NOT NULL DEFAULT 'OPORTUNIDAD',
            go_no_go TEXT DEFAULT 'PENDING',
            incoterm TEXT DEFAULT 'EXW',
            fx_rate REAL DEFAULT 1.0,
            target_margin_pct REAL DEFAULT 0.30,
            freight_eur REAL DEFAULT 0,
            customs_pct REAL DEFAULT 0,
            logistics_notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(client_id) REFERENCES clients(id)
        );

        CREATE TABLE IF NOT EXISTS project_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            system_id INTEGER,
            version_label TEXT NOT NULL,
            area_sqm REAL NOT NULL,
            fx_rate REAL NOT NULL,
            freight_eur REAL NOT NULL,
            customs_pct REAL NOT NULL,
            target_margin_pct REAL NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id),
            FOREIGN KEY(system_id) REFERENCES systems(id)
        );

        CREATE TABLE IF NOT EXISTS stage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            from_stage TEXT,
            to_stage TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id)
        );

        CREATE TABLE IF NOT EXISTS shipping_routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin_port TEXT NOT NULL,
            destination_port TEXT NOT NULL,
            carrier TEXT,
            transit_days INTEGER,
            container_20_eur REAL,
            container_40_eur REAL,
            container_40hc_eur REAL,
            insurance_pct REAL DEFAULT 0.005,
            valid_from TEXT,
            valid_until TEXT,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS customs_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            country TEXT NOT NULL,
            hs_code TEXT NOT NULL,
            category TEXT,
            dai_pct REAL DEFAULT 0.0,
            itbis_pct REAL DEFAULT 0.18,
            other_pct REAL DEFAULT 0.0,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS fx_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            base_currency TEXT NOT NULL DEFAULT 'EUR',
            target_currency TEXT NOT NULL,
            rate REAL NOT NULL,
            updated_at TEXT NOT NULL,
            source TEXT DEFAULT 'Manual'
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'viewer',
            full_name TEXT,
            email TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            offer_number TEXT NOT NULL,
            client_name TEXT NOT NULL,
            project_name TEXT NOT NULL,
            waste_pct REAL DEFAULT 5,
            margin_pct REAL DEFAULT 33,
            fx_rate REAL DEFAULT 1.18,
            lines_json TEXT NOT NULL,
            total_product_eur REAL DEFAULT 0,
            total_logistic_eur REAL DEFAULT 0,
            total_final_eur REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',
            incoterm TEXT DEFAULT 'EXW',
            route_id INTEGER,
            container_count INTEGER DEFAULT 0,
            validity_days INTEGER DEFAULT 30,
            created_at TEXT NOT NULL,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS order_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            offer_id INTEGER NOT NULL,
            sku TEXT NOT NULL,
            name TEXT,
            family TEXT,
            unit TEXT,
            qty_input REAL NOT NULL,
            qty_logistic REAL,
            price_unit_eur REAL,
            cost_exw_eur REAL,
            m2_total REAL DEFAULT 0,
            weight_total_kg REAL DEFAULT 0,
            pallets_theoretical REAL DEFAULT 0,
            pallets_logistic INTEGER DEFAULT 0,
            alerts_text TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(offer_id) REFERENCES pending_offers(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_order_lines_offer ON order_lines(offer_id);

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            offer_id INTEGER,
            action TEXT NOT NULL,
            detail TEXT,
            username TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_log_offer ON audit_log(offer_id);

        CREATE TABLE IF NOT EXISTS doc_sequences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prefix TEXT UNIQUE NOT NULL,
            last_number INTEGER NOT NULL DEFAULT 0
        );

        -- Mirror de purchase.order Odoo: una orden a fábrica (Fassa) por oferta aprobada.
        CREATE TABLE IF NOT EXISTS factory_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            offer_id INTEGER NOT NULL,
            name TEXT NOT NULL,                          -- "PO-0001" (Odoo: purchase.order.name)
            state TEXT NOT NULL DEFAULT 'draft',         -- draft, sent, to_approve, purchase, done, cancel
            partner_ref TEXT NOT NULL DEFAULT 'FASSA',
            date_planned TEXT,                           -- ready_date estimada Fassa
            sent_to_factory_at TEXT,
            confirmed_at TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(offer_id) REFERENCES pending_offers(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_factory_orders_offer ON factory_orders(offer_id);

        -- Mirror de stock.picking Odoo: una orden logística por oferta aprobada.
        CREATE TABLE IF NOT EXISTS logistics_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            offer_id INTEGER NOT NULL,
            name TEXT NOT NULL,                          -- "OUT-0001" (Odoo: stock.picking.name)
            state TEXT NOT NULL DEFAULT 'draft',         -- draft, waiting, confirmed, assigned, done, cancel
            route_id INTEGER,
            container_type TEXT,                         -- 20' / 40' / 40HC
            booking_ref TEXT,                            -- BL naviera
            departure_date TEXT,
            eta_date TEXT,
            delivered_at TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(offer_id) REFERENCES pending_offers(id) ON DELETE CASCADE,
            FOREIGN KEY(route_id) REFERENCES shipping_routes(id)
        );
        CREATE INDEX IF NOT EXISTS idx_logistics_orders_offer ON logistics_orders(offer_id);

        CREATE TABLE IF NOT EXISTS family_defaults (
            category TEXT PRIMARY KEY,
            discount_pct REAL NOT NULL DEFAULT 50,
            discount_extra_pct REAL NOT NULL DEFAULT 5,
            display_order INTEGER DEFAULT 99,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            field TEXT NOT NULL,
            old_value REAL,
            new_value REAL,
            user_id INTEGER,
            username TEXT,
            changed_at TEXT NOT NULL,
            notes TEXT,
            FOREIGN KEY(product_id) REFERENCES products(id)
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT
        );

        -- Perfil físico de contenedor (§3 spec logística). Editable sin redeploy.
        -- floor_stowage_factor: techo de carga geométrica del suelo en estiba real
        -- con placas (operativa Arias: 0.80 = 80% del suelo aprovechable).
        CREATE TABLE IF NOT EXISTS container_profiles (
            type TEXT PRIMARY KEY,               -- '20', '40', '40HC'
            inner_length_m REAL NOT NULL,
            inner_width_m REAL NOT NULL,
            inner_height_m REAL NOT NULL,
            payload_kg REAL NOT NULL,
            door_clearance_m REAL NOT NULL DEFAULT 0.30,
            stowage_factor REAL NOT NULL DEFAULT 0.90,  -- peso/volumen
            floor_stowage_factor REAL NOT NULL DEFAULT 1.0,  -- m² suelo
            notes TEXT
        );

        -- Perfil físico de palé por familia (§2 spec). Cada SKU puede overridear
        -- en products.pallet_* si su embalaje difiere del default familiar.
        CREATE TABLE IF NOT EXISTS pallet_profiles (
            category TEXT PRIMARY KEY,           -- 'PLACAS', 'PERFILES', ...
            pallet_length_m REAL NOT NULL,
            pallet_width_m REAL NOT NULL,
            pallet_height_m REAL NOT NULL,       -- altura del palé cargado
            stackable_levels INTEGER NOT NULL DEFAULT 1,  -- niveles apilables dentro del contenedor
            allow_mix_floor INTEGER NOT NULL DEFAULT 1,   -- bool: puede compartir suelo con otras familias
            notes TEXT
        );
        """
    )
    # Migraciones para DBs existentes — usa _safe_add_column (allowlist valida col + tipo).
    prod_cols = {r[1] for r in db.execute("PRAGMA table_info(products)").fetchall()}
    for col, typ in [('subfamily', 'TEXT'), ('pvp_per_m2', 'REAL'), ('precio_arias_m2', 'REAL'),
                     ('content_per_unit', 'TEXT'), ('pack_size', 'TEXT'),
                     ('pvp_eur_unit', 'REAL'), ('precio_arias_eur_unit', 'REAL'),
                     ('discount_pct', 'REAL DEFAULT 50'),
                     ('discount_extra_pct', 'REAL'),
                     # Fase A logística: overrides per-SKU sobre el pallet_profiles de la familia.
                     ('pallet_length_m', 'REAL'),
                     ('pallet_width_m', 'REAL'),
                     ('pallet_height_m', 'REAL'),
                     ('pallet_weight_kg', 'REAL'),
                     ('stackable_levels', 'INTEGER'),
                     ('allow_mix_floor', 'INTEGER')]:
        if col not in prod_cols:
            _safe_add_column(db, 'products', col, typ)
    client_cols = {r[1] for r in db.execute("PRAGMA table_info(clients)").fetchall()}
    for col, typ in [('rnc', 'TEXT'), ('address', 'TEXT')]:
        if col not in client_cols:
            _safe_add_column(db, 'clients', col, typ)
    offer_cols = {r[1] for r in db.execute("PRAGMA table_info(pending_offers)").fetchall()}
    if 'raw_hash' not in offer_cols:
        _safe_add_column(db, 'pending_offers', 'raw_hash', 'TEXT')
    if 'validity_days' not in offer_cols:
        _safe_add_column(db, 'pending_offers', 'validity_days', 'INTEGER DEFAULT 30')
        db.execute('UPDATE pending_offers SET validity_days = 30 WHERE validity_days IS NULL')
    fd_cols = {r[1] for r in db.execute("PRAGMA table_info(family_defaults)").fetchall()}
    if fd_cols and 'display_order' not in fd_cols:
        _safe_add_column(db, 'family_defaults', 'display_order', 'INTEGER DEFAULT 99')
    if fd_cols and 'discount_extra_pct' not in fd_cols:
        _safe_add_column(db, 'family_defaults', 'discount_extra_pct', 'REAL DEFAULT 5')
    db.commit()
    # Migración one-shot: aplicar descuento compuesto (base + extra) a precio_arias.
    # Se marca en app_settings para no volver a correr en restarts posteriores.
    _apply_compound_discount_once(db)
    _audit_fixes_20260423(db)
    _audit_catalog_fixes_20260423(db)
    _audit_catalog_fixes_20260423_v2(db)
    _audit_logistics_fixes_20260424(db)
    _audit_catalog_completion_20260424(db)
    _sync_fx_sources_20260424(db)
    _logistics_aggregated_calibration_20260425(db)
    _audit_misc_20260425(db)
    _cleanup_demo_data_20260425(db)
    _schema_cleanup_and_client_fk_20260425(db)
    _catalog_discount_completion_20260425(db)
    _catalog_real_data_from_pdf_20260425(db)
    _catalog_real_weights_20260425(db)
    _catalog_pdf_extras_and_discontinued_20260425(db)
    _catalog_discontinued_skus_20260425(db)


def _audit_fixes_20260423(db: sqlite3.Connection) -> None:
    """Migraciones one-shot derivadas de la auditoría 2026-04-23.

    - Cancela la oferta duplicada id=18 (compartía offer_number 2026-8464 con #19).
    - Alinea doc_sequences.OFR al máximo numérico existente para que
      el backend genere a partir de ahí números únicos y secuenciales.

    Idempotente: flag en app_settings para no re-ejecutar en restarts.
    """
    flag = db.execute("SELECT value FROM app_settings WHERE key = 'audit_fixes_20260423_applied'").fetchone()
    if flag:
        return

    dup = db.execute(
        "SELECT id, offer_number, status FROM pending_offers WHERE id = 18 AND offer_number = '2026-8464'"
    ).fetchone()
    if dup and dup['status'] == 'pending':
        db.execute(
            "UPDATE pending_offers SET status = 'cancelled', updated_at = ? WHERE id = 18",
            (now_iso(),),
        )
        db.execute(
            "INSERT INTO audit_log (offer_id, action, detail, username, created_at) VALUES (?,?,?,?,?)",
            (
                18,
                'OFFER_CANCELLED',
                'Auto-cancelada por auditoría 2026-04-23: offer_number 2026-8464 duplicado '
                'con oferta #19 (Palmira V2). Esta era la versión previa con proyecto sin nombre.',
                'audit-system',
                now_iso(),
            ),
        )

    max_num = 0
    for row in db.execute("SELECT offer_number FROM pending_offers").fetchall():
        n = row['offer_number'] or ''
        parts = n.split('-')
        if len(parts) == 2 and parts[1].isdigit():
            max_num = max(max_num, int(parts[1]))
    if max_num > 0:
        existing = db.execute("SELECT last_number FROM doc_sequences WHERE prefix = 'OFR'").fetchone()
        if existing:
            if existing['last_number'] < max_num:
                db.execute("UPDATE doc_sequences SET last_number = ? WHERE prefix = 'OFR'", (max_num,))
        else:
            db.execute("INSERT INTO doc_sequences (prefix, last_number) VALUES ('OFR', ?)", (max_num,))

    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ('audit_fixes_20260423_applied', now_iso(), now_iso()),
    )
    db.commit()
    print('[migration] auditoría 2026-04-23 aplicada: duplicado #18 cancelado, secuencia OFR alineada')


def _audit_catalog_fixes_20260423(db: sqlite3.Connection) -> None:
    """Correcciones de catálogo derivadas de la auditoría contra Tarifa Fassa
    Hispania Abril 2026 + Tarifa Gypsotech Abril 2026 + Anexo Gypsotech Nov 2025.

    Arias Group compra FCA Tarancón. Varios SKUs tenían cargado el precio del
    punto de recogida de Fátima-PT o Antas, no Tarancón. Esto hacía que el
    motor aplicara 50%+5% descuento sobre un PVP más bajo del real, dejando
    menos margen del calculado en presupuestos.

    También: bug numérico en pvp_per_m2 de AQUASUPER BA 13mm 1200×2700
    (9.5473 en lugar de 7.1605). No afecta cálculos (se usa unit_price_eur)
    pero sí la visualización si se expone pvp_per_m2.

    Idempotente: flag audit_catalog_fixes_20260423_applied en app_settings.
    Los precios nuevos aplican solo a ofertas futuras — lines_json congela
    el precio vigente al emitir, las ofertas ya enviadas no se recalculan.
    """
    flag = db.execute(
        "SELECT value FROM app_settings WHERE key = 'audit_catalog_fixes_20260423_applied'"
    ).fetchone()
    if flag:
        return

    # SKUs con punto de recogida corregido a Tarancón (PVP verificado contra
    # Tarifa Fassa Hispania Abril 2026, columna "Tarancón/Madrid"). El
    # precio_arias y unit_price se recalculan con descuento compuesto 50%+5%.
    tarancon_fixes = [
        # (sku, pvp_anterior, pvp_tarancon, nombre)
        ('420Y1A',  3.99,  5.48,  'KI 7'),
        ('1188F',   5.43,  7.14,  'FASSACOL ONE GRIS'),
        ('1783Y1A', 5.73,  7.15,  'FASSACOL PRIME GRIS'),
        ('1772Y1A', 11.45, 12.89, 'FASSACOL MULTI BLANCO'),
        ('1774Y1A', 13.67, 14.85, 'FASSACOL FLEX BLANCO'),
        ('1778Y1A', 24.09, 25.58, 'FASSACOL ULTRAFLEX S1 BLANCO'),
        ('1779Y1A', 22.69, 24.19, 'FASSACOL ULTRAFLEX S1 GRIS'),
        ('1077F',   17.03, 18.89, 'AQUAZIP GE 97 Comp A'),
    ]
    updated = 0
    for sku, expected_old, new_pvp, _name in tarancon_fixes:
        row = db.execute(
            'SELECT pvp_eur_unit FROM products WHERE sku = ?', (sku,)
        ).fetchone()
        if not row:
            continue
        current = row['pvp_eur_unit']
        # Solo actualizar si el valor actual coincide con el que detectamos,
        # por si alguien lo corrigió manualmente antes de esta migración.
        if current is None or abs(float(current) - expected_old) > 0.01:
            continue
        new_arias = round(new_pvp * 0.475, 4)  # 50% + 5% compuesto
        db.execute(
            '''UPDATE products
               SET pvp_eur_unit = ?, precio_arias_eur_unit = ?, unit_price_eur = ?
               WHERE sku = ?''',
            (new_pvp, new_arias, new_arias, sku),
        )
        updated += 1

    # Bug numérico pvp_per_m2 AQUASUPER BA 13mm 1200×2700 (P00W003270A0).
    # PVP 23.20€ / (1.2 × 2.7) m² = 7.1605 €/m², no 9.5473.
    row = db.execute(
        "SELECT pvp_eur_unit, pvp_per_m2 FROM products WHERE sku = 'P00W003270A0'"
    ).fetchone()
    if row and row['pvp_per_m2'] and abs(float(row['pvp_per_m2']) - 9.5473) < 0.001:
        correct_m2 = round(float(row['pvp_eur_unit']) / (1.2 * 2.7), 4)
        db.execute(
            'UPDATE products SET pvp_per_m2 = ? WHERE sku = ?',
            (correct_m2, 'P00W003270A0'),
        )
        updated += 1

    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ('audit_catalog_fixes_20260423_applied', str(updated), now_iso()),
    )
    db.commit()
    if updated:
        print(f'[migration] catálogo 2026-04-23: {updated} SKUs corregidos a precio Tarancón + bug pvp_per_m2 AQUASUPER 2700')


def _audit_catalog_fixes_20260423_v2(db: sqlite3.Connection) -> None:
    """Segunda tanda de correcciones de catálogo (decisión comercial 2026-04-23).

    1) STD no-Tarancón → marcar como origen Calliano y ajustar PVP.
       Las 5 placas STD fuera de Tarancón son comerciales para Arias (se
       compran desde Calliano). Se mantiene el SKU actual (con prefijo P)
       para no romper referencias históricas, pero se añade "(origen
       Calliano)" al nombre y se actualiza PVP al precio Calliano €/m².

    2) Placas no comerciales eliminadas: AQUASUPER 13mm 2700/2800,
       AQUASUPER 18mm 2600, GypsoSILENS 13mm 2500/3000. Ninguna está
       referenciada en order_lines ni pending_offers.lines_json
       (verificado antes de la migración).

    3) Descripciones de cintas guardavivos (304064, 304065) corregidas
       a Tarifa Abril 2026 (45m y 153m; precios no cambian).

    4) Dimensiones de tornillos Externa Light (301240/241/244/245)
       corregidas a Tarifa Abril 2026 (Punta Clavo 30/40; Punta Broca
       32/41). Precios no cambian.

    Idempotente mediante flag en app_settings.
    """
    flag = db.execute(
        "SELECT value FROM app_settings WHERE key = 'audit_catalog_fixes_20260423_v2_applied'"
    ).fetchone()
    if flag:
        return

    updated = 0
    deleted = 0

    # 1) STD no-Tarancón → origen Calliano (Gypsotech Abril 2026, columna Calliano €/m²).
    std_calliano = [
        # (sku, m2_per_placa, calliano_eur_m2, pvp_anterior_esperado)
        ('P00A000260A0', 3.12, 5.59, 15.48),  # STD BA 10mm 1200×2600
        ('P00A000270A0', 3.24, 5.59, 16.08),  # STD BA 10mm 1200×2700
        ('P00A003320A0', 3.84, 4.55, 15.48),  # STD BA 13mm 1200×3200
        ('P00A003360A0', 4.32, 4.55, 17.42),  # STD BA 13mm 1200×3600
        ('P00A008250A0', 3.00, 7.15, 17.96),  # STD BA 18mm 1200×2500
    ]
    for sku, m2, rate, expected_old in std_calliano:
        row = db.execute(
            'SELECT pvp_eur_unit, name FROM products WHERE sku = ?', (sku,)
        ).fetchone()
        if not row or row['pvp_eur_unit'] is None:
            continue
        if abs(float(row['pvp_eur_unit']) - expected_old) > 0.01:
            continue
        new_pvp = round(m2 * rate, 2)
        new_arias = round(new_pvp * 0.475, 4)
        new_m2 = round(rate, 4)
        new_name = row['name'] if '(origen Calliano)' in (row['name'] or '') \
            else f"{row['name']} (origen Calliano)"
        db.execute(
            '''UPDATE products
               SET pvp_eur_unit = ?, precio_arias_eur_unit = ?, unit_price_eur = ?,
                   pvp_per_m2 = ?, name = ?
               WHERE sku = ?''',
            (new_pvp, new_arias, new_arias, new_m2, new_name, sku),
        )
        updated += 1

    # 2) Placas no comerciales eliminadas.
    non_commercial = [
        'P00W003270A0',  # AQUASUPER BA 13mm 1200×2700
        'P00W003280A0',  # AQUASUPER BA 13mm 1200×2800
        'P00W008260A0',  # AQUASUPER BA 18mm 1200×2600
        'P00GS03250A0',  # GypsoSILENS BA 13mm 1200×2500
        'P00GS03300A0',  # GypsoSILENS BA 13mm 1200×3000
    ]
    for sku in non_commercial:
        in_orders = db.execute(
            'SELECT 1 FROM order_lines WHERE sku = ? LIMIT 1', (sku,)
        ).fetchone()
        in_offers = db.execute(
            "SELECT 1 FROM pending_offers WHERE lines_json LIKE ? LIMIT 1",
            (f'%{sku}%',),
        ).fetchone()
        if in_orders or in_offers:
            continue  # protección defensiva — no borrar si algo lo referencia
        res = db.execute('DELETE FROM products WHERE sku = ?', (sku,))
        if res.rowcount:
            deleted += 1

    # 3) Cintas guardavivos — descripciones Tarifa Abril 2026 (45m y 153m).
    cintas_fix = [
        ('304064',
         'Cinta Guardavivos 50mm×12,5m — 10 rollos/caja',
         'Cinta Guardavivos 50mm×45m — 54 rollos/caja'),
        ('304065',
         'Cinta Guardavivos 50mm×30m — 10 rollos/caja',
         'Cinta Guardavivos 50mm×153m — 12 rollos/caja'),
    ]
    for sku, old_name, new_name in cintas_fix:
        row = db.execute(
            'SELECT name FROM products WHERE sku = ?', (sku,)
        ).fetchone()
        if row and row['name'] == old_name:
            db.execute(
                'UPDATE products SET name = ? WHERE sku = ?', (new_name, sku)
            )
            updated += 1

    # 4) Tornillos Externa Light — dimensiones Tarifa Abril 2026.
    tornillos_fix = [
        ('301240',
         'Tornillo Exterior Ø4,0×42 — 1.000ud',
         'Tornillo Exterior Punta Clavo Externa Light Ø4,0×30 — 1.000ud'),
        ('301241',
         'Tornillo Exterior Ø4,0×41 — 1.000ud',
         'Tornillo Exterior Punta Clavo Externa Light Ø4,0×40 — 1.000ud'),
        ('301244',
         'Tornillo Exterior Ø4,0×42 — 500ud',
         'Tornillo Exterior Punta Broca Externa Light Ø4,0×32 — 500ud'),
        ('301245',
         'Tornillo Exterior Ø4,0×41 — 500ud',
         'Tornillo Exterior Punta Broca Externa Light Ø4,0×41 — 500ud'),
    ]
    for sku, old_name, new_name in tornillos_fix:
        row = db.execute(
            'SELECT name FROM products WHERE sku = ?', (sku,)
        ).fetchone()
        if row and row['name'] == old_name:
            db.execute(
                'UPDATE products SET name = ? WHERE sku = ?', (new_name, sku)
            )
            updated += 1

    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ('audit_catalog_fixes_20260423_v2_applied',
         f'updated={updated};deleted={deleted}', now_iso()),
    )
    db.commit()
    if updated or deleted:
        print(
            f'[migration] catálogo v2: {updated} SKUs actualizados '
            f'(STD Calliano / cintas / tornillos), {deleted} placas no comerciales eliminadas'
        )


def _audit_logistics_fixes_20260424(db: sqlite3.Connection) -> None:
    """Correcciones de datos logísticos detectadas tras la auditoría de catálogo.

    1) STD origen Calliano — units_per_pallet y sqm_per_pallet estaban con
       valores de Tarancón (carga original), pero al ser placas no servibles
       desde Tarancón vienen de Calliano, donde la densidad de palé es
       distinta (más placas por palé). Impacto: el motor estimate_containers
       sobrestima palés cuando se pide una de estas 5 placas → contenedores
       extra innecesarios en la oferta.

       Tarifa Gypsotech Abril 2026, columna "N° PLACAS/PALÉ CALLIANO (L)":
       - P00A000260A0 STD 10mm 2600: 48→66, 149,76→205,92 m²/palé
       - P00A000270A0 STD 10mm 2700: 48→66, 155,52→213,84 m²/palé
       - P00A003320A0 STD 13mm 3200: 36→40, 138,24→153,60 m²/palé
       - P00A003360A0 STD 13mm 3600: 36→40, 155,52→172,80 m²/palé
       - P00A008250A0 STD 18mm 2500: 24→34,  72,00→102,00 m²/palé

    2) C367038269A Montante 70/37 Z1 — kg_per_unit calculado como si fuera
       2990mm (2,09 kg) pero el SKU indica 2690mm. Corregido a 0,70 kg/ml ×
       2,69 m = 1,88 kg (peso Fassa según anexo perfiles).

    Idempotente mediante flag en app_settings.
    """
    flag = db.execute(
        "SELECT value FROM app_settings WHERE key = 'audit_logistics_fixes_20260424_applied'"
    ).fetchone()
    if flag:
        return

    updated = 0

    # 1) STD Calliano — uds/palé y m²/palé correctos de origen Calliano.
    std_pallet_fix = [
        # (sku, old_upp, new_upp, new_sqm_per_pallet)
        ('P00A000260A0', 48.0, 66.0, 205.92),
        ('P00A000270A0', 48.0, 66.0, 213.84),
        ('P00A003320A0', 36.0, 40.0, 153.60),
        ('P00A003360A0', 36.0, 40.0, 172.80),
        ('P00A008250A0', 24.0, 34.0, 102.00),
    ]
    for sku, expected_old, new_upp, new_sqm in std_pallet_fix:
        row = db.execute(
            'SELECT units_per_pallet FROM products WHERE sku = ?', (sku,)
        ).fetchone()
        if not row or row['units_per_pallet'] is None:
            continue
        if abs(float(row['units_per_pallet']) - expected_old) > 0.01:
            continue
        db.execute(
            '''UPDATE products
               SET units_per_pallet = ?, sqm_per_pallet = ?
               WHERE sku = ?''',
            (new_upp, new_sqm, sku),
        )
        updated += 1

    # 2) Montante 70/37 Z1 2690mm — peso correcto.
    row = db.execute(
        "SELECT kg_per_unit FROM products WHERE sku = 'C367038269A'"
    ).fetchone()
    if row and row['kg_per_unit'] is not None and abs(float(row['kg_per_unit']) - 2.09) < 0.01:
        db.execute(
            "UPDATE products SET kg_per_unit = 1.88 WHERE sku = 'C367038269A'"
        )
        updated += 1

    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ('audit_logistics_fixes_20260424_applied', str(updated), now_iso()),
    )
    db.commit()
    if updated:
        print(f'[migration] logística 2026-04-24: {updated} SKUs corregidos (STD Calliano uds/palé + Montante 70/37 2690 peso)')


def _audit_catalog_completion_20260424(db: sqlite3.Connection) -> None:
    """Completar datos logísticos de consumibles y añadir perfiles Z1 faltantes.

    1) kg_per_unit estimado para 76 SKUs (tornillos, cintas, accesorios,
       GypsoCOMETE, trampillas) que tenían peso NULL. Valores conservadores
       basados en prácticas del sector (acero galvanizado, papel kraft,
       PVC, aluminio o placa yeso alta densidad según material). Se
       marcan como 'estimated' en notes para que se actualicen cuando
       lleguen las fichas técnicas oficiales.

    2) 14 nuevos SKUs de perfiles Z1 del Anexo Gypsotech Nov 2025 que no
       estaban cargados: Montante 48/35 Z1 (7 longitudes), Montante 70/37
       Z1 (4 longitudes faltantes además de 2690), Montante 90/40 Z1
       (2 longitudes faltantes) y Perfil TC 47 Z1 de 5300mm. Precio €/ml
       del anexo × longitud; kg = kg/ml × longitud.

    3) Corrige un error de carga: C344836299B (Montante 48/35 Z2 2990mm)
       tenía PVP 4,186€ (precio €/ml Z1 = 1,40) cuando le corresponde el
       €/ml Z2 = 2,15 → PVP = 6,43€. Afecta precio_arias y unit_price.

    Idempotente: INSERT OR IGNORE para no duplicar, flag en app_settings.
    Las ofertas ya emitidas NO cambian (lines_json congela el precio).
    """
    flag = db.execute(
        "SELECT value FROM app_settings WHERE key = 'audit_catalog_completion_20260424_applied'"
    ).fetchone()
    if flag:
        return

    now = now_iso()
    updated = 0
    inserted = 0

    # 1) Pesos estimados para consumibles sin kg_per_unit.
    # Peso por UNIDAD DE VENTA (caja/rollo/ud/paquete), no por pieza.
    estimated_weights = {
        # TORNILLOS — cajas de tornillo fosfatado/cincado + embalaje
        '304100': 1.00, '304101': 2.00, '304102': 10.00,   # PM Punta Clavo Ø3,5×25
        '304103': 1.40, '304104': 2.80, '304105': 8.40,   # PM Punta Clavo Ø3,5×35
        '304106': 3.60, '304107': 4.40,                    # PM Punta Clavo Ø3,5×45 / 55
        '304108': 3.50, '304109': 4.00,                    # PM Ø4,2×70 / Ø4,8×90
        '304115': 2.20, '304116': 3.00, '304117': 3.80,   # PM Punta Broca Ø3,5×25/35/45
        '304123': 2.30, '304124': 3.20, '304125': 4.10,   # AD Ø3,9×25/35/45
        '304126': 5.00, '304128': 3.50,                    # AD Ø3,9×55 / ×70
        '304133': 0.50, '304134': 1.80, '304135': 2.50,   # MM Punta Broca
        '301240': 3.00, '301241': 3.50,                    # Externa Light Punta Clavo
        '301244': 1.70, '301245': 2.20,                    # Externa Light Punta Broca
        # CINTAS Y MALLAS — peso por rollo
        '304056': 0.20, '304057': 0.60, '304058': 1.20,   # Cinta juntas papel
        '304064': 0.30, '304065': 1.00,                    # Cinta guardavivos
        '304075': 0.50, '304076': 0.70,                    # Banda estanca
        '304078': 0.15, '304079': 0.50,                    # Malla FV autoadhesiva
        '301121': 1.50,                                    # Malla Externa Light
        '700960': 9.00,                                    # Fassanet 160
        # ACCESORIOS PERFILES — peso por caja/paquete completo
        '304014': 3.50, '304015': 2.00,                    # Crucetas TC 47 / 60
        '304021': 7.00, '304022': 10.00, '304023': 7.00,   # Suspensión TC 47 90/180/240
        '304029': 8.00, '304030': 10.00,                   # Anclaje Directo 47/60 ×120
        '304036': 5.00,                                    # Anclaje Universal Omega M6
        '304049': 4.00, '304050': 6.00,                    # Aisladores acústicos
        '304095': 18.00, '304096': 18.00,                  # Varilla roscada M6 1000/2000
        '304097': 1.00,                                    # Manguito cilíndrico
        '1091001Y': 15.00,                                 # Cantonera yeso 2600mm PVC
        # GYPSOCOMETE — placa alta densidad + perfil aluminio + LED
        '301600': 6.00, '301601': 8.00, '301602': 12.00,   # ANGLE/CROSS/STAR 18mm
        '301600XL': 7.00, '301601XL': 9.00, '301602XL': 13.00, '301605XL': 25.00,
        '301606': 0.50, '301607': 0.70, '301606XL': 1.00,  # Recambios pantalla
        # TRAMPILLAS — acero lacado / aluminio / acero+placa fuego
        '304081': 2.50, '304082': 4.00, '304083': 6.00, '304084': 8.00,
        '304085': 2.00, '304086': 3.00, '304087': 5.00, '304088': 7.00,
        '304089': 9.00, '304090': 17.00,
        '301761': 8.00, '301762': 14.00, '301763': 17.00, '301764': 21.00,
        '301461': 12.00, '301462': 22.00, '301463': 27.00, '301464': 33.00,
    }
    for sku, weight in estimated_weights.items():
        row = db.execute(
            'SELECT kg_per_unit, notes FROM products WHERE sku = ?', (sku,)
        ).fetchone()
        if not row:
            continue
        if row['kg_per_unit'] is not None and float(row['kg_per_unit']) > 0:
            continue  # ya tiene peso, respetar
        new_notes = row['notes'] or ''
        tag = '[peso estimado 2026-04-24]'
        if tag not in new_notes:
            new_notes = (new_notes + ' ' + tag).strip()
        db.execute(
            'UPDATE products SET kg_per_unit = ?, notes = ? WHERE sku = ?',
            (weight, new_notes, sku),
        )
        updated += 1

    # 2) Perfiles Z1 faltantes (Anexo Gypsotech Nov 2025).
    # Estructura: (sku, name, longitud_m, eur_ml, kg_ml, uds_pallet, subfamily)
    new_perfiles = [
        ('C344836249A', 'Montante 48/35 Z1 — 2.490mm', 2.49, 1.40, 0.57, 480, 'MONTANTE 48/35'),
        ('C344836259A', 'Montante 48/35 Z1 — 2.590mm', 2.59, 1.40, 0.57, 480, 'MONTANTE 48/35'),
        ('C344836269A', 'Montante 48/35 Z1 — 2.690mm', 2.69, 1.40, 0.57, 480, 'MONTANTE 48/35'),
        ('C344836279A', 'Montante 48/35 Z1 — 2.790mm', 2.79, 1.40, 0.57, 480, 'MONTANTE 48/35'),
        ('C344836299A', 'Montante 48/35 Z1 — 2.990mm', 2.99, 1.40, 0.57, 480, 'MONTANTE 48/35'),
        ('C344836359A', 'Montante 48/35 Z1 — 3.590mm', 3.59, 1.40, 0.57, 480, 'MONTANTE 48/35'),
        ('C344836399A', 'Montante 48/35 Z1 — 3.990mm', 3.99, 1.40, 0.57, 480, 'MONTANTE 48/35'),
        ('C367038279A', 'Montante 70/37 Z1 — 2.790mm', 2.79, 1.78, 0.70, 250, 'MONTANTE 70/37'),
        ('C367038299A', 'Montante 70/37 Z1 — 2.990mm', 2.99, 1.78, 0.70, 250, 'MONTANTE 70/37'),
        ('C367038359A', 'Montante 70/37 Z1 — 3.590mm', 3.59, 1.78, 0.70, 250, 'MONTANTE 70/37'),
        ('C367038399A', 'Montante 70/37 Z1 — 3.990mm', 3.99, 1.78, 0.70, 250, 'MONTANTE 70/37'),
        ('C399041359A', 'Montante 90/40 Z1 — 3.590mm', 3.59, 2.23, 0.82, 200, 'MONTANTE 90/40'),
        ('C399041399A', 'Montante 90/40 Z1 — 3.990mm', 3.99, 2.23, 0.82, 200, 'MONTANTE 90/40'),
        ('C174717530A', 'Perfil TC 47 Z1 — 5.300mm',    5.30, 1.13, 0.44, 1080, 'TC 47'),
    ]
    for sku, name, longitud, eur_ml, kg_ml, upp, subfamily in new_perfiles:
        exists = db.execute(
            'SELECT 1 FROM products WHERE sku = ?', (sku,)
        ).fetchone()
        if exists:
            continue
        pvp = round(eur_ml * longitud, 4)
        precio_arias = round(pvp * 0.475, 4)  # 50% + 5% compuesto
        kg = round(kg_ml * longitud, 2)
        db.execute(
            '''INSERT INTO products
               (sku, name, category, source_catalog, unit, unit_price_eur,
                kg_per_unit, units_per_pallet, pvp_eur_unit,
                precio_arias_eur_unit, discount_pct, discount_extra_pct,
                subfamily, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (sku, name, 'PERFILES', 'ANEXO-Nov2025', 'barra',
             precio_arias, kg, float(upp), pvp, precio_arias,
             50.0, 5.0, subfamily,
             '[añadido auditoría 2026-04-24]'),
        )
        inserted += 1

    # 3) Corregir Montante 48/35 Z2 2990mm (cargado con precio Z1).
    # PVP correcto: 2,15 €/ml × 2,99 m = 6,4285€.
    row = db.execute(
        "SELECT pvp_eur_unit FROM products WHERE sku = 'C344836299B'"
    ).fetchone()
    if row and row['pvp_eur_unit'] is not None and abs(float(row['pvp_eur_unit']) - 4.186) < 0.01:
        new_pvp = 6.4285
        new_arias = round(new_pvp * 0.475, 4)
        db.execute(
            '''UPDATE products
               SET pvp_eur_unit = ?, precio_arias_eur_unit = ?, unit_price_eur = ?
               WHERE sku = ?''',
            (new_pvp, new_arias, new_arias, 'C344836299B'),
        )
        updated += 1

    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ('audit_catalog_completion_20260424_applied',
         f'updated={updated};inserted={inserted}', now),
    )
    db.commit()
    if updated or inserted:
        print(
            f'[migration] catálogo completado 2026-04-24: '
            f'{updated} SKUs actualizados (pesos + Montante 48/35 Z2), '
            f'{inserted} perfiles Z1 nuevos insertados'
        )


def _cleanup_demo_data_20260425(db: sqlite3.Connection) -> None:
    """Elimina cliente y proyecto 'demo' del seed inicial.

    El seed creaba un cliente "Promotor Demo / Arias Group Demo" con un
    proyecto "Torre piloto - baños" para que la app no estuviera vacía
    en demos. Tras la audit 2026-04-25 con datos reales en producción,
    Oliver lo considera ruido visual.

    Idempotente: marca flag en app_settings, comprueba que el demo no
    tenga ofertas asociadas (defensivo).
    """
    flag = db.execute(
        "SELECT value FROM app_settings WHERE key = 'cleanup_demo_data_20260425_applied'"
    ).fetchone()
    if flag:
        return

    # Identificar cliente demo por email canónico (más robusto que ID).
    demo = db.execute(
        "SELECT id FROM clients WHERE email = 'demo@example.com' OR name = 'Promotor Demo' LIMIT 1"
    ).fetchone()
    deleted_clients = 0
    deleted_projects = 0
    deleted_events = 0

    if demo:
        demo_id = demo['id']
        # Verificación defensiva: si alguna oferta lo referencia, abortar.
        ofertas_demo = db.execute(
            "SELECT COUNT(*) AS c FROM pending_offers "
            "WHERE client_name IN (SELECT name FROM clients WHERE id = ?)",
            (demo_id,),
        ).fetchone()['c']
        if ofertas_demo == 0:
            # Cascada manual: stage_events → projects → clients.
            res = db.execute(
                "DELETE FROM stage_events WHERE project_id IN "
                "(SELECT id FROM projects WHERE client_id = ?)",
                (demo_id,),
            )
            deleted_events = res.rowcount
            res = db.execute("DELETE FROM projects WHERE client_id = ?", (demo_id,))
            deleted_projects = res.rowcount
            res = db.execute("DELETE FROM clients WHERE id = ?", (demo_id,))
            deleted_clients = res.rowcount

    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ('cleanup_demo_data_20260425_applied',
         f'clients={deleted_clients};projects={deleted_projects};events={deleted_events}',
         now_iso()),
    )
    db.commit()
    if deleted_clients or deleted_projects:
        print(
            f'[migration] demo data eliminada: {deleted_clients} cliente(s), '
            f'{deleted_projects} proyecto(s), {deleted_events} stage_events'
        )


def _audit_misc_20260425(db: sqlite3.Connection) -> None:
    """Decisiones operativas Oliver 2026-04-25:

    1) Eliminar MM 30 GRIS (SKU 611Y1A) — descatalogado de operativa Arias.
       Solo elimina si el SKU no aparece en order_lines ni en
       pending_offers.lines_json (defensivo).

    2) FX EUR/USD oficial corregido a 1.18 (no 1.085 como decía el
       'Manual Abril 2026'). El 1.085 estaba desfasado; el cambio real
       de mercado es 1.18 según Oliver. Las ofertas históricas con FX
       1.18 (#11, #13, #16, #18, #19) estaban bien — eran correctas.

    Idempotente vía flag en app_settings.
    """
    flag = db.execute(
        "SELECT value FROM app_settings WHERE key = 'audit_misc_20260425_applied'"
    ).fetchone()
    if flag:
        return

    deleted_mm30 = 0
    in_orders = db.execute("SELECT 1 FROM order_lines WHERE sku = '611Y1A' LIMIT 1").fetchone()
    in_offers = db.execute(
        "SELECT 1 FROM pending_offers WHERE lines_json LIKE '%611Y1A%' LIMIT 1"
    ).fetchone()
    if not in_orders and not in_offers:
        cursor = db.execute("DELETE FROM products WHERE sku = '611Y1A'")
        deleted_mm30 = cursor.rowcount

    # FX 1.18 — nuevo registro en fx_rates (mantiene histórico) +
    # actualizar app_settings.
    db.execute(
        "INSERT INTO fx_rates (base_currency, target_currency, rate, updated_at, source) "
        "VALUES ('EUR', 'USD', 1.18, ?, ?)",
        (now_iso(), 'Manual 2026-04-25 (corrección Oliver)'),
    )
    db.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES ('fx_eur_usd', ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        ('1.18', now_iso()),
    )

    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ('audit_misc_20260425_applied',
         f'mm30_deleted={deleted_mm30};fx_set=1.18', now_iso()),
    )
    db.commit()
    print(
        f'[migration] misc 2026-04-25: MM 30 eliminado ({deleted_mm30}), '
        f'FX EUR/USD actualizado a 1.18'
    )


def _schema_cleanup_and_client_fk_20260425(db: sqlite3.Connection) -> None:
    """Limpieza de schema decidida en auditoría 2026-04-25:

    1) DROP `pickup_pricing` — tabla muerta. 0 referencias en código fuera del
       CREATE original (idea de pickup points alternativos que no se llevó a
       producción). Solo se borra si está vacía (defensivo).

    2) ADD `pending_offers.client_id INTEGER` — el enlace actual a clientes va
       por `client_name` (texto), lo que rompe silenciosamente si renombras al
       cliente. Backfill por matching exacto sobre `clients.name` o
       `clients.company`. `client_name` se mantiene por compat (no lo
       borramos: las ofertas históricas son inmutables y su texto es contrato).

    Idempotente vía flag en app_settings. El paso 2 también es idempotente
    porque _safe_add_column comprueba si la columna ya existe.
    """
    flag = db.execute(
        "SELECT value FROM app_settings WHERE key = 'schema_cleanup_and_client_fk_20260425'"
    ).fetchone()
    if flag:
        return

    # 1) Drop pickup_pricing si existe y está vacía.
    pp_dropped = False
    pp_exists = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pickup_pricing'"
    ).fetchone()
    if pp_exists:
        rows = db.execute('SELECT COUNT(*) AS c FROM pickup_pricing').fetchone()['c']
        if rows == 0:
            db.execute('DROP TABLE pickup_pricing')
            pp_dropped = True
        else:
            print(f'[migration] pickup_pricing tiene {rows} filas — no se borra (revisar manualmente)')

    # 2) Añadir client_id a pending_offers + backfill.
    offer_cols = {r[1] for r in db.execute("PRAGMA table_info(pending_offers)").fetchall()}
    backfilled = 0
    if 'client_id' not in offer_cols:
        _safe_add_column(db, 'pending_offers', 'client_id', 'INTEGER')
        # Backfill: emparejar offer.client_name con clients.name o clients.company.
        cursor = db.execute("""
            UPDATE pending_offers
            SET client_id = (
                SELECT id FROM clients
                WHERE clients.name = pending_offers.client_name
                   OR clients.company = pending_offers.client_name
                LIMIT 1
            )
            WHERE client_id IS NULL
        """)
        backfilled = cursor.rowcount
        # Diagnóstico: ofertas que NO encontraron cliente (texto fuera de DB).
        orphan = db.execute(
            "SELECT COUNT(*) AS c FROM pending_offers WHERE client_id IS NULL"
        ).fetchone()['c']
        if orphan:
            print(
                f'[migration] {orphan} oferta(s) sin client_id tras backfill — '
                f'su client_name no matchea ningún clients.name/company. '
                f'Revisa manualmente o crea el cliente.'
            )

    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ('schema_cleanup_and_client_fk_20260425',
         f'pickup_pricing_dropped={pp_dropped};client_id_backfilled={backfilled}',
         now_iso()),
    )
    db.commit()
    print(
        f'[migration] schema cleanup 2026-04-25: pickup_pricing dropped={pp_dropped}, '
        f'pending_offers.client_id backfilled={backfilled}'
    )


def _catalog_discount_completion_20260425(db: sqlite3.Connection) -> None:
    """Auditoría de catálogo 2026-04-25 (Oliver):

    1) `discount_extra_pct` estaba NULL en 191 SKUs cuando debería ser 5.0.
       El precio Arias YA reflejaba el descuento compuesto (50%+5% = ratio
       0,475 sobre PVP), pero la columna no lo declaraba. Cosmético, pero
       confunde la UI y rompe queries de auditoría. Backfill a 5.0.

    2) 2 SKUs FASSACOL (PASTAS) tenían precio_arias desviado del descuento
       estándar — no era política comercial diferente, era un error de
       carga. Se corrige a `precio_arias_eur_unit = pvp_eur_unit × 0,475`:
         - 1773Y1A FASSACOL MULTI GRIS: 5,83 → 5,52 €
         - 1775Y1A FASSACOL FLEX GRIS: 6,60 → 6,27 €
       También se actualiza `unit_price_eur` (campo legacy que el motor
       todavía lee y debe quedar sincronizado con `precio_arias_eur_unit`).

    REGLA OFERTAS INMUTABLES: las 4 ofertas que ya contienen estos SKUs
    (#13, #18, #19, #21) NO se tocan. `lines_json` y `total_final_eur`
    son contractuales — el cliente firmó con el precio del momento. El
    nuevo precio aplica solo a futuras ofertas.

    Idempotente vía flag en app_settings.
    """
    flag = db.execute(
        "SELECT value FROM app_settings WHERE key = 'catalog_discount_completion_20260425'"
    ).fetchone()
    if flag:
        return

    # 1) Backfill discount_extra_pct = 5.0 donde sea NULL.
    cursor = db.execute(
        "UPDATE products SET discount_extra_pct = 5.0 WHERE discount_extra_pct IS NULL"
    )
    extra_backfilled = cursor.rowcount

    # 2) Corregir las 2 PASTAS desviadas. PVP × 0,475 redondeado a 2 decimales.
    fassacol_fixes = [
        ('1773Y1A', 5.52),  # PVP 11,63 × 0,475 = 5,52425 → 5,52
        ('1775Y1A', 6.27),  # PVP 13,20 × 0,475 = 6,27000 → 6,27
    ]
    pastas_fixed = 0
    for sku, new_arias in fassacol_fixes:
        cur = db.execute(
            "UPDATE products SET unit_price_eur = ?, precio_arias_eur_unit = ? "
            "WHERE sku = ? AND ABS(precio_arias_eur_unit - ?) > 0.01",
            (new_arias, new_arias, sku, new_arias),
        )
        pastas_fixed += cur.rowcount

    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ('catalog_discount_completion_20260425',
         f'extra_pct_backfilled={extra_backfilled};fassacol_fixed={pastas_fixed}',
         now_iso()),
    )
    db.commit()
    print(
        f'[migration] catalog 2026-04-25: discount_extra_pct backfilled='
        f'{extra_backfilled}, FASSACOL precios corregidos={pastas_fixed} '
        f'(ofertas históricas intactas)'
    )


# ── Datos extraídos del PDF ANEXO GYPSOTECH NOVIEMBRE 2025 (perfiles) ──
# Esto es la **fuente de verdad** para `kg_per_ml` de PERFILES Fassa, que
# permite recalcular `kg_per_unit` real (sin estimación) como
#   kg_per_unit_real = kg_per_ml × (longitud_mm / 1000)
# Los códigos son los SKUs Arias (= Cód. Artículo Fassa).
# Si Fassa publica un anexo nuevo, esta tabla se actualiza aquí.
_PERFILES_KG_PER_ML_BY_PREFIX = {
    # PERFILES — prefijo del SKU (sin últimos 4 chars de longitud) → kg/ml
    'C344836': 0.57,    # MONTANTE 48/35 (Z1 y Z2)
    'C367038': 0.70,    # MONTANTE 70/37
    'C399041': 0.82,    # MONTANTE 90/40
    'C3910041': 0.87,   # MONTANTE 100/40
    'C4612548': 0.98,   # MONTANTE 125/47
    'C4615048': 1.10,   # MONTANTE 150/47
    'U304830': 0.45,    # RAIL 48
    'U307030': 0.55,    # RAIL 70 Z1 (Z2 = 0.57)
    'U309030': 0.64,    # RAIL 90 Z1 (Z2 = 0.69)
    'U3010030': 0.70,   # RAIL 100
    'U3512535': 0.81,   # RAIL 125
    'U4015040': 0.91,   # RAIL 150
    'C174717': 0.44,    # PERFIL TC 47
    'C154830': 0.45,    # PERFIL TC 48
    'C286028': 0.60,    # PERFIL TC 60
    'U472447': 0.53,    # PERFIL SIERRA TC 47/48/60
    'L233430':  0.25,   # PERFIL ANGULAR Z1 y Z2
    'U201928': 0.24,    # PERFIL CLIP
    'U193019': 0.33,    # PERFIL U 30
    'E168218': 0.0,     # OMEGA — peso no publicado
}


def _catalog_real_data_from_pdf_20260425(db: sqlite3.Connection) -> None:
    """Schema completo de catálogo + backfill desde Tarifa Fassa 2026.

    Datos extraídos de:
      - Tarifa Gypsotech Abril 2026 (placas, pastas, tornillos, trampillas,
        accesorios, cintas, GypsoCOMETE)
      - Anexo Gypsotech Noviembre 2025 (perfiles, accesorios, tornillos,
        trampillas con uds/palé real y kg/ml para perfiles)

    Cambios:

    1) NUEVAS COLUMNAS estructuradas en `products`:
       - Dimensiones: length_mm, width_mm, thickness_mm, dim_a_mm, dim_b_mm,
         dim_c_mm, diameter_mm, espesor_acero_mm
       - Empaquetado: kg_per_ml, box_units (separa uds/caja del uds/palé real),
         peso_saco_kg
       - Comercial: min_order_qty, dispo_tarancon, tariff_origen,
         pvp_calliano_eur, pvp_onda_lerida_eur, pvp_antas_eur
       - Metadata: norma_text, color, description_long, tiempo_trabajab_min

    2) BACKFILL automático desde el `name` y `pack_size` (regex en SQL):
       - length_mm, width_mm para placas (de "1200×2500" → 1200 y 2500)
       - thickness_mm para placas (de "BA 13 MM" → 13)
       - longitud para perfiles (de "— 2.490mm" → 2490)
       - dimensiones para tornillos (de "Ø3,5×25" → diameter 3.5, length 25)
       - box_units para tornillos/cintas/accesorios (de "— 1.000ud" → 1000)

    3) BACKFILL kg_per_ml para PERFILES desde tabla embebida _PERFILES_KG_PER_ML.
       A partir de eso, recálculo de kg_per_unit real (= kg_per_ml × longitud_m)
       para los 41 SKUs de perfiles → desaparece la marca [peso estimado] de
       toda la familia.

    4) DROP de columnas cache que generan drift:
       - products.pvp_per_m2 (calculable: pvp_eur_unit × upp / sqm_pp)
       - products.precio_arias_m2 (idem con precio_arias_eur_unit)

    5) DEFAULT operativo:
       - dispo_tarancon = 'green' para todos (afinable luego desde UI admin)

    REGLA OFERTAS INMUTABLES: SOLO se toca `products`. Las ofertas existentes
    quedan intactas (lines_json es contractual).

    Idempotente vía flag en app_settings.
    """
    flag = db.execute(
        "SELECT value FROM app_settings WHERE key = 'catalog_real_data_from_pdf_20260425'"
    ).fetchone()
    if flag:
        return

    # 1) Schema: nuevas columnas (idempotente vía _safe_add_column).
    new_cols = [
        # Dimensiones físicas estructuradas
        ('length_mm', 'INTEGER'),
        ('width_mm', 'INTEGER'),
        ('thickness_mm', 'REAL'),
        ('dim_a_mm', 'REAL'),
        ('dim_b_mm', 'REAL'),
        ('dim_c_mm', 'REAL'),
        ('diameter_mm', 'REAL'),
        ('espesor_acero_mm', 'REAL'),
        # Empaquetado
        ('kg_per_ml', 'REAL'),
        ('box_units', 'INTEGER'),
        ('peso_saco_kg', 'REAL'),
        # Comercial
        ('min_order_qty', 'INTEGER'),
        ('dispo_tarancon', 'TEXT'),
        ('tariff_origen', 'TEXT'),
        ('pvp_calliano_eur', 'REAL'),
        ('pvp_onda_lerida_eur', 'REAL'),
        ('pvp_antas_eur', 'REAL'),
        # Metadata
        ('norma_text', 'TEXT'),
        ('color', 'TEXT'),
        ('description_long', 'TEXT'),
        ('tiempo_trabajab_min', 'INTEGER'),
    ]
    existing = {r[1] for r in db.execute('PRAGMA table_info(products)').fetchall()}
    cols_added = 0
    for col, typ in new_cols:
        if col not in existing:
            _safe_add_column(db, 'products', col, typ)
            cols_added += 1

    # 2) Default dispo_tarancon = 'green' donde sea NULL (sin info contraria).
    db.execute("UPDATE products SET dispo_tarancon = 'green' WHERE dispo_tarancon IS NULL")

    # 3) BACKFILL via regex SQL (sin Python). SQLite regex es limitado, usamos
    #    LIKE con wildcards y substr() — más torpe pero portable.

    # 3a) PLACAS: extraer thickness desde "BA 13mm" (lowercase en la DB Arias).
    #     LIKE en SQLite es case-insensitive por defecto para ASCII.
    placas_thick = [(6, '%BA 6mm%'), (9.5, '%BA 9,5mm%'), (12.5, '%BA 13mm%'),
                    (15, '%BA 15mm%'), (18, '%BA 18mm%'), (20, '%BA 20mm%'),
                    (25, '%BA 25mm%')]
    for thick, like in placas_thick:
        db.execute(
            "UPDATE products SET thickness_mm = ? "
            "WHERE category = 'PLACAS' AND name LIKE ? AND thickness_mm IS NULL",
            (thick, like),
        )

    # 3b) PLACAS: width_mm = 1200 (todas las placas Fassa estándar).
    db.execute(
        "UPDATE products SET width_mm = 1200 "
        "WHERE category = 'PLACAS' AND width_mm IS NULL"
    )

    # 3c) PLACAS: extraer length_mm de content_per_unit "1200×2500 mm (...)".
    #     Patrón: el segundo número entre × y mm.
    placas = db.execute(
        "SELECT id, content_per_unit FROM products "
        "WHERE category = 'PLACAS' AND length_mm IS NULL AND content_per_unit LIKE '1200×%'"
    ).fetchall()
    placas_filled = 0
    for p in placas:
        try:
            # "1200×2500 mm (3.00 m²/placa)" → 2500
            after_x = p['content_per_unit'].split('×', 1)[1]
            length = int(''.join(c for c in after_x.split(' ', 1)[0] if c.isdigit()))
            if 500 <= length <= 4000:
                db.execute('UPDATE products SET length_mm = ? WHERE id = ?', (length, p['id']))
                placas_filled += 1
        except (ValueError, IndexError):
            pass

    # 3d) PLACAS: tariff_origen del prefijo del SKU (P → Tarancón, L → Calliano).
    db.execute(
        "UPDATE products SET tariff_origen = 'Tarancón' "
        "WHERE category = 'PLACAS' AND sku LIKE 'P%' AND tariff_origen IS NULL"
    )
    db.execute(
        "UPDATE products SET tariff_origen = 'Calliano' "
        "WHERE category = 'PLACAS' AND sku LIKE 'L%' AND tariff_origen IS NULL"
    )

    # 3e) PERFILES: backfill kg_per_ml desde la tabla embebida + recalcular kg_per_unit.
    perfiles_filled = 0
    perfiles_kg_recalc = 0
    perfiles = db.execute(
        "SELECT id, sku, name, kg_per_unit FROM products WHERE category = 'PERFILES'"
    ).fetchall()
    for pr in perfiles:
        sku = pr['sku'] or ''
        kg_ml = None
        for prefix, value in _PERFILES_KG_PER_ML_BY_PREFIX.items():
            if sku.startswith(prefix):
                kg_ml = value
                break
        if kg_ml is None or kg_ml <= 0:
            continue
        db.execute('UPDATE products SET kg_per_ml = ? WHERE id = ?', (kg_ml, pr['id']))
        perfiles_filled += 1
        # Extraer longitud:
        # 1) Primero del nombre "— 2.490mm" / "— 3.000mm" si está presente.
        # 2) Si no, de los últimos 4 chars del SKU Fassa (`xxxA`/`xxxB` →
        #    primeros 3 dígitos × 10 = longitud_mm). Ej: C344836249A → 249 → 2490mm.
        name = pr['name'] or ''
        length_mm = None
        if '—' in name:
            tail = name.split('—', 1)[1].strip()
            digits = tail.replace('.', '').replace(' ', '').replace('mm', '')
            try:
                cand = int(digits)
                if 500 <= cand <= 6000:
                    length_mm = cand
            except ValueError:
                pass
        if length_mm is None:
            # Patrón Fassa: últimos 3 dígitos antes del sufijo de variante × 10.
            # Sufijo puede ser sólo letras ('A'/'BA'/'B') o letra+número ('Z1'/'Z2').
            # Ej: C344836249A   → 249 → 2490mm  (sufijo A)
            #     C1548300BA    → 300 → 3000mm  (sufijo BA)
            #     E168218300Z1  → 300 → 3000mm  (sufijo Z1)
            m = re.search(r'(\d{3})[A-Z][A-Z0-9]*$', sku)
            if m:
                cand = int(m.group(1)) * 10
                if 500 <= cand <= 6000:
                    length_mm = cand
        if length_mm:
            db.execute('UPDATE products SET length_mm = ? WHERE id = ?', (length_mm, pr['id']))
            kg_real = round(kg_ml * (length_mm / 1000.0), 3)
            db.execute(
                'UPDATE products SET kg_per_unit = ? WHERE id = ?',
                (kg_real, pr['id']),
            )
            perfiles_kg_recalc += 1
    # Limpia la marca [peso estimado] de los perfiles que ahora tienen peso real.
    db.execute(
        "UPDATE products SET notes = REPLACE(REPLACE(notes, '[peso estimado]', ''), "
        "'[peso estimado] ', '') WHERE category = 'PERFILES' AND kg_per_ml > 0"
    )

    # 3f) PASTAS: peso_saco_kg de pack_size ("5 kg" / "10 kg" / "25 kg").
    for kg in (5.0, 10.0, 12.0, 15.0, 20.0, 25.0):
        db.execute(
            "UPDATE products SET peso_saco_kg = ? "
            "WHERE category = 'PASTAS' AND peso_saco_kg IS NULL "
            "AND (pack_size LIKE ? OR content_per_unit LIKE ?)",
            (kg, f'%{int(kg) if kg == int(kg) else kg} kg%',
             f'%{int(kg) if kg == int(kg) else kg} kg%'),
        )

    # 3g) PASTAS: color por defecto blanco (revisable desde UI admin).
    db.execute(
        "UPDATE products SET color = 'Blanco' "
        "WHERE category = 'PASTAS' AND color IS NULL"
    )

    # 3h) TORNILLOS / CINTAS / ACCESORIOS: box_units extraído del name.
    #     Patrón: "— 1.000ud" / "— 24 rollos/caja" / "— 50 unidades".
    #     SQLite no tiene regex robusta, usamos UPDATE por valores comunes.
    for n in (250, 500, 1000, 3000, 5000):
        db.execute(
            "UPDATE products SET box_units = ? "
            "WHERE box_units IS NULL AND category IN ('TORNILLOS', 'CINTAS', 'ACCESORIOS') "
            "AND (name LIKE ? OR name LIKE ?)",
            (n, f'%— {n}ud%', f'%— {n}.000ud%' if n == 1000 else f'%— {n} ud%'),
        )

    # 3i) Norma textual por familia (defaults Fassa).
    norma_by_family = {
        'PLACAS': 'UNE EN 520',
        'PERFILES': 'UNE 14195 / EN 14195',
        'PASTAS': 'UNE EN 13963',
        'CINTAS': 'UNE EN 13963',
    }
    for fam, norma in norma_by_family.items():
        db.execute(
            'UPDATE products SET norma_text = ? WHERE category = ? AND norma_text IS NULL',
            (norma, fam),
        )

    # 3j) GYPSOCOMETE: dispo='yellow' (todos bajo pedido según PDF abril 2026).
    db.execute(
        "UPDATE products SET dispo_tarancon = 'yellow' WHERE category = 'GYPSOCOMETE'"
    )

    # 4) Cleanup: drop columnas cache (calculables on-the-fly).
    cache_dropped = []
    for col in ('pvp_per_m2', 'precio_arias_m2'):
        if col in existing:
            try:
                db.execute(f'ALTER TABLE products DROP COLUMN {col}')
                cache_dropped.append(col)
            except sqlite3.OperationalError:
                pass  # SQLite < 3.35 no soporta DROP COLUMN — saltamos silenciosamente.

    # Marcar migración aplicada.
    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ('catalog_real_data_from_pdf_20260425',
         f'cols_added={cols_added};placas_length={placas_filled};'
         f'perfiles_kg_ml={perfiles_filled};perfiles_kg_recalc={perfiles_kg_recalc};'
         f'cache_dropped={",".join(cache_dropped) or "none"}',
         now_iso()),
    )
    db.commit()
    print(
        f'[migration] catalog real-data 2026-04-25: {cols_added} cols nuevas, '
        f'{placas_filled} placas con length_mm, {perfiles_filled} perfiles con kg/ml, '
        f'{perfiles_kg_recalc} perfiles con kg_per_unit recalculado real, '
        f'cache dropped: {cache_dropped or "none"}'
    )


# ── Pesos reales aportados por Oliver 2026-04-25 (cintas, tornillos,
# accesorios, trampillas, GypsoCOMETE). Derivados de proformas + albaranes.
# Cada fila: (sku, kg_per_unit_final_en_unidad_de_venta, fuente_nota).
# La regla de unidad de venta sigue lo que publica el catálogo Fassa:
#   TORNILLOS, ACCESORIOS, GYPSOCOMETE → caja/embalaje
#   CINTAS                              → rollo
#   TRAMPILLAS                          → unidad
# Cuando Oliver dio el peso por unidad individual y la unidad de venta es
# caja, multiplicamos por uds/caja para alinear con el motor logístico
# (qty × kg_per_unit donde qty va en la unidad de venta).
_REAL_WEIGHTS_20260425 = [
    # CINTAS (kg/rollo, directo)
    ('304056', 0.28),  # Cinta Juntas 50mm×23m
    ('304057', 0.60),  # Cinta Juntas 50mm×75m
    ('304058', 1.15),  # Cinta Juntas 50mm×150m
    ('301121', 5.60),  # Malla Externa Light 50m
    ('304075', 0.53),  # Banda Estanca 50mm×30m
    # TORNILLOS (kg/caja, directo)
    ('304101', 1.40),  # PM Punta Clavo Ø3,5×25 — 1.000ud
    ('304104', 1.84),  # PM Punta Clavo Ø3,5×35 — 1.000ud
    ('304115', 1.45),  # PM Punta Broca Ø3,5×25 — 1.000ud
    ('304134', 1.05),  # MM Punta Broca Ø3,5×9,5 — 1.000ud
    ('301244', 1.15),  # Externa Light Ø4,0×32 — 500ud
    # TRAMPILLAS (kg/unidad, directo)
    ('304081', 1.25),  # Trampilla Click Metálica 300×300
    ('304082', 1.80),  # Trampilla Click Metálica 400×400
    ('304086', 2.10),  # Trampilla Click Aluminio Aqua H1 300×300
    # ACCESORIOS (Oliver dio kg/unidad → multiplicar × uds/caja)
    # Cruceta TC 60: 0.05 kg/ud × 25 ud/caja = 1.25 kg/caja
    ('304015', 1.25),
    # Suspensión TC 47 90mm: 0.06 × 100 = 6.00 kg/caja
    ('304021', 6.00),
    # Cantonera Yeso 2.6m: 0.65 × 100 = 65.00 kg/caja
    ('1091001Y', 65.00),
    # GYPSOCOMETE (Oliver dio kg/unidad → multiplicar × uds/embalaje)
    # ANGLE 240×240: 0.45 × 2 = 0.90 kg/embalaje
    ('301600', 0.90),
    # LINE 2000mm — solo existe la versión XL en la DB; aplico ahí: 2.40 × 5 = 12 kg/caja
    ('301605XL', 12.00),
]


def _catalog_real_weights_20260425(db: sqlite3.Connection) -> None:
    """Pesos reales aportados por Oliver 2026-04-25 para 17 SKUs de cintas,
    tornillos, accesorios, trampillas y GypsoCOMETE.

    Regla de unidad de venta (según catálogo Fassa Abril 2026):
      - TORNILLOS, ACCESORIOS, GYPSOCOMETE → caja/embalaje
      - CINTAS                              → rollo
      - TRAMPILLAS                          → unidad

    Cuando Oliver dio el peso por unidad individual y la unidad de venta es
    caja (accesorios + GypsoCOMETE), se multiplica por uds/caja para que el
    motor logístico (qty × kg_per_unit, qty en unidad de venta) calcule peso
    correcto. Eso ya está hecho en la tabla embebida _REAL_WEIGHTS_20260425.

    Pendientes de confirmación (no se aplican):
      - 304000 Pieza Empalme TC 47 (no existe en DB; Oliver pendiente confirmar
        si quería decir 304007).
      - 304015 — el peso 0,05 kg corresponde a "Cruceta TC 47" según Oliver,
        pero en la DB el SKU 304015 es CRUCETA TC 60 (304014 es la TC 47).
        Aquí se aplica el peso al SKU 304015 (TC 60) calculado como 0,05 × 25
        uds/caja = 1,25 kg/caja, asumiendo que Oliver se refería al SKU que
        existe; pendiente confirmar si quería al 304014.

    Limpia la marca [peso estimado] de los notes de cada SKU actualizado.
    Idempotente vía flag en app_settings.
    """
    flag = db.execute(
        "SELECT value FROM app_settings WHERE key = 'catalog_real_weights_20260425'"
    ).fetchone()
    if flag:
        return

    updated = 0
    not_found = []
    for sku, kg in _REAL_WEIGHTS_20260425:
        cur = db.execute(
            "UPDATE products SET kg_per_unit = ?, "
            "notes = TRIM(REPLACE(REPLACE(COALESCE(notes, ''), "
            "'[peso estimado 2026-04-24]', ''), '[peso estimado]', '')) "
            "WHERE sku = ?",
            (kg, sku),
        )
        if cur.rowcount == 0:
            not_found.append(sku)
        else:
            updated += cur.rowcount

    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ('catalog_real_weights_20260425',
         f'updated={updated};not_found={",".join(not_found) or "none"}',
         now_iso()),
    )
    db.commit()
    print(
        f'[migration] catalog real-weights 2026-04-25: {updated} SKUs con peso '
        f'real aplicado (cintas+tornillos+accesorios+trampillas+gypsocomete). '
        f'Not found: {not_found or "none"}'
    )


# ── Datos extraídos del Anexo Gypsotech Noviembre 2025 (PERFILES) ──
# Cada entrada: prefijo del SKU (sin últimos 4 chars de longitud) →
# {upp_real, min_order, dim_a_mm, dim_b_mm, dim_c_mm, espesor_acero_mm}
# Los uds/palé son los REALES (no uds/caja). Permite limpiar el lío histórico
# de "units_per_pallet hospitando uds/caja" en perfiles.
_PERFILES_EXTRAS_BY_PREFIX = {
    # MONTANTES
    'C344836':  {'upp': 480,  'min': 10, 'a': 34, 'b': 46.5,  'c': 36, 'esp': 0.60},
    'C367038':  {'upp': 250,  'min': 10, 'a': 36, 'b': 69.5,  'c': 38, 'esp': 0.60},
    'C399041':  {'upp': 200,  'min': 10, 'a': 39, 'b': 88.5,  'c': 41, 'esp': 0.60},
    'C3910041': {'upp': 160,  'min': 8,  'a': 39, 'b': 98.5,  'c': 41, 'esp': 0.60},
    'C4612548': {'upp': 120,  'min': 4,  'a': 46, 'b': 123.5, 'c': 48, 'esp': 0.60},
    'C4615048': {'upp': 120,  'min': 4,  'a': 46, 'b': 148.5, 'c': 48, 'esp': 0.60},
    # RAILS
    'U304830':  {'upp': 560,  'min': 10, 'a': 30, 'b': 48,    'c': 30, 'esp': 0.55},
    'U307030':  {'upp': 350,  'min': 10, 'a': 30, 'b': 70,    'c': 30, 'esp': 0.55},
    'U309030':  {'upp': 280,  'min': 10, 'a': 30, 'b': 90,    'c': 30, 'esp': 0.55},
    'U3010030': {'upp': 160,  'min': 8,  'a': 30, 'b': 100,   'c': 30, 'esp': 0.55},
    'U3512535': {'upp': 120,  'min': 4,  'a': 35, 'b': 125,   'c': 35, 'esp': 0.55},
    'U4015040': {'upp': 120,  'min': 4,  'a': 40, 'b': 150,   'c': 40, 'esp': 0.55},
    # OTROS
    'E168218':  {'upp': 600,  'min': 10, 'a': 18, 'b': 82,    'c': 16, 'esp': 0.55},
    'C174717':  {'upp': 1440, 'min': 10, 'a': 18, 'b': 47,    'c': 18, 'esp': 0.60},
    'C1548300': {'upp': 1440, 'min': 10, 'a': 15, 'b': 48,    'c': 15, 'esp': 0.60},
    'C286028':  {'upp': 480,  'min': 10, 'a': 28, 'b': 60,    'c': 28, 'esp': 0.60},
    'U472447':  {'upp': 600,  'min': 10, 'a': 47, 'b': 17,    'c': 47, 'esp': 0.70},
    'L233430':  {'upp': 1080, 'min': 30, 'a': 23, 'b': 34,    'c': None, 'esp': 0.55},
    'U201928':  {'upp': 420,  'min': 10, 'a': 28, 'b': 19,    'c': 20, 'esp': 0.55},
    'U193019':  {'upp': 1280, 'min': 10, 'a': 19, 'b': 30,    'c': 19, 'esp': 0.55},
}

# Box_units explícitos por SKU (uds dentro de la unidad de venta "caja").
# Cuando la unidad de venta es la caja (tornillos, accesorios, gypsocomete),
# `box_units` indica cuántas unidades individuales lleva la caja.
# CINTAS: rollos por caja (la unidad de venta es el rollo, pero se compran
# en cajas para almacén).
_BOX_UNITS_BY_SKU = {
    # ACCESORIOS — Anexo Nov 2025 pp. 6-8
    '304000': 100,    # Horquilla Cuelgue Rápida M6 TC 47
    '301008': 100,    # Horquilla Cuelgue M6 TC 48
    '304001': 50,     # Horquilla Cuelgue M6 TC 60
    '304007': 50,     # Pieza Empalme TC 47
    '304008': 50,     # Pieza Empalme TC 60
    '304014': 50,     # Cruceta TC 47
    '304015': 25,     # Cruceta TC 60
    '304021': 100,    # Suspensión TC 47 90mm
    '304022': 100,    # Suspensión TC 47 180mm
    '304023': 50,     # Suspensión TC 47 240mm
    '304029': 100,    # Anclaje Directo 47×120
    '304030': 100,    # Anclaje Directo 60×120
    '301060': 100,    # Gancho Fijación Vigas
    '304042': 100,    # Clip Horizontal 4-10
    '304043': 100,    # Clip Horizontal 10-15
    '304036': 100,    # Anclaje Universal Omega M6
    '304049': 20,     # Aislador Acústico TC 47
    '304050': 25,     # Aislador Acústico Trasdosado
    '304095': 100,    # Varilla Roscada Ø6×1000
    '304096': 50,     # Varilla Roscada Ø6×2000
    '304097': 100,    # Manguito Cilíndrico Ø6×20
    # CINTAS — Anexo Nov 2025 p. 9 (rollos por caja)
    '304056': 24,     # Cinta Juntas 23m
    '304057': 20,     # Cinta Juntas 75m
    '304058': 10,     # Cinta Juntas 150m
    '304078': 54,     # Malla FV 50m × 45m
    '304079': 12,     # Malla FV 50m × 153m
    '301121':  6,     # Malla Externa Light 50m
    '304064': 10,     # Cinta Guardavivos 12.5m
    '304065': 10,     # Cinta Guardavivos 30m
    '304075': 22,     # Banda Estanca 50mm
    '304076': 15,     # Banda Estanca 70mm
    '304077': 11,     # Banda Estanca 90mm
    # GYPSOCOMETE — Tarifa Gypsotech Abril 2026 p. 46
    '301600': 2,      # ANGLE
    '301601': 2,      # CROSS
    '301602': 2,      # STAR
    '301605': 5,      # LINE 2m
    '301606': 5,      # Recambio pantalla 2m
    '301607': 5,      # Recambio pantalla 3m
    '301600XL': 2,
    '301601XL': 2,
    '301602XL': 2,
    '301605XL': 5,
    '301606XL': 5,
}


def _catalog_pdf_extras_and_discontinued_20260425(db: sqlite3.Connection) -> None:
    """Datos adicionales extraídos del PDF + flags para SKUs descartados.

    1) BACKFILL adicional desde Anexo Gypsotech Nov 2025:
       - units_per_pallet REAL para los 41 perfiles (de 480, 250, 200, etc.
         según modelo). Esto sobreescribe los valores históricos donde la
         columna venía de uds/caja en lugar de uds/palé real.
       - min_order_qty para perfiles (10, 4, 8, 30 según modelo).
       - dim_a_mm, dim_b_mm, dim_c_mm para perfiles (sección C/U/Ω).
       - espesor_acero_mm (0.55 / 0.60 / 0.70).

    2) BACKFILL box_units para 40+ SKUs de accesorios, cintas, GypsoCOMETE
       (de la tarifa y anexo). Esto separa el "uds/caja" del
       "units_per_pallet" real.

    3) NUEVAS COLUMNAS para gestionar SKUs descartados:
       - is_active (BOOLEAN, default 1) — sigue en operativa Arias.
       - discontinued_reason (TEXT) — motivo si is_active=0.
         Valores comunes: 'caribbean_unsuitable', 'oversized_logistics',
         'fassa_discontinued', 'low_demand', etc.

    Esta migración deja el SCHEMA listo pero NO marca ningún SKU como
    descartado. Oliver indicará en migración 0011 qué SKUs descartar y por
    qué motivo concreto.

    REGLA OFERTAS INMUTABLES: solo `products`. Las ofertas históricas no se
    tocan — sus pesos y units_per_pallet quedan congelados en lines_json.

    Idempotente vía flag.
    """
    flag = db.execute(
        "SELECT value FROM app_settings WHERE key = 'catalog_pdf_extras_and_discontinued_20260425'"
    ).fetchone()
    if flag:
        return

    # 1) Schema: añadir is_active + discontinued_reason.
    existing = {r[1] for r in db.execute('PRAGMA table_info(products)').fetchall()}
    cols_added = 0
    if 'is_active' not in existing:
        # Nota: no podemos usar DEFAULT 1 porque la allowlist de _safe_add_column
        # no lo permite. Hacemos ALTER + UPDATE explícito.
        _safe_add_column(db, 'products', 'is_active', 'INTEGER')
        db.execute('UPDATE products SET is_active = 1 WHERE is_active IS NULL')
        cols_added += 1
    if 'discontinued_reason' not in existing:
        _safe_add_column(db, 'products', 'discontinued_reason', 'TEXT')
        cols_added += 1

    # 2) Backfill PERFILES con datos del Anexo Nov 2025.
    perfiles_updated = 0
    perfiles = db.execute("SELECT id, sku FROM products WHERE category = 'PERFILES'").fetchall()
    for pr in perfiles:
        sku = pr['sku'] or ''
        spec = None
        for prefix, data in _PERFILES_EXTRAS_BY_PREFIX.items():
            if sku.startswith(prefix):
                spec = data
                break
        if spec is None:
            continue
        db.execute(
            "UPDATE products SET units_per_pallet = ?, min_order_qty = ?, "
            "dim_a_mm = ?, dim_b_mm = ?, dim_c_mm = ?, espesor_acero_mm = ? "
            "WHERE id = ?",
            (spec['upp'], spec['min'], spec['a'], spec['b'], spec['c'], spec['esp'],
             pr['id']),
        )
        perfiles_updated += 1

    # 3) Backfill box_units para los SKUs catalogados.
    box_updated = 0
    for sku, n in _BOX_UNITS_BY_SKU.items():
        cur = db.execute(
            "UPDATE products SET box_units = ? WHERE sku = ? AND box_units IS NULL",
            (n, sku),
        )
        box_updated += cur.rowcount

    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ('catalog_pdf_extras_and_discontinued_20260425',
         f'cols_added={cols_added};perfiles_updated={perfiles_updated};'
         f'box_units_set={box_updated}',
         now_iso()),
    )
    db.commit()
    print(
        f'[migration] catalog pdf-extras 2026-04-25: +{cols_added} cols '
        f'(is_active+discontinued_reason), {perfiles_updated} perfiles con upp/min/dims real, '
        f'{box_updated} SKUs con box_units explícito'
    )


# Lista de SKUs descartados — decisión Oliver 2026-04-25:
#
# - Placas con longitud > 2.600 mm: la dimensión hace que el flete por
#   m² sea inviable para distribución Caribe (no caben optimizadas en
#   contenedor 40HC y bloquean el aprovechamiento). Demanda pequeña de
#   placas largas en proyectos RD.
# - Perfil TC 47 5.300 mm: idéntica razón — un perfil de 5.30 m
#   desperdicia el contenedor (12,03 m útiles → 2 piezas y media).
#
# Trampillas, EXTERNA, SILENS, LIGNUM, FASSATHERM se MANTIENEN. La
# dimensión es lo que filtra, no el tipo de placa.
_DISCONTINUED_OVERSIZED = [
    # Placas STD > 2.600 mm
    ('P00A000270A0', 'oversized_logistics'),  # STD BA 10mm 1200×2700 (Calliano)
    ('P00A003270A0', 'oversized_logistics'),  # STD BA 13mm 1200×2700
    ('P00A003280A0', 'oversized_logistics'),  # STD BA 13mm 1200×2800
    ('P00A000300A0', 'oversized_logistics'),  # STD BA 10mm 1200×3000
    ('P00A003300A0', 'oversized_logistics'),  # STD BA 13mm 1200×3000
    ('P00A005300A0', 'oversized_logistics'),  # STD BA 15mm 1200×3000
    ('P00A008300A0', 'oversized_logistics'),  # STD BA 18mm 1200×3000
    ('P00A003320A0', 'oversized_logistics'),  # STD BA 13mm 1200×3200 (Calliano)
    ('P00A003360A0', 'oversized_logistics'),  # STD BA 13mm 1200×3600 (Calliano)
    # Placas SIMPLY > 2.600 mm
    ('P00Y003280A0', 'oversized_logistics'),  # GypsoSIMPLY BA 13mm 1200×2800
    ('P00Y003300A0', 'oversized_logistics'),  # GypsoSIMPLY BA 13mm 1200×3000
    # Placas AQUA H2 > 2.600 mm
    ('P00H003280A0', 'oversized_logistics'),  # AQUA H2 BA 13mm 1200×2800
    ('P00H003300A0', 'oversized_logistics'),  # AQUA H2 BA 13mm 1200×3000
    ('P00H005300A0', 'oversized_logistics'),  # AQUA H2 BA 15mm 1200×3000
    # Placas AQUASUPER > 2.600 mm
    ('P00W003300A0', 'oversized_logistics'),  # AQUASUPER BA 13mm 1200×3000
    ('P00W005300A0', 'oversized_logistics'),  # AQUASUPER BA 15mm 1200×3000
    ('P00W008300A0', 'oversized_logistics'),  # AQUASUPER BA 18mm 1200×3000
    # Placas FOCUS > 2.600 mm
    ('P00F005280A0', 'oversized_logistics'),  # FOCUS BA 15mm 1200×2800
    ('P00F003300A0', 'oversized_logistics'),  # FOCUS BA 13mm 1200×3000
    ('P00F005300A2', 'oversized_logistics'),  # FOCUS BA 15mm 1200×3000
    # Placa LIGNUM > 2.600 mm
    ('P00LB03300AC', 'oversized_logistics'),  # GypsoLIGNUM BA 13mm 1200×3000
    # Perfil más largo que un contenedor 40HC útil
    ('C174717530A', 'oversized_logistics'),   # Perfil TC 47 Z1 — 5.300mm
]


def _catalog_discontinued_skus_20260425(db: sqlite3.Connection) -> None:
    """Marca como descartados los 22 SKUs cuya dimensión hace inviable la
    logística Caribe (Oliver 2026-04-25):

      - 21 placas con longitud > 2.600 mm (STD, SIMPLY, AQUA H2, AQUASUPER,
        FOCUS, LIGNUM)
      - 1 perfil TC 47 Z1 — 5.300 mm

    El criterio NO es por tipo de placa (EXTERNA, SILENS, LIGNUM, FASSATHERM
    se MANTIENEN — son productos válidos para Caribe). Solo se descartan por
    DIMENSIÓN > 2.600 mm que hace inviable el flete optimizado en contenedor
    40HC (12,03 m útiles).

    Verificado: 0 ofertas activas usan estos 22 SKUs (consulta cruzada
    pending_offers + order_lines, status != cancelled).

    REGLA OFERTAS INMUTABLES: si en futuro alguna oferta histórica los
    cita, NO se actualiza — el catálogo refleja la operativa actual, las
    ofertas mantienen su precio congelado.

    Idempotente vía flag.
    """
    flag = db.execute(
        "SELECT value FROM app_settings WHERE key = 'catalog_discontinued_skus_20260425'"
    ).fetchone()
    if flag:
        return

    discontinued = 0
    not_found = []
    for sku, reason in _DISCONTINUED_OVERSIZED:
        cur = db.execute(
            "UPDATE products SET is_active = 0, discontinued_reason = ? "
            "WHERE sku = ? AND (is_active IS NULL OR is_active = 1)",
            (reason, sku),
        )
        if cur.rowcount == 0:
            not_found.append(sku)
        else:
            discontinued += cur.rowcount

    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ('catalog_discontinued_skus_20260425',
         f'discontinued={discontinued};not_found={",".join(not_found) or "none"}',
         now_iso()),
    )
    db.commit()
    print(
        f'[migration] catalog discontinued 2026-04-25: {discontinued} SKUs '
        f'descartados por oversized_logistics (placas >2600mm + TC 47 5.300mm). '
        f'Not found: {not_found or "none"}'
    )


def _logistics_aggregated_calibration_20260425(db: sqlite3.Connection) -> None:
    """Calibración operativa del motor logístico (sesión Oliver 2026-04-25).

    Replica en SQLite la migración Alembic 0003_aggregated_logistics:
    - 40HC y 40' payload_kg 28000 → 26500 (nominal real Fassa).
    - Nueva columna container_profiles.floor_stowage_factor (default 1.0,
      0.80 para todos los tipos: representa el techo de carga geométrica
      con placas según operativa Arias).

    Idempotente vía flag en app_settings.
    """
    flag = db.execute(
        "SELECT value FROM app_settings WHERE key = 'logistics_aggregated_calibration_20260425'"
    ).fetchone()
    if flag:
        return

    cols = {r[1] for r in db.execute("PRAGMA table_info(container_profiles)").fetchall()}
    if 'floor_stowage_factor' not in cols:
        db.execute(
            "ALTER TABLE container_profiles ADD COLUMN floor_stowage_factor REAL NOT NULL DEFAULT 1.0"
        )
    db.execute("UPDATE container_profiles SET payload_kg = 26500 WHERE type IN ('40', '40HC')")
    db.execute("UPDATE container_profiles SET floor_stowage_factor = 0.80")

    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ('logistics_aggregated_calibration_20260425', 'applied', now_iso()),
    )
    db.commit()
    print('[migration] logística agregada calibrada: 40HC payload 26500, floor_stowage 0.80')


def _sync_fx_sources_20260424(db: sqlite3.Connection) -> None:
    """Sincroniza app_settings.fx_eur_usd con fx_rates EUR→USD.

    Bug histórico: catálogo (/products) y dashboard (/quote) leían el FX de
    tablas distintas (fx_rates vs app_settings.fx_eur_usd), lo que producía
    valores divergentes en pantalla y como default del cotizador. Ya se
    unificó en código (todos usan get_current_fx_eur_usd que prioriza
    fx_rates), pero los valores en DB siguen pudiendo discrepar.

    Esta migración alinea app_settings.fx_eur_usd con el último rate
    EUR→USD de fx_rates. Si fx_rates no tiene ningún rate, no hace nada
    (deja el comportamiento actual: app_settings es la única fuente).

    Idempotente: si ya están sincronizados, el UPDATE no cambia nada.
    """
    flag = db.execute(
        "SELECT value FROM app_settings WHERE key = 'fx_sync_20260424_applied'"
    ).fetchone()
    if flag:
        return

    row = db.execute(
        "SELECT rate FROM fx_rates WHERE base_currency='EUR' AND target_currency='USD' "
        "ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        # Sin rate oficial en fx_rates → no tocamos app_settings.
        db.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
            ('fx_sync_20260424_applied', 'no-fx_rates-row', now_iso()),
        )
        db.commit()
        return

    canonical = float(row['rate'])
    db.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES ('fx_eur_usd', ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (str(canonical), now_iso()),
    )
    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ('fx_sync_20260424_applied', f'synced_to={canonical}', now_iso()),
    )
    db.commit()
    print(f'[migration] FX sources sincronizadas: app_settings.fx_eur_usd ← {canonical} (fx_rates EUR→USD)')


def _apply_compound_discount_once(db: sqlite3.Connection) -> None:
    """Recalcula precio_arias_eur_unit usando descuento compuesto (base × extra).

    Fórmula: precio_arias = pvp × (1 - base/100) × (1 - extra/100)
    Solo corre una vez (flag en app_settings). Las ofertas ya guardadas no
    se tocan: guardan su propio precio congelado en lines_json.
    """
    flag = db.execute("SELECT value FROM app_settings WHERE key = 'compound_discount_applied_v1'").fetchone()
    if flag:
        return
    rows = db.execute(
        '''SELECT p.id, p.pvp_eur_unit, p.discount_pct, p.discount_extra_pct,
                  p.precio_arias_eur_unit, p.pvp_per_m2,
                  fd.discount_extra_pct AS fd_extra
           FROM products p
           LEFT JOIN family_defaults fd ON fd.category = p.category'''
    ).fetchall()
    updated = 0
    for r in rows:
        pvp = r['pvp_eur_unit']
        if pvp is None:
            continue
        base = r['discount_pct'] if r['discount_pct'] is not None else 50
        extra_override = r['discount_extra_pct']
        fd_extra = r['fd_extra'] if r['fd_extra'] is not None else 5
        extra = extra_override if extra_override is not None else fd_extra
        new_arias = round(float(pvp) * (1 - float(base) / 100) * (1 - float(extra) / 100), 4)
        pvp_m2 = r['pvp_per_m2']
        new_arias_m2 = round(float(pvp_m2) * (1 - float(base) / 100) * (1 - float(extra) / 100), 4) if pvp_m2 else None
        db.execute(
            'UPDATE products SET precio_arias_eur_unit = ?, unit_price_eur = ?, precio_arias_m2 = ? WHERE id = ?',
            (new_arias, new_arias, new_arias_m2, r['id'])
        )
        updated += 1
    db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ('compound_discount_applied_v1', str(updated), now_iso())
    )
    db.commit()
    if updated:
        print(f'[migration] descuento compuesto aplicado a {updated} productos')


def seed_db() -> None:
    db = get_db()
    now = now_iso()

    systems = [
        ('Sistema cerámica estándar', 'Adhesivo base para revestimiento cerámico interior', 0.08),
        ('Sistema impermeabilización baño', 'Impermeabilización cementosa flexible para baños y zonas húmedas', 0.10),
        ('Sistema placa estándar interior', 'Placa estándar de yeso para tabiques y trasdosados interiores', 0.05),
        ('Sistema placa humedad', 'Placa hidrófuga para baños/cocinas/humedad', 0.05),
        ('Sistema protección fuego', 'Placa de protección pasiva al fuego', 0.05),
    ]
    for row in systems:
        # Portable upsert that works on both SQLite 3.24+ and Postgres.
        db.execute(
            'INSERT INTO systems (name, description, default_waste_pct) VALUES (?, ?, ?) '
            'ON CONFLICT (name) DO NOTHING',
            row,
        )

    # Cliente y proyecto demo eliminados (Oliver 2026-04-25): la DB de
    # producción ya tiene clientes y proyectos reales — el demo era ruido
    # visual. Para entornos nuevos (CI, dev local), los clientes se crean
    # via UI o vía _seed_calc_fixtures en tests.

    # Seed shipping routes
    if db.execute('SELECT COUNT(*) AS c FROM shipping_routes').fetchone()['c'] == 0:
        routes = [
            ('Valencia', 'Santo Domingo (Caucedo)', 'MSC', 18, 1800, 2800, 2950, 0.005, '2026-04-01', '2026-09-30', 'Ruta principal Tarancón → Valencia → Caribe'),
            ('Barcelona', 'Santo Domingo (Caucedo)', 'Hapag-Lloyd', 20, 1950, 2950, 3100, 0.005, '2026-04-01', '2026-09-30', 'Alternativa Barcelona'),
            ('Valencia', 'Puerto Príncipe (Haití)', 'CMA CGM', 21, 2100, 3200, 3350, 0.005, '2026-04-01', '2026-09-30', 'Ruta Haití vía transhipment'),
            ('Valencia', 'San Juan (Puerto Rico)', 'MSC', 16, 1700, 2700, 2850, 0.005, '2026-04-01', '2026-09-30', 'Puerto Rico'),
            ('Valencia', 'Kingston (Jamaica)', 'Hapag-Lloyd', 19, 1900, 2900, 3050, 0.005, '2026-04-01', '2026-09-30', 'Jamaica'),
        ]
        for r in routes:
            db.execute('''INSERT INTO shipping_routes
                (origin_port, destination_port, carrier, transit_days, container_20_eur, container_40_eur,
                 container_40hc_eur, insurance_pct, valid_from, valid_until, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''', r)

    # Seed customs rates (República Dominicana)
    if db.execute('SELECT COUNT(*) AS c FROM customs_rates').fetchone()['c'] == 0:
        customs = [
            ('República Dominicana', '6809.11', 'Placas yeso laminado', 0.20, 0.18, 0.02, 'DAI 20% + ITBIS 18% + selectivo 2%'),
            ('República Dominicana', '7216.61', 'Perfilería metálica',  0.20, 0.18, 0.00, 'DAI 20% + ITBIS 18%'),
            ('República Dominicana', '7318.15', 'Tornillería',          0.20, 0.18, 0.00, 'DAI 20% + ITBIS 18%'),
            ('República Dominicana', '3214.90', 'Pastas y adhesivos',   0.20, 0.18, 0.00, 'DAI 20% + ITBIS 18%'),
            ('República Dominicana', '6806.90', 'Cintas y mallas',      0.20, 0.18, 0.00, 'DAI 20% + ITBIS 18%'),
            ('República Dominicana', '7326.90', 'Accesorios metálicos', 0.20, 0.18, 0.00, 'DAI 20% + ITBIS 18%'),
            ('República Dominicana', '6811.82', 'Placa exterior fibrocemento', 0.20, 0.18, 0.00, 'DAI 20% + ITBIS 18%'),
            ('Haití',                '6809.11', 'Placas yeso laminado', 0.15, 0.10, 0.00, 'Tarifa preferencial CARICOM'),
            ('Puerto Rico',          '6809.11', 'Placas yeso laminado', 0.00, 0.00, 0.00, 'Territorio USA — libre de aranceles'),
        ]
        for c in customs:
            db.execute('''INSERT INTO customs_rates
                (country, hs_code, category, dai_pct, itbis_pct, other_pct, notes)
                VALUES (?,?,?,?,?,?,?)''', c)

    # Seed perfiles físicos de contenedor (§3 spec logística).
    # Dimensiones interiores estándar ISO. payload nominal Fassa Arias = 26500 kg.
    # floor_stowage_factor 0.80 representa el techo de carga geométrica con placas
    # según operativa Arias (calibración Oliver 2026-04-25).
    if db.execute('SELECT COUNT(*) AS c FROM container_profiles').fetchone()['c'] == 0:
        profiles = [
            # type, inner_length, inner_width, inner_height, payload_kg, door, stow_w/v, stow_floor, notes
            ('20',   5.90,  2.35, 2.39, 21500, 0.30, 0.90, 0.80, 'Contenedor 20 pies estándar'),
            ('40',   12.03, 2.35, 2.39, 26500, 0.30, 0.90, 0.80, 'Contenedor 40 pies — payload operativo Arias 26500 kg'),
            ('40HC', 12.03, 2.35, 2.69, 26500, 0.30, 0.90, 0.80, 'Contenedor 40 High Cube — 30cm más alto'),
        ]
        for p in profiles:
            db.execute('''INSERT INTO container_profiles
                (type, inner_length_m, inner_width_m, inner_height_m, payload_kg,
                 door_clearance_m, stowage_factor, floor_stowage_factor, notes)
                VALUES (?,?,?,?,?,?,?,?,?)''', p)

    # Seed perfiles de palé por familia (§2 spec logística).
    # Valores iniciales editables desde UI de masters; cada SKU puede overridear
    # en products.pallet_* si su embalaje físico difiere del default familiar.
    if db.execute('SELECT COUNT(*) AS c FROM pallet_profiles').fetchone()['c'] == 0:
        pallets = [
            # category, L, W, H, levels, allow_mix, notes
            ('PLACAS',     2.50, 1.20, 0.30, 3, 1, 'Palé placa yeso 1200x2500 — apilable 3 niveles, el hueco lateral (1.15m) y los pisos superiores admiten mezcla'),
            ('PERFILES',   3.00, 0.80, 0.35, 2, 1, 'Palé perfiles metálicos — apilable 2 niveles, mezcla suelo OK'),
            ('TORNILLOS',  1.20, 0.80, 1.00, 2, 1, 'Palé cajas de tornillería'),
            ('CINTAS',     1.20, 0.80, 1.00, 2, 1, 'Palé cintas y mallas'),
            ('PASTAS',     1.20, 0.80, 1.20, 1, 1, 'Palé sacos de pasta — sin apilado (peso)'),
            ('ACCESORIOS', 1.20, 0.80, 1.00, 2, 1, 'Palé accesorios varios'),
        ]
        for pr in pallets:
            db.execute('''INSERT INTO pallet_profiles
                (category, pallet_length_m, pallet_width_m, pallet_height_m,
                 stackable_levels, allow_mix_floor, notes)
                VALUES (?,?,?,?,?,?,?)''', pr)

    # Fix idempotente: PLACAS debe admitir mezcla de suelo (regla Arias) incluso
    # si una versión previa del seed la marcó en 0.
    db.execute("UPDATE pallet_profiles SET allow_mix_floor = 1 "
               "WHERE category = 'PLACAS' AND allow_mix_floor = 0")

    # Seed FX rates
    if db.execute('SELECT COUNT(*) AS c FROM fx_rates').fetchone()['c'] == 0:
        fx = [
            ('EUR', 'USD', 1.18, now, 'Manual Abril 2026'),
            ('EUR', 'DOP', 65.80, now, 'Manual Abril 2026 — Peso Dominicano'),
            ('USD', 'DOP', 60.65, now, 'Manual Abril 2026'),
        ]
        for f in fx:
            db.execute('INSERT INTO fx_rates (base_currency, target_currency, rate, updated_at, source) VALUES (?,?,?,?,?)', f)

    # Seed users if empty — passwords vienen de variables de entorno (NO hardcodear).
    if db.execute('SELECT COUNT(*) AS c FROM users').fetchone()['c'] == 0:
        ana_pass = os.environ.get('SEED_ANA_PASSWORD')
        oli_pass = os.environ.get('SEED_OLI_PASSWORD')
        if not ana_pass or not oli_pass:
            print('⚠ No hay usuarios y SEED_ANA_PASSWORD / SEED_OLI_PASSWORD no están configurados.')
            print('  Define ambas en .env y reinicia, o crea el primer admin manualmente:')
            print('  python -c "import bcrypt;print(bcrypt.hashpw(b\'TUPASS\',bcrypt.gensalt()).decode())"')
        else:
            now_tz = now_iso()
            users = [
                ('ana', bcrypt.hashpw(ana_pass.encode(), bcrypt.gensalt()).decode(), 'admin', 'Ana Mar Pérez', 'amperez@ariasgroupcaribe.com'),
                ('oli', bcrypt.hashpw(oli_pass.encode(), bcrypt.gensalt()).decode(), 'admin', 'Oli', 'oli@ariasgroupcaribe.com'),
            ]
            for u in users:
                db.execute('INSERT INTO users (username, password_hash, role, full_name, email, created_at) VALUES (?,?,?,?,?,?)',
                           (*u, now_tz))

    db.commit()


# ── MOTOR DE CÁLCULO UNIFICADO ────────────────────────────────────
# Portado del bot V2 (Apps Script). Un solo motor para /quote, /api/save-offer
# y /api/order. Alertas por línea + optimizador de contenedor por familia.

CONTAINERS = {
    '20':   {'pallets': 10, 'kg': 21500},
    '40':   {'pallets': 20, 'kg': 26500},
    '40HC': {'pallets': 24, 'kg': 26500},
}

FAMILY_MAP = {
    'placas': 'PLACAS', 'placa yeso': 'PLACAS', 'placa': 'PLACAS',
    'perfiles': 'PERFILES', 'perfil': 'PERFILES',
    'tornillos': 'TORNILLOS',
    'cintas': 'CINTAS', 'mallas': 'CINTAS', 'cinta': 'CINTAS', 'malla': 'CINTAS',
    'accesorios': 'ACCESORIOS', 'accesorio': 'ACCESORIOS',
    'pastas': 'PASTAS', 'pasta': 'PASTAS',
    'adhesivo': 'PASTAS', 'adhesivos': 'PASTAS',
    'impermeabilización': 'PASTAS', 'impermeabilizacion': 'PASTAS',
    'revoco': 'PASTAS', 'revocos': 'PASTAS',
    'mampostería': 'PASTAS', 'mamposteria': 'PASTAS',
    'trampillas': 'TRAMPILLAS', 'trampilla': 'TRAMPILLAS',
    'gypsocomete': 'GYPSOCOMETE',
}


def _num(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def detect_family(category: str) -> str:
    return FAMILY_MAP.get((category or '').strip().lower(), 'DESCONOCIDA')


def compute_line(prod: Any, qty: float) -> dict[str, Any]:
    """Calcula una línea normalizada con peso, palés, coste y alertas.

    prod: dict/Row con {sku, name, category, unit, unit_price_eur,
                        kg_per_unit, units_per_pallet, sqm_per_pallet}.
    qty: cantidad en la unidad del producto (ya aplicado desperdicio).
    """
    if not isinstance(prod, dict):
        prod = dict(prod)
    sku = prod.get('sku') or ''
    name = prod.get('name') or ''
    family = detect_family(prod.get('category') or '')
    unit = (prod.get('unit') or 'ud').lower()
    price = _num(prod.get('unit_price_eur'))
    kg_unit = _num(prod.get('kg_per_unit'))
    upp = _num(prod.get('units_per_pallet'))
    sqm_pp = _num(prod.get('sqm_per_pallet'))
    qty = _num(qty)
    alerts: list[str] = []

    if price <= 0:
        alerts.append(f'{family} {sku}: falta precio unitario')
    if upp <= 0 and family in ('PLACAS', 'PERFILES', 'PASTAS'):
        alerts.append(f'{family} {sku}: falta unidades/palé')
    if kg_unit <= 0 and family in ('PLACAS', 'PASTAS', 'PERFILES'):
        alerts.append(f'{family} {sku}: falta peso unitario')
    if kg_unit <= 0 and family in ('TORNILLOS', 'CINTAS', 'ACCESORIOS', 'TRAMPILLAS', 'GYPSOCOMETE'):
        alerts.append(f'{family} {sku}: sin peso unitario, peso total = 0')

    m2_total = 0.0
    if unit in ('board', 'placa') and upp > 0 and sqm_pp > 0:
        m2_per_unit = sqm_pp / upp
        m2_total = qty * m2_per_unit
    elif unit in ('m2', 'm²'):
        m2_total = qty

    weight_total = qty * kg_unit
    pallets_theoretical = (qty / upp) if upp > 0 else 0.0
    pallets_logistic = math.ceil(pallets_theoretical) if upp > 0 else 0
    cost_exw = qty * price

    return {
        'ok': True,
        'sku': sku,
        'name': name,
        'family': family,
        'unit': unit,
        'qty_input': qty,
        'units': int(math.ceil(qty)) if unit not in ('ml', 'm2', 'm²') else round(qty, 2),
        'price_unit_eur': round(price, 4),
        'm2_total': round(m2_total, 2),
        'weight_total_kg': round(weight_total, 2),
        'pallets_theoretical': round(pallets_theoretical, 3),
        'pallets_logistic': pallets_logistic,
        'cost_exw_eur': round(cost_exw, 2),
        'alerts': alerts,
    }


def _container_result(key: str, units: int, pallets: float, weight: float) -> dict[str, Any]:
    d = CONTAINERS[key]
    per_pal = pallets / units if units > 0 else 0
    per_wei = weight / units if units > 0 else 0
    pal_occ = per_pal / d['pallets'] if d['pallets'] else 0
    wei_occ = per_wei / d['kg'] if d['kg'] else 0
    label = {'20': "20'", '40': "40'", '40HC': '40HC'}[key]
    return {
        'type_key': key,
        'recommended': label,
        'units': units,
        'pallets_capacity_per_unit': d['pallets'],
        'weight_capacity_per_unit_kg': d['kg'],
        'pallet_occupancy': round(pal_occ, 3),
        'weight_occupancy': round(wei_occ, 3),
        'score': round(pal_occ + wei_occ, 3),
    }


def estimate_containers(pallets_logistic: float, weight_kg: float,
                        family_breakdown: dict[str, int] | None = None) -> dict[str, Any] | None:
    pallets = _num(pallets_logistic)
    weight = _num(weight_kg)
    if pallets <= 0 and weight <= 0:
        return None

    fams = set((family_breakdown or {}).keys())
    only_plates = fams == {'PLACAS'}
    has_profiles = 'PERFILES' in fams

    if has_profiles:
        order = ['40HC', '40']
    elif only_plates:
        order = ['20', '40', '40HC']
    else:
        order = ['40HC', '40', '20']

    for key in order:
        d = CONTAINERS[key]
        if pallets <= d['pallets'] and weight <= d['kg']:
            return _container_result(key, 1, pallets, weight)

    best = None
    for key in order:
        d = CONTAINERS[key]
        u_pal = math.ceil(pallets / d['pallets']) if d['pallets'] else 0
        u_wei = math.ceil(weight / d['kg']) if d['kg'] else 0
        units = max(u_pal, u_wei, 1)
        cand = _container_result(key, units, pallets, weight)
        if best is None \
           or cand['units'] < best['units'] \
           or (cand['units'] == best['units'] and cand['score'] > best['score']):
            best = cand
    return best


def build_offer_breakdown(raw_lines: list[dict[str, Any]],
                          computed: list[dict[str, Any]],
                          margin_global_pct: float,
                          logistic_global_eur: float) -> dict[str, Any]:
    """Fuente única de verdad para el cálculo de un presupuesto.

    Devuelve un breakdown por línea con TODOS los valores que necesitan los
    consumidores (frontend preview, save_offer al persistir, offer_pdf al
    renderizar, exports a Odoo/NetSuite, etc.) así nadie tiene que recalcular.

    Estructura:
      {
        'lines': [
          {sku, name, family, unit, qty_neta, qty_waste, price_arias_eur,
           log_unit_eur, margin_pct, cost_line_eur, log_line_eur,
           sale_line_eur, sale_unit_eur},
          ...
        ],
        'totals': {product_cost_eur, logistic_eur, sale_eur},
        'meta': {has_per_line_log, margin_global_pct},
      }

    Regla:
      - margen por línea = raw_lines[i]['margin'] si viene, si no margen global
      - flete por línea  = raw_lines[i]['log_unit_cost'] * qty_con_waste
        Si NINGUNA línea trae log_unit_cost, el flete global del payload se
        prorratea proporcional al coste de producto por línea (equivalente
        matemáticamente al cálculo global previo, mantiene compat con bot).
      - venta línea = cost_producto / (1 - margen_línea) + flete_línea
        (flete es pass-through: no genera margen, igual que el frontend).
    """
    product_cost_total = sum(_num(cl.get('cost_exw_eur')) for cl in computed)
    if product_cost_total <= 0 or not computed:
        return {
            'lines': [],
            'totals': {'product_cost_eur': 0.0, 'logistic_eur': 0.0, 'sale_eur': 0.0},
            'meta': {'has_per_line_log': False, 'margin_global_pct': margin_global_pct},
        }

    has_per_line_log = any(_num(li.get('log_unit_cost', 0)) > 0 for li in raw_lines)
    margin_global = margin_global_pct if margin_global_pct < 1 else margin_global_pct / 100

    breakdown_lines: list[dict[str, Any]] = []
    total_product = 0.0
    total_logistic = 0.0
    total_sale = 0.0
    for i, cl in enumerate(computed):
        line_input = raw_lines[i] if i < len(raw_lines) else {}
        cost_prod = _num(cl.get('cost_exw_eur'))
        qty_w = _num(cl.get('qty_input'))
        qty_neta = _num(cl.get('qty_original', cl.get('qty_input', 0)))
        price_arias = _num(cl.get('price_unit_eur')) or (cost_prod / qty_w if qty_w else 0)

        m_raw = line_input.get('margin')
        if m_raw is None or m_raw == '':
            margin_line = margin_global
        else:
            m_val = _num(m_raw)
            margin_line = m_val / 100 if m_val >= 1 else m_val

        if has_per_line_log:
            log_unit = _num(line_input.get('log_unit_cost', 0))
            log_line = log_unit * qty_w
        else:
            share = cost_prod / product_cost_total
            log_line = logistic_global_eur * share
            log_unit = (log_line / qty_w) if qty_w else 0.0

        sale_product = cost_prod / max(1 - margin_line, 0.01) if margin_line < 1 else cost_prod
        sale_line = sale_product + log_line
        sale_unit = (sale_line / qty_w) if qty_w else 0.0

        breakdown_lines.append({
            'sku': cl.get('sku'),
            'name': cl.get('name'),
            'family': cl.get('family'),
            'unit': cl.get('unit'),
            'qty_neta': round(qty_neta, 2),
            'qty_waste': round(qty_w, 2),
            'price_arias_eur': round(price_arias, 4),
            'log_unit_eur': round(log_unit, 4),
            'margin_pct': round(margin_line * 100, 2),
            'cost_line_eur': round(cost_prod, 2),
            'log_line_eur': round(log_line, 2),
            'sale_line_eur': round(sale_line, 2),
            'sale_unit_eur': round(sale_unit, 4),
        })

        total_product += cost_prod
        total_logistic += log_line
        total_sale += sale_line

    return {
        'lines': breakdown_lines,
        'totals': {
            'product_cost_eur': round(total_product, 2),
            'logistic_eur': round(total_logistic, 2),
            'sale_eur': round(total_sale, 2),
        },
        'meta': {
            'has_per_line_log': has_per_line_log,
            'margin_global_pct': margin_global_pct,
        },
    }


def compute_offer_sale_totals(raw_lines: list[dict[str, Any]],
                              computed: list[dict[str, Any]],
                              margin_global_pct: float,
                              logistic_global_eur: float) -> tuple[float, float, float]:
    """Wrapper legacy: devuelve solo (product_cost, logistic, sale) como tuple.

    Nuevo código debe usar build_offer_breakdown para tener acceso al detalle
    por línea. Este wrapper se mantiene para no romper callers existentes.
    """
    r = build_offer_breakdown(raw_lines, computed, margin_global_pct, logistic_global_eur)
    t = r['totals']
    return t['product_cost_eur'], t['logistic_eur'], t['sale_eur']


def compute_totals(lines: list[dict[str, Any]]) -> dict[str, Any]:
    ok_lines = [l for l in lines if l.get('ok', True)]
    total_cost = sum(_num(l.get('cost_exw_eur')) for l in ok_lines)
    total_weight = sum(_num(l.get('weight_total_kg')) for l in ok_lines)
    total_m2 = sum(_num(l.get('m2_total')) for l in ok_lines)
    total_pal_t = sum(_num(l.get('pallets_theoretical')) for l in ok_lines)
    total_pal_l = sum(_num(l.get('pallets_logistic')) for l in ok_lines)
    fam_breakdown: dict[str, int] = {}
    for l in ok_lines:
        f = l.get('family') or 'DESCONOCIDA'
        fam_breakdown[f] = fam_breakdown.get(f, 0) + 1
    containers = estimate_containers(total_pal_l, total_weight, fam_breakdown)
    return {
        'cost_exw_eur': round(total_cost, 2),
        'weight_total_kg': round(total_weight, 2),
        'm2_total': round(total_m2, 2),
        'pallets_theoretical': round(total_pal_t, 3),
        'pallets_logistic': int(math.ceil(total_pal_l)),
        'family_breakdown': fam_breakdown,
        'containers': containers,
    }


def dedup_alerts(lines: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for l in lines:
        for a in (l.get('alerts') or []):
            if a not in seen:
                seen.add(a)
                out.append(a)
    return out


# ── PERSISTENCIA: order_lines, audit_log, hash dedupe, secuencia ──

def save_order_lines(db: sqlite3.Connection, offer_id: int,
                     computed_lines: list[dict[str, Any]]) -> None:
    now = now_iso()
    for cl in computed_lines:
        db.execute(
            '''INSERT INTO order_lines
            (offer_id, sku, name, family, unit, qty_input, qty_logistic,
             price_unit_eur, cost_exw_eur, m2_total, weight_total_kg,
             pallets_theoretical, pallets_logistic, alerts_text, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (
                offer_id,
                cl.get('sku'),
                cl.get('name'),
                cl.get('family'),
                cl.get('unit'),
                _num(cl.get('qty_original', cl.get('qty_input', 0))),
                _num(cl.get('units', cl.get('qty_input', 0))),
                _num(cl.get('price_unit_eur')),
                _num(cl.get('cost_exw_eur')),
                _num(cl.get('m2_total')),
                _num(cl.get('weight_total_kg')),
                _num(cl.get('pallets_theoretical')),
                int(_num(cl.get('pallets_logistic'))),
                ' | '.join(cl.get('alerts') or []) or None,
                now,
            )
        )


def log_audit(db: sqlite3.Connection, offer_id: int | None, action: str,
              detail: str = '', username: str = '') -> None:
    if not username:
        try:
            username = current_user.username if current_user.is_authenticated else 'system'
        except RuntimeError:
            username = 'system'
    db.execute(
        'INSERT INTO audit_log (offer_id, action, detail, username, created_at) VALUES (?,?,?,?,?)',
        (offer_id, action, detail, username, now_iso()),
    )


def compute_raw_hash(raw_text: str) -> str:
    import hashlib
    return hashlib.sha256(raw_text.strip().encode()).hexdigest()[:16]


def find_offer_by_hash(db: sqlite3.Connection, raw_hash: str) -> dict[str, Any] | None:
    row = db.execute(
        'SELECT * FROM pending_offers WHERE raw_hash = ? ORDER BY created_at DESC LIMIT 1',
        (raw_hash,),
    ).fetchone()
    return dict(row) if row else None


def next_sequence(db: sqlite3.Connection, prefix: str) -> str:
    row = db.execute('SELECT last_number FROM doc_sequences WHERE prefix = ?', (prefix,)).fetchone()
    if row:
        n = row['last_number'] + 1
        db.execute('UPDATE doc_sequences SET last_number = ? WHERE prefix = ?', (n, prefix))
    else:
        n = 1
        db.execute('INSERT INTO doc_sequences (prefix, last_number) VALUES (?, ?)', (prefix, n))
    return f'{prefix}-{n:04d}'


def generate_offer_number(db: sqlite3.Connection) -> str:
    """Genera offer_number único y secuencial con formato YYYY-NNNN.

    Usa doc_sequences.OFR como contador. A diferencia de next_sequence,
    el prefijo es el año actual (no 'OFR-') para mantener continuidad
    con el formato histórico '2026-XXXX' ya enviado a clientes.

    Garantiza unicidad: itera el counter hasta que no exista un
    offer_number idéntico en pending_offers (defensivo ante colisiones
    con números ya emitidos antes de la auditoría).
    """
    year = datetime.now(timezone.utc).year
    row = db.execute("SELECT last_number FROM doc_sequences WHERE prefix = 'OFR'").fetchone()
    n = (row['last_number'] + 1) if row else 1
    while True:
        candidate = f'{year}-{n:04d}'
        exists = db.execute(
            'SELECT 1 FROM pending_offers WHERE offer_number = ?', (candidate,)
        ).fetchone()
        if not exists:
            break
        n += 1
    if row:
        db.execute("UPDATE doc_sequences SET last_number = ? WHERE prefix = 'OFR'", (n,))
    else:
        db.execute("INSERT INTO doc_sequences (prefix, last_number) VALUES ('OFR', ?)", (n,))
    return candidate


def calculate_quote(system_id: int, area_sqm: float, freight_eur: float, target_margin_pct: float, fx_rate: float) -> dict[str, Any]:
    db = get_db()
    system = db.execute('SELECT * FROM systems WHERE id = ?', (system_id,)).fetchone()
    comps = db.execute(
        '''SELECT sc.consumption_per_sqm, sc.waste_pct AS sc_waste_pct,
                  p.sku, p.name, p.category, p.unit, p.unit_price_eur,
                  p.kg_per_unit, p.units_per_pallet, p.sqm_per_pallet
           FROM system_components sc JOIN products p ON p.id = sc.product_id
           WHERE sc.system_id = ?''',
        (system_id,),
    ).fetchall()

    lines: list[dict[str, Any]] = []
    line_items: list[dict[str, Any]] = []  # backward-compat shape for templates

    for c in comps:
        cd = dict(c)
        waste = max(_num(system['default_waste_pct']), _num(cd['sc_waste_pct']))
        gross_area = area_sqm * (1 + waste)
        raw_qty = gross_area * _num(cd['consumption_per_sqm'])
        qty = math.ceil(raw_qty) if (cd.get('unit') or '').lower() in ('board', 'bag', 'bucket', 'ud', 'unit') else raw_qty

        line = compute_line(cd, qty)
        line['waste_pct'] = waste
        line['consumption_per_sqm'] = cd['consumption_per_sqm']
        lines.append(line)

        # Back-compat fields for existing calculator.html / project_detail.html
        line_items.append({
            'sku': line['sku'],
            'name': line['name'],
            'category': cd.get('category') or line['family'],
            'unit': line['unit'],
            'consumption_per_sqm': cd['consumption_per_sqm'],
            'waste_pct': waste,
            'units': line['units'],
            'display_qty': line['units'],
            'pallets': line['pallets_logistic'],
            'coverage_sqm': line['m2_total'] if line['m2_total'] else None,
            'product_cost_eur': line['cost_exw_eur'],
            'weight_kg': line['weight_total_kg'],
            'alerts': line['alerts'],
        })

    totals = compute_totals(lines)
    product_cost = totals['cost_exw_eur']

    landed_total = product_cost + freight_eur
    sale_total = landed_total / max(1 - target_margin_pct, 0.01)
    gross_margin_eur = sale_total - landed_total
    price_per_sqm = sale_total / area_sqm if area_sqm else 0

    cont = totals.get('containers')
    if cont and cont['type_key'] == '20':
        c20 = cont['units']; c40 = 0
    elif cont and cont['type_key'] in ('40', '40HC'):
        c20 = 0; c40 = cont['units']
    else:
        c20 = math.ceil(totals['pallets_logistic'] / 10) if totals['pallets_logistic'] else 0
        c40 = math.ceil(totals['pallets_logistic'] / 20) if totals['pallets_logistic'] else 0

    return {
        'system_name': system['name'],
        'area_sqm': area_sqm,
        'line_items': line_items,
        'summary': {
            'total_units': sum(_num(l.get('units')) for l in lines),
            'total_pallets': totals['pallets_logistic'],
            'total_pallets_theoretical': totals['pallets_theoretical'],
            'total_weight_kg': totals['weight_total_kg'],
            'm2_total': totals['m2_total'],
            'product_cost_eur': product_cost,
            'freight_eur': round(freight_eur, 2),
            'landed_total_eur': round(landed_total, 2),
            'sale_total_eur': round(sale_total, 2),
            'gross_margin_eur': round(gross_margin_eur, 2),
            'gross_margin_pct': round(gross_margin_eur / sale_total, 4) if sale_total else 0,
            'price_per_sqm_eur': round(price_per_sqm, 2),
            'containers_20_est': c20,
            'containers_40_est': c40,
            'container_recommendation': cont,
            'family_breakdown': totals['family_breakdown'],
            'fx_rate': fx_rate,
            'sale_total_local': round(sale_total * fx_rate, 2),
            'alerts': dedup_alerts(lines),
        },
    }


# ── Authentication Routes ─────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if user and bcrypt.checkpw(password.encode(), user['password_hash'].encode()):
            login_user(User(user['id'], user['username'], user['role']))
            flash(f'Bienvenido/a, {user["full_name"] or user["username"]}.')
            next_page = _safe_next_url(request.args.get('next'))
            return redirect(next_page or url_for('dashboard'))
        flash('Usuario o contraseña incorrectos.')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Sesión cerrada.')
    return redirect(url_for('login'))


@app.route('/')
@login_required
def dashboard():
    db = get_db()
    # Pipeline stages
    stage_counts = db.execute('SELECT stage, COUNT(*) AS qty FROM projects GROUP BY stage ORDER BY qty DESC').fetchall()
    projects = db.execute(
        '''SELECT p.*, c.name AS client_name, c.company
           FROM projects p JOIN clients c ON c.id = p.client_id ORDER BY p.created_at DESC LIMIT 10'''
    ).fetchall()
    clients_count = db.execute('SELECT COUNT(*) AS c FROM clients').fetchone()['c']
    products_count = db.execute('SELECT COUNT(*) AS c FROM products').fetchone()['c']
    systems_count = db.execute('SELECT COUNT(*) AS c FROM systems').fetchone()['c']
    
    # Financial data (merged from dashboard_financial)
    pipeline = db.execute('''
        SELECT stage, COUNT(*) as count, COALESCE(SUM(CASE WHEN result_json IS NOT NULL 
            THEN json_extract(result_json, '$.summary.sale_total_eur') ELSE 0 END), 0) as value
        FROM projects LEFT JOIN project_quotes ON projects.id = project_quotes.project_id
        GROUP BY stage ORDER BY count DESC
    ''').fetchall()
    # Ofertas desde pending_offers (cotizador principal)
    recent_offers = db.execute('''
        SELECT * FROM pending_offers ORDER BY created_at DESC LIMIT 10
    ''').fetchall()
    quotes_data = []
    total_pipeline_eur = 0
    total_confirmed_eur = 0
    for o in recent_offers:
        lines = json.loads(o['lines_json']) if o['lines_json'] else []
        margin_pct = float(o['margin_pct'] or 20)
        quotes_data.append({
            'id': o['id'], 'offer_number': o['offer_number'],
            'project_name': o['project_name'],
            'client_name': o['client_name'],
            'status': o['status'],
            'incoterm': o['incoterm'] or 'EXW',
            'sale_total_eur': o['total_final_eur'],
            'product_eur': o['total_product_eur'],
            'logistic_eur': o['total_logistic_eur'] or 0,
            'margin_pct': margin_pct,
            'lines': len(lines),
            'containers': o['container_count'] or 0,
            'created_at': (o['created_at'] or '')[:10],
        })
        if o['status'] in ('pending', 'approved'):
            total_pipeline_eur += o['total_final_eur'] or 0
        if o['status'] == 'approved':
            total_confirmed_eur += o['total_final_eur'] or 0

    offers_count = db.execute('SELECT COUNT(*) FROM pending_offers').fetchone()[0]
    counts = {
        'clients': clients_count, 'projects': db.execute('SELECT COUNT(*) FROM projects').fetchone()[0],
        'go': db.execute("SELECT COUNT(*) FROM projects WHERE go_no_go='GO'").fetchone()[0],
        'products': products_count, 'systems': systems_count,
        'quotes': offers_count,
    }
    # Fuente única: get_current_fx_eur_usd() prioriza fx_rates (rate oficial
    # con histórico) sobre app_settings.fx_eur_usd (legacy override). Antes
    # cada endpoint leía de un sitio distinto y aparecían FX divergentes
    # entre el catálogo /products y el cotizador /quote.
    fx = {'USD': get_current_fx_eur_usd()}

    # Top empresas por volumen € (via clients.company)
    top_companies = db.execute('''
        SELECT c.company, COUNT(*) n, SUM(o.total_final_eur) total
        FROM pending_offers o
        JOIN clients c ON c.name = o.client_name OR c.company = o.client_name
        WHERE o.status IN ('pending','approved') AND c.company IS NOT NULL
        GROUP BY c.company ORDER BY total DESC LIMIT 5
    ''').fetchall()
    if not top_companies:
        top_companies = db.execute('''
            SELECT client_name as company, COUNT(*) n, SUM(total_final_eur) total
            FROM pending_offers WHERE status IN ('pending','approved')
            GROUP BY client_name ORDER BY total DESC LIMIT 5
        ''').fetchall()

    # Ofertas por estado con % e importe
    status_counts = db.execute('''
        SELECT status, COUNT(*) n, SUM(total_final_eur) total
        FROM pending_offers GROUP BY status
    ''').fetchall()
    total_offers = sum(r['n'] for r in status_counts)
    approved_n = next((r['n'] for r in status_counts if r['status'] == 'approved'), 0)
    rejected_n = next((r['n'] for r in status_counts if r['status'] == 'rejected'), 0)
    pending_n = next((r['n'] for r in status_counts if r['status'] == 'pending'), 0)
    conversion_rate = round(approved_n / total_offers * 100, 1) if total_offers > 0 else 0

    # Familias más cotizadas (de lines_json)
    all_lines = []
    for o in db.execute("SELECT lines_json FROM pending_offers WHERE status IN ('pending','approved')"):
        lines_data = json.loads(o['lines_json']) if o['lines_json'] else []
        all_lines.extend(lines_data)
    family_totals: dict[str, float] = {}
    for li in all_lines:
        fam = li.get('family', '?')
        family_totals[fam] = family_totals.get(fam, 0) + (li.get('price', 0) * li.get('qty', 0))
    top_families = sorted(family_totals.items(), key=lambda x: -x[1])[:6]

    return render_template('dashboard.html',
        stage_counts=stage_counts, projects=projects, clients=clients_count,
        products=products_count, systems=systems_count,
        quotes_data=quotes_data, total_pipeline_eur=total_pipeline_eur,
        total_confirmed_eur=total_confirmed_eur, stage_data={row['stage']: row['count'] for row in pipeline},
        counts=counts, fx=fx, stages=STAGES,
        top_companies=top_companies, status_counts=status_counts,
        conversion_rate=conversion_rate, top_families=top_families,
        pending_n=pending_n, approved_n=approved_n, rejected_n=rejected_n,
        total_offers=total_offers)


@app.route('/clients', methods=['GET', 'POST'])
@login_required
def clients():
    db = get_db()
    if request.method == 'POST':
        db.execute(
            '''INSERT INTO clients (name, company, rnc, email, phone, address, country, score, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                request.form['name'],
                request.form.get('company'),
                request.form.get('rnc'),
                request.form.get('email'),
                request.form.get('phone'),
                request.form.get('address'),
                request.form.get('country') or 'República Dominicana',
                int(request.form.get('score') or 50),
                now_iso(),
            ),
        )
        db.commit()
        flash('Cliente creado.')
        return redirect(url_for('clients'))
    rows = db.execute('SELECT * FROM clients ORDER BY created_at DESC').fetchall()
    return render_template('clients.html', clients=rows)


@app.route('/products')
@login_required
def products():
    db = get_db()
    rows = db.execute('''SELECT p.*, COALESCE(fd.display_order, 99) AS cat_order
                         FROM products p
                         LEFT JOIN family_defaults fd ON fd.category = p.category
                         ORDER BY cat_order,
                                  COALESCE(p.subfamily, ''),
                                  p.name''').fetchall()
    # Agrupar por categoría y subfamilia
    groups: dict[str, dict[str, list[dict]]] = {}
    for r in rows:
        cat = r['category'] or 'SIN CATEGORÍA'
        sub = r['subfamily'] or ''
        groups.setdefault(cat, {}).setdefault(sub, []).append(dict(r))
    # Resumen
    totals = {cat: sum(len(v) for v in subs.values()) for cat, subs in groups.items()}
    missing = sum(1 for r in rows if r['pvp_eur_unit'] is None)
    fd_rows = db.execute('SELECT category, discount_pct, discount_extra_pct FROM family_defaults').fetchall()
    fam_defaults = {r['category']: r['discount_pct'] for r in fd_rows}
    fam_extras = {r['category']: (r['discount_extra_pct'] if r['discount_extra_pct'] is not None else 5) for r in fd_rows}
    fx_eur_usd = get_current_fx_eur_usd()
    return render_template('products.html',
                           groups=groups,
                           totals=totals,
                           grand_total=len(rows),
                           missing=missing,
                           fam_defaults=fam_defaults,
                           fam_extras=fam_extras,
                           fx_eur_usd=fx_eur_usd,
                           is_admin=(getattr(current_user, 'role', None) == 'admin'))


@app.route('/api/products/<int:product_id>', methods=['GET'])
@login_required
def api_get_product(product_id: int):
    db = get_db()
    p = db.execute('SELECT * FROM products WHERE id = ?', (product_id,)).fetchone()
    if not p:
        return jsonify({'ok': False, 'error': 'not found'}), 404
    hist = db.execute('''SELECT field, old_value, new_value, username, changed_at
                         FROM price_history WHERE product_id = ?
                         ORDER BY changed_at DESC LIMIT 20''', (product_id,)).fetchall()
    return jsonify({'ok': True, 'product': dict(p), 'history': [dict(h) for h in hist]})


@app.route('/api/products/<int:product_id>', methods=['POST'])
@admin_required
def api_update_product(product_id: int):
    db = get_db()
    existing = db.execute('SELECT * FROM products WHERE id = ?', (product_id,)).fetchone()
    if not existing:
        return jsonify({'ok': False, 'error': 'not found'}), 404
    data = request.get_json() or {}
    # Campos editables
    editable = ['name', 'subfamily', 'unit', 'content_per_unit', 'pack_size',
                'pvp_eur_unit', 'precio_arias_eur_unit', 'discount_pct', 'discount_extra_pct',
                'kg_per_unit', 'units_per_pallet', 'sqm_per_pallet', 'notes']
    changes = []
    sets = []
    vals: list[Any] = []
    for f in editable:
        if f not in data:
            continue
        new_v = data[f]
        old_v = existing[f]
        # normalizar números
        if f in ('pvp_eur_unit', 'precio_arias_eur_unit', 'discount_pct', 'discount_extra_pct',
                 'kg_per_unit', 'units_per_pallet', 'sqm_per_pallet'):
            new_v = float(new_v) if new_v not in (None, '') else None
        if new_v == old_v:
            continue
        sets.append(f'{f} = ?')
        vals.append(new_v)
        changes.append((f, old_v, new_v))
    if not sets:
        return jsonify({'ok': True, 'changed': 0, 'message': 'sin cambios'})
    # Auto-sync: si cambió pvp_eur_unit, discount_pct o discount_extra_pct
    # y no envió precio_arias_eur_unit explícito, recalcular con compuesto.
    if 'precio_arias_eur_unit' not in data:
        pvp_new = next((v for f, _, v in changes if f == 'pvp_eur_unit'), None)
        disc_new = next((v for f, _, v in changes if f == 'discount_pct'), None)
        extra_new = next((v for f, _, v in changes if f == 'discount_extra_pct'), None)
        pvp = pvp_new if pvp_new is not None else existing['pvp_eur_unit']
        disc = disc_new if disc_new is not None else (existing['discount_pct'] or 50)
        # Efectivo extra: override del producto > default de la familia > 0
        try:
            existing_extra = existing['discount_extra_pct']
        except (IndexError, KeyError):
            existing_extra = None
        extra_override = extra_new if extra_new is not None else existing_extra
        if extra_override is None:
            fd = db.execute('SELECT discount_extra_pct FROM family_defaults WHERE category = ?',
                            (existing['category'],)).fetchone()
            extra_override = (fd['discount_extra_pct'] if fd and fd['discount_extra_pct'] is not None else 0)
        extra = float(extra_override)
        if pvp is not None:
            arias = round(float(pvp) * (1 - float(disc) / 100) * (1 - extra / 100), 4)
            if arias != existing['precio_arias_eur_unit']:
                sets.append('precio_arias_eur_unit = ?')
                vals.append(arias)
                changes.append(('precio_arias_eur_unit', existing['precio_arias_eur_unit'], arias))
    # Mantener unit_price_eur alineado con precio_arias_eur_unit (motor de cálculo)
    final_arias = next((v for f, _, v in changes if f == 'precio_arias_eur_unit'), None)
    if final_arias is not None:
        sets.append('unit_price_eur = ?')
        vals.append(final_arias)
    vals.append(product_id)
    db.execute(f'UPDATE products SET {", ".join(sets)} WHERE id = ?', vals)
    # Auditar solo campos numéricos en price_history
    now_ts = now_iso()
    user_id = current_user.id
    username = current_user.username
    for field, old, new in changes:
        try:
            old_n = float(old) if old is not None else None
            new_n = float(new) if new is not None else None
        except (TypeError, ValueError):
            continue
        db.execute('''INSERT INTO price_history
                      (product_id, field, old_value, new_value, user_id, username, changed_at)
                      VALUES (?,?,?,?,?,?,?)''',
                   (product_id, field, old_n, new_n, user_id, username, now_ts))
    db.commit()
    updated = db.execute('SELECT * FROM products WHERE id = ?', (product_id,)).fetchone()
    return jsonify({'ok': True, 'changed': len(changes), 'product': dict(updated)})


@app.route('/projects', methods=['GET', 'POST'])
@login_required
def projects():
    db = get_db()
    if request.method == 'POST':
        db.execute(
            '''INSERT INTO projects
            (client_id, name, project_type, location, area_sqm, stage, go_no_go, incoterm, fx_rate, target_margin_pct, freight_eur, customs_pct, logistics_notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                int(request.form['client_id']),
                request.form['name'],
                request.form.get('project_type'),
                request.form.get('location'),
                float(request.form.get('area_sqm') or 0),
                request.form.get('stage') or 'OPORTUNIDAD',
                request.form.get('go_no_go') or 'PENDING',
                request.form.get('incoterm') or 'EXW',
                float(request.form.get('fx_rate') or 1),
                float(request.form.get('target_margin_pct') or 0.30),
                float(request.form.get('freight_eur') or 0),
                float(request.form.get('customs_pct') or 0.18),
                request.form.get('logistics_notes'),
                now_iso(),
            ),
        )
        db.commit()
        flash('Proyecto creado.')
        return redirect(url_for('projects'))
    rows = db.execute(
        '''SELECT p.*, c.name AS client_name, c.company
           FROM projects p JOIN clients c ON c.id = p.client_id ORDER BY p.created_at DESC'''
    ).fetchall()
    clients = db.execute('SELECT id, name, company FROM clients ORDER BY name').fetchall()
    return render_template('projects.html', projects=rows, clients=clients, stages=STAGES)


@app.route('/projects/<int:project_id>', methods=['GET', 'POST'])
@login_required
def project_detail(project_id: int):
    db = get_db()
    project = db.execute(
        '''SELECT p.*, c.name AS client_name, c.company, c.email, c.phone
           FROM projects p JOIN clients c ON c.id = p.client_id WHERE p.id = ?''',
        (project_id,),
    ).fetchone()
    if not project:
        return 'Project not found', 404

    if request.method == 'POST':
        action = request.form['action']
        if action == 'advance_stage':
            to_stage = request.form['to_stage']
            note = request.form.get('note')
            db.execute('UPDATE projects SET stage = ? WHERE id = ?', (to_stage, project_id))
            db.execute(
                'INSERT INTO stage_events (project_id, from_stage, to_stage, note, created_at) VALUES (?, ?, ?, ?, ?)',
                (project_id, project['stage'], to_stage, note, now_iso()),
            )
            db.commit()
            flash('Etapa actualizada.')
        elif action == 'save_project':
            db.execute(
                '''UPDATE projects SET area_sqm = ?, fx_rate = ?, target_margin_pct = ?, freight_eur = ?, customs_pct = ?, incoterm = ?, go_no_go = ?, logistics_notes = ?
                   WHERE id = ?''',
                (
                    float(request.form.get('area_sqm') or 0),
                    float(request.form.get('fx_rate') or 1),
                    float(request.form.get('target_margin_pct') or 0.30),
                    float(request.form.get('freight_eur') or 0),
                    float(request.form.get('customs_pct') or 0.18),
                    request.form.get('incoterm') or 'EXW',
                    request.form.get('go_no_go') or 'PENDING',
                    request.form.get('logistics_notes'),
                    project_id,
                ),
            )
            db.commit()
            flash('Proyecto actualizado.')
        elif action == 'create_quote':
            system_id = int(request.form['system_id'])
            version_label = request.form.get('version_label') or f"V{db.execute('SELECT COUNT(*) AS c FROM project_quotes WHERE project_id = ?', (project_id,)).fetchone()['c'] + 1}"
            area_sqm = float(request.form.get('area_sqm') or project['area_sqm'] or 0)
            fx_rate = float(request.form.get('fx_rate') or project['fx_rate'] or 1)
            freight_eur = float(request.form.get('freight_eur') or project['freight_eur'] or 0)
            target_margin_pct = float(request.form.get('target_margin_pct') or project['target_margin_pct'] or 0.30)
            result = calculate_quote(system_id, area_sqm, freight_eur, target_margin_pct, fx_rate)
            db.execute(
                '''INSERT INTO project_quotes (project_id, system_id, version_label, area_sqm, fx_rate, freight_eur, customs_pct, target_margin_pct, result_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (project_id, system_id, version_label, area_sqm, fx_rate, freight_eur, 0, target_margin_pct, json.dumps(result), now_iso()),
            )
            db.commit()
            flash('Oferta/cálculo guardado.')
        return redirect(url_for('project_detail', project_id=project_id))

    systems = db.execute('SELECT * FROM systems ORDER BY name').fetchall()
    quotes = db.execute(
        '''SELECT q.*, s.name AS system_name FROM project_quotes q
           LEFT JOIN systems s ON s.id = q.system_id WHERE q.project_id = ? ORDER BY q.created_at DESC''',
        (project_id,),
    ).fetchall()
    events = db.execute('SELECT * FROM stage_events WHERE project_id = ? ORDER BY created_at DESC', (project_id,)).fetchall()

    parsed_quotes = []
    for q in quotes:
        payload = json.loads(q['result_json'])
        parsed_quotes.append({'meta': q, 'payload': payload})

    return render_template('project_detail.html', project=project, systems=systems, quotes=parsed_quotes, events=events, stages=STAGES)


@app.route('/calculator', methods=['GET', 'POST'])
@login_required
def calculator():
    db = get_db()
    systems = db.execute('SELECT * FROM systems ORDER BY name').fetchall()
    result = None
    if request.method == 'POST':
        result = calculate_quote(
            int(request.form['system_id']),
            float(request.form.get('area_sqm') or 0),
            float(request.form.get('freight_eur') or 0),
            float(request.form.get('target_margin_pct') or 0.30),
            float(request.form.get('fx_rate') or 1),
        )
    return render_template('calculator.html', systems=systems, result=result)


@app.route('/quote')
@login_required
def quote():
    db = get_db()
    
    # Get clients for selector
    clients = db.execute('SELECT id, name, company, email FROM clients ORDER BY name').fetchall()
    clients_data = [dict(c) for c in clients]
    
    products = db.execute(
        'SELECT id, sku, name, category, subfamily, unit, unit_price_eur, units_per_pallet, sqm_per_pallet, kg_per_unit '
        'FROM products ORDER BY category, name'
    ).fetchall()
    products_data = []
    for p in products:
        pd = dict(p)
        # V4 already has correct per-unit prices
        pd['price_per_unit'] = pd['unit_price_eur']
        pd['unit_label'] = pd['unit']
        
        # Assign subfamily if missing based on name patterns
        if not pd.get('subfamily'):
            cat = pd['category']
            name = pd['name'].upper()
            if cat == 'TORNILLOS':
                if 'PUNTA CLAVO' in name and 'ALTA' not in name and '3,9' not in name:
                    pd['subfamily'] = 'PM PC'
                elif 'PUNTA BROCA' in name and 'METAL-METAL' not in name and 'EXTERNA' not in name and 'Ø13' not in name:
                    pd['subfamily'] = 'PM PB'
                elif 'ALTA DENSIDAD' in name or 'Ø3,9' in name or '3,9X' in name:
                    pd['subfamily'] = 'AD PC'
                elif 'METAL-METAL' in name or 'Ø13' in name or '13X' in name:
                    pd['subfamily'] = 'MM PB'
                elif 'EXTERNA' in name or 'EL PB' in name or 'EXTERIOR' in name:
                    pd['subfamily'] = 'EL PB'
                else:
                    # Default by SKU pattern
                    sku = pd.get('sku', '')
                    if sku.startswith('3041') or sku.startswith('3012'):
                        pd['subfamily'] = 'PM PC'
            elif cat == 'PASTAS':
                if 'FASSAJOINT' in name or 'FASSAFLASH' in name:
                    if '8H' in name:
                        pd['subfamily'] = 'Fassajoint 8h'
                    elif 'IDEAL' in name:
                        pd['subfamily'] = 'Fassajoint Ideal'
                    else:
                        pd['subfamily'] = 'Fassajoint 3h'
                elif 'GYPSOFILLER' in name:
                    pd['subfamily'] = 'Gypsofiller'
                elif 'GYPSOMAF' in name:
                    pd['subfamily'] = 'Gypsomaf'
                elif 'FAST' in name:
                    pd['subfamily'] = 'Fast 299'
                else:
                    pd['subfamily'] = 'Fassajoint 3h'
            elif cat == 'TRAMPILLAS':
                if 'METALICA' in name or 'CLICK' in name:
                    pd['subfamily'] = 'Metálica'
                elif 'ALUMINIO' in name or 'AQUASUPER' in name:
                    pd['subfamily'] = 'Aluminio AQUASUPER'
                elif 'EI60' in name:
                    pd['subfamily'] = 'EI60'
                elif 'EI120' in name:
                    pd['subfamily'] = 'EI120'
                else:
                    pd['subfamily'] = 'Metálica'
            elif cat == 'ACCESORIOS':
                if 'HORQUILLA' in name:
                    pd['subfamily'] = 'Horquilla'
                elif 'EMPALME' in name:
                    pd['subfamily'] = 'Pieza Empalme'
                elif 'CRUCETA' in name:
                    pd['subfamily'] = 'Cruceta'
                elif 'SUSPENSION' in name or 'SUSPENSIÓN' in name:
                    pd['subfamily'] = 'Suspensión'
                elif 'ANCLAJE' in name or 'DIRECTO' in name or 'UNIVERSAL' in name:
                    pd['subfamily'] = 'Anclaje'
                elif 'AISLADOR' in name or 'ACUSTICO' in name or 'ACÚSTICO' in name:
                    pd['subfamily'] = 'Aislador'
                elif 'VARILLA' in name:
                    pd['subfamily'] = 'Varilla'
                elif 'MANGUITO' in name:
                    pd['subfamily'] = 'Manguito'
                elif 'GANCHO' in name:
                    pd['subfamily'] = 'Gancho'
                elif 'CLIP' in name and 'PERFIL' not in name:
                    pd['subfamily'] = 'Clip'
                elif 'ESQUINERO' in name:
                    pd['subfamily'] = 'Esquinero'
                else:
                    pd['subfamily'] = 'Horquilla'
            elif cat == 'GYPSOCOMETE':
                if 'ANGLE' in name: pd['subfamily'] = 'ANGLE'
                elif 'CROSS' in name: pd['subfamily'] = 'CROSS'
                elif 'STAR' in name: pd['subfamily'] = 'STAR'
                elif 'LINE' in name: pd['subfamily'] = 'LINE'
                elif 'GALAXY' in name: pd['subfamily'] = 'Galaxy'
                elif 'MIX' in name: pd['subfamily'] = 'Mix'
                else:
                    pd['subfamily'] = 'LINE'
        
        products_data.append(pd)
    
    families = sorted(set(p['category'] for p in products_data))
    # Custom order for families
    family_order = ['PLACAS', 'PERFILES', 'ACCESORIOS', 'CINTAS', 'TORNILLOS', 'PASTAS', 'GYPSOCOMETE', 'TRAMPILLAS']
    families_ordered = [f for f in family_order if f in families] + [f for f in families if f not in family_order]
    
    # Subfamilies for ALL families (grouped by family)
    subfamilies_map = {}
    for p in products_data:
        sf = p.get('subfamily')
        if sf and sf.strip():
            fam = p['category']
            if fam not in subfamilies_map:
                subfamilies_map[fam] = []
            if sf not in subfamilies_map[fam]:
                subfamilies_map[fam].append(sf)
    
    # Map names for display — exact index names (without GYPSOTECH® and page numbers)
    subfamily_labels = {
        'PLACAS': {
            'STD': 'STD Tipo A 10',
            'STD Zero': 'STD zero Tipo A 11',
            'POCKET STD': 'GypsoPocket STD Tipo A 11',
            'SIMPLY': 'GypsoSIMPLY Tipo A 11',
            'FOCUS': 'FOCUS Tipo DFI 12',
            'FOCUS Ultra': 'FOCUS ultra Tipo DFIR 12',
            'FOCUS Zero': 'FOCUS zero Tipo DFI 13',
            'SILENS': 'GypsoSILENS Tipo DI 13',
            'AQUA H2': 'AQUA Tipo EH2 14',
            'POCKET AQUA H2': 'GypsoPocket AQUA Tipo EH2 14',
            'POCKET AQUASUPER': 'GypsoPocket AQUASUPER Tipo EH1 14',
            'AQUASUPER': 'AQUASUPER Tipo EH1/DEH1 15',
            'EXTERNA': 'EXTERNA light 15',
            'VAPOR': 'VAPOR 16',
            'LIGNUM': 'GypsoLIGNUM Tipo DEFH1IR 16',
            'LIGNUM Zero': 'GypsoLIGNUM zero Tipo DEFH1I',
        },
        'PERFILES': {
            'Montante': 'MONTANTE',
            'Rail': 'RAIL',
            'Techo': 'PERFIL TECHO CONTINUO',
            'Sierra': 'PERFIL SIERRA',
            'Angular': 'PERFIL ANGULAR',
            'Clip': 'PERFIL CLIP',
            'U': 'PERFIL U',
            'Omega': 'OMEGA',
        },
        'TORNILLOS': {
            'PM PC': 'PLACA-METAL PUNTA CLAVO',
            'PM PB': 'PLACA-METAL PUNTA BROCA',
            'AD PC': 'PLACA-METAL ALTA DENSIDAD',
            'MM PB': 'METAL-METAL PUNTA BROCA',
            'EL PB': 'EXTERNA LIGHT',
        },
        'CINTAS': {
            'Juntas': 'CINTA DE JUNTAS',
            'FV Autoadhesiva': 'CINTAS DE REFUERZO',
            'Guardavivos': 'CINTA GUARDAVIVOS',
            'Bandas Estancas': 'BANDAS ESTANCAS',
            'Externa Light': 'MALLA EXTERNA LIGHT',
        },
        'PASTAS': {
            'Fassajoint 3h': 'PASTAS DE JUNTAS EN POLVO',
            'Fassajoint 8h': 'PASTAS DE JUNTAS EN POLVO',
            'Fassajoint Ideal': 'PASTAS DE JUNTAS EN POLVO',
            'Gypsofiller': 'PASTA DE JUNTAS PREPARADA',
            'Gypsomaf': 'PASTA DE AGARRE',
            'Fast 299': 'RASEO ARMADO',
        },
        'TRAMPILLAS': {
            'Metálica': 'METÁLICAS',
            'Aluminio AQUASUPER': 'ALUMINIO-PLACA AQUASUPER H1',
            'EI60': 'RESISTENTES AL FUEGO',
            'EI120': 'RESISTENTES AL FUEGO',
        },
        'ACCESORIOS': {
            'Horquilla': 'HORQUILLAS',
            'Pieza Empalme': 'PIEZAS DE EMPALME',
            'Cruceta': 'CRUCETAS',
            'Suspensión': 'PIEZAS DE SUSPENSIÓN',
            'Anclaje': 'PIEZAS ANCLAJE',
            'Aislador': 'AISLADORES ACÚSTICOS',
            'Varilla': 'OTROS ELEMENTOS DE CUELGUE',
            'Manguito': 'OTROS ELEMENTOS DE CUELGUE',
            'Gancho': 'OTROS ELEMENTOS DE CUELGUE',
            'Clip': 'OTROS ELEMENTOS DE CUELGUE',
            'Esquinero': 'OTROS ELEMENTOS DE CUELGUE',
        },
        'GYPSOCOMETE': {
            'LINE': 'GypsoCOMETE',
            'ANGLE': 'GypsoCOMETE',
            'CROSS': 'GypsoCOMETE',
            'STAR': 'GypsoCOMETE',
            'Galaxy': 'GypsoCOMETE GALAXY',
            'Mix': 'GypsoCOMETE MIX',
        },
    }
    
    # Build friendly subfamilies dict (deduplicated, preserving order)
    subfamilies_friendly = {}
    for fam, sfs in subfamilies_map.items():
        labels = subfamily_labels.get(fam, {})
        seen = set()
        unique = []
        for sf in sfs:
            label = labels.get(sf, sf)
            if label not in seen:
                seen.add(label)
                unique.append({'key': sf, 'label': label})
        subfamilies_friendly[fam] = unique
    
    systems = db.execute(
        '''SELECT s.*, GROUP_CONCAT(
            json_object('sku', p.sku, 'name', p.name, 'category', p.category,
                        'unit', p.unit, 'unit_price_eur', p.unit_price_eur,
                        'consumption_per_sqm', sc.consumption_per_sqm,
                        'waste_pct', sc.waste_pct)
        ) as components_json
           FROM systems s
           LEFT JOIN system_components sc ON sc.system_id = s.id
           LEFT JOIN products p ON p.id = sc.product_id
           GROUP BY s.id'''
    ).fetchall()
    systems_data = []
    for s in systems:
        sd = dict(s)
        comps = json.loads('[' + (sd.get('components_json') or '') + ']') if sd.get('components_json') else []
        systems_data.append({
            'id': sd['id'], 'name': sd['name'], 'description': sd['description'],
            'components': comps
        })
    
    routes = db.execute('SELECT * FROM shipping_routes').fetchall()
    routes_data = [dict(r) for r in routes]
    # Fuente única (ver dashboard): el cotizador toma el rate oficial de
    # fx_rates, no app_settings. Si fx_rates está vacío, fallback a
    # app_settings y luego al hardcoded 1.18.
    fx_rate = get_current_fx_eur_usd()
    projects_raw = db.execute(
        'SELECT id, client_id, name, area_sqm, incoterm FROM projects ORDER BY created_at DESC'
    ).fetchall()
    projects_data = [dict(p) for p in projects_raw]

    edit_offer = None
    edit_id = request.args.get('edit')
    if edit_id:
        row = db.execute('SELECT * FROM pending_offers WHERE id = ?', (edit_id,)).fetchone()
        if row:
            edit_offer = dict(row)
            edit_offer['lines'] = json.loads(row['lines_json']) if row['lines_json'] else []

    return render_template('quote.html', products=products_data, clients=clients_data,
                          families=families_ordered,
                          subfamilies=subfamilies_friendly, systems=systems_data,
                          routes=routes_data, fx_rate=fx_rate,
                          projects=projects_data, edit_offer=edit_offer)


@app.route('/projects/<int:project_id>/quote/<int:quote_id>/pdf')
@login_required
def quote_pdf(project_id: int, quote_id: int):
    db = get_db()
    project = db.execute(
        '''SELECT p.*, c.name AS client_name, c.company, c.email, c.phone, c.country
           FROM projects p JOIN clients c ON c.id = p.client_id WHERE p.id = ?''',
        (project_id,),
    ).fetchone()
    if not project:
        return 'Proyecto no encontrado', 404

    quote = db.execute(
        '''SELECT q.*, s.name AS system_name FROM project_quotes q
           LEFT JOIN systems s ON s.id = q.system_id WHERE q.id = ? AND q.project_id = ?''',
        (quote_id, project_id),
    ).fetchone()
    if not quote:
        return 'Oferta no encontrada', 404

    payload = json.loads(quote['result_json'])
    summary = payload['summary']
    line_items = payload['line_items']

    # ── PDF SETUP ─────────────────────────────────────────────────
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=18*mm, bottomMargin=18*mm,
        title=f"Oferta {quote['version_label']} - {project['name']}",
    )

    # Paleta oficial Arias Group (alineada con offer_pdf y design system).
    NAVY   = colors.HexColor('#1A3557')
    BLUE   = colors.HexColor('#2563A8')
    GOLD   = BLUE                          # acentos antes en dorado → ahora azul
    LGRAY  = colors.HexColor('#EEF3F9')    # fondo tabla / bloques (BLUE_PALE)
    MGRAY  = colors.HexColor('#5C7A99')    # labels/meta (STONE)
    WHITE  = colors.white

    styles = getSampleStyleSheet()

    def sty(name, parent='Normal', **kw):
        return ParagraphStyle(name, parent=styles[parent], **kw)

    S = {
        'brand':    sty('brand',    fontSize=9,  textColor=GOLD,  fontName='Helvetica'),
        'h1':       sty('h1',       fontSize=18, textColor=NAVY,  fontName='Helvetica-Bold', spaceAfter=2),
        'h2':       sty('h2',       fontSize=11, textColor=NAVY,  fontName='Helvetica-Bold', spaceBefore=10, spaceAfter=4),
        'label':    sty('label',    fontSize=8,  textColor=MGRAY, fontName='Helvetica'),
        'value':    sty('value',    fontSize=9,  textColor=NAVY,  fontName='Helvetica-Bold'),
        'body':     sty('body',     fontSize=9,  textColor=colors.HexColor('#4A4540'), leading=14),
        'small':    sty('small',    fontSize=7,  textColor=MGRAY),
        'total_l':  sty('total_l',  fontSize=10, textColor=WHITE, fontName='Helvetica-Bold', alignment=TA_RIGHT),
        'total_v':  sty('total_v',  fontSize=10, textColor=WHITE, fontName='Helvetica-Bold', alignment=TA_RIGHT),
        'right':    sty('right',    fontSize=9,  textColor=NAVY,  alignment=TA_RIGHT),
        'center':   sty('center',   fontSize=9,  textColor=NAVY,  alignment=TA_CENTER),
        'footer':   sty('footer',   fontSize=7,  textColor=MGRAY, alignment=TA_CENTER),
        'cond':     sty('cond',     fontSize=8,  textColor=colors.HexColor('#4A4540'), leading=13),
    }

    story = []
    W = A4[0] - 40*mm  # usable width

    # ── HEADER BLOCK ──────────────────────────────────────────────
    for el in _ag_unified_header('OFERTA TÉCNICA', W):
        story.append(el)
    story.append(Spacer(1, 5*mm))

    # ── REFERENCE ROW ─────────────────────────────────────────────
    ref_date = quote['created_at'][:10]
    # project_quotes (oferta técnica interna) no tiene columna validity_days; la
    # validez vive en pending_offers (oferta comercial → offer_pdf). Si en el
    # futuro se añade la columna o se vuelca al payload result_json, este lookup
    # la recoge sin tocar más código.
    quote_keys = quote.keys()
    if 'validity_days' in quote_keys and quote['validity_days']:
        validity = int(quote['validity_days'])
    else:
        validity = int(_num(summary.get('validity_days', 30)) or 30)
    ref_data = [[
        Paragraph(f"<b>Ref:</b> {quote['version_label']}", S['body']),
        Paragraph(f"<b>Fecha:</b> {ref_date}", S['body']),
        Paragraph(f"<b>Incoterm:</b> {project['incoterm']}", S['body']),
        Paragraph(f"<b>Validez:</b> {validity} días", S['body']),
    ]]
    ref_tbl = Table(ref_data, colWidths=[W*0.28, W*0.24, W*0.24, W*0.24])
    ref_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0,0),(-1,-1), LGRAY),
        ('TOPPADDING',    (0,0),(-1,-1), 5),
        ('BOTTOMPADDING', (0,0),(-1,-1), 5),
        ('LEFTPADDING',   (0,0),(-1,-1), 6),
        ('BOX', (0,0),(-1,-1), 0.5, GOLD),
    ]))
    story.append(ref_tbl)
    story.append(Spacer(1, 5*mm))

    # ── CLIENT + PROJECT ──────────────────────────────────────────
    cp_data = [
        [Paragraph('<b>CLIENTE</b>', sty('cp_h', fontSize=8, textColor=GOLD, fontName='Helvetica-Bold')),
         Paragraph('<b>PROYECTO</b>', sty('cp_h2', fontSize=8, textColor=GOLD, fontName='Helvetica-Bold'))],
        [Paragraph(f"{project['client_name']}<br/>{project['company'] or ''}", S['value']),
         Paragraph(f"{project['name']}<br/>{project['location'] or ''}", S['value'])],
        [Paragraph(f"{project['email'] or ''} · {project['phone'] or ''}", S['label']),
         Paragraph(f"{project['project_type'] or ''} · {project['area_sqm']:,.0f} m²", S['label'])],
    ]
    cp_tbl = Table(cp_data, colWidths=[W*0.5, W*0.5])
    cp_tbl.setStyle(TableStyle([
        ('TOPPADDING',    (0,0),(-1,-1), 3),
        ('BOTTOMPADDING', (0,0),(-1,-1), 3),
        ('LEFTPADDING',   (0,0),(-1,-1), 0),
        ('LINEBELOW', (0,2),(-1,2), 0.5, colors.HexColor('#D0CBBC')),
    ]))
    story.append(cp_tbl)
    story.append(Spacer(1, 5*mm))

    # ── SYSTEM TITLE ──────────────────────────────────────────────
    story.append(Paragraph(f"Sistema: {payload['system_name']}", S['h2']))
    story.append(Paragraph(
        f"Superficie a tratar: <b>{payload['area_sqm']:,.0f} m²</b>  ·  "
        f"Palés estimados: <b>{summary['total_pallets']}</b>  ·  "
        f"Peso total: <b>{summary['total_weight_kg']:,.0f} kg</b>  ·  "
        f"Cont. 40': <b>{summary['containers_40_est']}</b>",
        S['body']
    ))
    story.append(Spacer(1, 3*mm))

    # ── LINE ITEMS TABLE ──────────────────────────────────────────
    li_head = [
        Paragraph('SKU', sty('th', fontSize=8, textColor=WHITE, fontName='Helvetica-Bold')),
        Paragraph('Producto', sty('th2', fontSize=8, textColor=WHITE, fontName='Helvetica-Bold')),
        Paragraph('Unid.', sty('th3', fontSize=8, textColor=WHITE, fontName='Helvetica-Bold', alignment=TA_CENTER)),
        Paragraph('Cant.', sty('th4', fontSize=8, textColor=WHITE, fontName='Helvetica-Bold', alignment=TA_RIGHT)),
        Paragraph('Palés', sty('th5', fontSize=8, textColor=WHITE, fontName='Helvetica-Bold', alignment=TA_RIGHT)),
        Paragraph('Coste €', sty('th6', fontSize=8, textColor=WHITE, fontName='Helvetica-Bold', alignment=TA_RIGHT)),
    ]
    li_rows = [li_head]
    for i, li in enumerate(line_items):
        bg = LGRAY if i % 2 == 0 else WHITE
        li_rows.append([
            Paragraph(li['sku'], S['small']),
            Paragraph(li['name'], S['body']),
            Paragraph(li['unit'], S['center']),
            Paragraph(f"{li['units']:,}", S['right']),
            Paragraph(f"{li['pallets']:.2f}", S['right']),
            Paragraph(f"€ {li['product_cost_eur']:,.2f}", S['right']),
        ])

    li_tbl = Table(li_rows, colWidths=[W*0.14, W*0.36, W*0.08, W*0.12, W*0.12, W*0.18])
    li_style = [
        ('BACKGROUND', (0,0),(-1,0), BLUE),
        ('TOPPADDING',    (0,0),(-1,-1), 4),
        ('BOTTOMPADDING', (0,0),(-1,-1), 4),
        ('LEFTPADDING',   (0,0),(-1,-1), 4),
        ('RIGHTPADDING',  (0,0),(-1,-1), 4),
        ('GRID', (0,0),(-1,-1), 0.25, colors.HexColor('#D0CBBC')),
        ('ROWBACKGROUNDS', (0,1),(-1,-1), [LGRAY, WHITE]),
    ]
    li_tbl.setStyle(TableStyle(li_style))
    story.append(li_tbl)
    story.append(Spacer(1, 5*mm))

    # ── FINANCIAL SUMMARY ─────────────────────────────────────────
    story.append(Paragraph('Resumen Económico', S['h2']))

    fin_rows = [
        ['Coste producto EXW fábrica', f"€ {summary['product_cost_eur']:,.2f}"],
        ['Flete internacional estimado', f"€ {summary['freight_eur']:,.2f}"],
        ['Coste total puesto en destino', f"€ {summary['landed_total_eur']:,.2f}"],
    ]
    fin_tbl = Table(
        [[Paragraph(r[0], S['body']), Paragraph(r[1], S['right'])] for r in fin_rows],
        colWidths=[W*0.65, W*0.35]
    )
    fin_tbl.setStyle(TableStyle([
        ('TOPPADDING',    (0,0),(-1,-1), 4),
        ('BOTTOMPADDING', (0,0),(-1,-1), 4),
        ('LEFTPADDING',   (0,0),(-1,-1), 4),
        ('ROWBACKGROUNDS', (0,0),(-1,-1), [LGRAY, WHITE, LGRAY]),
        ('LINEBELOW', (0,-1),(-1,-1), 0.5, colors.HexColor('#D0CBBC')),
    ]))
    story.append(fin_tbl)
    story.append(Spacer(1, 2*mm))

    # Total price highlight
    total_row = Table(
        [[Paragraph('PRECIO TOTAL DE VENTA (EXW)', S['total_l']),
          Paragraph(f"€ {summary['sale_total_eur']:,.2f}", S['total_v'])]],
        colWidths=[W*0.65, W*0.35]
    )
    total_row.setStyle(TableStyle([
        ('BACKGROUND', (0,0),(-1,-1), NAVY),
        ('TOPPADDING',    (0,0),(-1,-1), 8),
        ('BOTTOMPADDING', (0,0),(-1,-1), 8),
        ('LEFTPADDING',   (0,0),(-1,-1), 6),
        ('RIGHTPADDING',  (0,0),(-1,-1), 6),
    ]))
    story.append(total_row)
    story.append(Spacer(1, 2*mm))

    margin_pct = summary['gross_margin_pct'] * 100
    story.append(Paragraph(
        f"Margen bruto: <b>{margin_pct:.1f}%</b>  ·  "
        f"€/m²: <b>€ {summary['price_per_sqm_eur']:,.2f}</b>  ·  "
        f"FX: <b>{summary['fx_rate']}</b>",
        S['small']
    ))
    story.append(Spacer(1, 6*mm))

    # ── CONDITIONS ────────────────────────────────────────────────
    story.append(HRFlowable(width=W, thickness=0.5, color=GOLD))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph('Condiciones Comerciales', S['h2']))

    conditions = [
        f"<b>Incoterm:</b> {project['incoterm']} — Fassa Hispania, C. Londres 1, 16400 Tarancón, Cuenca, España.",
        "<b>Forma de pago:</b> 100% prepago por transferencia bancaria antes de emisión de orden de producción.",
        "<b>Plazo de entrega:</b> Según confirmación de fábrica tras recepción de pago. Stock estándar: aprox. 2 días hábiles hasta carga.",
        "<b>Validez de oferta:</b> 30 días naturales desde fecha de emisión.",
        "<b>Normativa:</b> Todos los productos cumplen normativa europea vigente (CE, ETA, EN). Fichas técnicas disponibles bajo solicitud.",
        "<b>Logística:</b> Cotización de flete no incluida — se facilita gestión con operador logístico recomendado.",
    ]
    for c in conditions:
        story.append(Paragraph(c, S['cond']))
        story.append(Spacer(1, 2*mm))

    story.append(Spacer(1, 8*mm))

    # ── SIGNATURE BLOCK ───────────────────────────────────────────
    sig_data = [[
        Paragraph('Por Fassa – Arias Group\n\n\n\n___________________________\nOliver González Arias\nDirector Comercial', S['body']),
        Paragraph(f'Por {project["company"] or project["client_name"]}\n\n\n\n___________________________\n{project["client_name"]}\n', S['body']),
    ]]
    sig_tbl = Table(sig_data, colWidths=[W*0.5, W*0.5])
    sig_tbl.setStyle(TableStyle([
        ('TOPPADDING',    (0,0),(-1,-1), 6),
        ('LEFTPADDING',   (0,0),(-1,-1), 0),
        ('LINEABOVE', (0,0),(-1,-1), 0.5, colors.HexColor('#D0CBBC')),
    ]))
    story.append(sig_tbl)
    story.append(Spacer(1, 6*mm))

    # ── FOOTER ────────────────────────────────────────────────────
    story.append(HRFlowable(width=W, thickness=0.3, color=MGRAY))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        'Fassa – Arias Group · Distribución Técnica Fassa Bortolo · Caribe · '
        'Documento confidencial generado automáticamente · No válido sin firma',
        S['footer']
    ))

    doc.build(story)
    buffer.seek(0)

    filename = f"Oferta_{quote['version_label']}_{project['name'].replace(' ','_')}.pdf"
    response = make_response(buffer.read())
    response.headers['Content-Type'] = 'application/pdf'
    disp = 'attachment' if request.args.get('download') else 'inline'
    response.headers['Content-Disposition'] = f'{disp}; filename="{filename}"'
    return response


@app.route('/masters', methods=['GET', 'POST'])
@login_required
def masters():
    db = get_db()
    tab = request.args.get('tab', 'shipping')

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add_shipping':
            db.execute('''INSERT INTO shipping_routes
                (origin_port, destination_port, carrier, transit_days,
                 container_20_eur, container_40_eur, container_40hc_eur,
                 insurance_pct, valid_from, valid_until, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (
                request.form['origin_port'],
                request.form['destination_port'],
                request.form.get('carrier'),
                int(request.form.get('transit_days') or 0),
                float(request.form.get('container_20_eur') or 0),
                float(request.form.get('container_40_eur') or 0),
                float(request.form.get('container_40hc_eur') or 0),
                float(request.form.get('insurance_pct') or 0.005),
                request.form.get('valid_from'),
                request.form.get('valid_until'),
                request.form.get('notes'),
            ))
            db.commit()
            flash('Ruta añadida.')

        elif action == 'delete_shipping':
            db.execute('DELETE FROM shipping_routes WHERE id = ?', (request.form['id'],))
            db.commit()
            flash('Ruta eliminada.')

        elif action == 'add_customs':
            db.execute('''INSERT INTO customs_rates
                (country, hs_code, category, dai_pct, itbis_pct, other_pct, notes)
                VALUES (?,?,?,?,?,?,?)''', (
                request.form['country'],
                request.form['hs_code'],
                request.form.get('category'),
                float(request.form.get('dai_pct') or 0),
                float(request.form.get('itbis_pct') or 0.18),
                float(request.form.get('other_pct') or 0),
                request.form.get('notes'),
            ))
            db.commit()
            flash('Arancel añadido.')

        elif action == 'delete_customs':
            db.execute('DELETE FROM customs_rates WHERE id = ?', (request.form['id'],))
            db.commit()
            flash('Arancel eliminado.')

        elif action == 'update_fx':
            db.execute('UPDATE fx_rates SET rate = ?, updated_at = ?, source = ? WHERE id = ?', (
                float(request.form['rate']),
                now_iso(),
                request.form.get('source', 'Manual'),
                int(request.form['id']),
            ))
            db.commit()
            flash('Tipo de cambio actualizado.')

        elif action == 'add_fx':
            db.execute('''INSERT INTO fx_rates (base_currency, target_currency, rate, updated_at, source)
                VALUES (?,?,?,?,?)''', (
                request.form.get('base_currency', 'EUR'),
                request.form['target_currency'],
                float(request.form['rate']),
                now_iso(),
                request.form.get('source', 'Manual'),
            ))
            db.commit()
            flash('FX añadido.')

        return redirect(url_for('masters', tab=tab))

    shipping = db.execute('SELECT * FROM shipping_routes ORDER BY origin_port, destination_port').fetchall()
    customs  = db.execute('SELECT * FROM customs_rates ORDER BY country, hs_code').fetchall()
    fx       = db.execute('SELECT * FROM fx_rates ORDER BY base_currency, target_currency').fetchall()
    return render_template('masters.html', shipping=shipping, customs=customs, fx=fx, tab=tab)


@app.route('/dashboard/financial')
@login_required
def dashboard_financial():
    db = get_db()

    # Pipeline value by stage
    pipeline = db.execute('''
        SELECT stage, COUNT(*) as count,
               SUM(CASE WHEN go_no_go='GO' THEN 1 ELSE 0 END) as go_count
        FROM projects GROUP BY stage
    ''').fetchall()

    # Recent quotes with margins
    recent_quotes = db.execute('''
        SELECT q.*, p.name as project_name, c.name as client_name, c.company,
               p.stage, s.name as system_name
        FROM project_quotes q
        JOIN projects p ON p.id = q.project_id
        JOIN clients c ON c.id = p.client_id
        LEFT JOIN systems s ON s.id = q.system_id
        ORDER BY q.created_at DESC LIMIT 20
    ''').fetchall()

    # Parse margins from JSON
    quotes_data = []
    total_pipeline_eur = 0
    total_confirmed_eur = 0
    for q in recent_quotes:
        payload = json.loads(q['result_json'])
        s = payload['summary']
        margin_pct = round(s['gross_margin_pct'] * 100, 1)
        quotes_data.append({
            'id': q['id'],
            'project_id': q['project_id'],
            'version_label': q['version_label'],
            'project_name': q['project_name'],
            'client_name': q['client_name'],
            'company': q['company'],
            'stage': q['stage'],
            'system_name': q['system_name'],
            'area_sqm': q['area_sqm'],
            'sale_total_eur': s['sale_total_eur'],
            'landed_total_eur': s['landed_total_eur'],
            'gross_margin_eur': s['gross_margin_eur'],
            'gross_margin_pct': margin_pct,
            'price_per_sqm': s['price_per_sqm_eur'],
            'pallets': s['total_pallets'],
            'containers_40': s['containers_40_est'],
            'created_at': q['created_at'][:10],
            'margin_ok': margin_pct >= 18,
        })
        total_pipeline_eur += s['sale_total_eur']
        if q['stage'] in ('PREPAGO VALIDADO', 'ORDEN BLOQUEADA', 'PEDIDO A FASSA',
                           'CONFIRMACIÓN FÁBRICA', 'EXPEDICIÓN (BL)', 'ENTREGA'):
            total_confirmed_eur += s['sale_total_eur']

    # Stage distribution
    stage_data = {row['stage']: row['count'] for row in pipeline}

    # Counts
    counts = {
        'clients':  db.execute('SELECT COUNT(*) FROM clients').fetchone()[0],
        'projects': db.execute('SELECT COUNT(*) FROM projects').fetchone()[0],
        'go':       db.execute("SELECT COUNT(*) FROM projects WHERE go_no_go='GO'").fetchone()[0],
        'products': db.execute('SELECT COUNT(*) FROM products').fetchone()[0],
        'quotes':   db.execute('SELECT COUNT(*) FROM project_quotes').fetchone()[0],
    }

    # FX rates
    fx = {r['target_currency']: r['rate']
          for r in db.execute('SELECT * FROM fx_rates WHERE base_currency="EUR"').fetchall()}

    return render_template('dashboard_financial.html',
        quotes_data=quotes_data,
        total_pipeline_eur=total_pipeline_eur,
        total_confirmed_eur=total_confirmed_eur,
        stage_data=stage_data,
        counts=counts,
        fx=fx,
        stages=STAGES,
    )


# Custom filter
# ── CRM: Clientes + Proyectos unificados ─────────────────────────
@app.route('/crm', methods=['GET', 'POST'])
@login_required
def crm():
    db = get_db()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add_client':
            db.execute(
                '''INSERT INTO clients (name, company, email, phone, country, score, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (request.form['name'], request.form.get('company'),
                 request.form.get('email'), request.form.get('phone'),
                 request.form.get('country', 'RD'), request.form.get('score', 'C'),
                 now_iso())
            )
            flash('Cliente creado.')
        elif action == 'add_project':
            db.execute(
                '''INSERT INTO projects (name, client_id, stage, go_no_go, area_sqm, incoterm, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (request.form['name'], int(request.form['client_id']),
                 request.form.get('stage', 'CLIENTE'), request.form.get('go_no_go', 'GO'),
                 float(request.form.get('area_sqm', 0) or 0),
                 request.form.get('incoterm', 'EXW'),
                 now_iso())
            )
            flash('Proyecto creado.')
        db.commit()
        return redirect(url_for('crm'))
    
    clients = db.execute('SELECT * FROM clients ORDER BY name').fetchall()
    projects = db.execute(
        '''SELECT p.*, c.name AS client_name, c.company
           FROM projects p JOIN clients c ON c.id = p.client_id ORDER BY p.created_at DESC'''
    ).fetchall()
    return render_template('crm.html', clients=clients, projects=projects, stages=STAGES)


# ── Presupuestos: Listado de ofertas generadas ──────────────────
@app.route('/presupuestos')
@login_required
def presupuestos():
    db = get_db()
    offers = db.execute('SELECT * FROM pending_offers ORDER BY created_at DESC').fetchall()
    offers_data = [dict(o) for o in offers]
    # Adjuntar factory_order y logistics_order vinculados (auto-creados al aprobar).
    fo_rows = db.execute(
        'SELECT offer_id, name, state, sent_to_factory_at, confirmed_at FROM factory_orders'
    ).fetchall()
    lo_rows = db.execute(
        'SELECT offer_id, name, state, departure_date, eta_date, delivered_at FROM logistics_orders'
    ).fetchall()
    fo_by_offer = {r['offer_id']: dict(r) for r in fo_rows}
    lo_by_offer = {r['offer_id']: dict(r) for r in lo_rows}
    for o in offers_data:
        o['factory_order'] = fo_by_offer.get(o['id'])
        o['logistics_order'] = lo_by_offer.get(o['id'])
    routes = db.execute('SELECT * FROM shipping_routes').fetchall()
    return render_template('presupuestos.html', offers=offers_data, routes=[dict(r) for r in routes])


# ── Configuración: Rutas, aranceles, FX ─────────────────────────
@app.route('/config', methods=['GET', 'POST'])
@login_required
def config():
    db = get_db()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add_route':
            db.execute(
                '''INSERT INTO shipping_routes (carrier, origin_port, destination_port, transit_days,
                   container_20_eur, container_40_eur, container_40hc_eur)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (request.form['carrier'], request.form['origin_port'],
                 request.form['destination_port'], int(request.form.get('transit_days', 25) or 25),
                 float(request.form.get('container_20_eur', 0) or 0),
                 float(request.form.get('container_40_eur', 0) or 0),
                 float(request.form.get('container_40hc_eur', 0) or 0))
            )
            flash('Ruta añadida.')
        elif action == 'add_customs':
            db.execute(
                '''INSERT INTO customs_rates (country, hs_code, category, dai_pct, itbis_pct, other_pct, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (request.form['country'], request.form['hs_code'],
                 request.form.get('category', ''), float(request.form.get('dai_pct', 0) or 0),
                 float(request.form.get('itbis_pct', 0) or 0), float(request.form.get('other_pct', 0) or 0),
                 request.form.get('notes', ''))
            )
            flash('Arancel añadido.')
        elif action == 'add_fx':
            # No natural unique key on fx_rates; OR REPLACE was vestigial. Plain INSERT
            # works identically on both backends.
            db.execute(
                '''INSERT INTO fx_rates (base_currency, target_currency, rate, updated_at, source)
                   VALUES (?, ?, ?, ?, ?)''',
                (request.form['base_currency'], request.form['target_currency'],
                 float(request.form['rate']), now_iso(),
                 request.form.get('source', 'Manual'))
            )
            flash('Tipo de cambio actualizado.')
        elif action == 'update_route':
            route_id = request.form.get('route_id')
            db.execute(
                '''UPDATE shipping_routes SET carrier=?, origin_port=?, destination_port=?,
                   container_20_eur=?, container_40_eur=?, container_40hc_eur=? WHERE id=?''',
                (request.form['carrier'], request.form['origin_port'],
                 request.form['destination_port'],
                 float(request.form.get('container_20_eur', 0) or 0),
                 float(request.form.get('container_40_eur', 0) or 0),
                 float(request.form.get('container_40hc_eur', 0) or 0),
                 route_id)
            )
            flash('Ruta actualizada.')
        elif action == 'update_fx_setting':
            new_fx = float(request.form['fx_eur_usd'])
            now = now_iso()
            # Escribir en AMBAS tablas para mantener coherencia: fx_rates es
            # lo que get_current_fx_eur_usd() lee primero (catálogo/cotizador),
            # app_settings se conserva por compatibilidad con el editor actual.
            db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES ('fx_eur_usd', ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (str(new_fx), now)
            )
            db.execute(
                "INSERT INTO fx_rates (base_currency, target_currency, rate, updated_at, source) "
                "VALUES ('EUR', 'USD', ?, ?, ?)",
                (new_fx, now, 'Manual desde /masters')
            )
            flash(f'EUR/USD actualizado a {new_fx} (aplicado a catálogo y cotizador)')
        db.commit()
        return redirect(url_for('config'))

    routes = db.execute('SELECT * FROM shipping_routes').fetchall()
    customs = db.execute('SELECT * FROM customs_rates').fetchall()
    fx = db.execute('SELECT * FROM fx_rates').fetchall()
    fx_setting = db.execute("SELECT value, updated_at FROM app_settings WHERE key='fx_eur_usd'").fetchone()
    return render_template('config.html',
                          routes=[dict(r) for r in routes],
                          customs=[dict(c) for c in customs],
                          fx=[dict(f) for f in fx],
                          fx_eur_usd=float(fx_setting['value']) if fx_setting else 1.18,
                          fx_updated=(fx_setting['updated_at'][:16].replace('T', ' ') if fx_setting and fx_setting['updated_at'] else None))


# ── API: Save pending offer ───────────────────────────────────────
@app.route('/api/save-offer', methods=['POST'])
@login_required
def save_offer():
    db = get_db()
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400

    waste_pct = _num(data.get('wastePct', 5)) / 100
    margin_pct = _num(data.get('margin', 33)) / 100
    logistic = _num(data.get('logisticCost', 0))
    fx = _num(data.get('fx', 1.18))

    raw_lines = data.get('lines', [])
    input_lines: list[dict[str, Any]] = []
    computed: list[dict[str, Any]] = []
    skipped: list[str] = []
    for li in raw_lines:
        sku = li.get('sku')
        qty = _num(li.get('qty', 0))
        if not sku or qty <= 0:
            continue
        prod = db.execute('SELECT * FROM products WHERE sku = ?', (sku,)).fetchone()
        if not prod:
            skipped.append(sku)
            continue
        pd = dict(prod)
        qty_with_waste = math.ceil(qty * (1 + waste_pct))
        line = compute_line(pd, qty_with_waste)
        line['qty_original'] = qty
        computed.append(line)
        input_lines.append({
            'sku': pd['sku'], 'name': pd['name'], 'family': pd['category'],
            'unit': pd['unit'], 'price': pd['unit_price_eur'], 'qty': qty,
            'margin': _num(li.get('margin', data.get('margin', 33))),
            'log_unit_cost': _num(li.get('log_unit_cost', 0)),
            'log_cost_manual': bool(li.get('log_cost_manual', False)),
            'competitor_price_eur': _num(li.get('competitor_price_eur', 0)),
            'note': li.get('note'),
        })

    totals = compute_totals(computed)
    # Fuente única de cálculo: build_offer_breakdown devuelve totales + breakdown
    # por línea. El breakdown se mergea en input_lines para persistirse en
    # lines_json y que offer_pdf, exports, etc. lean valores ya calculados sin
    # recalcular.
    breakdown = build_offer_breakdown(input_lines, computed, margin_pct, logistic)
    product_cost = breakdown['totals']['product_cost_eur']
    logistic_eur = breakdown['totals']['logistic_eur']
    total_final = breakdown['totals']['sale_eur']
    for li, br in zip(input_lines, breakdown['lines']):
        li.update({
            'qty_waste': br['qty_waste'],
            'cost_line_eur': br['cost_line_eur'],
            'log_line_eur': br['log_line_eur'],
            'sale_line_eur': br['sale_line_eur'],
            'sale_unit_eur': br['sale_unit_eur'],
            'margin_applied_pct': br['margin_pct'],
        })

    container_count = (totals.get('containers') or {}).get('units', 0) or _num(data.get('containerCount', 0))

    # offer_number SIEMPRE generado por backend con unicidad garantizada.
    # Ignoramos data.get('offerNumber'): el frontend lo enviaba aleatorio
    # ('2026-' + Date.now().slice(-4)), lo que causó colisiones históricas
    # (p.ej. ofertas #18 y #19 con el mismo '2026-8464').
    offer_num = generate_offer_number(db)
    raw_hash = compute_raw_hash(json.dumps(input_lines, sort_keys=True))
    dup = find_offer_by_hash(db, raw_hash)
    if dup:
        return jsonify({
            'ok': False,
            'error': f'Oferta duplicada (#{dup["offer_number"]})',
            'existing_offer_number': dup['offer_number'],
        }), 409

    validity_days = int(_num(data.get('validityDays', 30)) or 30)
    db.execute(
        '''INSERT INTO pending_offers
        (offer_number, client_name, project_name, waste_pct, margin_pct, fx_rate,
         lines_json, total_product_eur, total_logistic_eur, total_final_eur,
         status, incoterm, container_count, validity_days, raw_hash, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            offer_num,
            data.get('client', ''),
            data.get('project', ''),
            data.get('wastePct', 5),
            data.get('margin', 33),
            fx,
            json.dumps(input_lines),
            round(product_cost, 2),
            round(logistic_eur, 2),
            round(total_final, 2),
            'pending',
            data.get('incoterm', 'EXW'),
            int(container_count),
            validity_days,
            raw_hash,
            now_iso(),
        )
    )
    offer_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    save_order_lines(db, offer_id, computed)
    log_audit(db, offer_id, 'OFFER_CREATED',
              f'{offer_num} | {len(computed)} líneas | €{round(total_final, 2)}')
    db.commit()
    return jsonify({
        'ok': True,
        'offer_number': offer_num,
        'offer_id': offer_id,
        'product_cost_eur': round(product_cost, 2),
        'total_final_eur': round(total_final, 2),
        'total_weight_kg': totals['weight_total_kg'],
        'pallets_logistic': totals['pallets_logistic'],
        'container_recommendation': totals.get('containers'),
        'alerts': dedup_alerts(computed),
        'skipped_skus': skipped,
    })


@app.template_filter('from_json')
def from_json(value):
    try:
        return json.loads(value)
    except:
        return []


@app.context_processor
def inject_now() -> dict[str, Any]:
    return {
        'current_year': datetime.now(timezone.utc).year,
        'stages': STAGES,
        'current_user': current_user,
    }


# ── Logistics page ────────────────────────────────────────────────
@app.route('/logistics')
@login_required
def logistics():
    db = get_db()
    offers = db.execute(
        'SELECT * FROM pending_offers ORDER BY created_at DESC'
    ).fetchall()
    offers_data = [dict(o) for o in offers]
    routes = db.execute('SELECT * FROM shipping_routes').fetchall()
    routes_data = [dict(r) for r in routes]
    return render_template('logistics.html', offers=offers_data, routes=routes_data)


# ── API: Full offer update (from cotizador edit) ─────────────────

@app.route('/api/update-full-offer', methods=['POST'])
@login_required
def update_full_offer():
    db = get_db()
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400

    edit_id = data.get('editId')
    if not edit_id:
        return jsonify({'error': 'No editId'}), 400

    existing = db.execute('SELECT * FROM pending_offers WHERE id = ?', (edit_id,)).fetchone()
    if not existing:
        return jsonify({'error': 'Oferta no encontrada'}), 404
    if existing['status'] == 'confirmed':
        return jsonify({'error': 'No se puede editar una oferta confirmada'}), 403

    waste_pct = _num(data.get('wastePct', 5)) / 100
    margin_pct = _num(data.get('margin', 20)) / 100
    logistic = _num(data.get('logisticCost', 0))
    fx = _num(data.get('fx', 1.18))

    input_lines = data.get('lines', [])
    computed: list[dict[str, Any]] = []
    for li in input_lines:
        sku = li.get('sku')
        qty = _num(li.get('qty', 0))
        if not sku or qty <= 0:
            continue
        prod = db.execute('SELECT * FROM products WHERE sku = ?', (sku,)).fetchone()
        if not prod:
            continue
        qty_with_waste = math.ceil(qty * (1 + waste_pct))
        line = compute_line(dict(prod), qty_with_waste)
        line['qty_original'] = qty
        computed.append(line)

    totals = compute_totals(computed)
    # Fuente única de cálculo + persistir breakdown en lines_json (igual que save_offer).
    breakdown = build_offer_breakdown(input_lines, computed, margin_pct, logistic)
    product_cost = breakdown['totals']['product_cost_eur']
    logistic_eur = breakdown['totals']['logistic_eur']
    total_final = breakdown['totals']['sale_eur']
    for li, br in zip(input_lines, breakdown['lines']):
        li.update({
            'qty_waste': br['qty_waste'],
            'cost_line_eur': br['cost_line_eur'],
            'log_line_eur': br['log_line_eur'],
            'sale_line_eur': br['sale_line_eur'],
            'sale_unit_eur': br['sale_unit_eur'],
            'margin_applied_pct': br['margin_pct'],
        })
    container_count = (totals.get('containers') or {}).get('units', 0) or _num(data.get('containerCount', 0))

    validity_days = int(_num(data.get('validityDays', existing['validity_days'] or 30)) or 30)
    db.execute(
        '''UPDATE pending_offers SET
           offer_number = ?, client_name = ?, project_name = ?,
           waste_pct = ?, margin_pct = ?, fx_rate = ?,
           lines_json = ?, total_product_eur = ?, total_logistic_eur = ?,
           total_final_eur = ?, incoterm = ?, container_count = ?,
           validity_days = ?, raw_hash = ?, updated_at = ?
           WHERE id = ?''',
        (
            data.get('offerNumber', existing['offer_number']),
            data.get('client', existing['client_name']),
            data.get('project', existing['project_name']),
            data.get('wastePct', 5),
            data.get('margin', 20),
            fx,
            json.dumps(input_lines),
            round(product_cost, 2),
            round(logistic_eur, 2),
            round(total_final, 2),
            data.get('incoterm', 'EXW'),
            int(container_count),
            validity_days,
            compute_raw_hash(json.dumps(input_lines, sort_keys=True)),
            now_iso(),
            edit_id,
        )
    )
    db.execute('DELETE FROM order_lines WHERE offer_id = ?', (edit_id,))
    save_order_lines(db, edit_id, computed)
    log_audit(db, edit_id, 'OFFER_EDITED',
              f'{data.get("offerNumber")} | {len(computed)} líneas | €{round(total_final, 2)}')
    db.commit()
    return jsonify({'ok': True, 'offer_id': edit_id, 'total_final_eur': round(total_final, 2)})


# ── API: Update offer logistics ──────────────────────────────────
@app.route('/api/update-offer', methods=['POST'])
@login_required
def update_offer():
    db = get_db()
    data = request.get_json()
    offer_id = data.get('id')
    if not offer_id:
        return jsonify({'error': 'No ID'}), 400
    
    new_status = data.get('status', 'pending')
    db.execute(
        '''UPDATE pending_offers SET
           incoterm = ?, route_id = ?, container_count = ?,
           total_logistic_eur = ?, total_final_eur = ?,
           status = ?, updated_at = ?
           WHERE id = ?''',
        (
            data.get('incoterm', 'EXW'),
            data.get('route_id'),
            data.get('container_count', 0),
            data.get('logistic_cost', 0),
            data.get('final_total', 0),
            new_status,
            now_iso(),
            offer_id,
        )
    )
    log_audit(db, offer_id, 'OFFER_UPDATED',
              f'status={new_status} | incoterm={data.get("incoterm")} | €{data.get("final_total",0)}')
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/config-delete', methods=['POST'])
@admin_required
def config_delete():
    db = get_db()
    data = request.get_json() or {}
    item_type = data.get('type')
    item_id = data.get('id')
    if item_type == 'route':
        db.execute('DELETE FROM shipping_routes WHERE id = ?', (item_id,))
    elif item_type == 'customs':
        db.execute('DELETE FROM customs_rates WHERE id = ?', (item_id,))
    else:
        return jsonify({'ok': False, 'error': 'Tipo inválido'}), 400
    db.commit()
    return jsonify({'ok': True})


# ── API: Compute logistics (motor nuevo — fase C spec logística) ────
@app.route('/api/compute-logistics', methods=['POST'])
@login_required
def api_compute_logistics():
    """Calcula contenedores + imputación de coste por SKU usando el motor
    de logistics/engine.py. Reemplaza la estimación lump-sum del motor viejo.

    Payload esperado:
      {
        "lines": [{"sku": "...", "qty": 120, "waste_pct": 5}, ...],
        "container_type": "40HC",
        "cost_per_container_eur": 5500.0
      }

    Respuesta:
      {
        "ok": true,
        "n_containers": 5,
        "dominant_family": "PLACAS",
        "dominant_driver": "pallets",
        "total_cost_eur": 27500,
        "per_sku": [
          {"sku": "...", "unit_log_cost_eur": 3.54, "m2_log_cost_eur": 1.18,
           "pallets": 10, "weight_total_kg": 12500}
        ]
      }
    """
    from logistics.engine import ContainerProfile, PalletProfile, SkuInput, compute_logistics

    db = get_db()
    data = request.get_json() or {}
    lines_in = data.get('lines') or []
    container_type = data.get('container_type') or '40HC'
    cost_per_container = float(data.get('cost_per_container_eur') or 0)

    if not lines_in:
        return jsonify({'ok': False, 'error': 'sin líneas'}), 400

    # Container profile.
    cp_row = db.execute(
        'SELECT * FROM container_profiles WHERE type = ?', (container_type,)
    ).fetchone()
    if not cp_row:
        return jsonify({'ok': False, 'error': f'container_type {container_type!r} no existe'}), 400
    # floor_stowage_factor puede no existir en DBs creadas antes de la
    # migración 0003 — usamos 1.0 como fallback (comportamiento previo).
    floor_stowage = cp_row['floor_stowage_factor'] if 'floor_stowage_factor' in cp_row.keys() else 1.0
    container = ContainerProfile(
        type=cp_row['type'],
        inner_length_m=cp_row['inner_length_m'],
        inner_width_m=cp_row['inner_width_m'],
        inner_height_m=cp_row['inner_height_m'],
        payload_kg=cp_row['payload_kg'],
        door_clearance_m=cp_row['door_clearance_m'],
        stowage_factor=cp_row['stowage_factor'],
        floor_stowage_factor=float(floor_stowage),
    )

    # Pallet profiles por familia.
    pallet_profiles: dict[str, PalletProfile] = {}
    for r in db.execute('SELECT * FROM pallet_profiles').fetchall():
        pallet_profiles[r['category']] = PalletProfile(
            category=r['category'],
            length_m=r['pallet_length_m'],
            width_m=r['pallet_width_m'],
            height_m=r['pallet_height_m'],
            stackable_levels=r['stackable_levels'],
            allow_mix_floor=bool(r['allow_mix_floor']),
        )

    # Build SkuInputs: completa metadata faltante desde la tabla products.
    skus: list[SkuInput] = []
    for ln in lines_in:
        sku = str(ln.get('sku') or '').strip()
        if not sku:
            continue
        waste_pct = float(ln.get('waste_pct') or 0) / 100
        qty_raw = float(ln.get('qty') or 0)
        qty = math.ceil(qty_raw * (1 + waste_pct))
        if qty <= 0:
            continue
        # Pull product metadata.
        p = db.execute(
            'SELECT category, unit_price_eur, kg_per_unit, units_per_pallet, sqm_per_pallet, '
            'pallet_length_m, pallet_width_m, pallet_height_m, pallet_weight_kg, '
            'stackable_levels, allow_mix_floor '
            'FROM products WHERE sku = ?', (sku,)
        ).fetchone()
        if not p:
            continue
        category = p['category']
        if category not in pallet_profiles:
            continue  # familia sin perfil — skip silencioso
        upp = p['units_per_pallet'] or 0
        sqm_pp = p['sqm_per_pallet']
        unit_area_m2 = (float(sqm_pp) / float(upp)) if (upp and sqm_pp) else 0
        skus.append(SkuInput(
            sku=sku,
            category=category,
            qty=qty,
            unit_weight_kg=float(p['kg_per_unit'] or 0),
            unit_area_m2=unit_area_m2,
            units_per_pallet=float(upp) if upp else 1,
            pallet_length_m=p['pallet_length_m'],
            pallet_width_m=p['pallet_width_m'],
            pallet_height_m=p['pallet_height_m'],
            pallet_weight_kg=p['pallet_weight_kg'],
            stackable_levels=p['stackable_levels'],
        ))

    if not skus:
        return jsonify({'ok': False, 'error': 'ningún SKU válido'}), 400

    try:
        result = compute_logistics(skus, container, pallet_profiles, cost_per_container)
    except KeyError as e:
        return jsonify({'ok': False, 'error': f'familia sin perfil: {e}'}), 400

    return jsonify({
        'ok': True,
        'n_containers': result.n_containers,                  # entero (físicos a reservar)
        'n_containers_decimal': result.n_containers_decimal,  # decimal (para coste imputado)
        'dominant_family': result.dominant_family,
        'dominant_driver': result.dominant_driver,
        'total_cost_eur': result.total_cost_eur,
        'extra_containers_by_family': result.extra_containers_by_family,
        'aggregate': {
            'total_floor_m2': result.total_floor_m2,
            'total_weight_kg': result.total_weight_kg,
            'total_cbm': result.total_cbm,
            'n_by_floor': result.n_by_floor,
            'n_by_weight': result.n_by_weight,
            'n_by_cbm': result.n_by_cbm,
        },
        'families': {
            cat: {
                'total_pallets': fr.total_pallets,
                'total_weight_kg': fr.total_weight_kg,
                'total_cbm': fr.total_cbm,
                'cap_geo_per_container': fr.cap_geo_per_container,
                'n_alone': fr.n_alone,
                'dominant_driver': fr.dominant_driver,
            } for cat, fr in result.families.items()
        },
        'per_sku': [
            {
                'sku': s.sku,
                'category': s.category,
                'pallets': s.pallets,
                'weight_total_kg': s.weight_total_kg,
                'unit_log_cost_eur': s.unit_log_cost_eur,
                'm2_log_cost_eur': s.m2_log_cost_eur,
            } for s in result.skus
        ],
        # Capacidad libre en los contenedores dominantes. UI-only.
        'free_capacity': {
            'per_container': {
                'weight_kg': result.free_weight_kg_per_cont,
                'cbm': result.free_cbm_per_cont,
                'floor_m2': result.free_floor_m2_per_cont,
            },
            'total': {
                'weight_kg': result.free_weight_kg_total,
                'cbm': result.free_cbm_total,
                'floor_m2': result.free_floor_m2_total,
            },
            'is_optimized': result.is_optimized,
        },
    })


# ── API: Delete offer ────────────────────────────────────────────
@app.route('/api/delete-offer', methods=['POST'])
@login_required
def delete_offer():
    db = get_db()
    offer_id = request.json.get('id')
    log_audit(db, offer_id, 'OFFER_DELETED')
    db.execute('DELETE FROM order_lines WHERE offer_id = ?', (offer_id,))
    db.execute('DELETE FROM pending_offers WHERE id = ?', (offer_id,))
    db.commit()
    return jsonify({'ok': True})


def _ensure_factory_order(db: sqlite3.Connection, offer: sqlite3.Row) -> dict | None:
    """Crea una factory_order para la oferta si no existe ya. Idempotente."""
    existing = db.execute(
        'SELECT id, name FROM factory_orders WHERE offer_id = ?', (offer['id'],)
    ).fetchone()
    if existing:
        return {'id': existing['id'], 'name': existing['name'], 'created': False}
    name = next_sequence(db, 'PO')
    db.execute(
        '''INSERT INTO factory_orders (offer_id, name, state, partner_ref, created_at)
           VALUES (?, ?, 'draft', 'FASSA', ?)''',
        (offer['id'], name, now_iso())
    )
    fo_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    log_audit(db, offer['id'], 'FACTORY_ORDER_CREATED',
              f'{name} ← {offer["offer_number"]}')
    return {'id': fo_id, 'name': name, 'created': True}


def _ensure_logistics_order(db: sqlite3.Connection, offer: sqlite3.Row) -> dict | None:
    """Crea una logistics_order para la oferta si no existe ya. Idempotente."""
    existing = db.execute(
        'SELECT id, name FROM logistics_orders WHERE offer_id = ?', (offer['id'],)
    ).fetchone()
    if existing:
        return {'id': existing['id'], 'name': existing['name'], 'created': False}
    name = next_sequence(db, 'OUT')
    db.execute(
        '''INSERT INTO logistics_orders (offer_id, name, state, route_id, created_at)
           VALUES (?, ?, 'draft', ?, ?)''',
        (offer['id'], name, offer['route_id'], now_iso())
    )
    lo_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    log_audit(db, offer['id'], 'LOGISTICS_ORDER_CREATED',
              f'{name} ← {offer["offer_number"]}')
    return {'id': lo_id, 'name': name, 'created': True}


@app.route('/api/offer-status', methods=['POST'])
@login_required
def offer_status():
    db = get_db()
    data = request.get_json() or {}
    offer_id = data.get('id')
    new_status = data.get('status')
    if new_status not in ('pending', 'approved', 'rejected'):
        return jsonify({'ok': False, 'error': 'Estado inválido'}), 400
    offer = db.execute('SELECT * FROM pending_offers WHERE id = ?', (offer_id,)).fetchone()
    if not offer:
        return jsonify({'ok': False, 'error': 'Oferta no encontrada'}), 404
    db.execute('UPDATE pending_offers SET status = ?, updated_at = ? WHERE id = ?',
               (new_status, now_iso(), offer_id))
    log_audit(db, offer_id, f'STATUS_{new_status.upper()}',
              f'{offer["offer_number"]} → {new_status}')

    # Auto-trigger: al aprobar, generar preorden Fassa + orden logística (idempotente).
    factory_order = None
    logistics_order = None
    if new_status == 'approved':
        factory_order = _ensure_factory_order(db, offer)
        logistics_order = _ensure_logistics_order(db, offer)

    db.commit()
    return jsonify({
        'ok': True,
        'status': new_status,
        'factory_order': factory_order,
        'logistics_order': logistics_order,
    })


# ── Export JSON: contrato común para migración futura a Odoo/NetSuite/SAP ──
# Schema documentado en HANDOFF.md §4. Naming alineado con convenciones Odoo
# (res.partner, sale.order, purchase.order, stock.picking) para que un import
# al ERP comercial sea trivial el día que se decida migrar.

# Mapeo legacy.status → Odoo sale.order.state
_OFFER_STATUS_TO_ODOO = {
    'pending':  'sent',     # cotización enviada al cliente
    'approved': 'sale',     # cliente confirmó / venta cerrada
    'rejected': 'cancel',   # cliente rechazó
}

EXPORT_SCHEMA_VERSION = '1.0'


@app.route('/api/export/cotizacion/<int:offer_id>')
@login_required
def export_cotizacion(offer_id: int):
    """Devuelve la cotización en el contrato JSON canonical (HANDOFF.md §4)."""
    db = get_db()
    offer = db.execute('SELECT * FROM pending_offers WHERE id = ?', (offer_id,)).fetchone()
    if not offer:
        return jsonify({'ok': False, 'error': 'Oferta no encontrada'}), 404

    client = db.execute(
        'SELECT * FROM clients WHERE name = ? OR company = ? LIMIT 1',
        (offer['client_name'], offer['client_name'])
    ).fetchone()

    lines = db.execute(
        'SELECT * FROM order_lines WHERE offer_id = ? ORDER BY id', (offer_id,)
    ).fetchall()

    route = None
    if offer['route_id']:
        route = db.execute(
            'SELECT * FROM shipping_routes WHERE id = ?', (offer['route_id'],)
        ).fetchone()

    factory = db.execute(
        'SELECT * FROM factory_orders WHERE offer_id = ?', (offer_id,)
    ).fetchone()
    logistics = db.execute(
        'SELECT * FROM logistics_orders WHERE offer_id = ?', (offer_id,)
    ).fetchone()

    audit_rows = db.execute(
        'SELECT action, detail, username, created_at FROM audit_log WHERE offer_id = ? ORDER BY id',
        (offer_id,)
    ).fetchall()

    fx = offer['fx_rate'] or 1.18
    total_eur = offer['total_final_eur'] or 0

    payload = {
        '$schema_version': EXPORT_SCHEMA_VERSION,
        'exported_at': now_iso(),
        'source_system': 'arias-app-v1',
        'source_offer_id': offer['id'],

        'partner': {
            'name': (client['company'] if client and client['company'] else offer['client_name']),
            'is_company': bool(client and client['company']) if client else True,
            'vat': (client['rnc'] if client else None),         # Odoo: res.partner.vat
            'country_code': (client['country'] if client else None),
            'phone': (client['phone'] if client else None),
            'email': (client['email'] if client else None),
            'street': (client['address'] if client else None),
        },

        'sale_order': {
            'name': offer['offer_number'],                       # Odoo: sale.order.name
            'state': _OFFER_STATUS_TO_ODOO.get(offer['status'], 'draft'),
            'date_order': (offer['created_at'] or '')[:10],
            'currency_id': 'EUR',
            'pricelist_currency': 'USD',
            'fx_rate': fx,
            'incoterm': offer['incoterm'] or 'EXW',

            'order_line': [
                {
                    'default_code': l['sku'],                    # Odoo: product.product.default_code
                    'name': l['name'],
                    'product_uom_qty': l['qty_input'],           # Odoo: sale.order.line.product_uom_qty
                    'product_uom': l['unit'],
                    'price_unit': l['price_unit_eur'],           # Odoo: sale.order.line.price_unit
                    'x_family': l['family'],                     # extensión custom (Odoo Studio: x_*)
                    'x_weight_kg': l['weight_total_kg'],
                    'x_m2_total': l['m2_total'],
                    'x_pallets_logistic': l['pallets_logistic'],
                    'x_alerts': l['alerts_text'],
                }
                for l in lines
            ],

            'x_logistics': {
                'container_count': offer['container_count'] or 0,
                'route_origin': route['origin_port'] if route else None,
                'route_destination': route['destination_port'] if route else None,
                'carrier_name': route['carrier'] if route else None,
                'transit_days': route['transit_days'] if route else None,
            },

            'x_economics': {
                'amount_product_eur': offer['total_product_eur'],
                'amount_logistic_eur': offer['total_logistic_eur'],
                'amount_total_eur': total_eur,
                'amount_total_usd': round(total_eur * fx, 2),
                'margin_pct': offer['margin_pct'],
                'waste_pct': offer['waste_pct'],
            },
        },

        'purchase_order': (
            {
                'name': factory['name'],                          # Odoo: purchase.order.name
                'state': factory['state'],
                'partner_ref': factory['partner_ref'],
                'date_planned': factory['date_planned'],
                'sent_to_factory_at': factory['sent_to_factory_at'],
                'confirmed_at': factory['confirmed_at'],
            }
            if factory else None
        ),

        'stock_picking': (
            {
                'name': logistics['name'],                        # Odoo: stock.picking.name
                'state': logistics['state'],
                'container_type': logistics['container_type'],
                'booking_ref': logistics['booking_ref'],
                'departure_date': logistics['departure_date'],
                'eta_date': logistics['eta_date'],
                'delivered_at': logistics['delivered_at'],
            }
            if logistics else None
        ),

        'audit': [
            {
                'action': a['action'],
                'detail': a['detail'],
                'user': a['username'],
                'at': a['created_at'],
            }
            for a in audit_rows
        ],
    }

    return jsonify(payload)


# ── PDF for confirmed offer — professional 2-page document ──────────
@app.route('/api/offer-pdf/<int:offer_id>')
@login_required
def offer_pdf(offer_id):
    db = get_db()
    offer = db.execute('SELECT * FROM pending_offers WHERE id = ?', (offer_id,)).fetchone()
    if not offer:
        return 'Oferta no encontrada', 404
    
    lines = json.loads(offer['lines_json'])
    total_eur = offer['total_final_eur']
    fx = offer['fx_rate'] or 1.18
    total_usd = total_eur * fx
    ref_date = offer['created_at'][:10]

    client_row = db.execute(
        'SELECT name, company, address, country, rnc FROM clients WHERE name = ? OR company = ? LIMIT 1',
        (offer['client_name'], offer['client_name'])
    ).fetchone()
    project_row = db.execute(
        'SELECT location FROM projects WHERE name = ? LIMIT 1',
        (offer['project_name'],)
    ).fetchone()
    client_company = (client_row['company'] if client_row and client_row['company'] else offer['client_name'])
    client_address = (client_row['address'] if client_row and client_row['address'] else '')
    client_country = (client_row['country'] if client_row and client_row['country'] else 'República Dominicana')
    client_rnc = (client_row['rnc'] if client_row and client_row['rnc'] else '—')
    location_text = (project_row['location'] if project_row and project_row['location'] else (client_address or client_country))
    
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=10*mm, bottomMargin=10*mm,
    )
    
    # Paleta oficial Arias Group (de la factura proforma real)
    NAVY = colors.HexColor('#1A3557')          # primario: headers, títulos, marca
    BLUE = colors.HexColor('#2563A8')          # acento: enlaces, subheaders, líneas
    BLUE_PALE = colors.HexColor('#EEF3F9')     # fondo tabla
    GRAY_FAINT = colors.HexColor('#F5F7FA')    # fondo alterno
    SKY_PALE = colors.HexColor('#C5D5E8')      # divisor sutil
    INK = colors.HexColor('#1C2B3A')           # texto cuerpo
    STONE = colors.HexColor('#5C7A99')         # labels/meta
    CREAM = colors.HexColor('#FFFDE7')         # fondo nota
    GOLD_DARK = colors.HexColor('#B8860B')     # acento SOLO para notas puntuales
    WHITE = colors.white
    # Aliases para código existente (no romper nada)
    GOLD = BLUE                                # GOLD ya no se usa como acento general → ahora es BLUE
    GOLD_SOFT = BLUE_PALE
    BONE = BLUE_PALE
    LGRAY = BLUE_PALE
    MGRAY = STONE
    NAVY_DEEP = NAVY
    SECTION_BG = NAVY
    
    styles = getSampleStyleSheet()
    def S(name, parent='Normal', **kw):
        return ParagraphStyle(name, parent=styles[parent], **kw)
    
    sty = {
        'brand': S('brand', fontSize=8, textColor=GOLD, fontName='Helvetica-Bold', leading=10),
        'subtitle': S('sub', fontSize=9, textColor=NAVY, fontName='Helvetica', leading=12),
        'systems': S('sys', fontSize=8, textColor=MGRAY, fontName='Helvetica', leading=10),
        'h1': S('h1', fontSize=11, textColor=NAVY, fontName='Helvetica-Bold', leading=13, spaceAfter=2),
        'p': S('p', fontSize=7.5, textColor=INK, fontName='Helvetica', leading=11),
        'small': S('small', fontSize=7, textColor=MGRAY, fontName='Helvetica', leading=9),
        'bold': S('bold', fontSize=7.5, textColor=NAVY, fontName='Helvetica-Bold', leading=11),
        'right': S('right', fontSize=7.5, textColor=NAVY, fontName='Helvetica', alignment=TA_RIGHT, leading=11),
        'center': S('center', fontSize=7.5, textColor=NAVY, fontName='Helvetica', alignment=TA_CENTER, leading=11),
        'th_c': S('thc', fontSize=7.5, textColor=WHITE, fontName='Helvetica-Bold', alignment=TA_CENTER, leading=11),
        'th_r': S('thr', fontSize=7.5, textColor=WHITE, fontName='Helvetica-Bold', alignment=TA_RIGHT, leading=11),
        'cond': S('cond', fontSize=7, textColor=INK, fontName='Helvetica', leading=10),
        'total_l': S('tl', fontSize=9, textColor=WHITE, fontName='Helvetica-Bold', alignment=TA_RIGHT),
        'total_v': S('tv', fontSize=9, textColor=WHITE, fontName='Helvetica-Bold', alignment=TA_RIGHT),
        'footer': S('foot', fontSize=6, textColor=MGRAY, fontName='Helvetica', alignment=TA_CENTER),
        'ref_label': S('rl', fontSize=7, textColor=MGRAY, fontName='Helvetica', leading=9),
        'ref_value': S('rv', fontSize=7.5, textColor=NAVY, fontName='Helvetica-Bold', leading=10),
        'sig': S('sig', fontSize=7, textColor=NAVY, fontName='Helvetica', leading=10),
    }
    
    story = []
    W = A4[0] - 30*mm  # usable width
    
    # ════════════════════════════════════════════════════
    # PAGE 1: OFERTA COMERCIAL
    # ════════════════════════════════════════════════════
    
    # Header: logo sobre fondo blanco + barra navy con texto
    logo_path = str(BASE_DIR / 'static' / 'logos' / 'arias_group_logo_1000px.png')
    if os.path.exists(logo_path):
        logo_row = Table([[RLImage(logo_path, width=45*mm, height=16*mm, kind='proportional'), '']],
                         colWidths=[W*0.4, W*0.6])
        logo_row.setStyle(TableStyle([
            ('LEFTPADDING', (0,0), (0,0), 0),
            ('TOPPADDING', (0,0), (-1,-1), 0),
            ('BOTTOMPADDING', (0,0), (-1,-1), 1),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ]))
        story.append(logo_row)
    hdr = [[
        Paragraph('ARIAS GROUP CARIBE', S('brand_h', fontSize=10, textColor=WHITE, fontName='Helvetica-Bold', leading=12)),
        Paragraph('PROPUESTA ECONÓMICA', S('pt', fontSize=11, textColor=WHITE, fontName='Helvetica-Bold', alignment=TA_CENTER)),
    ]]
    hdr_tbl = Table(hdr, colWidths=[W*0.3, W*0.7])
    hdr_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), NAVY),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('LEFTPADDING', (0,0), (0,0), 14),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(hdr_tbl)
    story.append(Spacer(1, 1*mm))
    story.append(Paragraph('Sistemas Constructivos Fassa Bortolo', sty['systems']))
    story.append(Spacer(1, 3*mm))
    
    # Reference block
    ref_data = [
        [Paragraph('Fecha de emisión', sty['ref_label']), Paragraph(ref_date, sty['ref_value']),
         Paragraph('N° de referencia', sty['ref_label']), Paragraph(offer['offer_number'], sty['ref_value'])],
        [Paragraph('Cliente / Contacto', sty['ref_label']), Paragraph(offer['client_name'], sty['ref_value']),
         Paragraph('Proyecto', sty['ref_label']), Paragraph(offer['project_name'], sty['ref_value'])],
        [Paragraph('Empresa', sty['ref_label']), Paragraph(client_company, sty['ref_value']),
         Paragraph('Ubicación', sty['ref_label']), Paragraph(location_text, sty['ref_value'])],
        [Paragraph('RNC', sty['ref_label']), Paragraph(client_rnc, sty['ref_value']),
         Paragraph('Dirección', sty['ref_label']), Paragraph(client_address or '—', sty['ref_value'])],
        [Paragraph('Sistema ofertado', sty['ref_label']), Paragraph('Suministro completo', sty['ref_value']),
         Paragraph('Incoterm', sty['ref_label']), Paragraph(offer['incoterm'] or 'EXW', sty['ref_value'])],
    ]
    ref_tbl = Table(ref_data, colWidths=[W*0.18, W*0.32, W*0.18, W*0.32])
    ref_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), LGRAY),
        ('TOPPADDING', (0,0), (-1,-1), 2),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ('LEFTPADDING', (0,0), (-1,-1), 5),
        ('RIGHTPADDING', (0,0), (-1,-1), 5),
        ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor('#CCCCCC')),
    ]))
    story.append(ref_tbl)
    story.append(Spacer(1, 4*mm))

    # Section 1
    story.append(Paragraph('1.  OBJETO Y ALCANCE', sty['h1']))
    story.append(Spacer(1, 1*mm))
    story.append(Paragraph('La presente propuesta cubre el suministro de materiales del sistema <b>Suministro completo</b>, fabricados por Fassa Bortolo (Italia/España), para el proyecto <b>{}</b>. Arias Group Caribe SRL actúa como distribuidor técnico exclusivo para República Dominicana y el Caribe.'.format(offer['project_name']), sty['p']))
    story.append(Paragraph('Alcance específico: Suministro de materiales — ver detalle hoja 2', sty['p']))
    story.append(Spacer(1, 4*mm))

    # Section 2 — RESUMEN (precalcula totales logísticos para dar contexto)
    story.append(Paragraph('2.  RESUMEN ECONÓMICO', sty['h1']))
    n_lineas = len(lines)
    agg_for_summary = _aggregate_lines_by_sku(lines)
    sum_units = 0
    sum_pallets = 0
    sum_kg = 0.0
    for _l in agg_for_summary:
        q_w = math.ceil(_l['qty'] * (1 + offer['waste_pct']/100))
        sum_units += q_w
        _p = db.execute('SELECT units_per_pallet, kg_per_unit FROM products WHERE sku = ?', (_l['sku'],)).fetchone()
        if _p and _p['units_per_pallet'] and _p['units_per_pallet'] > 0:
            sum_pallets += math.ceil(q_w / _p['units_per_pallet'])
        if _p and _p['kg_per_unit']:
            sum_kg += q_w * _p['kg_per_unit']
    econ_data = [
        [Paragraph('Concepto', sty['th_c']), Paragraph('Detalle', sty['th_c'])],
        [Paragraph('Nº de referencias', sty['p']), Paragraph(f'{n_lineas}', sty['right'])],
        [Paragraph('Unidades totales', sty['p']), Paragraph(f'{sum_units:,}', sty['right'])],
        [Paragraph('Palés totales', sty['p']), Paragraph(f'{sum_pallets:,}' if sum_pallets else '—', sty['right'])],
        [Paragraph('Peso bruto aproximado', sty['p']), Paragraph(f'{sum_kg:,.0f} kg' if sum_kg else '—', sty['right'])],
        [Paragraph('Moneda', sty['p']), Paragraph('USD (dólares estadounidenses)', sty['right'])],
        [Paragraph('Tipo de cambio aplicado', sty['p']), Paragraph(f'{fx:.3f} EUR/USD', sty['right'])],
        [Paragraph(f'<b>TOTAL {offer["incoterm"] or "EXW"} (USD)</b>', sty['bold']), Paragraph(f'<b>$ {total_usd:,.2f}</b>', sty['right'])],
    ]
    econ_tbl = Table(econ_data, colWidths=[W*0.65, W*0.35])
    econ_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), NAVY),
        ('TEXTCOLOR', (0,0), (-1,0), WHITE),
        ('BACKGROUND', (0,-1), (-1,-1), GOLD_SOFT),
        ('LINEABOVE', (0,-1), (-1,-1), 1, GOLD),
        ('LINEBELOW', (0,0), (-1,-2), 0.3, colors.HexColor('#CCCCCC')),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
    ]))
    story.append(econ_tbl)
    story.append(Spacer(1, 1*mm))
    story.append(Paragraph('* Importes sujetos a confirmación de pedido formal. No incluyen impuestos locales ni gastos de aduana en destino. Detalle línea-a-línea en Hoja 2.', sty['small']))
    story.append(Spacer(1, 4*mm))

    # Section 3
    story.append(Paragraph('3.  CONDICIONES COMERCIALES', sty['h1']))
    story.append(Spacer(1, 1*mm))
    validity = int(offer['validity_days']) if offer['validity_days'] else 30
    conds = [
        ('Pago', '100% prepago por transferencia bancaria antes de emisión de orden de producción.'),
        ('Validez de oferta', f"{validity} días calendario desde la fecha de emisión."),
        ('Plazo de entrega', 'Según confirmación de fábrica tras recepción de pago.'),
        ('Puerto de embarque', 'Valencia, España'),
        ('Incoterm aplicable', f"{offer['incoterm'] or 'EXW'} — riesgo y responsabilidad se transfieren al comprador."),
        ('Divisa', 'USD (dólares estadounidenses)'),
    ]
    cond_data = [[Paragraph('Concepto', sty['th_c']), Paragraph('Detalle', sty['th_c'])]]
    for label, detail in conds:
        cond_data.append([Paragraph(f"<b>{label}</b>", sty['bold']), Paragraph(detail, sty['p'])])
    cond_tbl = Table(cond_data, colWidths=[W*0.25, W*0.75])
    cond_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), NAVY),
        ('TEXTCOLOR', (0,0), (-1,0), WHITE),
        ('BACKGROUND', (0,1), (0,-1), LGRAY),
        ('LINEBELOW', (0,0), (-1,-1), 0.3, colors.HexColor('#CCCCCC')),
        ('TOPPADDING', (0,0), (-1,-1), 2),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ('LEFTPADDING', (0,0), (-1,-1), 5),
        ('RIGHTPADDING', (0,0), (-1,-1), 5),
    ]))
    story.append(cond_tbl)
    story.append(Spacer(1, 4*mm))

    # Section 4
    story.append(Paragraph('4.  OBSERVACIONES TÉCNICAS', sty['h1']))
    story.append(Spacer(1, 1*mm))
    obs = [
        'Los materiales ofertados cuentan con marcado CE y están fabricados bajo estándares europeos (EN, ETA según aplique).',
        'Producto de origen europeo (Italia/España) con trazabilidad de lote.',
        'Soporte técnico disponible durante todo el proceso: especificación, suministro y aplicación.',
        'Disponibilidad de formación para aplicadores bajo coordinación con Fassa Bortolo España.',
    ]
    for o in obs:
        story.append(Paragraph(f"•  {o}", sty['cond']))
    story.append(Spacer(1, 4*mm))

    # Section 5
    story.append(Paragraph('5.  ACEPTACIÓN Y FIRMA', sty['h1']))
    story.append(Spacer(1, 1*mm))
    story.append(Paragraph('Para confirmar esta propuesta, el cliente deberá firmar y devolver este documento. La firma implica aceptación de las condiciones comerciales indicadas.', sty['p']))
    story.append(Spacer(1, 3*mm))

    sig_data = [[
        Paragraph('<b>ARIAS GROUP CARIBE SRL</b><br/>RNC: 1-33-63109-1<br/>Av. Independencia Km 6, Plaza Comercial Átala I, Suite 203<br/>Santo Domingo, D.N.<br/><br/>___________________________<br/>Director Comercial<br/>Fecha: _______________', sty['sig']),
        Paragraph(f'<b>{client_company}</b><br/>RNC: {client_rnc}<br/>{offer["client_name"]}<br/><br/>___________________________<br/>Firma y sello<br/>Fecha: _______________', sty['sig']),
    ]]
    sig_tbl = Table(sig_data, colWidths=[W*0.5, W*0.5])
    sig_tbl.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('TOPPADDING', (0,0), (-1,-1), 2),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
    ]))
    story.append(sig_tbl)
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph('Esta propuesta es de carácter confidencial y está dirigida exclusivamente al destinatario indicado.', sty['footer']))
    story.append(PageBreak())
    
    # ════════════════════════════════════════════════════
    # PAGE 2: DETALLE DE PRODUCTOS
    # ════════════════════════════════════════════════════
    
    # Header página 2: logo blanco + barra navy
    if os.path.exists(logo_path):
        logo_row2 = Table([[RLImage(logo_path, width=55*mm, height=20*mm, kind='proportional'), '']],
                          colWidths=[W*0.4, W*0.6])
        logo_row2.setStyle(TableStyle([
            ('LEFTPADDING', (0,0), (0,0), 0),
            ('TOPPADDING', (0,0), (-1,-1), 0),
            ('BOTTOMPADDING', (0,0), (-1,-1), 2),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ]))
        story.append(logo_row2)
    hdr2 = [[
        Paragraph('ARIAS GROUP CARIBE', S('brand_h2', fontSize=10, textColor=WHITE, fontName='Helvetica-Bold', leading=12)),
        Paragraph('DESGLOSE PRESUPUESTO', S('dp', fontSize=11, textColor=WHITE, fontName='Helvetica-Bold', alignment=TA_CENTER)),
    ]]
    hdr2_tbl = Table(hdr2, colWidths=[W*0.3, W*0.7])
    hdr2_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), NAVY),
        ('TOPPADDING', (0,0), (-1,-1), 10),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
        ('LEFTPADDING', (0,0), (0,0), 14),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(hdr2_tbl)
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(f'{offer["project_name"]}  ·  Ref. {offer["offer_number"]}', sty['systems']))
    story.append(Spacer(1, 6*mm))
    
    # Products table — desglose cara-cliente (sin palés/m²; ir a resumen log. si CIF)
    prod_data = [[
        Paragraph('Descripción producto', sty['th_c']),
        Paragraph('Ref. Fassa', sty['th_c']),
        Paragraph('Ud.', sty['th_c']),
        Paragraph('Cantidad', sty['th_c']),
        Paragraph('Precio/ud ($)', sty['th_r']),
        Paragraph('Total ($)', sty['th_r']),
    ]]
    total_pal = 0
    total_m2 = 0
    total_kg = 0
    # Agregar líneas con mismo SKU para la tabla de detalle (evita duplicados)
    detail_lines = _aggregate_lines_by_sku(lines)
    # Para ofertas nuevas (post-2026-04-24): lines_json ya trae qty_waste,
    # sale_line_eur y sale_unit_eur calculados en el momento de guardar
    # (build_offer_breakdown). Se leen tal cual, sin recalcular.
    # Para ofertas viejas (sin esos campos): fallback al prorrateo por
    # proporción de coste, que era el comportamiento previo.
    product_cost_eur = offer['total_product_eur'] or 0
    for line in detail_lines:
        if 'sale_line_eur' in line and 'qty_waste' in line:
            qty_waste = int(line['qty_waste'])
            sale_line_eur = _num(line['sale_line_eur'])
            sale_unit_eur = _num(line.get('sale_unit_eur', sale_line_eur / qty_waste if qty_waste else 0))
            price_unit_sale_usd = sale_unit_eur * fx
        else:
            qty_waste = math.ceil(line['qty'] * (1 + offer['waste_pct']/100))
            cost_line_eur = line['price'] * qty_waste
            sale_line_eur = (cost_line_eur / product_cost_eur * total_eur) if product_cost_eur > 0 else 0
            price_unit_sale_usd = (sale_line_eur / qty_waste * fx) if qty_waste else 0

        # Lookup product for pallet/m2/kg data
        prod = db.execute('SELECT units_per_pallet, sqm_per_pallet, kg_per_unit FROM products WHERE sku = ?',
                         (line['sku'],)).fetchone()
        pal = 0
        m2 = 0
        if prod and prod['units_per_pallet'] and prod['units_per_pallet'] > 0:
            pal = math.ceil(qty_waste / prod['units_per_pallet'])
            total_pal += pal
        if prod and prod['sqm_per_pallet'] and prod['units_per_pallet'] and prod['units_per_pallet'] > 0:
            m2_per_unit = prod['sqm_per_pallet'] / prod['units_per_pallet']
            m2 = qty_waste * m2_per_unit
            total_m2 += m2
        if prod and prod['kg_per_unit']:
            total_kg += qty_waste * prod['kg_per_unit']

        prod_data.append([
            Paragraph(line['name'], sty['p']),
            Paragraph(line['sku'], sty['center']),
            Paragraph(line.get('unit', 'ud'), sty['center']),
            Paragraph(f'{qty_waste:,}', sty['center']),
            Paragraph(f"$ {price_unit_sale_usd:,.2f}", sty['right']),
            Paragraph(f"$ {sale_line_eur * fx:,.2f}", sty['right']),
        ])

    prod_data.append(['', '', '', '',
        Paragraph('<b>TOTAL</b>', sty['bold']),
        Paragraph(f"<b>$ {total_usd:,.2f}</b>", sty['right']),
    ])

    prod_tbl = Table(prod_data, colWidths=[W*0.38, W*0.16, W*0.08, W*0.12, W*0.12, W*0.14])
    prod_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), NAVY),
        ('TEXTCOLOR', (0,0), (-1,0), WHITE),
        ('BACKGROUND', (0,-1), (-1,-1), GOLD_SOFT),
        ('LINEBELOW', (0,0), (-1,-2), 0.3, colors.HexColor('#CCCCCC')),
        ('LINEABOVE', (0,-1), (-1,-1), 1, GOLD),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('LEFTPADDING', (0,0), (-1,-1), 4),
        ('RIGHTPADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(prod_tbl)
    waste_pct_int = int(round(offer['waste_pct'] or 0))
    if waste_pct_int > 0:
        story.append(Spacer(1, 1*mm))
        story.append(Paragraph(
            f'* Cantidades incluyen {waste_pct_int}% de merma sobre la necesidad neta del proyecto.',
            sty['small'],
        ))
    story.append(Spacer(1, 6*mm))

    # Logistics summary — solo cuando el flete lo gestiona Arias (no-EXW).
    # En EXW el cliente se encarga del transporte; incluir puerto destino,
    # contenedores, etc. confunde porque no aplica al alcance del pedido.
    incoterm_upper = (offer['incoterm'] or 'EXW').upper()
    if incoterm_upper != 'EXW':
        story.append(Paragraph('RESUMEN LOGÍSTICO', sty['h1']))
        story.append(Spacer(1, 2*mm))
        log_data = [
            [Paragraph('<b>Puerto salida</b>', sty['bold']), Paragraph('Valencia, España', sty['p']),
             Paragraph('<b>Puerto destino</b>', sty['bold']), Paragraph('Puerto Caucedo, R. Dominicana', sty['p'])],
            [Paragraph('<b>Tipo contenedor</b>', sty['bold']), Paragraph("40' Standard", sty['p']),
             Paragraph('<b>Nº contenedores</b>', sty['bold']), Paragraph(str(offer['container_count'] or 0), sty['p'])],
            [Paragraph('<b>Palés totales</b>', sty['bold']), Paragraph(f'{total_pal:,}' if total_pal else '—', sty['p']),
             Paragraph('<b>Peso bruto total</b>', sty['bold']), Paragraph(f'{total_kg:,.0f} kg' if total_kg else '—', sty['p'])],
            [Paragraph('<b>M² totales</b>', sty['bold']), Paragraph(f'{total_m2:,.0f} m²' if total_m2 else '—', sty['p']),
             Paragraph('<b>Incoterm</b>', sty['bold']), Paragraph(incoterm_upper, sty['p'])],
            [Paragraph('<b>Plazo entrega est.</b>', sty['bold']), Paragraph('Según confirmación', sty['p']),
             Paragraph('', sty['p']), Paragraph('', sty['p'])],
        ]
        log_tbl = Table(log_data, colWidths=[W*0.18, W*0.32, W*0.18, W*0.32])
        log_tbl.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), LGRAY),
            ('LINEBELOW', (0,0), (-1,-1), 0.3, colors.HexColor('#CCCCCC')),
            ('TOPPADDING', (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ('LEFTPADDING', (0,0), (-1,-1), 6),
            ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ]))
        story.append(log_tbl)
        story.append(Spacer(1, 6*mm))
    story.append(Paragraph('Arias Group Caribe SRL  ·  RNC 1-33-63109-1  ·  Av. Independencia Km 6, Plaza Comercial Átala I, Suite 203, Santo Domingo, D.N.  ·  Distribución técnica Fassa Bortolo', sty['footer']))
    
    doc.build(story)
    buffer.seek(0)
    
    resp = make_response(buffer.read())
    resp.headers['Content-Type'] = 'application/pdf'
    disp = 'attachment' if request.args.get('download') else 'inline'
    resp.headers['Content-Disposition'] = f'{disp}; filename="Presupuesto_{offer["offer_number"]}.pdf"'
    return resp


# ── PDF helpers (shared styles) ──────────────────────────────────

# Paleta oficial Arias Group (compartida por todos los PDFs)
AG_NAVY = colors.HexColor('#1A3557')       # primario
AG_BLUE = colors.HexColor('#2563A8')       # acento
AG_BLUE_PALE = colors.HexColor('#EEF3F9')  # fondo tabla
AG_GRAY_FAINT = colors.HexColor('#F5F7FA') # fondo alterno
AG_SKY_PALE = colors.HexColor('#C5D5E8')   # divisor
AG_INK = colors.HexColor('#1C2B3A')        # texto
AG_STONE = colors.HexColor('#5C7A99')      # labels
AG_GOLD_DARK = colors.HexColor('#B8860B')  # solo notas


def _pdf_styles():
    styles = getSampleStyleSheet()
    def s(name, parent='Normal', **kw):
        return ParagraphStyle(name, parent=styles[parent], **kw)
    return {
        'h1':    s('_h1', fontSize=14, fontName='Helvetica-Bold', textColor=AG_NAVY, spaceAfter=4),
        'h2':    s('_h2', fontSize=10, fontName='Helvetica-Bold', textColor=AG_NAVY, spaceBefore=8, spaceAfter=2),
        'p':     s('_p',  fontSize=8, leading=10, textColor=AG_INK),
        'small': s('_sm', fontSize=7, leading=9, textColor=AG_STONE),
        'right': s('_rt', fontSize=8, leading=10, alignment=TA_RIGHT, textColor=AG_INK),
        'th':    s('_th', fontSize=7, fontName='Helvetica-Bold', textColor=colors.white),
        'td':    s('_td', fontSize=7, leading=9, textColor=AG_INK),
        'tdr':   s('_tdr', fontSize=7, leading=9, alignment=TA_RIGHT, textColor=AG_INK),
    }


def _pdf_table_style():
    return TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), AG_NAVY),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('LINEBELOW', (0, 0), (-1, 0), 2, AG_BLUE),
        ('GRID', (0, 0), (-1, -1), 0.3, AG_SKY_PALE),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, AG_GRAY_FAINT]),
    ])


def _ag_unified_header(title_text, W, logo_width_mm=55):
    """Header unificado para todos los PDFs Arias: logo blanco + barra navy con título en oro+blanco."""
    logo_path = str(BASE_DIR / 'static' / 'logos' / 'arias_group_logo_1000px.png')
    elements = []
    if os.path.exists(logo_path):
        logo_row = Table(
            [[RLImage(logo_path, width=logo_width_mm*mm, height=20*mm, kind='proportional'), '']],
            colWidths=[W*0.4, W*0.6]
        )
        logo_row.setStyle(TableStyle([
            ('LEFTPADDING', (0,0), (0,0), 0),
            ('TOPPADDING', (0,0), (-1,-1), 0),
            ('BOTTOMPADDING', (0,0), (-1,-1), 2),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ]))
        elements.append(logo_row)
    hdr = [[
        Paragraph('<b>ARIAS GROUP CARIBE</b>', ParagraphStyle(
            'brand_hdr', fontSize=10, textColor=colors.white, fontName='Helvetica-Bold', leading=12)),
        Paragraph(f'<b>{title_text}</b>', ParagraphStyle(
            'title_hdr', fontSize=11, textColor=colors.white, fontName='Helvetica-Bold', alignment=TA_CENTER, leading=13)),
    ]]
    hdr_tbl = Table(hdr, colWidths=[W*0.35, W*0.65])
    hdr_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), AG_NAVY),
        ('TOPPADDING', (0,0), (-1,-1), 10),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
        ('LEFTPADDING', (0,0), (0,0), 14),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    elements.append(hdr_tbl)
    return elements


def _ag_unified_footer():
    """Footer único para todos los PDFs Arias."""
    style = ParagraphStyle(
        'ag_foot', fontSize=6, textColor=AG_STONE, fontName='Helvetica',
        alignment=TA_CENTER, leading=8
    )
    return Paragraph(
        'Arias Group Caribe SRL  ·  RNC 1-33-63109-1  ·  '
        'Av. Independencia Km 6, Plaza Comercial Átala I, Suite 203, Santo Domingo, D.N.  ·  '
        'Distribuidor técnico Fassa Bortolo',
        style
    )


def _aggregate_lines_by_sku(lines):
    """Agrupa líneas con el mismo SKU sumando cantidades. Mantiene el resto de datos del primero."""
    aggregated = {}
    for l in lines:
        sku = l.get('sku', '')
        if not sku:
            continue
        if sku in aggregated:
            existing = aggregated[sku]
            qty_keys = ['qty', 'qty_input', 'qty_logistic']
            for k in qty_keys:
                if k in l and l.get(k) is not None:
                    existing[k] = _num(existing.get(k, 0)) + _num(l.get(k, 0))
            for k in ('pallets_logistic', 'm2_total', 'weight_total_kg'):
                if k in l and l.get(k) is not None:
                    existing[k] = _num(existing.get(k, 0)) + _num(l.get(k, 0))
        else:
            aggregated[sku] = dict(l)
    return list(aggregated.values())


def _load_offer_with_lines(offer_id: int):
    db = get_db()
    offer = db.execute('SELECT * FROM pending_offers WHERE id = ?', (offer_id,)).fetchone()
    if not offer:
        return None, None
    ol = db.execute('SELECT * FROM order_lines WHERE offer_id = ? ORDER BY id', (offer_id,)).fetchall()
    if not ol:
        raw_lines = json.loads(offer['lines_json']) if offer['lines_json'] else []
        computed = []
        for li in raw_lines:
            prod = db.execute('SELECT * FROM products WHERE sku = ?', (li.get('sku'),)).fetchone()
            if prod:
                cl = compute_line(dict(prod), _num(li.get('qty', 0)))
                computed.append(cl)
        return offer, computed
    return offer, [dict(r) for r in ol]


def _logistics_breakdown_for_offer(db, offer, lines):
    """Recalcula el desglose logístico (floor/weight/cbm + driver dominante) con
    el motor agregado para mostrarlo en PDFs como diagnóstico.

    NO sobrescribe `offer['container_count']`: ese valor es el que el cliente
    vio en la oferta firmada y es contractual. Aquí solo se enriquece la
    presentación con métricas que no afectan al precio.

    Devuelve un LogisticsResult o None si faltan datos.
    """
    from logistics.engine import ContainerProfile, PalletProfile, SkuInput, compute_logistics

    log_row = db.execute(
        'SELECT container_type FROM logistics_orders WHERE offer_id = ? ORDER BY id DESC LIMIT 1',
        (offer['id'],)
    ).fetchone()
    container_type = (log_row['container_type'] if log_row and log_row['container_type'] else '40HC')

    cp_row = db.execute('SELECT * FROM container_profiles WHERE type = ?', (container_type,)).fetchone()
    if not cp_row:
        return None
    floor_stowage = cp_row['floor_stowage_factor'] if 'floor_stowage_factor' in cp_row.keys() else 1.0
    container = ContainerProfile(
        type=cp_row['type'],
        inner_length_m=cp_row['inner_length_m'],
        inner_width_m=cp_row['inner_width_m'],
        inner_height_m=cp_row['inner_height_m'],
        payload_kg=cp_row['payload_kg'],
        door_clearance_m=cp_row['door_clearance_m'],
        stowage_factor=cp_row['stowage_factor'],
        floor_stowage_factor=float(floor_stowage),
    )

    pallet_profiles: dict[str, PalletProfile] = {}
    for r in db.execute('SELECT * FROM pallet_profiles').fetchall():
        pallet_profiles[r['category']] = PalletProfile(
            category=r['category'],
            length_m=r['pallet_length_m'],
            width_m=r['pallet_width_m'],
            height_m=r['pallet_height_m'],
            stackable_levels=r['stackable_levels'],
            allow_mix_floor=bool(r['allow_mix_floor']),
        )

    cost_per_container = 0.0
    route_id = offer['route_id'] if 'route_id' in offer.keys() else None
    if route_id:
        col_map = {'20': 'container_20_eur', '40': 'container_40_eur', '40HC': 'container_40hc_eur'}
        col = col_map.get(container_type, 'container_40hc_eur')
        rt = db.execute(f'SELECT {col} AS c FROM shipping_routes WHERE id = ?', (route_id,)).fetchone()
        if rt:
            cost_per_container = float(rt['c'] or 0)

    skus: list[SkuInput] = []
    for ln in lines:
        sku = str(ln.get('sku') or '').strip()
        if not sku:
            continue
        qty = _num(ln.get('qty_logistic') or ln.get('qty_input') or ln.get('qty'))
        if qty <= 0:
            continue
        p = db.execute(
            'SELECT category, kg_per_unit, units_per_pallet, sqm_per_pallet, '
            'pallet_length_m, pallet_width_m, pallet_height_m, pallet_weight_kg, '
            'stackable_levels FROM products WHERE sku = ?', (sku,)
        ).fetchone()
        if not p or p['category'] not in pallet_profiles:
            continue
        upp = p['units_per_pallet'] or 0
        sqm_pp = p['sqm_per_pallet']
        unit_area_m2 = (float(sqm_pp) / float(upp)) if (upp and sqm_pp) else 0
        skus.append(SkuInput(
            sku=sku, category=p['category'], qty=qty,
            unit_weight_kg=float(p['kg_per_unit'] or 0),
            unit_area_m2=unit_area_m2,
            units_per_pallet=float(upp) if upp else 1,
            pallet_length_m=p['pallet_length_m'], pallet_width_m=p['pallet_width_m'],
            pallet_height_m=p['pallet_height_m'], pallet_weight_kg=p['pallet_weight_kg'],
            stackable_levels=p['stackable_levels'],
        ))

    if not skus:
        return None
    try:
        return compute_logistics(skus, container, pallet_profiles, cost_per_container)
    except (KeyError, ValueError):
        return None


_DRIVER_LABELS = {'floor': 'Suelo (huella palés)', 'weight': 'Peso bruto', 'cbm': 'Volumen'}


# ── PDF: PreOrden Fábrica ─────────────────────────────────────────

@app.route('/api/preorden-pdf/<int:offer_id>')
@login_required
def preorden_pdf(offer_id):
    offer, lines = _load_offer_with_lines(offer_id)
    if not offer:
        return 'Oferta no encontrada', 404

    S = _pdf_styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm, topMargin=15*mm, bottomMargin=15*mm)
    W = A4[0] - 30*mm
    story = []

    db = get_db()
    num_pre = next_sequence(db, 'PRE')
    db.commit()

    ref_date = (offer['created_at'] or '')[:10]
    for el in _ag_unified_header('PREORDEN DE SUMINISTRO A FASSA', W):
        story.append(el)
    story.append(Spacer(1, 4*mm))

    meta = [
        ['Nº PreOrden', num_pre, 'Fecha emisión', ref_date],
        ['Comprador', 'ARIAS GROUP CARIBE SRL', 'RNC', '1-33-63109-1'],
        ['Responsable compras', 'Ana Mar Pérez Marrero — Directora Operaciones', 'Ref. interna', f"PRES-{offer['id']}"],
        ['Punto de entrega', 'A negociar (EXW fábrica o FCA Valencia)', 'Plazo requerido', 'A confirmar'],
    ]
    mt = Table([[Paragraph(str(c), S['p']) for c in row] for row in meta],
               colWidths=[W*0.18, W*0.40, W*0.18, W*0.24])
    mt.setStyle(TableStyle([
        ('TOPPADDING', (0,0), (-1,-1), 3), ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('FONTSIZE', (0,0), (-1,-1), 8),
        ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#cccccc')),
        ('BACKGROUND', (0,0), (0,-1), colors.HexColor('#F2F0EB')),
        ('BACKGROUND', (2,0), (2,-1), colors.HexColor('#F2F0EB')),
    ]))
    story.append(mt)
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph('DETALLE DE PRODUCTO A PEDIR', S['h2']))
    head = [Paragraph(h, S['th']) for h in ['Producto', 'Ref. Fassa', 'Ud. venta', 'Cantidad', 'Formato']]
    rows = [head]
    agg_lines = _aggregate_lines_by_sku(lines)
    for l in agg_lines:
        qty = _num(l.get('qty_input', l.get('qty_logistic', l.get('qty', 0))))
        prod = db.execute(
            'SELECT pack_size, content_per_unit FROM products WHERE sku = ?',
            (l.get('sku', ''),)
        ).fetchone()
        formato = ''
        if prod:
            parts = [p.strip() for p in [prod['content_per_unit'], prod['pack_size']] if p and p.strip()]
            formato = ' · '.join(parts)
        rows.append([
            Paragraph(str(l.get('name', '')), S['td']),
            Paragraph(str(l.get('sku', '')), S['td']),
            Paragraph(str(l.get('unit', '')), S['td']),
            Paragraph(f"{qty:,.0f}", S['tdr']),
            Paragraph(str(formato) if formato else '—', S['td']),
        ])
    t = Table(rows, colWidths=[W*0.34, W*0.14, W*0.10, W*0.12, W*0.30])
    t.setStyle(_pdf_table_style())
    story.append(t)
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph('ASPECTOS A CONFIRMAR CON FASSA', S['h2']))
    cond_data = [
        ['Fecha embarque', 'A confirmar'],
        ['Logística / contenedores', 'A determinar por Fassa Bortolo'],
        ['Condición de pago', 'Según acuerdo'],
    ]
    lt = Table([[Paragraph(r[0], S['p']), Paragraph(r[1], S['p'])] for r in cond_data],
               colWidths=[W*0.35, W*0.65])
    lt.setStyle(TableStyle([
        ('TOPPADDING', (0,0), (-1,-1), 2), ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#cccccc')),
    ]))
    story.append(lt)
    story.append(Spacer(1, 6*mm))
    story.append(_ag_unified_footer())

    log_audit(db, offer_id, 'PREORDEN_PDF', f'{num_pre}')
    db.commit()

    doc.build(story)
    buffer.seek(0)
    resp = make_response(buffer.read())
    resp.headers['Content-Type'] = 'application/pdf'
    disp = 'attachment' if request.args.get('download') else 'inline'
    resp.headers['Content-Disposition'] = f'{disp}; filename="PreOrden_{num_pre}.pdf"'
    return resp


# ── PDF: Orden Logística ──────────────────────────────────────────

@app.route('/api/orden-logistica-pdf/<int:offer_id>')
@login_required
def orden_logistica_pdf(offer_id):
    offer, lines = _load_offer_with_lines(offer_id)
    if not offer:
        return 'Oferta no encontrada', 404

    S = _pdf_styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm, topMargin=15*mm, bottomMargin=15*mm)
    W = A4[0] - 30*mm
    story = []

    db = get_db()
    num_ol = next_sequence(db, 'LOG')
    db.commit()

    ref_date = (offer['created_at'] or '')[:10]
    total_pal = sum(int(_num(l.get('pallets_logistic', 0))) for l in lines)
    total_kg = sum(_num(l.get('weight_total_kg', 0)) for l in lines)
    total_m2 = sum(_num(l.get('m2_total', 0)) for l in lines)

    # Container count CONGELADO de la oferta (lo que el cliente firmó). El nº
    # impreso aquí debe coincidir con el del cotizador y con el offer_pdf.
    container_count = int(_num(offer['container_count']) or 0)
    log_row = db.execute(
        'SELECT container_type FROM logistics_orders WHERE offer_id = ? ORDER BY id DESC LIMIT 1',
        (offer['id'],)
    ).fetchone()
    container_type = (log_row['container_type'] if log_row and log_row['container_type'] else '40HC')

    # Diagnóstico en vivo (modelo agregado): solo enriquece la presentación,
    # NO sustituye container_count.
    log_result = _logistics_breakdown_for_offer(db, offer, lines)

    # ── PAGE 1: Calculadora Logística ─────────────────────────────
    for el in _ag_unified_header('ORDEN LOGÍSTICA — CALCULADORA', W):
        story.append(el)
    story.append(Spacer(1, 4*mm))

    meta = [
        ['Nº Orden', num_ol, 'Fecha', ref_date],
        ['Ref. Pedido', offer['offer_number'], 'Cliente', offer['client_name']],
        ['Proyecto', offer['project_name'], 'Incoterm', offer['incoterm'] or 'EXW'],
    ]
    mt = Table([[Paragraph(c, S['p']) for c in row] for row in meta],
               colWidths=[W*0.15, W*0.35, W*0.15, W*0.35])
    mt.setStyle(TableStyle([
        ('TOPPADDING', (0,0), (-1,-1), 2), ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#cccccc')),
    ]))
    story.append(mt)
    story.append(Spacer(1, 3*mm))

    story.append(Paragraph('DESGLOSE DE MERCANCÍA', S['h2']))
    head = [Paragraph(h, S['th']) for h in ['Producto', 'SKU', 'Familia', 'Ud.', 'Cant.', 'Palés', 'Peso (kg)']]
    rows = [head]
    agg_lines_ol = _aggregate_lines_by_sku(lines)
    for l in agg_lines_ol:
        pal = int(_num(l.get('pallets_logistic', 0)))
        kg = _num(l.get('weight_total_kg', 0))
        qty = _num(l.get('qty_input', l.get('qty_logistic', l.get('qty', 0))))
        rows.append([
            Paragraph(str(l.get('name', '')), S['td']),
            Paragraph(str(l.get('sku', '')), S['td']),
            Paragraph(str(l.get('family', '')), S['td']),
            Paragraph(str(l.get('unit', '')), S['td']),
            Paragraph(f"{qty:,.0f}", S['tdr']),
            Paragraph(f"{pal:,}", S['tdr']),
            Paragraph(f"{kg:,.0f}" if kg else '—', S['tdr']),
        ])
    rows.append([
        Paragraph('<b>TOTALES</b>', S['td']), '', '', '', '',
        Paragraph(f"<b>{total_pal:,}</b>", S['tdr']),
        Paragraph(f"<b>{total_kg:,.0f}</b>", S['tdr']),
    ])
    t = Table(rows, colWidths=[W*0.30, W*0.13, W*0.11, W*0.07, W*0.11, W*0.09, W*0.14])
    t.setStyle(_pdf_table_style())
    story.append(t)
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph('CONTENEDORES', S['h2']))
    if container_count > 0:
        cont_rows = [['Recomendación', f"{container_count} × {container_type}"]]
        if log_result:
            driver_label = _DRIVER_LABELS.get(log_result.dominant_driver, log_result.dominant_driver or '—')
            cont_rows += [
                ['Driver dominante', driver_label],
                ['Suelo (huella palés)', f"{log_result.total_floor_m2:.1f} m² · ocupa {log_result.n_by_floor:.2f} cont."],
                ['Peso bruto', f"{log_result.total_weight_kg:,.0f} kg · ocupa {log_result.n_by_weight:.2f} cont."],
                ['Volumen', f"{log_result.total_cbm:.1f} m³ · ocupa {log_result.n_by_cbm:.2f} cont."],
            ]
        else:
            cont_rows += [
                ['Palés totales', str(total_pal)],
                ['Peso total', f"{total_kg:,.0f} kg"],
            ]
    else:
        cont_rows = [['Sin datos logísticos suficientes', '']]
    ct = Table([[Paragraph(r[0], S['p']), Paragraph(r[1], S['p'])] for r in cont_rows],
               colWidths=[W*0.35, W*0.65])
    ct.setStyle(TableStyle([
        ('TOPPADDING', (0,0), (-1,-1), 2), ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#cccccc')),
    ]))
    story.append(ct)
    story.append(Spacer(1, 3*mm))

    story.append(Paragraph('RESUMEN COSTES', S['h2']))
    product_cost = _num(offer['total_product_eur'])
    logistic_cost = _num(offer['total_logistic_eur'])
    total_final = _num(offer['total_final_eur'])
    fx = _num(offer['fx_rate']) or 1.18
    cost_rows = [
        ['Coste mercancía EXW', f"€ {product_cost:,.2f}"],
        ['Coste logístico', f"€ {logistic_cost:,.2f}"],
        ['TOTAL €', f"€ {total_final:,.2f}"],
        ['TOTAL USD (FX {:.3f})'.format(fx), f"$ {total_final * fx:,.2f}"],
    ]
    cst = Table([[Paragraph(r[0], S['p']), Paragraph(f"<b>{r[1]}</b>", S['tdr'])] for r in cost_rows],
                colWidths=[W*0.55, W*0.45])
    cst.setStyle(TableStyle([
        ('TOPPADDING', (0,0), (-1,-1), 2), ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#cccccc')),
        ('BACKGROUND', (0,2), (-1,2), colors.HexColor('#e8e8e8')),
    ]))
    story.append(cst)

    # ── PAGE 2: Solicitud Agente Logística ────────────────────────
    story.append(PageBreak())
    for el in _ag_unified_header('SOLICITUD AL AGENTE DE CARGA', W):
        story.append(el)
    story.append(Spacer(1, 4*mm))

    sol_meta = [
        ['Ref.', offer['offer_number'], 'Fecha', ref_date],
        ['Cliente/Proyecto', f"{offer['client_name']} — {offer['project_name']}", 'Respuesta', 'A la brevedad'],
    ]
    smt = Table([[Paragraph(c, S['p']) for c in row] for row in sol_meta],
                colWidths=[W*0.12, W*0.38, W*0.12, W*0.38])
    smt.setStyle(TableStyle([
        ('TOPPADDING', (0,0), (-1,-1), 2), ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#cccccc')),
    ]))
    story.append(smt)
    story.append(Spacer(1, 3*mm))

    story.append(Paragraph('MERCANCÍA A TRANSPORTAR', S['h2']))
    sh = [Paragraph(h, S['th']) for h in ['Producto', 'SKU', 'Ud.', 'Cant.', 'Palés', 'Peso (kg)']]
    srows = [sh]
    for l in agg_lines_ol:
        pal = int(_num(l.get('pallets_logistic', 0)))
        kg = _num(l.get('weight_total_kg', 0))
        qty = _num(l.get('qty_input', l.get('qty_logistic', l.get('qty', 0))))
        srows.append([
            Paragraph(str(l.get('name', '')), S['td']),
            Paragraph(str(l.get('sku', '')), S['td']),
            Paragraph(str(l.get('unit', '')), S['td']),
            Paragraph(f"{qty:,.0f}", S['tdr']),
            Paragraph(f"{pal:,}", S['tdr']),
            Paragraph(f"{kg:,.0f}" if kg else '—', S['tdr']),
        ])
    st2 = Table(srows, colWidths=[W*0.34, W*0.14, W*0.08, W*0.14, W*0.12, W*0.18])
    st2.setStyle(_pdf_table_style())
    story.append(st2)
    story.append(Spacer(1, 3*mm))

    story.append(Paragraph('DETALLE LOGÍSTICO', S['h2']))
    cont_txt = f"{container_count} × {container_type}" if container_count > 0 else 'Por determinar'
    det = [
        ['Contenedores necesarios', cont_txt],
        ['Peso bruto total', f"{total_kg:,.0f} kg" if total_kg else 'Por determinar'],
        ['Palés totales', str(total_pal)],
        ['Origen', 'Tarancón / Valencia (España)'],
        ['Destino', 'Santo Domingo (Caucedo)'],
        ['Fecha embarque estimada', 'Por determinar'],
    ]
    dt = Table([[Paragraph(r[0], S['p']), Paragraph(r[1], S['p'])] for r in det],
               colWidths=[W*0.35, W*0.65])
    dt.setStyle(TableStyle([
        ('TOPPADDING', (0,0), (-1,-1), 2), ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#cccccc')),
    ]))
    story.append(dt)
    story.append(Spacer(1, 3*mm))

    story.append(Paragraph('SERVICIOS A COTIZAR', S['h2']))
    servicios = [
        'Flete terrestre Tarancón → Valencia',
        'THC origen + despacho de exportación',
        'Flete marítimo Valencia → Caucedo',
        'Seguro de transporte',
        'THC destino + despacho de importación',
        'Gastos portuarios destino',
        'Arrastre hasta almacén',
    ]
    serv_rows = [[Paragraph(f"{i+1}.", S['tdr']), Paragraph(s, S['p'])] for i, s in enumerate(servicios)]
    svt = Table(serv_rows, colWidths=[W*0.06, W*0.94])
    svt.setStyle(TableStyle([
        ('TOPPADDING', (0,0), (-1,-1), 2), ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#cccccc')),
    ]))
    story.append(svt)
    story.append(Spacer(1, 6*mm))
    story.append(_ag_unified_footer())

    log_audit(db, offer_id, 'ORDEN_LOG_PDF', f'{num_ol}')
    db.commit()

    doc.build(story)
    buffer.seek(0)
    resp = make_response(buffer.read())
    resp.headers['Content-Type'] = 'application/pdf'
    disp = 'attachment' if request.args.get('download') else 'inline'
    resp.headers['Content-Disposition'] = f'{disp}; filename="OrdenLogistica_{num_ol}.pdf"'
    return resp


# ── Bot API endpoints ────────────────────────────────────────────
# Estos endpoints autentican con BOT_API_TOKEN (Bearer-style), no con
# sesión de cookies; por tanto se exentan de CSRF (no aplica al modelo).
@app.route('/api/products', methods=['GET'])
@csrf.exempt
@bot_token_required
def api_products():
    """Consulta productos por SKU, nombre o familia"""
    db = get_db()
    sku = request.args.get('sku')
    name = request.args.get('name')
    family = request.args.get('family')
    
    if sku:
        p = db.execute(
            'SELECT sku, name, category, subfamily, unit, unit_price_eur, units_per_pallet, sqm_per_pallet FROM products WHERE sku = ?',
            (sku,)
        ).fetchone()
        if p:
            return jsonify({'ok': True, 'product': dict(p)})
        return jsonify({'ok': False, 'error': f'SKU {sku} no encontrado'}), 404
    
    query = 'SELECT sku, name, category, subfamily, unit, unit_price_eur FROM products WHERE 1=1'
    params = []
    if name:
        query += ' AND name LIKE ?'
        params.append(f'%{name}%')
    if family:
        query += ' AND category = ?'
        params.append(family.upper())
    query += ' ORDER BY category, name LIMIT 50'
    
    products = [dict(r) for r in db.execute(query, params).fetchall()]
    return jsonify({'ok': True, 'products': products, 'count': len(products)})


@app.route('/api/families', methods=['GET'])
@csrf.exempt
@bot_token_required
def api_families():
    """Lista familias y subfamilias disponibles"""
    db = get_db()
    families = db.execute('SELECT DISTINCT category FROM products ORDER BY category').fetchall()
    result = {}
    for f in families:
        cat = f[0]
        subs = db.execute('SELECT DISTINCT subfamily FROM products WHERE category = ? AND subfamily IS NOT NULL AND subfamily != "" ORDER BY subfamily', (cat,)).fetchall()
        result[cat] = [s[0] for s in subs]
    return jsonify({'ok': True, 'families': result})


@app.route('/api/order', methods=['POST'])
@csrf.exempt
@bot_token_required
def api_order():
    """Crea pedido desde bot. Usa el mismo motor que /api/save-offer."""
    data = request.get_json()
    if not data:
        return jsonify({'ok': False, 'error': 'No data'}), 400

    db = get_db()
    client_name = data.get('client', 'Bot Pedido')
    items = data.get('items', [])
    if not items:
        return jsonify({'ok': False, 'error': 'No items'}), 400

    waste_pct = _num(data.get('wastePct', 0)) / 100
    margin_pct = _num(data.get('margin', 33)) / 100

    input_lines: list[dict[str, Any]] = []
    computed: list[dict[str, Any]] = []
    skipped: list[str] = []

    for item in items:
        sku = item.get('sku')
        qty = _num(item.get('qty', 0))
        if not sku or qty <= 0:
            continue
        prod = db.execute('SELECT * FROM products WHERE sku = ?', (sku,)).fetchone()
        if not prod:
            skipped.append(sku)
            continue
        pd = dict(prod)
        qty_with_waste = math.ceil(qty * (1 + waste_pct)) if waste_pct > 0 else qty
        line = compute_line(pd, qty_with_waste)
        line['qty_original'] = qty
        computed.append(line)
        input_lines.append({
            'sku': pd['sku'], 'name': pd['name'], 'family': pd['category'],
            'unit': pd['unit'], 'price': pd['unit_price_eur'], 'qty': qty,
        })

    if not computed:
        return jsonify({'ok': False, 'error': 'No valid products found', 'skipped': skipped}), 400

    totals = compute_totals(computed)
    product_cost = totals['cost_exw_eur']
    total_final = product_cost / max(1 - margin_pct, 0.01) if margin_pct < 1 else product_cost

    order_num = data.get('order_number') or next_sequence(db, 'PED')
    container_count = (totals.get('containers') or {}).get('units', 0)

    raw_hash = compute_raw_hash(json.dumps(input_lines, sort_keys=True))
    dup = find_offer_by_hash(db, raw_hash)
    if dup:
        return jsonify({
            'ok': False,
            'error': f'Pedido duplicado (#{dup["offer_number"]})',
            'existing_order_number': dup['offer_number'],
        }), 409

    db.execute(
        '''INSERT INTO pending_offers
        (offer_number, client_name, project_name, waste_pct, margin_pct, fx_rate,
         lines_json, total_product_eur, total_logistic_eur, total_final_eur,
         status, incoterm, container_count, raw_hash, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (order_num, client_name, data.get('project', 'Pedido Bot'),
         _num(data.get('wastePct', 0)), _num(data.get('margin', 33)), 1.18,
         json.dumps(input_lines), round(product_cost, 2), 0, round(total_final, 2),
         'pending', data.get('incoterm', 'EXW'), int(container_count), raw_hash, now_iso())
    )
    offer_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    save_order_lines(db, offer_id, computed)
    log_audit(db, offer_id, 'ORDER_CREATED',
              f'{order_num} | {client_name} | {len(computed)} líneas | €{round(total_final, 2)}')
    db.commit()

    return jsonify({
        'ok': True,
        'order_number': order_num,
        'offer_id': offer_id,
        'total_eur': round(total_final, 2),
        'product_cost_eur': round(product_cost, 2),
        'items': len(computed),
        'total_weight_kg': totals['weight_total_kg'],
        'pallets_logistic': totals['pallets_logistic'],
        'container_recommendation': totals.get('containers'),
        'alerts': dedup_alerts(computed),
        'skipped_skus': skipped,
    })


@app.route('/api/orders', methods=['GET'])
@csrf.exempt
@bot_token_required
def api_orders():
    """Lista pedidos de un cliente"""
    db = get_db()
    client = request.args.get('client')
    if client:
        orders = db.execute(
            "SELECT offer_number, project_name, total_final_eur, status, incoterm, created_at FROM pending_offers WHERE client_name LIKE ? ORDER BY created_at DESC LIMIT 20",
            (f'%{client}%',)
        ).fetchall()
    else:
        orders = db.execute(
            "SELECT offer_number, client_name, project_name, total_final_eur, status, incoterm, created_at FROM pending_offers ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    return jsonify({'ok': True, 'orders': [dict(o) for o in orders]})


@app.route('/api/ficha-tecnica/<sku>')
@csrf.exempt
@bot_token_required
def api_ficha_tecnica(sku):
    """Devuelve ficha técnica del producto"""
    db = get_db()
    p = db.execute('SELECT * FROM products WHERE sku = ?', (sku,)).fetchone()
    if not p:
        return jsonify({'ok': False, 'error': f'SKU {sku} no encontrado'}), 404
    pd = dict(p)
    ficha = {
        'sku': pd['sku'], 'name': pd['name'], 'category': pd['category'],
        'subfamily': pd.get('subfamily'), 'unit': pd['unit'],
        'price_eur': pd['unit_price_eur'],
        'units_per_pallet': pd.get('units_per_pallet'),
        'sqm_per_pallet': pd.get('sqm_per_pallet'),
    }
    return jsonify({'ok': True, 'ficha': ficha})


def _bootstrap_db() -> None:
    """Idempotent: CREATE TABLE IF NOT EXISTS + seed solo si tablas vacías."""
    db_path = Path(app.config['DATABASE'])
    parent = db_path.parent
    print(f'[bootstrap] DB_PATH={db_path}')
    print(f'[bootstrap] parent={parent} exists={parent.exists()}')
    # En Render la carpeta del Disk puede existir pero la app necesita crear
    # subdirectorios propios; mkdir -p es no-op si ya existe.
    parent.mkdir(parents=True, exist_ok=True)
    print(f'[bootstrap] parent writable={os.access(parent, os.W_OK)}')
    with app.app_context():
        init_db()
        seed_db()
    print(f'[bootstrap] init/seed OK → db_exists={db_path.exists()} size={db_path.stat().st_size if db_path.exists() else 0}')


# Bootstrap on import cuando se sirve bajo WSGI (waitress/gunicorn en Render).
# `python app.py` sigue pasando por __main__ más abajo. Tests no setean el flag.
if os.environ.get('RUN_INIT_ON_IMPORT') == '1':
    try:
        _bootstrap_db()
    except Exception as e:
        import traceback
        print(f'[bootstrap] init/seed falló: {e}')
        traceback.print_exc()


if __name__ == '__main__':
    _bootstrap_db()
    host = os.environ.get('FLASK_HOST', '127.0.0.1')
    port = int(os.environ.get('FLASK_PORT', '5001'))
    app.run(debug=_debug, host=host, port=port)
