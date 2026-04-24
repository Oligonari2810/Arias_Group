"""Data-access layer for Arias_Group.

SPEC-002a (this PR) ships the skeleton only: an engine factory, a minimal
session context manager, and nothing wired into `app.py` yet. Schema
migrations live in `alembic/` and are empty until SPEC-002b.

Public API:
    from db import get_engine, session_scope

Both helpers read `DATABASE_URL` from the environment; callers can pass an
override for tests.
"""
from .engine import get_engine
from .session import session_scope

__all__ = ['get_engine', 'session_scope']
