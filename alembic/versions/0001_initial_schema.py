"""initial schema (SPEC-002b)

Revision ID: 0001
Revises:
Create Date: 2026-04-19

Mirrors the 19 tables that app.py creates in init_db() on SQLite, translated
to a proper Postgres schema:
- Monetary amounts:   NUMERIC(14, 4)
- Percentages:        NUMERIC(5, 4) / NUMERIC(6, 4)
- Physical quantities:NUMERIC(12, 3)
- Timestamps:         TIMESTAMPTZ NOT NULL DEFAULT NOW()
- JSON payloads:      JSONB
- Primary keys:       BIGSERIAL
- project_stage:      ENUM (26 verbatim values from app.py:29-56)
- Other enums:        go_no_go, incoterm, offer_status, user_role

Seed data is NOT inserted here — app.seed_db() continues to own that via
Python, so prod can tweak seeds without a schema migration.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# -- Enums (names must match type casts in future SQL) -----------------------

_PROJECT_STAGES = [
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

project_stage_enum = postgresql.ENUM(*_PROJECT_STAGES, name='project_stage_enum', create_type=False)
go_no_go_enum = postgresql.ENUM('PENDING', 'GO', 'NO_GO', name='go_no_go_enum', create_type=False)
incoterm_enum = postgresql.ENUM('EXW', 'FOB', 'CIF', 'DAP', 'CPT', 'DDP', name='incoterm_enum', create_type=False)
offer_status_enum = postgresql.ENUM(
    'pending', 'sent', 'accepted', 'rejected', 'expired',
    name='offer_status_enum', create_type=False,
)
user_role_enum = postgresql.ENUM('admin', 'viewer', 'sales', 'warehouse', 'accountant',
                                 name='user_role_enum', create_type=False)


# ---------------------------------------------------------------------------

def upgrade() -> None:
    bind = op.get_bind()

    # Explicit enum creation (using create_type=True inside create_table is
    # flaky across multi-table scenarios — we create types up-front).
    project_stage_enum.create(bind, checkfirst=True)
    go_no_go_enum.create(bind, checkfirst=True)
    incoterm_enum.create(bind, checkfirst=True)
    offer_status_enum.create(bind, checkfirst=True)
    user_role_enum.create(bind, checkfirst=True)

    # -- clients -----------------------------------------------------------
    op.create_table(
        'clients',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('company', sa.Text()),
        sa.Column('rnc', sa.String(32)),
        sa.Column('email', sa.String(255)),
        sa.Column('phone', sa.String(64)),
        sa.Column('address', sa.Text()),
        sa.Column('country', sa.String(64), server_default='República Dominicana'),
        sa.Column('score', sa.SmallInteger(), server_default='50',
                  nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('score BETWEEN 0 AND 100', name='clients_score_range'),
    )
    op.create_index('idx_clients_rnc', 'clients', ['rnc'],
                    postgresql_where=sa.text('rnc IS NOT NULL'))

    # -- products ----------------------------------------------------------
    op.create_table(
        'products',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column('sku', sa.String(64), nullable=False, unique=True),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('category', sa.String(64), nullable=False),
        sa.Column('subfamily', sa.String(64)),
        sa.Column('source_catalog', sa.String(64), nullable=False),
        sa.Column('unit', sa.String(32), nullable=False),
        sa.Column('unit_price_eur', sa.Numeric(14, 4), nullable=False),
        sa.Column('kg_per_unit', sa.Numeric(12, 3)),
        sa.Column('units_per_pallet', sa.Numeric(10, 2)),
        sa.Column('sqm_per_pallet', sa.Numeric(10, 3)),
        sa.Column('notes', sa.Text()),
        sa.Column('pvp_per_m2', sa.Numeric(14, 4)),
        sa.Column('precio_arias_m2', sa.Numeric(14, 4)),
        sa.Column('content_per_unit', sa.String(64)),
        sa.Column('pack_size', sa.String(64)),
        sa.Column('pvp_eur_unit', sa.Numeric(14, 4)),
        sa.Column('precio_arias_eur_unit', sa.Numeric(14, 4)),
        sa.Column('discount_pct', sa.Numeric(5, 2), server_default='50.00'),
    )
    op.create_index('idx_products_category_subfamily', 'products', ['category', 'subfamily'])
    op.create_index('idx_products_sku_lower', 'products', [sa.text('LOWER(sku)')])

    # -- systems -----------------------------------------------------------
    op.create_table(
        'systems',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column('name', sa.Text(), nullable=False, unique=True),
        sa.Column('description', sa.Text()),
        sa.Column('default_waste_pct', sa.Numeric(5, 4), server_default='0.0800'),
    )

    # -- system_components -------------------------------------------------
    op.create_table(
        'system_components',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column('system_id', sa.BigInteger(),
                  sa.ForeignKey('systems.id', ondelete='CASCADE'), nullable=False),
        sa.Column('product_id', sa.BigInteger(),
                  sa.ForeignKey('products.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('consumption_per_sqm', sa.Numeric(10, 4), nullable=False),
        sa.Column('waste_pct', sa.Numeric(5, 4), server_default='0.0000'),
        sa.UniqueConstraint('system_id', 'product_id', name='uniq_system_components_pair'),
    )

    # -- projects ----------------------------------------------------------
    op.create_table(
        'projects',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column('client_id', sa.BigInteger(),
                  sa.ForeignKey('clients.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('project_type', sa.String(64)),
        sa.Column('location', sa.Text()),
        sa.Column('area_sqm', sa.Numeric(12, 3), server_default='0'),
        sa.Column('stage', project_stage_enum, nullable=False, server_default='OPORTUNIDAD'),
        sa.Column('go_no_go', go_no_go_enum, server_default='PENDING'),
        sa.Column('incoterm', incoterm_enum, server_default='EXW'),
        sa.Column('fx_rate', sa.Numeric(10, 6), server_default='1.000000'),
        sa.Column('target_margin_pct', sa.Numeric(5, 4), server_default='0.3000'),
        sa.Column('freight_eur', sa.Numeric(14, 4), server_default='0'),
        sa.Column('customs_pct', sa.Numeric(5, 4), server_default='0.0000'),
        sa.Column('logistics_notes', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # -- project_quotes ----------------------------------------------------
    op.create_table(
        'project_quotes',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column('project_id', sa.BigInteger(),
                  sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('system_id', sa.BigInteger(),
                  sa.ForeignKey('systems.id', ondelete='SET NULL')),
        sa.Column('version_label', sa.String(32), nullable=False),
        sa.Column('area_sqm', sa.Numeric(12, 3), nullable=False),
        sa.Column('fx_rate', sa.Numeric(10, 6), nullable=False),
        sa.Column('freight_eur', sa.Numeric(14, 4), nullable=False),
        sa.Column('customs_pct', sa.Numeric(5, 4), nullable=False),
        sa.Column('target_margin_pct', sa.Numeric(5, 4), nullable=False),
        sa.Column('result_json', postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # -- stage_events ------------------------------------------------------
    op.create_table(
        'stage_events',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column('project_id', sa.BigInteger(),
                  sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('from_stage', project_stage_enum),
        sa.Column('to_stage', project_stage_enum, nullable=False),
        sa.Column('note', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('idx_stage_events_project_created',
                    'stage_events', ['project_id', sa.text('created_at DESC')])

    # -- shipping_routes ---------------------------------------------------
    op.create_table(
        'shipping_routes',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column('origin_port', sa.String(128), nullable=False),
        sa.Column('destination_port', sa.String(128), nullable=False),
        sa.Column('carrier', sa.String(64)),
        sa.Column('transit_days', sa.SmallInteger()),
        sa.Column('container_20_eur', sa.Numeric(12, 2)),
        sa.Column('container_40_eur', sa.Numeric(12, 2)),
        sa.Column('container_40hc_eur', sa.Numeric(12, 2)),
        sa.Column('insurance_pct', sa.Numeric(5, 4), server_default='0.0050'),
        sa.Column('valid_from', sa.Date()),
        sa.Column('valid_until', sa.Date()),
        sa.Column('notes', sa.Text()),
    )
    op.create_index('idx_shipping_routes_pair_valid',
                    'shipping_routes', ['origin_port', 'destination_port', 'valid_from'])

    # -- customs_rates -----------------------------------------------------
    op.create_table(
        'customs_rates',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column('country', sa.String(64), nullable=False),
        sa.Column('hs_code', sa.String(16), nullable=False),
        sa.Column('category', sa.String(128)),
        sa.Column('dai_pct', sa.Numeric(5, 4), server_default='0.0000'),
        sa.Column('itbis_pct', sa.Numeric(5, 4), server_default='0.1800'),
        sa.Column('other_pct', sa.Numeric(5, 4), server_default='0.0000'),
        sa.Column('notes', sa.Text()),
        sa.UniqueConstraint('country', 'hs_code', name='uniq_customs_rates_pair'),
    )

    # -- fx_rates ----------------------------------------------------------
    op.create_table(
        'fx_rates',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column('base_currency', sa.String(3), nullable=False, server_default='EUR'),
        sa.Column('target_currency', sa.String(3), nullable=False),
        sa.Column('rate', sa.Numeric(14, 8), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('source', sa.String(64), server_default='Manual'),
    )
    op.create_index('idx_fx_rates_latest',
                    'fx_rates', ['base_currency', 'target_currency', sa.text('updated_at DESC')])

    # -- users -------------------------------------------------------------
    op.create_table(
        'users',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column('username', sa.String(64), nullable=False, unique=True),
        sa.Column('password_hash', sa.String(255), nullable=False),
        sa.Column('role', user_role_enum, nullable=False, server_default='viewer'),
        sa.Column('full_name', sa.String(255)),
        sa.Column('email', sa.String(255), unique=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('TRUE')),
        sa.Column('last_login_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # -- pending_offers ----------------------------------------------------
    op.create_table(
        'pending_offers',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column('offer_number', sa.String(32), nullable=False),
        sa.Column('client_name', sa.Text(), nullable=False),
        sa.Column('project_name', sa.Text(), nullable=False),
        sa.Column('waste_pct', sa.Numeric(5, 4), server_default='0.0500'),
        sa.Column('margin_pct', sa.Numeric(5, 4), server_default='0.3300'),
        sa.Column('fx_rate', sa.Numeric(10, 6), server_default='1.085000'),
        sa.Column('lines_json', postgresql.JSONB(), nullable=False),
        sa.Column('total_product_eur', sa.Numeric(14, 4), server_default='0'),
        sa.Column('total_logistic_eur', sa.Numeric(14, 4), server_default='0'),
        sa.Column('total_final_eur', sa.Numeric(14, 4), server_default='0'),
        sa.Column('status', offer_status_enum, server_default='pending'),
        sa.Column('incoterm', incoterm_enum, server_default='EXW'),
        sa.Column('route_id', sa.BigInteger(),
                  sa.ForeignKey('shipping_routes.id', ondelete='SET NULL')),
        sa.Column('container_count', sa.SmallInteger(), server_default='0'),
        sa.Column('raw_hash', sa.String(64)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True)),
    )
    op.create_index('idx_pending_offers_status_created',
                    'pending_offers', ['status', sa.text('created_at DESC')])
    op.create_index('idx_pending_offers_client_project',
                    'pending_offers', ['client_name', 'project_name'])
    op.create_index('idx_pending_offers_raw_hash', 'pending_offers', ['raw_hash'],
                    postgresql_where=sa.text('raw_hash IS NOT NULL'))

    # -- order_lines -------------------------------------------------------
    op.create_table(
        'order_lines',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column('offer_id', sa.BigInteger(),
                  sa.ForeignKey('pending_offers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('sku', sa.String(64), nullable=False),
        sa.Column('name', sa.Text()),
        sa.Column('family', sa.String(32)),
        sa.Column('unit', sa.String(32)),
        sa.Column('qty_input', sa.Numeric(12, 3), nullable=False),
        sa.Column('qty_logistic', sa.Numeric(12, 3)),
        sa.Column('price_unit_eur', sa.Numeric(14, 4)),
        sa.Column('cost_exw_eur', sa.Numeric(14, 4)),
        sa.Column('m2_total', sa.Numeric(12, 3), server_default='0'),
        sa.Column('weight_total_kg', sa.Numeric(12, 3), server_default='0'),
        sa.Column('pallets_theoretical', sa.Numeric(12, 3), server_default='0'),
        sa.Column('pallets_logistic', sa.Integer(), server_default='0'),
        sa.Column('alerts_text', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('idx_order_lines_offer', 'order_lines', ['offer_id'])

    # -- audit_log ---------------------------------------------------------
    op.create_table(
        'audit_log',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        # offer_id is NOT a FK — audit log must survive offer deletion.
        sa.Column('offer_id', sa.BigInteger()),
        sa.Column('action', sa.String(64), nullable=False),
        sa.Column('detail', postgresql.JSONB()),
        sa.Column('username', sa.String(64)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('idx_audit_log_created', 'audit_log', [sa.text('created_at DESC')])
    op.create_index('idx_audit_log_offer_created',
                    'audit_log', ['offer_id', sa.text('created_at DESC')])

    # -- doc_sequences -----------------------------------------------------
    op.create_table(
        'doc_sequences',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column('prefix', sa.String(16), nullable=False, unique=True),
        sa.Column('last_number', sa.Integer(), nullable=False, server_default='0'),
    )

    # -- pickup_pricing ----------------------------------------------------
    op.create_table(
        'pickup_pricing',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column('product_id', sa.BigInteger(),
                  sa.ForeignKey('products.id', ondelete='CASCADE'), nullable=False),
        sa.Column('pickup_point', sa.String(128), nullable=False),
        sa.Column('price_eur_unit', sa.Numeric(14, 4), nullable=False),
        sa.Column('notes', sa.Text()),
        sa.UniqueConstraint('product_id', 'pickup_point', name='uniq_pickup_pricing_pair'),
    )

    # -- family_defaults ---------------------------------------------------
    op.create_table(
        'family_defaults',
        sa.Column('category', sa.String(64), primary_key=True),
        sa.Column('discount_pct', sa.Numeric(5, 2), nullable=False, server_default='50'),
        sa.Column('display_order', sa.SmallInteger(), server_default='99'),
        sa.Column('notes', sa.Text()),
    )

    # -- price_history -----------------------------------------------------
    op.create_table(
        'price_history',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column('product_id', sa.BigInteger(),
                  sa.ForeignKey('products.id', ondelete='CASCADE'), nullable=False),
        sa.Column('field', sa.String(32), nullable=False),
        sa.Column('old_value', sa.Numeric(14, 4)),
        sa.Column('new_value', sa.Numeric(14, 4)),
        sa.Column('user_id', sa.BigInteger(),
                  sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('username', sa.String(64)),
        sa.Column('changed_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('notes', sa.Text()),
    )
    op.create_index('idx_price_history_product_changed',
                    'price_history', ['product_id', sa.text('changed_at DESC')])

    # -- app_settings ------------------------------------------------------
    op.create_table(
        'app_settings',
        sa.Column('key', sa.String(128), primary_key=True),
        sa.Column('value', postgresql.JSONB(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True)),
    )


# ---------------------------------------------------------------------------

def downgrade() -> None:
    # Drop in reverse dependency order.
    for table in [
        'app_settings',
        'price_history',
        'family_defaults',
        'pickup_pricing',
        'doc_sequences',
        'audit_log',
        'order_lines',
        'pending_offers',
        'users',
        'fx_rates',
        'customs_rates',
        'shipping_routes',
        'stage_events',
        'project_quotes',
        'projects',
        'system_components',
        'systems',
        'products',
        'clients',
    ]:
        op.drop_table(table)

    bind = op.get_bind()
    for enum in (user_role_enum, offer_status_enum, incoterm_enum,
                 go_no_go_enum, project_stage_enum):
        enum.drop(bind, checkfirst=True)
