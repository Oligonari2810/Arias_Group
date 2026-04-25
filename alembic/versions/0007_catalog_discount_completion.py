"""catalog discount completion — backfill discount_extra_pct + corregir 2 FASSACOL

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-25

Auditoría de catálogo 2026-04-25 (Oliver):

1) `products.discount_extra_pct` estaba NULL en 191 SKUs aunque el precio Arias
   YA reflejaba el descuento compuesto 50%+5% (ratio 0,475 sobre PVP). Backfill
   a 5.0 para que la metadata declare lo que el precio ya hace.

2) 2 SKUs FASSACOL tenían `precio_arias_eur_unit` desviado del descuento
   estándar — no era política comercial diferente, era un error de carga:
     - 1773Y1A FASSACOL MULTI GRIS: 5,83 → 5,52 € (PVP 11,63 × 0,475)
     - 1775Y1A FASSACOL FLEX GRIS:  6,60 → 6,27 € (PVP 13,20 × 0,475)
   También se actualiza `unit_price_eur` (campo legacy sincronizado con
   `precio_arias_eur_unit` por contrato del motor).

REGLA OFERTAS INMUTABLES: las ofertas que ya contienen estos SKUs (#13, #18,
#19, #21) NO se tocan. `pending_offers.lines_json` y `total_final_eur` son
contractuales — el cliente firmó con el precio del momento.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = '0007'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) Backfill metadata.
    op.execute("""
        UPDATE products SET discount_extra_pct = 5.0
        WHERE discount_extra_pct IS NULL
    """)

    # 2) Corregir las 2 FASSACOL.
    op.execute("""
        UPDATE products
        SET unit_price_eur = 5.52,
            precio_arias_eur_unit = 5.52
        WHERE sku = '1773Y1A'
          AND ABS(precio_arias_eur_unit - 5.52) > 0.01
    """)
    op.execute("""
        UPDATE products
        SET unit_price_eur = 6.27,
            precio_arias_eur_unit = 6.27
        WHERE sku = '1775Y1A'
          AND ABS(precio_arias_eur_unit - 6.27) > 0.01
    """)


def downgrade() -> None:
    # Revertir solo precios FASSACOL (la metadata extra_pct=5.0 era ya correcta).
    op.execute("""
        UPDATE products
        SET unit_price_eur = 5.83,
            precio_arias_eur_unit = 5.83
        WHERE sku = '1773Y1A'
    """)
    op.execute("""
        UPDATE products
        SET unit_price_eur = 6.60,
            precio_arias_eur_unit = 6.60
        WHERE sku = '1775Y1A'
    """)
