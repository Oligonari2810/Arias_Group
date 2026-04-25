"""catalog pdf-extras + discontinued schema

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-25

1) Backfill datos adicionales del Anexo Gypsotech Nov 2025:
   - units_per_pallet REAL para perfiles (480/250/200/etc según modelo).
   - min_order_qty (10/4/8/30).
   - dim_a_mm, dim_b_mm, dim_c_mm, espesor_acero_mm (sección física).

2) Backfill box_units explícito para 40 SKUs de accesorios, cintas, GypsoCOMETE.

3) Nuevas columnas para SKUs descartados:
   - is_active (BOOLEAN, default TRUE)
   - discontinued_reason (TEXT)
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0010'
down_revision: Union[str, None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('products', sa.Column('is_active', sa.Boolean(), nullable=True))
    op.add_column('products', sa.Column('discontinued_reason', sa.String(64), nullable=True))
    op.execute("UPDATE products SET is_active = TRUE WHERE is_active IS NULL")
    op.alter_column('products', 'is_active', nullable=False, server_default=sa.text('TRUE'))
    op.create_index(
        'idx_products_is_active', 'products', ['is_active'],
        postgresql_where=sa.text('is_active = FALSE'),
    )

    # NOTE: el backfill detallado (units_per_pallet, dimensiones, box_units) se
    # aplica por la función SQLite `_catalog_pdf_extras_and_discontinued_20260425`
    # ya en producción. En cutover a Postgres se importan via migrator y los
    # datos vienen ya rellenos.


def downgrade() -> None:
    op.drop_index('idx_products_is_active', table_name='products')
    op.drop_column('products', 'discontinued_reason')
    op.drop_column('products', 'is_active')
