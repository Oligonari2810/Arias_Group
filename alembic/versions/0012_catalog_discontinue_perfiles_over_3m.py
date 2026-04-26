"""catalog discontinue perfiles >3m — montantes 3.590mm y 3.990mm fuera

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-25

Decisión Oliver 2026-04-25 (continuación del filtro >2.600mm para placas):
descartar también todos los perfiles cuya longitud supere los 3.000 mm.

Mismo criterio que placas: la dimensión hace inviable la optimización del
contenedor 40HC. 8 perfiles montante (Montante 48/35, 70/37, 90/40, 125/47,
150/47 en longitudes 3.590mm y 3.990mm) pasan a is_active=0.

Verificado: 0 ofertas activas usan estos SKUs.

REGLA OFERTAS INMUTABLES: solo `products`. Si en futuro alguna oferta
histórica los cita, NO se actualiza.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = '0012'
down_revision: Union[str, None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        UPDATE products
        SET is_active = FALSE,
            discontinued_reason = 'oversized_logistics'
        WHERE category = 'PERFILES'
          AND length_mm > 3000
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE products
        SET is_active = TRUE,
            discontinued_reason = NULL
        WHERE category = 'PERFILES'
          AND length_mm > 3000
          AND discontinued_reason = 'oversized_logistics'
    """)
