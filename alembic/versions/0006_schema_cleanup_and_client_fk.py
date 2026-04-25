"""schema cleanup — drop pickup_pricing + add pending_offers.client_id FK

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-25

Auditoría 2026-04-25 del schema:

1) `pickup_pricing` se diseñó para ofrecer precios alternativos por punto de
   recogida (Tarancón vs Fátima, etc.) pero nunca llegó a operativa. 0
   referencias en código fuera del CREATE original. Se elimina si está vacía
   (defensivo: si por algún motivo hay filas, abortar).

2) `pending_offers` enlaza con `clients` por texto (`client_name`), lo que
   rompe silenciosamente si renombras al cliente. Se añade FK explícita
   `client_id INTEGER REFERENCES clients(id)` y se backfillea por matching
   sobre `clients.name` o `clients.company`. `client_name` se mantiene como
   parte del contrato congelado de la oferta — no se borra.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) Drop pickup_pricing si está vacía. Si tiene filas, fallar fuerte
    #    para que el operador decida qué hacer con esos datos.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT COUNT(*) FROM pickup_pricing")).scalar() or 0
    if rows > 0:
        raise RuntimeError(
            f'pickup_pricing tiene {rows} filas — esta migración asume tabla '
            f'vacía. Revisa los datos antes de aplicar 0006.'
        )
    op.drop_table('pickup_pricing')

    # 2) Añadir client_id a pending_offers (Postgres permite ALTER con FK).
    # Sin CONSTRAINT NOT NULL aún: ofertas históricas pueden no matchear.
    # Una vez todos los nulls se resuelvan, se puede endurecer en 0007.
    op.add_column(
        'pending_offers',
        sa.Column('client_id', sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        'fk_pending_offers_client_id',
        source_table='pending_offers',
        referent_table='clients',
        local_cols=['client_id'],
        remote_cols=['id'],
    )
    op.create_index(
        'idx_pending_offers_client_id', 'pending_offers', ['client_id']
    )

    # Backfill: emparejar por texto exacto.
    op.execute("""
        UPDATE pending_offers
        SET client_id = (
            SELECT id FROM clients
            WHERE clients.name = pending_offers.client_name
               OR clients.company = pending_offers.client_name
            LIMIT 1
        )
        WHERE client_id IS NULL
    """)


def downgrade() -> None:
    op.drop_index('idx_pending_offers_client_id', table_name='pending_offers')
    op.drop_constraint(
        'fk_pending_offers_client_id', 'pending_offers', type_='foreignkey'
    )
    op.drop_column('pending_offers', 'client_id')
    # No re-creamos pickup_pricing en downgrade (era una tabla muerta).
