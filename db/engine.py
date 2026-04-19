"""SQLAlchemy engine factory.

Single source of truth for the DB connection. Other modules (session,
Alembic env, tests) go through `get_engine()` — never build their own
engine directly.
"""
from __future__ import annotations

import os
import threading

from sqlalchemy import Engine, create_engine


_DEFAULT_POOL_SIZE = 5
_DEFAULT_MAX_OVERFLOW = 10
_DEFAULT_POOL_TIMEOUT = 30

# Module-level cache keyed by (url, echo). Protected by a lock because
# Flask threads may race on the first engine construction.
_engines: dict[tuple[str, bool], Engine] = {}
_lock = threading.Lock()


def _resolve_url(url: str | None) -> str:
    resolved = url or os.environ.get('DATABASE_URL')
    if not resolved:
        raise RuntimeError(
            'DATABASE_URL is not set. Copy .env.example to .env and fill it in, '
            'or export DATABASE_URL before running.'
        )
    return resolved


def get_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """Return a SQLAlchemy engine for the given URL.

    Engines are cached per (url, echo) so repeated calls during a process
    reuse the same connection pool.
    """
    key = (_resolve_url(url), echo)
    eng = _engines.get(key)
    if eng is not None:
        return eng
    with _lock:
        eng = _engines.get(key)
        if eng is not None:
            return eng
        eng = create_engine(
            key[0],
            pool_size=_DEFAULT_POOL_SIZE,
            max_overflow=_DEFAULT_MAX_OVERFLOW,
            pool_timeout=_DEFAULT_POOL_TIMEOUT,
            pool_pre_ping=True,       # drop dead connections gracefully
            future=True,
            echo=echo,
        )
        _engines[key] = eng
        return eng


def reset_engine_cache() -> None:
    """Dispose every cached engine and empty the cache.

    Intended for tests that need a fresh pool; production code should not
    call this.
    """
    with _lock:
        for eng in _engines.values():
            eng.dispose()
        _engines.clear()
