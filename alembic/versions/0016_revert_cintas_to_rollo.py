"""revert cintas to unit=rollo (corrige bug de 0015)

Revision ID: 0016
Revises: 0015
Create Date: 2026-04-25

La migración 0015 cambió erróneamente unit='rollo' → unit='caja' en CINTAS.
Eso rompía el flujo de venta: el cliente cotiza por rollo (unidad de venta
real), Fassa solo sirve cajas completas (restricción logística, NO
unidad comercial). Con unit='caja' una oferta histórica con qty=100
(rollos) se recalcularía como 100 × kg_caja → ~10x el peso real.

Esta migración revierte:
  - unit pasa de 'caja' a 'rollo'
  - kg_per_unit = kg/rollo (= kg_caja_oficial / box_units)
  - Mantiene los pesos kg/caja oficiales aportados por Oliver como
    fuente de verdad, traducidos a kg/rollo dividiendo por uds/caja.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = '0016'
down_revision: Union[str, None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CINTAS_KG_PER_ROLLO = [
    ('301121', 5.6000),
    ('304056', 0.2800),
    ('304057', 0.6000),
    ('304058', 1.1500),
    ('304064', 0.3000),
    ('304065', 1.7380),
    ('304075', 0.5264),
    ('304076', 1.1613),
    ('304078', 0.0880),
    ('304079', 1.1983),
    ('700960', 8.1000),
]


def upgrade() -> None:
    for sku, kg in _CINTAS_KG_PER_ROLLO:
        op.execute(
            f"UPDATE products SET unit='rollo', kg_per_unit={kg} "
            f"WHERE sku='{sku}' AND category='CINTAS'"
        )


def downgrade() -> None:
    pass  # No revertimos el revert; la 0015 quedó cancelada operativamente.
