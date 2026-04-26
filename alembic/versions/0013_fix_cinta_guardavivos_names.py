"""fix cinta guardavivos names — corregir 304064/304065

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-25

2 SKUs de Cinta Guardavivos tenían el name mezclado con Malla FV
(50mm×45m — 54 rollos/caja para 304064, 50mm×153m — 12 para 304065).

Verificado contra Anexo Gypsotech Noviembre 2025: las cintas Guardavivos
correctas son:
  - 304064 = 50mm×12,5m, 10 rollos/caja
  - 304065 = 50mm×30m,   10 rollos/caja
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = '0013'
down_revision: Union[str, None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        UPDATE products SET name = 'Cinta Guardavivos 50mm×12,5m — 10 rollos/caja'
        WHERE sku = '304064'
    """)
    op.execute("""
        UPDATE products SET name = 'Cinta Guardavivos 50mm×30m — 10 rollos/caja'
        WHERE sku = '304065'
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE products SET name = 'Cinta Guardavivos 50mm×45m — 54 rollos/caja'
        WHERE sku = '304064'
    """)
    op.execute("""
        UPDATE products SET name = 'Cinta Guardavivos 50mm×153m — 12 rollos/caja'
        WHERE sku = '304065'
    """)
