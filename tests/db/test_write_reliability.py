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
