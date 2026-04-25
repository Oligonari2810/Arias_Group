"""catalog discontinued SKUs — placas >2.600mm + TC 47 5.300mm

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-25

Decisión Oliver 2026-04-25: descartar 22 SKUs cuya dimensión hace inviable
el flete optimizado en contenedor 40HC (12,03 m útiles):

  - 21 placas con longitud > 2.600 mm (STD, SIMPLY, AQUA H2, AQUASUPER,
    FOCUS, LIGNUM)
  - 1 perfil TC 47 Z1 — 5.300 mm

Criterio: NO por tipo de placa (EXTERNA, SILENS, LIGNUM, FASSATHERM se
mantienen — válidos para Caribe). Solo por DIMENSIÓN.

Verificado: 0 ofertas activas usan estos SKUs.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = '0011'
down_revision: Union[str, None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DISCONTINUED_SKUS = [
    'P00A000270A0', 'P00A003270A0', 'P00A003280A0', 'P00A000300A0',
    'P00A003300A0', 'P00A005300A0', 'P00A008300A0', 'P00A003320A0',
    'P00A003360A0', 'P00Y003280A0', 'P00Y003300A0', 'P00H003280A0',
    'P00H003300A0', 'P00H005300A0', 'P00W003300A0', 'P00W005300A0',
    'P00W008300A0', 'P00F005280A0', 'P00F003300A0', 'P00F005300A2',
    'P00LB03300AC', 'C174717530A',
]


def upgrade() -> None:
    sku_list = ', '.join(f"'{s}'" for s in _DISCONTINUED_SKUS)
    op.execute(f"""
        UPDATE products
        SET is_active = FALSE,
            discontinued_reason = 'oversized_logistics'
        WHERE sku IN ({sku_list})
    """)


def downgrade() -> None:
    sku_list = ', '.join(f"'{s}'" for s in _DISCONTINUED_SKUS)
    op.execute(f"""
        UPDATE products
        SET is_active = TRUE,
            discontinued_reason = NULL
        WHERE sku IN ({sku_list})
    """)
