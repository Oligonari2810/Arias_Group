"""logistics aggregated model — 40HC operational params + floor_stowage

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-25

Cambios derivados de la calibración operativa de Arias (sesión 2026-04-25
con Oliver) sobre el motor logístico:

1) `container_profiles.40HC.payload_kg`: 28000 → 26500
   Payload nominal real Fassa para 40HC. El valor 28000 venía de un dato
   genérico de la industria; en las cargas reales con placas de yeso, el
   tope efectivo está en 26500 kg.

2) Nueva columna `container_profiles.floor_stowage_factor` (default 1.0,
   pero 0.80 para 20'/40'/40HC).
   Representa la fracción del suelo interior aprovechable en estiba real
   con palés de placa. El 20% restante es margen para sujeción, accesos
   y palés irregulares. Antes la lógica del motor asumía 100% de uso de
   suelo, lo que sobreestimaba la capacidad geométrica.

3) Combinación: 40HC operativo Arias = 22,67 m² · 23.850 kg · 68,44 m³
   - usable_floor = 12,06 × 2,35 × 0,80 = 22,67 m²
   - usable_payload = 26500 × 0,90 = 23.850 kg
   - usable_cbm = 12,03 × 2,35 × 2,69 × 0,90 = 68,44 m³

Idempotente vía SQLAlchemy: ADD COLUMN no falla si la columna ya existe
en SQLite (gracias a batch_alter_table); en Postgres es transaccional.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) Nueva columna floor_stowage_factor.
    with op.batch_alter_table('container_profiles') as b:
        b.add_column(sa.Column(
            'floor_stowage_factor',
            sa.Numeric(5, 4),
            nullable=False,
            server_default='1.0000',
        ))

    # 2) 40HC con valores operativos Arias.
    op.execute("""
        UPDATE container_profiles
        SET payload_kg = 26500,
            floor_stowage_factor = 0.80
        WHERE type = '40HC'
    """)
    # 40' estándar mismo régimen (cargas similares).
    op.execute("""
        UPDATE container_profiles
        SET payload_kg = 26500,
            floor_stowage_factor = 0.80
        WHERE type = '40'
    """)
    # 20' menos crítico para placas (no se usa habitualmente con placas largas),
    # pero por consistencia aplicamos también 0.80 al suelo. Payload deja igual
    # (21500 sigue siendo el nominal del 20').
    op.execute("""
        UPDATE container_profiles
        SET floor_stowage_factor = 0.80
        WHERE type = '20'
    """)


def downgrade() -> None:
    op.execute("UPDATE container_profiles SET payload_kg = 28000 WHERE type IN ('40', '40HC')")
    with op.batch_alter_table('container_profiles') as b:
        b.drop_column('floor_stowage_factor')
