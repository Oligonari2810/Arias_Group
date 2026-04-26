"""rendimientos kg/m² + L/m² pinturas + fix peso_saco bug

Revision ID: 0018
Revises: 0017
Create Date: 2026-04-25

Datos rendimiento Oliver 2026-04-25 (Tarifa Fassa Hispania):

1) Backfill rendimiento_kg_per_m2:
   - Fassajoint 1H/2H/3H/8H (351E1, 352E1, 353E1, 354, 356, 358U3, 560901): 0,4
   - KX 16 W2 (1259Y1): 15,0 (capa 10mm)
   - Gypsomaf (359, 360E1): 1,0 (capa 2mm)

2) Nueva columna rendimiento_l_per_m2 + backfill pinturas:
   - GYP010000 (14L), GYP010001 (5L): 0,20 L/m² (default 5 m²/L)

3) Fix peso_saco_kg (bug regex 0008 que matcheaba "5 kg" en "25 kg"):
   - 1259Y1, 359: 5,0 → 25,0
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0018'
down_revision: Union[str, None] = '0017'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('products', sa.Column('rendimiento_l_per_m2', sa.Numeric(6, 3), nullable=True))

    # Rendimiento kg/m²
    op.execute("""
        UPDATE products SET rendimiento_kg_per_m2 = 0.4
        WHERE sku IN ('351E1','352E1','353E1','354','356','358U3','560901')
    """)
    op.execute("UPDATE products SET rendimiento_kg_per_m2 = 15.0 WHERE sku = '1259Y1'")
    op.execute("UPDATE products SET rendimiento_kg_per_m2 = 1.0 WHERE sku IN ('359','360E1')")

    # Rendimiento L/m² pinturas
    op.execute("UPDATE products SET rendimiento_l_per_m2 = 0.20 WHERE sku IN ('GYP010000','GYP010001')")

    # Fix peso_saco_kg
    op.execute("UPDATE products SET peso_saco_kg = 25.0 WHERE sku IN ('1259Y1','359')")


def downgrade() -> None:
    op.execute("UPDATE products SET rendimiento_kg_per_m2 = NULL WHERE sku IN ('351E1','352E1','353E1','354','356','358U3','560901','1259Y1','359','360E1')")
    op.drop_column('products', 'rendimiento_l_per_m2')
