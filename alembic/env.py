"""Alembic environment — online migrations only (we always have a DB).

Pulls the engine from `db.engine.get_engine`, which reads DATABASE_URL.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from db.engine import get_engine

# Alembic Config object, provides access to the .ini file values.
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No declarative metadata yet — SPEC-002b will introduce the schema.
# Keeping target_metadata=None means `alembic revision --autogenerate`
# won't work until 002b lands, which is intentional.
target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — not used; fail loudly if invoked."""
    raise RuntimeError(
        'Offline mode is not supported. Use online mode against a live Postgres '
        '(docker compose up -d postgres or Render managed Postgres).'
    )


def run_migrations_online() -> None:
    """Run migrations in 'online' mode with a live connection."""
    engine = get_engine()
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
