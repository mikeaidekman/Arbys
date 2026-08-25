"""Engine configuration and write reliability.

SQLite in the default `delete` journal mode allows one writer and blocks
readers while it writes. Five services in this app write concurrently — the
discovery pass alone bursts one transaction per changed group — which produced
18 "database is locked" errors and 6 QueuePool timeouts in a single day, each
one swallowed by a persistence path that must never break a trade.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from arbys.db import session as db_session


@pytest.fixture(autouse=True)
def _isolated_engine(tmp_path: Path):
    os.environ["ARBYS_DB_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'wal.db'}"
    db_session.reset_engine()
    yield
    db_session.reset_engine()
    os.environ.pop("ARBYS_DB_URL", None)


async def test_sqlite_connections_are_wal_with_a_busy_timeout():
    """WAL is the whole point: it lets readers and the writer proceed together.
    `synchronous=NORMAL` is safe under WAL and stops an fsync-per-commit from
    holding the write lock through the discovery burst."""
    engine = db_session.configure_engine()
    async with engine.connect() as conn:
        journal = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()
        synchronous = (await conn.exec_driver_sql("PRAGMA synchronous")).scalar()
        busy = (await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar()
    assert str(journal).lower() == "wal"
    # NORMAL is 1; FULL (the default this replaces) is 2.
    assert int(synchronous) == 1
    assert int(busy) == 15000


async def test_postgres_url_gets_no_sqlite_pragmas():
    """ARBYS_DB_URL may point at Postgres, where PRAGMA is a syntax error.
    The gate is on the dialect, and getting it wrong breaks startup entirely.

    No server is contacted: the engine is lazy, so building it is enough to
    prove the pragma hook was not registered for this dialect.
    """
    engine = db_session.configure_engine(
        "postgresql+asyncpg://user:pw@localhost:5432/nowhere"
    )
    assert engine.dialect.name == "postgresql"
    # The connect hook is only attached for sqlite; nothing to fire here.
    assert not db_session.sqlite_pragmas_registered(engine)


async def test_sqlite_pool_is_sized_for_the_concurrent_writers():
    """Six QueuePool timeouts say the default 5 + 10 is genuinely exhausted.

    A file-backed aiosqlite URL gets `AsyncAdaptedQueuePool`, so both figures
    are real. `_max_overflow` is private, deliberately — SQLAlchemy exposes no
    public accessor for it, and asserting the configured value is worth more
    than skipping the check.
    """
    engine = db_session.configure_engine()
    assert engine.pool.size() == 10
    assert engine.pool._max_overflow == 20


async def test_memory_sqlite_still_configures():
    """An in-memory URL must not be handed QueuePool sizing.

    Verified 2026-08-25: SQLAlchemy 2.0.51 gives `:memory:` a `StaticPool` and
    raises `TypeError: Invalid argument(s) 'pool_size','max_overflow'` if they
    are passed, so this is a real crash the guard prevents, not a hypothetical.
    """
    engine = db_session.configure_engine("sqlite+aiosqlite:///:memory:")
    assert engine.dialect.name == "sqlite"


async def test_run_write_commits_and_reports_success():
    from arbys.db import repositories as repo
    from arbys.db.session import create_all, run_write

    await create_all()
    ok = await run_write(
        "test.account", lambda s: repo.ensure_paper_account(s, "acct-ok")
    )
    assert ok is True
    assert db_session.dropped_write_stats()["dropped_writes"] == 0


async def test_run_write_retries_a_locked_database_then_succeeds():
    """`database is locked` is transient by nature: the point of retrying is
    that the burst which caused it has usually passed a few milliseconds
    later. A retried write must not be counted as dropped."""
    from sqlalchemy.exc import OperationalError

    from arbys.db.session import create_all, run_write

    await create_all()
    db_session.reset_dropped_writes()
    attempts = {"n": 0}

    async def work(_session):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OperationalError("stmt", {}, Exception("database is locked"))

    ok = await run_write("test.retry", work)
    assert ok is True
    assert attempts["n"] == 3
    assert db_session.dropped_write_stats()["dropped_writes"] == 0


async def test_run_write_counts_a_write_it_finally_abandons():
    """The defect this whole plan exists for: a swallowed write used to be
    indistinguishable from a successful one."""
    from sqlalchemy.exc import OperationalError

    from arbys.db.session import create_all, run_write

    await create_all()
    db_session.reset_dropped_writes()

    async def always_locked(_session):
        raise OperationalError("stmt", {}, Exception("database is locked"))

    ok = await run_write("test.always", always_locked)
    assert ok is False
    stats = db_session.dropped_write_stats()
    assert stats["dropped_writes"] == 1
    assert "test.always" in str(stats["last_dropped_write"])


async def test_run_write_does_not_retry_a_non_lock_error():
    """A genuine bug must not be retried three times and filed as
    contention — it is counted once and logged as itself."""
    from arbys.db.session import create_all, run_write

    await create_all()
    db_session.reset_dropped_writes()
    attempts = {"n": 0}

    async def boom(_session):
        attempts["n"] += 1
        raise ValueError("not a lock")

    ok = await run_write("test.bug", boom)
    assert ok is False
    assert attempts["n"] == 1
    assert db_session.dropped_write_stats()["dropped_writes"] == 1


async def test_run_write_never_raises():
    """Callers rely on this: a broken trade is worse than an unrecorded one."""
    from arbys.db.session import create_all, run_write

    await create_all()

    async def boom(_session):
        raise RuntimeError("anything")

    assert await run_write("test.noraise", boom) is False
