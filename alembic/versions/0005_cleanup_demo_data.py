"""cleanup demo data — eliminar cliente Promotor Demo + Proyecto Demo

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-25

El seed inicial de seed_db() creaba un cliente "Promotor Demo / Arias
Group Demo" con un proyecto "Torre piloto - baños" para que la app no
arrancara vacía en demos. Tras la auditoría 2026-04-25 con datos reales
de producción, Oliver lo considera ruido visual y se quita.

Defensivo: solo elimina si no hay ofertas asociadas.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        DELETE FROM stage_events
        WHERE project_id IN (
            SELECT p.id FROM projects p
            JOIN clients c ON c.id = p.client_id
            WHERE c.email = 'demo@example.com' OR c.name = 'Promotor Demo'
        )
    """)
    op.execute("""
        DELETE FROM projects
        WHERE client_id IN (
            SELECT id FROM clients
            WHERE email = 'demo@example.com' OR name = 'Promotor Demo'
        )
        AND id NOT IN (
            SELECT DISTINCT project_id FROM project_quotes WHERE project_id IS NOT NULL
        )
    """)
    op.execute("""
        DELETE FROM clients
        WHERE (email = 'demo@example.com' OR name = 'Promotor Demo')
        AND id NOT IN (SELECT DISTINCT client_id FROM projects)
        AND name NOT IN (SELECT DISTINCT client_name FROM pending_offers)
    """)


def downgrade() -> None:
    # No re-creamos los datos demo (eran de seed; deliberadamente eliminados).
    pass
