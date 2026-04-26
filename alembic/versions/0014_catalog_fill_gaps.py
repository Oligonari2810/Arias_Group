"""catalog fill gaps — datos extraíbles del name

Revision ID: 0014
Revises: 0013
Create Date: 2026-04-25

Auditoría 2026-04-25 detectó huecos en columnas estructuradas que sí
estaban presentes en el name del SKU. Backfill automático:

1) PLACAS thickness_mm:
   - P00A000250A0, P00A000260A0 (STD BA 10mm) → 9.5
   - P00XL03200EI (EXTERNA LIGHT BR 13mm) → 12.5

2) TORNILLOS box_units (regex sobre name '— X.XXXud'):
   17 SKUs con 1.000 / 5.000 / 3.000 / 250 / 500 según el name.

3) ACCESORIOS — 1091001Y Cantonera Yeso → box_units=100.

4) CINTAS — 700960 Fassanet 160 → box_units=1.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = '0014'
down_revision: Union[str, None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) PLACAS thickness
    op.execute("UPDATE products SET thickness_mm = 9.5 WHERE sku IN ('P00A000250A0','P00A000260A0')")
    op.execute("UPDATE products SET thickness_mm = 12.5 WHERE sku = 'P00XL03200EI'")

    # 2) TORNILLOS box_units (Postgres regex)
    op.execute(r"""
        UPDATE products
        SET box_units = CAST(REPLACE(substring(name FROM '—\s*(\d[\d.]*)\s*ud'), '.', '') AS INTEGER)
        WHERE category = 'TORNILLOS'
          AND box_units IS NULL
          AND name ~ '—\s*\d[\d.]*\s*ud'
    """)

    # 3) ACCESORIOS Cantonera
    op.execute("UPDATE products SET box_units = 100 WHERE sku = '1091001Y' AND box_units IS NULL")

    # 4) CINTAS Fassanet
    op.execute("UPDATE products SET box_units = 1 WHERE sku = '700960' AND box_units IS NULL")


def downgrade() -> None:
    op.execute("UPDATE products SET thickness_mm = NULL WHERE sku IN ('P00A000250A0','P00A000260A0','P00XL03200EI')")
    op.execute("UPDATE products SET box_units = NULL WHERE sku IN ('1091001Y','700960')")
    op.execute("UPDATE products SET box_units = NULL WHERE category='TORNILLOS' AND name ~ '—\\s*\\d[\\d.]*\\s*ud'")
