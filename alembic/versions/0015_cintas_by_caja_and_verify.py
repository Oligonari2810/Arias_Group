"""cintas by caja + verify weights — pesos kg/caja oficiales

Revision ID: 0015
Revises: 0014
Create Date: 2026-04-25

Decisión Oliver 2026-04-25: Fassa Hispania solo sirve cajas completas
(no rollos sueltos), aunque en factura al cliente final se cuente por
rollos. Por tanto la unidad operativa real de CINTAS es la CAJA.

1) CINTAS pasan a unit='caja', kg_per_unit = kg/caja (Tarifa Fassa).
   Corrige 4 SKUs cuyo peso por rollo era erróneo:
     - 304065 Guardavivos 30m:        10,00 → 17,38 kg/caja
     - 304076 Banda Estanca 70mm:     10,50 → 17,42 kg/caja
     - 304078 Malla FV 45m:            8,10 →  4,75 kg/caja
     - 304079 Malla FV 153m:           6,00 → 14,38 kg/caja

2) Quita marca [peso estimado] de 8 SKUs verificados oficialmente:
     - TORNILLOS: 301240, 304102, 304109, 304117
     - TRAMPILLAS: 304090, 301462, 301764
     - GYPSOCOMETE: 301602XL
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = '0015'
down_revision: Union[str, None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CINTAS_KG_PER_CAJA = [
    ('301121', 33.60), ('304056',  6.72), ('304057', 12.00),
    ('304058', 11.50), ('304064',  3.00), ('304065', 17.38),
    ('304075', 11.58), ('304076', 17.42), ('304078',  4.75),
    ('304079', 14.38), ('700960',  8.10),
]
_VERIFIED = ['301240', '304102', '304109', '304117',
             '304090', '301462', '301764', '301602XL']


def upgrade() -> None:
    for sku, kg in _CINTAS_KG_PER_CAJA:
        op.execute(
            f"UPDATE products SET unit='caja', kg_per_unit={kg} "
            f"WHERE sku='{sku}' AND category='CINTAS'"
        )
    sku_list = ', '.join(f"'{s}'" for s in _VERIFIED)
    op.execute(f"""
        UPDATE products
        SET notes = TRIM(REPLACE(REPLACE(COALESCE(notes,''),
            '[peso estimado 2026-04-24]', ''),
            '[peso estimado]', ''))
        WHERE sku IN ({sku_list})
    """)


def downgrade() -> None:
    op.execute("UPDATE products SET unit='rollo' WHERE category='CINTAS'")
