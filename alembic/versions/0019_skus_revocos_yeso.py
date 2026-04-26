"""SKUs revocos + yeso proyectar — KS 9, MH 19, Yesodur 1

Revision ID: 0019
Revises: 0018
Create Date: 2026-04-25

3 SKUs nuevos en PASTAS — Tarifa Fassa Hispania Abr 2026 (Oliver):

  405Y1  KS 9 Revoco fondo       3,23 € · 64 sacos/palé · 25 kg · Gris   · EN 998-1 · 13,3 kg/m²
  1060   MH 19 Revoco hidrófugo  3,74 € · 64 sacos/palé · 25 kg · Gris   · EN 998-1 · 15,0 kg/m²
  1264Y1 Yesodur 1 Yeso proy.    4,40 € · 80 sacos/palé · 17 kg · Blanco · EN 13279-1 · 10,0 kg/m²

Origen Tarancón. Suministro por palé completo. Descuento Arias 0,475.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = '0019'
down_revision: Union[str, None] = '0018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO products (sku, name, category, subfamily, source_catalog, unit,
            unit_price_eur, kg_per_unit, units_per_pallet,
            pvp_eur_unit, precio_arias_eur_unit, discount_pct, discount_extra_pct,
            peso_saco_kg, color, norma_text, dispo_tarancon,
            tariff_origen, rendimiento_kg_per_m2, box_units, is_active, notes)
        VALUES
            ('405Y1', 'KS 9 Revoco fondo Gris — 25kg', 'PASTAS', 'Revocos y morteros',
             'Gypsotech Abr2026', 'saco', 1.5342, 25.0, 64,
             3.23, 1.5342, 50.0, 5.0,
             25.0, 'Gris', 'EN 998-1', 'green', 'Tarancón', 13.3, 1, TRUE,
             '25 kg/saco · 1600 kg/palé · Tarifa Abr 2026'),
            ('1060', 'MH 19 Revoco hidrófugo Gris — 25kg', 'PASTAS', 'Revocos y morteros',
             'Gypsotech Abr2026', 'saco', 1.7765, 25.0, 64,
             3.74, 1.7765, 50.0, 5.0,
             25.0, 'Gris', 'EN 998-1', 'green', 'Tarancón', 15.0, 1, TRUE,
             '25 kg/saco · 1600 kg/palé · Tarifa Abr 2026'),
            ('1264Y1', 'Yesodur 1 Yeso proyectar Blanco — 17kg', 'PASTAS', 'Yesos proyectar',
             'Gypsotech Abr2026', 'saco', 2.0900, 17.0, 80,
             4.40, 2.0900, 50.0, 5.0,
             17.0, 'Blanco', 'EN 13279-1', 'green', 'Tarancón', 10.0, 1, TRUE,
             '17 kg/saco · 1360 kg/palé · Tarifa Abr 2026')
    """)


def downgrade() -> None:
    op.execute("DELETE FROM products WHERE sku IN ('405Y1','1060','1264Y1')")
