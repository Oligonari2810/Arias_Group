"""Session / connection context managers."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Connection

from .engine import get_engine


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Connection]:
    """Yield a transactional SQLAlchemy Connection.

    Commits on clean exit, rolls back on exception. Callers that want
    auto-commit per-statement behaviour should call `engine.begin()`
    themselves; this helper is for multi-statement transactional units.
    """
    engine = get_engine(url)
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            yield conn
            trans.commit()
        except Exception:
            trans.rollback()
            raise
