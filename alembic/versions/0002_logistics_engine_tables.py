"""logistics engine tables (container_profiles, pallet_profiles) + product overrides

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-24

Spec-002b creó la migración inicial pero quedaron fuera dos tablas que
init_db() crea en SQLite y que el motor logístico (Fase A/B/C) consume:
container_profiles y pallet_profiles. También quedaron fuera columnas
extra que se añaden a products / pending_offers / family_defaults vía
_safe_add_column en init_db.

Esta migración cubre el gap. Como init_db() hace early-return en
Postgres (spec-002c), Alembic es la única vía para crear el schema en
ese backend.

Seed: insertamos los perfiles canónicos (3 contenedores ISO, 6 familias
de palé) con la misma data que app.py usa en SQLite. Idempotente vía
ON CONFLICT DO NOTHING.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- container_profiles ------------------------------------------------
    op.create_table(
        'container_profiles',
        sa.Column('type', sa.String(8), primary_key=True),  # '20', '40', '40HC'
        sa.Column('inner_length_m', sa.Numeric(8, 3), nullable=False),
        sa.Column('inner_width_m', sa.Numeric(8, 3), nullable=False),
        sa.Column('inner_height_m', sa.Numeric(8, 3), nullable=False),
        sa.Column('payload_kg', sa.Numeric(10, 2), nullable=False),
        sa.Column('door_clearance_m', sa.Numeric(6, 3), nullable=False, server_default='0.300'),
        sa.Column('stowage_factor', sa.Numeric(5, 4), nullable=False, server_default='0.9000'),
        sa.Column('notes', sa.Text()),
    )

    # -- pallet_profiles ---------------------------------------------------
    op.create_table(
        'pallet_profiles',
        sa.Column('category', sa.String(64), primary_key=True),
        sa.Column('pallet_length_m', sa.Numeric(8, 3), nullable=False),
        sa.Column('pallet_width_m', sa.Numeric(8, 3), nullable=False),
        sa.Column('pallet_height_m', sa.Numeric(8, 3), nullable=False),
        sa.Column('stackable_levels', sa.SmallInteger(), nullable=False, server_default='1'),
        sa.Column('allow_mix_floor', sa.SmallInteger(), nullable=False, server_default='1'),
        sa.Column('notes', sa.Text()),
    )

    # -- products: columnas extra que init_db añade vía _safe_add_column ---
    with op.batch_alter_table('products') as b:
        b.add_column(sa.Column('discount_extra_pct', sa.Numeric(5, 2)))
        b.add_column(sa.Column('pallet_length_m', sa.Numeric(8, 3)))
        b.add_column(sa.Column('pallet_width_m', sa.Numeric(8, 3)))
        b.add_column(sa.Column('pallet_height_m', sa.Numeric(8, 3)))
        b.add_column(sa.Column('pallet_weight_kg', sa.Numeric(10, 3)))
        b.add_column(sa.Column('stackable_levels', sa.SmallInteger()))
        b.add_column(sa.Column('allow_mix_floor', sa.SmallInteger()))

    # -- pending_offers: validity_days (auditoría 2026-04-23) --------------
    op.add_column(
        'pending_offers',
        sa.Column('validity_days', sa.SmallInteger(), nullable=False, server_default='30'),
    )

    # -- family_defaults: discount_extra_pct -------------------------------
    op.add_column(
        'family_defaults',
        sa.Column('discount_extra_pct', sa.Numeric(5, 2), nullable=False, server_default='5'),
    )

    # -- Seed: container_profiles (3 perfiles ISO) -------------------------
    # Mismos valores que app.py inserta en SQLite. Si ya existen, ON CONFLICT
    # DO NOTHING para no romper en re-runs.
    op.execute("""
        INSERT INTO container_profiles
            (type, inner_length_m, inner_width_m, inner_height_m, payload_kg,
             door_clearance_m, stowage_factor, notes)
        VALUES
            ('20',   5.900, 2.350, 2.390, 21500, 0.300, 0.9000,
             'Contenedor 20 pies estándar'),
            ('40',  12.030, 2.350, 2.390, 28000, 0.300, 0.9000,
             'Contenedor 40 pies estándar — payload operativo Arias'),
            ('40HC',12.030, 2.350, 2.690, 28000, 0.300, 0.9000,
             'Contenedor 40 High Cube — 30cm más alto que 40 estándar')
        ON CONFLICT (type) DO NOTHING
    """)

    # -- Seed: pallet_profiles (6 familias) --------------------------------
    op.execute("""
        INSERT INTO pallet_profiles
            (category, pallet_length_m, pallet_width_m, pallet_height_m,
             stackable_levels, allow_mix_floor, notes)
        VALUES
            ('PLACAS',     2.500, 1.200, 0.300, 3, 1,
             'Palé placa yeso 1200x2500 — apilable 3 niveles, el hueco lateral (1.15m) y los pisos superiores admiten mezcla'),
            ('PERFILES',   3.000, 0.800, 0.350, 2, 1,
             'Palé perfiles metálicos — apilable 2 niveles, mezcla suelo OK'),
            ('TORNILLOS',  1.200, 0.800, 1.000, 2, 1,
             'Palé cajas de tornillería'),
            ('CINTAS',     1.200, 0.800, 1.000, 2, 1,
             'Palé cintas y mallas'),
            ('PASTAS',     1.200, 0.800, 1.200, 1, 1,
             'Palé sacos de pasta — sin apilado (peso)'),
            ('ACCESORIOS', 1.200, 0.800, 1.000, 2, 1,
             'Palé accesorios varios')
        ON CONFLICT (category) DO NOTHING
    """)


def downgrade() -> None:
    op.drop_column('family_defaults', 'discount_extra_pct')
    op.drop_column('pending_offers', 'validity_days')
    with op.batch_alter_table('products') as b:
        for col in (
            'allow_mix_floor', 'stackable_levels', 'pallet_weight_kg',
            'pallet_height_m', 'pallet_width_m', 'pallet_length_m',
            'discount_extra_pct',
        ):
            b.drop_column(col)
    op.drop_table('pallet_profiles')
    op.drop_table('container_profiles')
