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
    'REAL DEFAULT 50', 'INTEGER DEFAULT 99',
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


def get_db() -> sqlite3.Connection:
    if 'db' not in g:
        g.db = sqlite3.connect(app.config['DATABASE'])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_: Any) -> None:
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db() -> None:
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
            discount_pct REAL DEFAULT 50
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
            fx_rate REAL DEFAULT 1.085,
            lines_json TEXT NOT NULL,
            total_product_eur REAL DEFAULT 0,
            total_logistic_eur REAL DEFAULT 0,
            total_final_eur REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',
            incoterm TEXT DEFAULT 'EXW',
            route_id INTEGER,
            container_count INTEGER DEFAULT 0,
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

        CREATE TABLE IF NOT EXISTS pickup_pricing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            pickup_point TEXT NOT NULL,
            price_eur_unit REAL NOT NULL,
            notes TEXT,
            FOREIGN KEY(product_id) REFERENCES products(id),
            UNIQUE(product_id, pickup_point)
        );

        CREATE TABLE IF NOT EXISTS family_defaults (
            category TEXT PRIMARY KEY,
            discount_pct REAL NOT NULL DEFAULT 50,
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
        """
    )
    # Migraciones para DBs existentes — usa _safe_add_column (allowlist valida col + tipo).
    prod_cols = {r[1] for r in db.execute("PRAGMA table_info(products)").fetchall()}
    for col, typ in [('subfamily', 'TEXT'), ('pvp_per_m2', 'REAL'), ('precio_arias_m2', 'REAL'),
                     ('content_per_unit', 'TEXT'), ('pack_size', 'TEXT'),
                     ('pvp_eur_unit', 'REAL'), ('precio_arias_eur_unit', 'REAL'),
                     ('discount_pct', 'REAL DEFAULT 50')]:
        if col not in prod_cols:
            _safe_add_column(db, 'products', col, typ)
    client_cols = {r[1] for r in db.execute("PRAGMA table_info(clients)").fetchall()}
    for col, typ in [('rnc', 'TEXT'), ('address', 'TEXT')]:
        if col not in client_cols:
            _safe_add_column(db, 'clients', col, typ)
    offer_cols = {r[1] for r in db.execute("PRAGMA table_info(pending_offers)").fetchall()}
    if 'raw_hash' not in offer_cols:
        _safe_add_column(db, 'pending_offers', 'raw_hash', 'TEXT')
    fd_cols = {r[1] for r in db.execute("PRAGMA table_info(family_defaults)").fetchall()}
    if fd_cols and 'display_order' not in fd_cols:
        _safe_add_column(db, 'family_defaults', 'display_order', 'INTEGER DEFAULT 99')
    db.commit()


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
        db.execute('INSERT OR IGNORE INTO systems (name, description, default_waste_pct) VALUES (?, ?, ?)', row)

    if db.execute('SELECT COUNT(*) AS c FROM clients').fetchone()['c'] == 0:
        db.execute(
            'INSERT INTO clients (name, company, email, phone, country, score, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
            ('Promotor Demo', 'Arias Group Demo', 'demo@example.com', '+1 809 000 0000', 'República Dominicana', 78, now),
        )
        client_id = db.execute('SELECT id FROM clients WHERE email = ?', ('demo@example.com',)).fetchone()['id']
        db.execute(
            '''INSERT INTO projects
            (client_id, name, project_type, location, area_sqm, stage, go_no_go, incoterm, fx_rate, target_margin_pct, freight_eur, customs_pct, logistics_notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (client_id, 'Torre piloto - baños', 'Hotelería', 'Punta Cana', 800, 'CÁLCULO DETALLADO', 'GO', 'EXW', 1.0, 0.33, 4200, 0.18, 'Demo seeded project', now),
        )

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

    # Seed FX rates
    if db.execute('SELECT COUNT(*) AS c FROM fx_rates').fetchone()['c'] == 0:
        fx = [
            ('EUR', 'USD', 1.085, now, 'Manual Abril 2026'),
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
    fx_setting = db.execute("SELECT value FROM app_settings WHERE key='fx_eur_usd'").fetchone()
    fx = {'USD': float(fx_setting['value']) if fx_setting else 1.085}

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
    fam_defaults = {r['category']: r['discount_pct']
                    for r in db.execute('SELECT category, discount_pct FROM family_defaults').fetchall()}
    return render_template('products.html',
                           groups=groups,
                           totals=totals,
                           grand_total=len(rows),
                           missing=missing,
                           fam_defaults=fam_defaults,
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
                'pvp_eur_unit', 'precio_arias_eur_unit', 'discount_pct',
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
        if f in ('pvp_eur_unit', 'precio_arias_eur_unit', 'discount_pct',
                 'kg_per_unit', 'units_per_pallet', 'sqm_per_pallet'):
            new_v = float(new_v) if new_v not in (None, '') else None
        if new_v == old_v:
            continue
        sets.append(f'{f} = ?')
        vals.append(new_v)
        changes.append((f, old_v, new_v))
    if not sets:
        return jsonify({'ok': True, 'changed': 0, 'message': 'sin cambios'})
    # Auto-sync: si cambió pvp_eur_unit o discount_pct y no envió precio_arias_eur_unit explícito, recalcular
    if 'precio_arias_eur_unit' not in data:
        pvp_new = next((v for f, _, v in changes if f == 'pvp_eur_unit'), None)
        disc_new = next((v for f, _, v in changes if f == 'discount_pct'), None)
        pvp = pvp_new if pvp_new is not None else existing['pvp_eur_unit']
        disc = disc_new if disc_new is not None else (existing['discount_pct'] or 50)
        if pvp is not None:
            arias = round(float(pvp) * (1 - float(disc) / 100), 4)
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
    fx_setting = db.execute("SELECT value FROM app_settings WHERE key='fx_eur_usd'").fetchone()
    fx_rate = float(fx_setting['value']) if fx_setting else 1.085
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

    NAVY   = colors.HexColor('#0D1B4B')
    BLUE   = colors.HexColor('#1A3A8F')
    GOLD   = colors.HexColor('#C9A84C')
    LGRAY  = colors.HexColor('#F2F0EB')
    MGRAY  = colors.HexColor('#888888')
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
    ref_data = [[
        Paragraph(f"<b>Ref:</b> {quote['version_label']}", S['body']),
        Paragraph(f"<b>Fecha:</b> {ref_date}", S['body']),
        Paragraph(f"<b>Incoterm:</b> {project['incoterm']}", S['body']),
        Paragraph(f"<b>Validez:</b> 30 días", S['body']),
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
            db.execute(
                '''INSERT OR REPLACE INTO fx_rates (base_currency, target_currency, rate, updated_at, source)
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
            db.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES ('fx_eur_usd', ?, ?)",
                (str(new_fx), now_iso())
            )
            flash(f'EUR/USD actualizado a {new_fx}')
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
                          fx_eur_usd=float(fx_setting['value']) if fx_setting else 1.085,
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
    fx = _num(data.get('fx', 1.085))

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
            'note': li.get('note'),
        })

    totals = compute_totals(computed)
    product_cost = totals['cost_exw_eur']
    cost_total = product_cost + logistic
    total_final = cost_total / max(1 - margin_pct, 0.01) if margin_pct < 1 else cost_total

    container_count = (totals.get('containers') or {}).get('units', 0) or _num(data.get('containerCount', 0))

    offer_num = data.get('offerNumber') or next_sequence(db, 'OFR')
    raw_hash = compute_raw_hash(json.dumps(input_lines, sort_keys=True))
    dup = find_offer_by_hash(db, raw_hash)
    if dup:
        return jsonify({
            'ok': False,
            'error': f'Oferta duplicada (#{dup["offer_number"]})',
            'existing_offer_number': dup['offer_number'],
        }), 409

    db.execute(
        '''INSERT INTO pending_offers
        (offer_number, client_name, project_name, waste_pct, margin_pct, fx_rate,
         lines_json, total_product_eur, total_logistic_eur, total_final_eur,
         status, incoterm, container_count, raw_hash, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            offer_num,
            data.get('client', ''),
            data.get('project', ''),
            data.get('wastePct', 5),
            data.get('margin', 33),
            fx,
            json.dumps(input_lines),
            round(product_cost, 2),
            round(logistic, 2),
            round(total_final, 2),
            'pending',
            data.get('incoterm', 'EXW'),
            int(container_count),
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
    fx = _num(data.get('fx', 1.085))

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
    product_cost = totals['cost_exw_eur']
    cost_total = product_cost + logistic
    total_final = cost_total / max(1 - margin_pct, 0.01) if margin_pct < 1 else cost_total
    container_count = (totals.get('containers') or {}).get('units', 0) or _num(data.get('containerCount', 0))

    db.execute(
        '''UPDATE pending_offers SET
           offer_number = ?, client_name = ?, project_name = ?,
           waste_pct = ?, margin_pct = ?, fx_rate = ?,
           lines_json = ?, total_product_eur = ?, total_logistic_eur = ?,
           total_final_eur = ?, incoterm = ?, container_count = ?,
           raw_hash = ?, updated_at = ?
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
            round(logistic, 2),
            round(total_final, 2),
            data.get('incoterm', 'EXW'),
            int(container_count),
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

    fx = offer['fx_rate'] or 1.085
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
    fx = offer['fx_rate'] or 1.085
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
        topMargin=15*mm, bottomMargin=15*mm,
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
        'h1': S('h1', fontSize=9, textColor=WHITE, fontName='Helvetica-Bold', leading=11),
        'p': S('p', fontSize=7.5, textColor=INK, fontName='Helvetica', leading=11),
        'small': S('small', fontSize=7, textColor=MGRAY, fontName='Helvetica', leading=9),
        'bold': S('bold', fontSize=7.5, textColor=NAVY, fontName='Helvetica-Bold', leading=11),
        'right': S('right', fontSize=7.5, textColor=NAVY, fontName='Helvetica', alignment=TA_RIGHT, leading=11),
        'center': S('center', fontSize=7.5, textColor=NAVY, fontName='Helvetica', alignment=TA_CENTER, leading=11),
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
        logo_row = Table([[RLImage(logo_path, width=55*mm, height=20*mm, kind='proportional'), '']],
                         colWidths=[W*0.4, W*0.6])
        logo_row.setStyle(TableStyle([
            ('LEFTPADDING', (0,0), (0,0), 0),
            ('TOPPADDING', (0,0), (-1,-1), 0),
            ('BOTTOMPADDING', (0,0), (-1,-1), 2),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ]))
        story.append(logo_row)
    hdr = [[
        Paragraph('ARIAS GROUP CARIBE', S('brand_h', fontSize=10, textColor=GOLD, fontName='Helvetica-Bold', leading=12)),
        Paragraph('PROPUESTA TÉCNICA Y COMERCIAL', S('pt', fontSize=11, textColor=WHITE, fontName='Helvetica-Bold', alignment=TA_CENTER)),
    ]]
    hdr_tbl = Table(hdr, colWidths=[W*0.3, W*0.7])
    hdr_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), NAVY),
        ('TOPPADDING', (0,0), (-1,-1), 10),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
        ('LEFTPADDING', (0,0), (0,0), 14),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(hdr_tbl)
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph('Sistemas Constructivos Fassa Bortolo', sty['systems']))
    story.append(Spacer(1, 6*mm))
    
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
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('LEFTPADDING', (0,0), (-1,-1), 5),
        ('RIGHTPADDING', (0,0), (-1,-1), 5),
        ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor('#CCCCCC')),
    ]))
    story.append(ref_tbl)
    story.append(Spacer(1, 5*mm))
    
    # Section 1
    story.append(Paragraph('1.  OBJETO Y ALCANCE', sty['h1']))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph('La presente propuesta cubre el suministro de materiales del sistema <b>Suministro completo</b>, fabricados por Fassa Bortolo (Italia/España), para el proyecto <b>{}</b>. Arias Group Caribe SRL actúa como distribuidor técnico exclusivo para República Dominicana y el Caribe.'.format(offer['project_name']), sty['p']))
    story.append(Paragraph('Alcance específico: Suministro de materiales — ver detalle hoja 2', sty['p']))
    story.append(Spacer(1, 4*mm))
    
    # Section 2 — RESUMEN (sin listar líneas; detalle completo va en Hoja 2)
    story.append(Paragraph('2.  RESUMEN ECONÓMICO', sty['h1']))
    story.append(Spacer(1, 2*mm))
    n_lineas = len(lines)
    total_prod_eur = offer['total_product_eur'] or 0
    total_log_eur = offer['total_logistic_eur'] or 0
    econ_data = [
        [Paragraph('<b>Concepto</b>', sty['center']), Paragraph('<b>Importe</b>', sty['center'])],
        [Paragraph(f'Nº de referencias incluidas', sty['p']), Paragraph(f'{n_lineas}', sty['right'])],
        [Paragraph('Subtotal productos (EUR)', sty['p']), Paragraph(f'€ {total_prod_eur:,.2f}', sty['right'])],
        [Paragraph('Logística (EUR)', sty['p']), Paragraph(f'€ {total_log_eur:,.2f}', sty['right'])],
        [Paragraph(f'<b>TOTAL {offer["incoterm"] or "EXW"} (EUR)</b>', sty['bold']), Paragraph(f'<b>€ {total_eur:,.2f}</b>', sty['right'])],
        [Paragraph(f'Tipo de cambio EUR/USD', sty['p']), Paragraph(f'{fx:.4f}', sty['right'])],
        [Paragraph(f'<b>TOTAL {offer["incoterm"] or "EXW"} (USD)</b>', sty['bold']), Paragraph(f'<b>$ {total_usd:,.2f}</b>', sty['right'])],
    ]
    econ_tbl = Table(econ_data, colWidths=[W*0.65, W*0.35])
    econ_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), NAVY),
        ('TEXTCOLOR', (0,0), (-1,0), WHITE),
        ('BACKGROUND', (0,-1), (-1,-1), GOLD_SOFT),
        ('BACKGROUND', (0,-3), (-1,-3), BONE),
        ('LINEABOVE', (0,-1), (-1,-1), 1, GOLD),
        ('LINEABOVE', (0,-3), (-1,-3), 0.5, GOLD),
        ('LINEBELOW', (0,0), (-1,-2), 0.3, colors.HexColor('#CCCCCC')),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
    ]))
    story.append(econ_tbl)
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph('* Importes sujetos a confirmación de pedido formal. No incluyen impuestos locales ni gastos de aduana en destino. Detalle línea-a-línea en Hoja 2.', sty['small']))
    story.append(Spacer(1, 4*mm))
    
    # Section 3
    story.append(Paragraph('3.  CONDICIONES COMERCIALES', sty['h1']))
    story.append(Spacer(1, 2*mm))
    conds = [
        ('Pago', '100% prepago por transferencia bancaria antes de emisión de orden de producción.'),
        ('Validez de oferta', f"{int(offer['margin_pct'])} días calendario desde la fecha de emisión."),
        ('Plazo de entrega', 'Según confirmación de fábrica tras recepción de pago.'),
        ('Puerto de embarque', 'Valencia, España'),
        ('Incoterm aplicable', f"{offer['incoterm'] or 'EXW'} — riesgo y responsabilidad se transfieren al comprador."),
        ('Divisa', 'USD (dólares estadounidenses)'),
    ]
    cond_data = [[Paragraph('<b>Concepto</b>', sty['center']), Paragraph('<b>Detalle</b>', sty['center'])]]
    for label, detail in conds:
        cond_data.append([Paragraph(f"<b>{label}</b>", sty['bold']), Paragraph(detail, sty['p'])])
    cond_tbl = Table(cond_data, colWidths=[W*0.25, W*0.75])
    cond_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), NAVY),
        ('TEXTCOLOR', (0,0), (-1,0), WHITE),
        ('BACKGROUND', (0,1), (0,-1), LGRAY),
        ('LINEBELOW', (0,0), (-1,-1), 0.3, colors.HexColor('#CCCCCC')),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('LEFTPADDING', (0,0), (-1,-1), 5),
        ('RIGHTPADDING', (0,0), (-1,-1), 5),
    ]))
    story.append(cond_tbl)
    story.append(Spacer(1, 4*mm))
    
    # Section 4
    story.append(Paragraph('4.  OBSERVACIONES TÉCNICAS', sty['h1']))
    story.append(Spacer(1, 2*mm))
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
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph('Para confirmar esta propuesta, el cliente deberá firmar y devolver este documento. La firma implica aceptación de las condiciones comerciales indicadas.', sty['p']))
    story.append(Spacer(1, 4*mm))
    
    sig_data = [[
        Paragraph('<b>ARIAS GROUP CARIBE SRL</b><br/>RNC: 1-33-63109-1<br/>Av. Independencia Km 6, Plaza Comercial Átala I, Suite 203<br/>Santo Domingo, D.N.<br/><br/>___________________________<br/>Director Comercial<br/>Fecha: _______________', sty['sig']),
        Paragraph(f'<b>{client_company}</b><br/>RNC: {client_rnc}<br/>{offer["client_name"]}<br/><br/>___________________________<br/>Firma y sello<br/>Fecha: _______________', sty['sig']),
    ]]
    sig_tbl = Table(sig_data, colWidths=[W*0.5, W*0.5])
    sig_tbl.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(sig_tbl)
    story.append(Spacer(1, 6*mm))
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
        Paragraph('ARIAS GROUP CARIBE', S('brand_h2', fontSize=10, textColor=GOLD, fontName='Helvetica-Bold', leading=12)),
        Paragraph('DETALLE DE PRODUCTOS', S('dp', fontSize=11, textColor=WHITE, fontName='Helvetica-Bold', alignment=TA_CENTER)),
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
    
    # Products table with auto-calculated pallets/m2
    prod_data = [[
        Paragraph('<b>Descripción producto</b>', sty['center']),
        Paragraph('<b>Ref. Fassa</b>', sty['center']),
        Paragraph('<b>Ud.</b>', sty['center']),
        Paragraph('<b>Cantidad</b>', sty['center']),
        Paragraph('<b>Palés</b>', sty['center']),
        Paragraph('<b>M²</b>', sty['center']),
        Paragraph('<b>Precio/ud (€)</b>', sty['right']),
        Paragraph('<b>Total (€)</b>', sty['right']),
    ]]
    total_pal = 0
    total_m2 = 0
    total_kg = 0
    # Agregar líneas con mismo SKU para la tabla de detalle (evita duplicados)
    detail_lines = _aggregate_lines_by_sku(lines)
    for line in detail_lines:
        qty_waste = math.ceil(line['qty'] * (1 + offer['waste_pct']/100))
        sub = line['price'] * qty_waste

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
            Paragraph(str(pal) if pal else '—', sty['center']),
            Paragraph(f'{m2:,.0f}' if m2 else '—', sty['center']),
            Paragraph(f"€ {line['price']:,.2f}", sty['right']),
            Paragraph(f"€ {sub:,.2f}", sty['right']),
        ])
    
    prod_data.append(['', '', '', '', '', '',
        Paragraph('<b>TOTAL</b>', sty['bold']),
        Paragraph(f"<b>€ {total_eur:,.2f}</b>", sty['right']),
    ])
    prod_data.append(['', '', '', '', '', '',
        Paragraph(f'TC EUR/USD: {fx:.4f}', sty['small']),
        Paragraph(f"<b>$ {total_usd:,.2f} USD</b>", sty['right']),
    ])
    
    prod_tbl = Table(prod_data, colWidths=[W*0.30, W*0.14, W*0.06, W*0.09, W*0.06, W*0.10, W*0.11, W*0.14])
    prod_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), NAVY),
        ('TEXTCOLOR', (0,0), (-1,0), WHITE),
        ('BACKGROUND', (0,-2), (-1,-1), GOLD_SOFT),
        ('BACKGROUND', (0,-3), (-1,-3), BONE),
        ('LINEBELOW', (0,0), (-1,-4), 0.3, colors.HexColor('#CCCCCC')),
        ('LINEABOVE', (0,-3), (-1,-3), 1, GOLD),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('LEFTPADDING', (0,0), (-1,-1), 4),
        ('RIGHTPADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(prod_tbl)
    story.append(Spacer(1, 6*mm))
    
    # Logistics summary
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
         Paragraph('<b>Incoterm</b>', sty['bold']), Paragraph(offer['incoterm'] or 'EXW', sty['p'])],
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
    total_pal = total_kg = total_m2 = 0
    fam_breakdown: dict[str, int] = {}
    for l in lines:
        total_pal += int(_num(l.get('pallets_logistic', 0)))
        total_kg += _num(l.get('weight_total_kg', 0))
        total_m2 += _num(l.get('m2_total', 0))
        f = l.get('family') or '?'
        fam_breakdown[f] = fam_breakdown.get(f, 0) + 1
    cont = estimate_containers(total_pal, total_kg, fam_breakdown)

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
    if cont:
        pal_occ = f"{cont['pallet_occupancy']*100:.0f}%"
        wei_occ = f"{cont['weight_occupancy']*100:.0f}%"
        cont_rows = [
            ['Recomendación', f"{cont['units']} x {cont['recommended']}"],
            ['Palés totales', f"{total_pal} (capacidad {cont['pallets_capacity_per_unit']}/cont.)"],
            ['Ocupación palés', pal_occ],
            ['Peso total', f"{total_kg:,.0f} kg (capacidad {cont['weight_capacity_per_unit_kg']:,}/cont.)"],
            ['Ocupación peso', wei_occ],
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
    fx = _num(offer['fx_rate']) or 1.085
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
    cont_txt = f"{cont['units']} x {cont['recommended']}" if cont else 'Por determinar'
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
         _num(data.get('wastePct', 0)), _num(data.get('margin', 33)), 1.085,
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


if __name__ == '__main__':
    with app.app_context():
        init_db()
        seed_db()
    host = os.environ.get('FLASK_HOST', '127.0.0.1')
    port = int(os.environ.get('FLASK_PORT', '5001'))
    app.run(debug=_debug, host=host, port=port)
