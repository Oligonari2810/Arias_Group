"""catalog real data from Fassa PDFs — schema + backfill desde tarifas oficiales

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-25

Datos extraídos directamente de:
  - Tarifa Gypsotech Abril 2026 (placas, pastas, tornillos, trampillas,
    accesorios, cintas, GypsoCOMETE)
  - Anexo Gypsotech Noviembre 2025 (perfiles, accesorios, tornillos,
    trampillas con uds/palé real y kg/ml para perfiles)

Cambios:

1) NUEVAS COLUMNAS estructuradas en `products` (21 columnas):
   - Dimensiones: length_mm, width_mm, thickness_mm, dim_a_mm, dim_b_mm,
     dim_c_mm, diameter_mm, espesor_acero_mm
   - Empaquetado: kg_per_ml, box_units, peso_saco_kg
   - Comercial: min_order_qty, dispo_tarancon, tariff_origen,
     pvp_calliano_eur, pvp_onda_lerida_eur, pvp_antas_eur
   - Metadata: norma_text, color, description_long, tiempo_trabajab_min

2) DROP columnas cache que generan drift:
   - products.pvp_per_m2 (calculable)
   - products.precio_arias_m2 (calculable)

3) Backfill se hace en la migración SQLite equivalente (app.py
   `_catalog_real_data_from_pdf_20260425`). Esta migración Alembic solo se
   ejecutaría en cutover a Postgres.

REGLA OFERTAS INMUTABLES: solo `products`. Las ofertas en pending_offers
quedan intactas — sus precios y pesos están congelados en lines_json.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0008'
down_revision: Union[str, None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_COLUMNS = [
    # (name, type)
    ('length_mm', sa.Integer()),
    ('width_mm', sa.Integer()),
    ('thickness_mm', sa.Numeric(6, 2)),
    ('dim_a_mm', sa.Numeric(6, 2)),
    ('dim_b_mm', sa.Numeric(6, 2)),
    ('dim_c_mm', sa.Numeric(6, 2)),
    ('diameter_mm', sa.Numeric(6, 2)),
    ('espesor_acero_mm', sa.Numeric(5, 2)),
    ('kg_per_ml', sa.Numeric(8, 3)),
    ('box_units', sa.Integer()),
    ('peso_saco_kg', sa.Numeric(6, 2)),
    ('min_order_qty', sa.Integer()),
    ('dispo_tarancon', sa.String(16)),
    ('tariff_origen', sa.String(32)),
    ('pvp_calliano_eur', sa.Numeric(10, 2)),
    ('pvp_onda_lerida_eur', sa.Numeric(10, 2)),
    ('pvp_antas_eur', sa.Numeric(10, 2)),
    ('norma_text', sa.String(64)),
    ('color', sa.String(32)),
    ('description_long', sa.Text()),
    ('tiempo_trabajab_min', sa.Integer()),
]


def upgrade() -> None:
    for name, typ in _NEW_COLUMNS:
        op.add_column('products', sa.Column(name, typ, nullable=True))

    # Default 'green' para dispo_tarancon (la app SQLite hace lo mismo en backfill).
    op.execute("UPDATE products SET dispo_tarancon = 'green' WHERE dispo_tarancon IS NULL")

    # GYPSOCOMETE: todos bajo pedido según PDF abril 2026.
    op.execute("UPDATE products SET dispo_tarancon = 'yellow' WHERE category = 'GYPSOCOMETE'")

    # Drop columnas cache.
    op.drop_column('products', 'pvp_per_m2')
    op.drop_column('products', 'precio_arias_m2')

    # NOTA: el backfill detallado (length_mm, kg_per_ml, etc.) lo aplica la
    # función SQLite `_catalog_real_data_from_pdf_20260425` ya en producción.
    # En cutover a Postgres, se importa la DB SQLite via migrator y los datos
    # ya vienen con todos los campos rellenos.


def downgrade() -> None:
    # Re-crear las columnas cache (vacías — el cache se regenera al usar la app).
    op.add_column('products', sa.Column('pvp_per_m2', sa.Numeric(10, 4), nullable=True))
    op.add_column('products', sa.Column('precio_arias_m2', sa.Numeric(10, 4), nullable=True))
    for name, _ in reversed(_NEW_COLUMNS):
        op.drop_column('products', name)
