"""catalog real weights — pesos reales aportados por Oliver 2026-04-25

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-25

Pesos derivados de proformas + albaranes (no publicados por Fassa en tarifa).
Aplicados a 18 SKUs de cintas, tornillos, accesorios, trampillas, GypsoCOMETE.

Regla de unidad de venta (según catálogo Fassa Abril 2026):
  TORNILLOS, ACCESORIOS, GYPSOCOMETE → caja/embalaje
  CINTAS                              → rollo
  TRAMPILLAS                          → unidad

Para accesorios y GypsoCOMETE, Oliver aportó el peso por unidad individual.
Aquí se multiplica por uds/caja para alinear con el motor logístico, que
calcula `qty × kg_per_unit` con qty en la unidad de venta.

REGLA OFERTAS INMUTABLES: solo `products`. Las ofertas existentes mantienen
el peso congelado en `lines_json` y `total_logistic_eur`.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = '0009'
down_revision: Union[str, None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_WEIGHTS = [
    # CINTAS (kg/rollo)
    ('304056', 0.28),
    ('304057', 0.60),
    ('304058', 1.15),
    ('301121', 5.60),
    ('304075', 0.53),
    # TORNILLOS (kg/caja)
    ('304101', 1.40),
    ('304104', 1.84),
    ('304115', 1.45),
    ('304134', 1.05),
    ('301244', 1.15),
    # TRAMPILLAS (kg/unidad)
    ('304081', 1.25),
    ('304082', 1.80),
    ('304086', 2.10),
    # ACCESORIOS (kg/caja, ya multiplicado por uds/caja)
    ('304015', 1.25),     # 0,05 × 25
    ('304021', 6.00),     # 0,06 × 100
    ('1091001Y', 65.00),  # 0,65 × 100
    # GYPSOCOMETE (kg/embalaje, ya multiplicado por uds/embalaje)
    ('301600', 0.90),     # 0,45 × 2
    ('301605XL', 12.00),  # 2,40 × 5
]


def upgrade() -> None:
    for sku, kg in _WEIGHTS:
        op.execute(
            f"UPDATE products SET kg_per_unit = {kg}, "
            f"notes = TRIM(REPLACE(REPLACE(COALESCE(notes, ''), "
            f"'[peso estimado 2026-04-24]', ''), '[peso estimado]', '')) "
            f"WHERE sku = '{sku}'"
        )


def downgrade() -> None:
    # No re-aplicamos los pesos antiguos (eran estimados, los nuevos son reales).
    pass
