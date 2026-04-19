"""Integration smoke tests for the db/ skeleton (SPEC-002a).

Skipped automatically when TEST_DATABASE_URL is not reachable — so local
devs without Docker running and GH Actions before the Postgres service
is provisioned do not fail the whole suite.
"""
from __future__ import annotations

import os

import pytest


def _testdb_url():
    return os.environ.get('TEST_DATABASE_URL')


pytestmark = pytest.mark.integration


def _skip_if_no_testdb():
    if not _testdb_url():
        pytest.skip('TEST_DATABASE_URL not set; start docker compose or configure env')


def test_get_engine_requires_url(monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    from db import get_engine
    from db.engine import reset_engine_cache
    reset_engine_cache()
    with pytest.raises(RuntimeError, match='DATABASE_URL'):
        get_engine()


def test_get_engine_connects_to_test_db():
    _skip_if_no_testdb()
    from sqlalchemy import text
    from db import get_engine
    eng = get_engine(_testdb_url())
    with eng.connect() as conn:
        one = conn.execute(text('SELECT 1')).scalar()
    assert one == 1


def test_session_scope_commits_on_success():
    _skip_if_no_testdb()
    from sqlalchemy import text
    from db import session_scope, get_engine

    url = _testdb_url()
    eng = get_engine(url)
    with eng.begin() as setup:
        setup.execute(text('CREATE TABLE IF NOT EXISTS _spec002a_smoke '
                           '(id SERIAL PRIMARY KEY, note TEXT)'))
        setup.execute(text('TRUNCATE _spec002a_smoke'))

    with session_scope(url) as conn:
        conn.execute(text("INSERT INTO _spec002a_smoke (note) VALUES ('ok')"))

    with eng.connect() as conn:
        count = conn.execute(text('SELECT COUNT(*) FROM _spec002a_smoke')).scalar()
    assert count == 1


def test_session_scope_rolls_back_on_error():
    _skip_if_no_testdb()
    from sqlalchemy import text
    from db import session_scope, get_engine

    url = _testdb_url()
    eng = get_engine(url)
    with eng.begin() as setup:
        setup.execute(text('CREATE TABLE IF NOT EXISTS _spec002a_rollback '
                           '(id SERIAL PRIMARY KEY, note TEXT)'))
        setup.execute(text('TRUNCATE _spec002a_rollback'))

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with session_scope(url) as conn:
            conn.execute(text("INSERT INTO _spec002a_rollback (note) VALUES ('nope')"))
            raise Boom()

    with eng.connect() as conn:
        count = conn.execute(text('SELECT COUNT(*) FROM _spec002a_rollback')).scalar()
    assert count == 0
