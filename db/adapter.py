"""sqlite3.Connection-compatible wrapper over psycopg3.

Purpose: keep app.py's ~100 existing queries working unchanged when the
backend switches to Postgres. The adapter exposes the subset of
sqlite3.Connection surface that app.py uses:

    db.execute(sql, params)        → _CursorResult
    db.executescript(sql)          → splits by ';' and runs each
    db.commit()                    → commit current tx
    db.rollback()                  → rollback current tx
    db.close()                     → close connection
    db.row_factory = sqlite3.Row   → no-op (always dict-like rows)
    db.last_insert_rowid()         → PostgreSQL lastval()

Rows are returned as CompatRow objects which support both `row['col']`
and `row[0]` access patterns, ensuring compatibility with existing code.

SQL is translated: `?` placeholders become `%s`, and well-known
SQLite-isms are converted to their PostgreSQL cousins.
"""
from __future__ import annotations

import os
import re

import psycopg
from psycopg.rows import dict_row

from .compat import translate_sql, CompatRow, wrap_rows, wrap_row


class _CursorResult:
    """Minimal wrapper exposing fetchone / fetchall / rowcount.
    
    Supports direct iteration for SQLite compatibility:
        for row in db.execute(sql):  # works!
            ...
    """

    def __init__(self, cursor):
        self._cur = cursor

    def fetchone(self):
        row = self._cur.fetchone()
        return wrap_row(row)

    def fetchall(self):
        rows = self._cur.fetchall()
        return wrap_rows(rows)

    def __iter__(self):
        """Enable direct iteration: for row in db.execute(sql)"""
        return self

    def __next__(self):
        """Fetch next row during iteration"""
        row = self._cur.fetchone()
        if row is None:
            raise StopIteration
        return wrap_row(row)

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self):
        # psycopg exposes INSERT ... RETURNING, not lastrowid.
        return None


class PgConnection:
    """sqlite3.Connection-compatible facade over psycopg3."""

    def __init__(self, dsn: str):
        self._dsn = _normalize_dsn(dsn)
        self._conn = psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False)

    # sqlite3-compatible methods ---------------------------------------

    def execute(self, sql: str, params=None) -> _CursorResult:
        translated = translate_sql(sql)
        cur = self._conn.cursor()
        cur.execute(translated, params or ())
        return _CursorResult(cur)

    def executescript(self, sql: str):
        cur = self._conn.cursor()
        for stmt in [s.strip() for s in sql.split(';') if s.strip()]:
            cur.execute(translate_sql(stmt))
        return _CursorResult(cur)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if not self._conn.closed:
            self._conn.close()

    def last_insert_rowid(self):
        """Get the last inserted ID (SQLite compatibility)."""
        cur = self._conn.cursor()
        cur.execute("SELECT lastval()")
        result = cur.fetchone()
        return result[0] if result else None

    # No-op setter — app.py assigns `g.db.row_factory = sqlite3.Row` in places.
    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, _value):
        pass


def _normalize_dsn(url: str) -> str:
    """psycopg accepts `postgresql://...`; strip SQLAlchemy's `+psycopg` suffix."""
    if url.startswith('postgresql+psycopg://'):
        return 'postgresql://' + url[len('postgresql+psycopg://'):]
    return url


def connect() -> PgConnection:
    """Open a connection using DATABASE_URL (required)."""
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise RuntimeError('DATABASE_URL is not set; cannot open Postgres adapter.')
    return PgConnection(url)


def is_configured() -> bool:
    """True when DATABASE_URL is set — app.py uses this to pick the backend."""
    return bool(os.environ.get('DATABASE_URL'))
