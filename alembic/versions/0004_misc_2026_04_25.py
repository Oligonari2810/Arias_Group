"""misc 2026-04-25 — eliminar MM 30 + corregir FX EUR/USD a 1.18

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-25

Decisión Oliver:
1) MM 30 GRIS (SKU 611Y1A) — descatalogado de la operativa Arias.
   Seguro eliminar: verificado que no aparece en order_lines ni en
   pending_offers.lines_json. Si en algún Postgres futuro hay referencias,
   la migración salta el DELETE (defensivo).

2) FX EUR/USD oficial corregido a 1.18 (no 1.085 como decía el dato
   "Manual Abril 2026"). El 1.085 estaba desfasado; el cambio real de
   mercado al 2026-04-25 es 1.18 según Oliver. Las ofertas históricas
   con FX 1.18 (#11, #13, #16, #18, #19) estaban bien — eran correctas
   desde el inicio.

   Inserta nuevo registro en fx_rates (mantiene histórico) y actualiza
   app_settings.fx_eur_usd para que el cotizador lo lea por default.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) MM 30 GRIS — eliminar si no está en uso.
    op.execute("""
        DELETE FROM products
        WHERE sku = '611Y1A'
          AND NOT EXISTS (
            SELECT 1 FROM order_lines WHERE order_lines.sku = '611Y1A'
          )
          AND NOT EXISTS (
            SELECT 1 FROM pending_offers
            WHERE pending_offers.lines_json::text LIKE '%611Y1A%'
          )
    """)

    # 2) FX EUR/USD a 1.18.
    op.execute("""
        INSERT INTO fx_rates (base_currency, target_currency, rate, updated_at, source)
        VALUES ('EUR', 'USD', 1.18, NOW(), 'Manual 2026-04-25 (corrección Oliver)')
    """)
    op.execute("""
        INSERT INTO app_settings (key, value, updated_at)
        VALUES ('fx_eur_usd', '"1.18"'::jsonb, NOW())
        ON CONFLICT (key) DO UPDATE
        SET value = '"1.18"'::jsonb, updated_at = NOW()
    """)


def downgrade() -> None:
    # No re-inserta MM 30 (era una decisión comercial). Solo revierte FX.
    op.execute("""
        INSERT INTO fx_rates (base_currency, target_currency, rate, updated_at, source)
        VALUES ('EUR', 'USD', 1.085, NOW(), 'Rollback to Manual Abril 2026')
    """)
    op.execute("""
        UPDATE app_settings SET value = '"1.085"'::jsonb, updated_at = NOW()
        WHERE key = 'fx_eur_usd'
    """)
