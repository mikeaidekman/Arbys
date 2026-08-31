"""One instance, enforced by the database rather than by a paragraph.

Two instances would each hold their own quote book, each publish
opportunities, and each submit tickets against the same venue credentials —
two ledgers agreeing with each other about nothing. Hosting makes this
reachable: a platform restarts on its own schedule and a rolling deploy starts
a second machine before retiring the first.

The Postgres cases need a real server and are skipped without one. The SQLite
case is the important one to keep green everywhere: **the suite must never
require Postgres**, or it stops being runnable on a laptop, which is a worse
problem than the one this guards against.
"""

from __future__ import annotations

import os

import pytest

from arbys.db import session as db_session
from arbys.db.session import acquire_singleton_lock, release_singleton_lock

PG_URL = os.environ.get("ARBYS_TEST_PG_URL")
needs_pg = pytest.mark.skipif(not PG_URL, reason="ARBYS_TEST_PG_URL not set")


@pytest.fixture(autouse=True)
async def _clean():
    yield
    await release_singleton_lock()
    db_session.reset_engine()


async def test_the_lock_is_a_no_op_on_sqlite(tmp_path):
    """SQLite has no advisory locks and needs none — a file database is not
    reachable from a second machine. Returning True keeps every dev run and
    the whole test suite free of a Postgres dependency."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'lock.db'}"
    assert await acquire_singleton_lock(url) is True
    # Idempotent, and a second call in the same process is not a second
    # instance — bootstrap() may run more than once in tests.
    assert await acquire_singleton_lock(url) is True


@needs_pg
async def test_the_lock_is_taken_and_released_on_postgres():
    assert PG_URL is not None
    assert await acquire_singleton_lock(PG_URL) is True
    await release_singleton_lock()
    # Released means genuinely available again, not merely forgotten.
    assert await acquire_singleton_lock(PG_URL) is True


@needs_pg
async def test_a_second_holder_is_refused():
    """The guarantee this file exists for.

    A second *connection* stands in for a second process: the lock is
    session-scoped, so a distinct connection is exactly what a second instance
    would present. `pg_try_advisory_lock` returns false rather than blocking —
    blocking would turn a misconfiguration into a hang that reads as a slow
    boot.
    """
    assert PG_URL is not None
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from arbys.db.session import SINGLETON_LOCK_KEY, normalise_asyncpg_url

    assert await acquire_singleton_lock(PG_URL) is True

    rival = create_async_engine(normalise_asyncpg_url(PG_URL), poolclass=NullPool)
    try:
        async with rival.connect() as conn:
            got = await conn.scalar(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": SINGLETON_LOCK_KEY}
            )
            assert got is False, "a second instance was allowed to take the lock"
    finally:
        await rival.dispose()


@needs_pg
async def test_the_lock_survives_pool_churn():
    """The lock connection is held outside the main pool.

    A session-scoped advisory lock dies with its connection. If it rode the
    application pool, a recycle would drop it silently — the app would keep
    running while the invariant it depends on had quietly stopped holding,
    which is the exact failure this design avoids by holding a dedicated
    connection.
    """
    assert PG_URL is not None
    from sqlalchemy import text

    from arbys.db.session import SINGLETON_LOCK_KEY

    assert await acquire_singleton_lock(PG_URL) is True

    # Churn the application engine hard, then confirm the lock is still ours.
    db_session.configure_engine(PG_URL)
    engine = db_session.get_engine()
    for _ in range(3):
        async with engine.connect() as conn:
            await conn.scalar(text("SELECT 1"))
    await engine.dispose()

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from arbys.db.session import normalise_asyncpg_url

    rival = create_async_engine(normalise_asyncpg_url(PG_URL), poolclass=NullPool)
    try:
        async with rival.connect() as conn:
            got = await conn.scalar(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": SINGLETON_LOCK_KEY}
            )
            assert got is False, "the lock was lost to pool churn"
    finally:
        await rival.dispose()


@needs_pg
async def test_bootstrap_refuses_when_the_lock_is_held():
    """The invariant is named in the error so a platform health check can
    surface it. A refused boot is visible; a quietly duplicated one is not."""
    assert PG_URL is not None
    from arbys.backend import state as state_module

    assert await acquire_singleton_lock(PG_URL) is True

    prev = os.environ.get("ARBYS_DB_URL")
    os.environ["ARBYS_DB_URL"] = PG_URL
    try:
        # A fresh AppState stands in for a second process; the lock is already
        # held by this one, which is exactly what it would find.
        state_module.reset_state()
        rival = state_module.AppState()
        # Held by *this* process, so the module-level guard would short-circuit.
        # Drop the in-process record without releasing the database lock, so the
        # rival genuinely has to ask Postgres.
        held_conn, held_engine = db_session._lock_conn, db_session._lock_engine
        db_session._lock_conn = None
        db_session._lock_engine = None
        try:
            with pytest.raises(RuntimeError, match="singleton lock"):
                await rival.bootstrap()
        finally:
            db_session._lock_conn, db_session._lock_engine = held_conn, held_engine
    finally:
        state_module.reset_state()
        if prev is None:
            os.environ.pop("ARBYS_DB_URL", None)
        else:
            os.environ["ARBYS_DB_URL"] = prev
