"""sku 560901 (Fassajoint 1H 25kg) + columna rendimiento_kg_per_m2

Revision ID: 0017
Revises: 0016
Create Date: 2026-04-25

Decisión Oliver 2026-04-25:

1) Crear SKU 560901 — Fassajoint 1H formato saco 25 kg, origen Tarancón.
   Datos Tarifa Fassa Hispania Abr 2026:
     - PVP 23,63 €/saco
     - Arias 11,22 €/saco (50% + 5% extra estándar)
     - 50 sacos/palé · 1.250 kg/palé neto
     - Color blanco · Norma UNE EN 13963
     - Tiempo trabajabilidad 60 min

2) Añadir columna `rendimiento_kg_per_m2` (REAL) a products.
   Permite registrar el consumo unitario por m² para pastas, pinturas,
   adhesivos. Cotizador podrá auto-calcular cantidades cuando se cotice
   por m² (futuro). Fassajoint 1H = 0,4 kg/m² default.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0017'
down_revision: Union[str, None] = '0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('products', sa.Column('rendimiento_kg_per_m2', sa.Numeric(6, 3), nullable=True))

    # Insertar SKU 560901
    op.execute("""
        INSERT INTO products (
            sku, name, category, subfamily, source_catalog, unit,
            unit_price_eur, kg_per_unit, units_per_pallet,
            pvp_eur_unit, precio_arias_eur_unit, discount_pct, discount_extra_pct,
            peso_saco_kg, color, norma_text, dispo_tarancon,
            tariff_origen, tiempo_trabajab_min, rendimiento_kg_per_m2,
            box_units, is_active, notes
        ) VALUES (
            '560901', 'FASSAJOINT 1H BLANCO 25kg', 'PASTAS', 'Pastas de juntas',
            'Gypsotech Abr2026', 'saco',
            11.22, 25.0, 50,
            23.63, 11.22, 50.0, 5.0,
            25.0, 'Blanco', 'UNE EN 13963', 'green',
            'Tarancón', 60, 0.4,
            1, TRUE, '25 kg/saco · 1.250 kg/palé · Tarifa Abr 2026'
        )
    """)

    # Backfill rendimiento para 351E1 (Fassajoint 1H 10kg, mismo rendimiento)
    op.execute("""
        UPDATE products SET rendimiento_kg_per_m2 = 0.4
        WHERE sku = '351E1' AND rendimiento_kg_per_m2 IS NULL
    """)


def downgrade() -> None:
    op.execute("DELETE FROM products WHERE sku = '560901'")
    op.drop_column('products', 'rendimiento_kg_per_m2')
