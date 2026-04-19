"""sqlite3.Connection-compatible wrapper over psycopg3.

Purpose: keep app.py's ~100 existing queries working unchanged when the
backend switches to Postgres.  The adapter exposes the subset of
sqlite3.Connection surface that app.py uses:

    db.execute(sql, params)        → _CursorResult
    db.executescript(sql)          → splits by ';' and runs each
    db.commit()                    → commit current tx
    db.rollback()                  → rollback current tx
    db.close()                     → close connection
    db.row_factory = sqlite3.Row   → no-op (always dict-like rows)

Rows are returned as plain dicts (from psycopg's dict_row factory), which
supports `row['col']` access exactly like sqlite3.Row for the string-key
patterns used throughout app.py.  Positional row access (row[0]) is only
done in init_db's PRAGMA code and in PDF-building helpers that work on
Python tuples, neither of which touches this adapter in production.

SQL is lightly translated: `?` placeholders become `%s`, and well-known
SQLite-isms (`INSERT OR IGNORE`) are converted to their Postgres cousins
(`ON CONFLICT DO NOTHING`).  `PRAGMA` statements raise explicitly — they
must be guarded in app.py (init_db does so via dialect check).
"""
from __future__ import annotations

import os
import re

import psycopg
from psycopg.rows import dict_row


_INSERT_OR_IGNORE = re.compile(r'\bINSERT\s+OR\s+IGNORE\b', re.IGNORECASE)
_PLACEHOLDER = re.compile(r'\?')


def _translate_sql(sql: str) -> str:
    """Rewrite SQLite-specific SQL to Postgres-compatible SQL."""
    if 'PRAGMA' in sql.upper():
        raise NotImplementedError(
            'PRAGMA statements are not valid on Postgres. '
            'Guard their usage in app.py via a dialect check.'
        )
    # INSERT OR IGNORE → plain INSERT with ON CONFLICT DO NOTHING at the end.
    if _INSERT_OR_IGNORE.search(sql):
        sql = _INSERT_OR_IGNORE.sub('INSERT', sql)
        sql = sql.rstrip().rstrip(';')
        if 'ON CONFLICT' not in sql.upper():
            sql = sql + ' ON CONFLICT DO NOTHING'
    # Replace ? placeholders with %s.
    sql = _PLACEHOLDER.sub('%s', sql)
    return sql


def _normalize_dsn(url: str) -> str:
    """psycopg accepts `postgresql://...`; strip SQLAlchemy's `+psycopg` suffix."""
    if url.startswith('postgresql+psycopg://'):
        return 'postgresql://' + url[len('postgresql+psycopg://'):]
    return url


class _CursorResult:
    """Minimal wrapper exposing fetchone / fetchall / rowcount."""

    def __init__(self, cursor):
        self._cur = cursor

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self):
        # psycopg exposes INSERT ... RETURNING, not lastrowid. app.py doesn't
        # rely on this attribute; provided for API completeness.
        return None


class PgConnection:
    """sqlite3.Connection-compatible facade over psycopg3."""

    def __init__(self, dsn: str):
        self._dsn = _normalize_dsn(dsn)
        self._conn = psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False)

    # sqlite3-compatible methods ---------------------------------------

    def execute(self, sql: str, params=None) -> _CursorResult:
        cur = self._conn.cursor()
        cur.execute(_translate_sql(sql), params or ())
        return _CursorResult(cur)

    def executescript(self, sql: str):
        # Naive statement split; good enough for app.py's init_db DDL script.
        cur = self._conn.cursor()
        for stmt in [s.strip() for s in sql.split(';') if s.strip()]:
            cur.execute(_translate_sql(stmt))
        return _CursorResult(cur)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if not self._conn.closed:
            self._conn.close()

    # No-op setter — app.py assigns `g.db.row_factory = sqlite3.Row` in places.
    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, _value):
        pass


def connect() -> PgConnection:
    """Open a connection using DATABASE_URL (required)."""
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise RuntimeError('DATABASE_URL is not set; cannot open Postgres adapter.')
    return PgConnection(url)


def is_configured() -> bool:
    """True when DATABASE_URL is set — app.py uses this to pick the backend."""
    return bool(os.environ.get('DATABASE_URL'))
