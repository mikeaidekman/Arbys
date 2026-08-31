"""Database configuration and session management."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

log = logging.getLogger(__name__)

DEFAULT_DB_URL = "sqlite+aiosqlite:///./arbys-local.db"


def _get_db_url() -> str:
    return os.environ.get("ARBYS_DB_URL", DEFAULT_DB_URL)


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# journal_mode is persisted in the database file; the rest are per-connection
# and so must be re-issued on every checkout.
#
# foreign_keys is OFF by default in SQLite -- the constraints are parsed and
# recorded and then simply not checked. That is not a dev/prod difference worth
# tolerating: it means dev cannot fail on a write that prod will reject, and it
# already cost a hosted deploy where discovery dropped every batch on an FK
# violation it had never once hit locally. Postgres always enforces; now so
# does dev.
_SQLITE_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("busy_timeout", "15000"),
    ("foreign_keys", "ON"),
)

_PRAGMA_FLAG = "_arbys_sqlite_pragmas"


def sqlite_pragmas_registered(engine: AsyncEngine) -> bool:
    """Whether this engine carries the SQLite pragma hook. For tests."""
    # AsyncEngine is __slots__-based and rejects arbitrary attributes; the
    # flag lives on the underlying sync Engine, which is a plain object.
    return getattr(engine.sync_engine, _PRAGMA_FLAG, False)


def _register_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Apply WAL and friends to every new SQLite connection.

    Gated on the dialect: `ARBYS_DB_URL` may point at Postgres, where PRAGMA
    is a syntax error, so issuing these unconditionally would break startup.
    """
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        try:
            for name, value in _SQLITE_PRAGMAS:
                cursor.execute(f"PRAGMA {name}={value}")
        finally:
            cursor.close()

    setattr(engine.sync_engine, _PRAGMA_FLAG, True)


# Query parameters libpq understands and asyncpg does not. Managed Postgres
# providers hand you a libpq-flavoured URL — Neon's carries both of these — and
# asyncpg raises `TypeError: connect() got an unexpected keyword argument` on
# each, at connect time, on the first real deploy.
#
# `sslmode` has a direct equivalent so it is translated; `channel_binding` has
# none, and dropping it is safe because asyncpg negotiates SCRAM channel
# binding on a TLS connection regardless. Add to this list rather than asking
# whoever sets ARBYS_DB_URL to hand-edit a connection string — that is a
# footgun that only ever fires in production.
_LIBPQ_ONLY_PARAMS = ("channel_binding",)


def normalise_asyncpg_url(url: str) -> str:
    """Make a libpq-flavoured Postgres URL safe for asyncpg.

    A no-op for SQLite and for URLs that name any other driver.
    """
    parsed = make_url(url)
    if parsed.drivername != "postgresql+asyncpg":
        return url
    query = {k: v for k, v in parsed.query.items() if k not in _LIBPQ_ONLY_PARAMS}
    if "sslmode" in query:
        query["ssl"] = query.pop("sslmode")
    # `str(URL)` masks the password as `***` — round-tripping through it would
    # hand the driver a literal `***` and fail authentication with an error
    # naming the credentials rather than this function.
    return parsed.set(query=query).render_as_string(hide_password=False)


# One arbitrary 64-bit constant, shared by every instance of this app and by
# nothing else. Postgres advisory locks are a single global namespace per
# database, so the value only has to be stable and unlikely to collide.
# 0x41524259_53 is "ARBYS" in ASCII.
SINGLETON_LOCK_KEY = 0x4152425953

_lock_engine: AsyncEngine | None = None
_lock_conn: object | None = None


async def acquire_singleton_lock(url: str | None = None) -> bool:
    """Take the process-wide singleton lock. False means someone else has it.

    Two instances of this app would each stream both venues, each publish
    opportunities, and each submit tickets against the same credentials —
    producing two ledgers that agree with each other about nothing. Hosting
    makes that reachable rather than theoretical: platforms restart on their
    own schedule and rolling deploys start a second machine before retiring
    the first.

    Three deliberate choices:

    * **`pg_try_advisory_lock`, never the blocking form.** Blocking would turn
      a misconfiguration into a hang that reads as a slow boot; failing
      immediately lets the caller say what is wrong.
    * **Session-scoped, on a dedicated connection outside the application
      pool.** The lock dies with its connection, so riding the app pool would
      let a routine recycle drop it silently — the app would keep running
      while the invariant it depends on had quietly stopped holding.
    * **A no-op on SQLite**, gated on the dialect exactly as the WAL pragmas
      above are. A file database is not reachable from a second machine, and
      requiring Postgres would make the test suite unrunnable on a laptop —
      a worse problem than the one this guards against.

    Idempotent: calling it twice in one process is not a second instance.
    """
    global _lock_engine, _lock_conn
    resolved = normalise_asyncpg_url(url or _get_db_url())
    if make_url(resolved).get_backend_name() != "postgresql":
        return True
    if _lock_conn is not None:
        return True

    from sqlalchemy import text
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(resolved, poolclass=NullPool)
    conn = await engine.connect()
    try:
        got = await conn.scalar(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": SINGLETON_LOCK_KEY}
        )
    except Exception:
        await conn.close()
        await engine.dispose()
        raise
    if not got:
        await conn.close()
        await engine.dispose()
        return False
    _lock_engine, _lock_conn = engine, conn
    return True


async def release_singleton_lock() -> None:
    """Drop the lock and its connection. Safe to call when nothing is held.

    Rarely needed in production — a dying process releases a session-scoped
    lock on its own, which is the property that stops a hard-killed instance
    from wedging it forever. Tests need it, and an orderly shutdown may as well
    use it.
    """
    global _lock_engine, _lock_conn
    conn, engine = _lock_conn, _lock_engine
    _lock_conn, _lock_engine = None, None
    if conn is not None:
        with contextlib.suppress(Exception):
            await conn.close()  # type: ignore[attr-defined]
    if engine is not None:
        with contextlib.suppress(Exception):
            await engine.dispose()


def configure_engine(url: str | None = None) -> AsyncEngine:
    """(Re)configure the global engine. Call this from tests to swap DB URL."""
    global _engine, _session_factory
    resolved = normalise_asyncpg_url(url or _get_db_url())
    kwargs: dict[str, object] = {"pool_pre_ping": True, "future": True}
    parsed = make_url(resolved)
    backend = parsed.get_backend_name()
    # An in-memory SQLite database uses a pool class that rejects these, and
    # sizing it would be meaningless anyway.
    #
    # `:memory:` is not the only spelling: `sqlite+aiosqlite://` (no database
    # part at all) is also legal and also in-memory, and `make_url(...).database`
    # is `None` for it rather than the literal string `":memory:"` -- so a
    # substring check on the raw URL misses it and `create_async_engine` dies
    # with `TypeError: Invalid argument(s) 'pool_size','max_overflow'`, which
    # is exactly the crash this guard exists to prevent. Check the parsed
    # database instead of the raw URL text.
    if parsed.database not in (None, ":memory:"):
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
    _engine = create_async_engine(resolved, **kwargs)
    if backend == "sqlite":
        _register_sqlite_pragmas(_engine)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    global _engine, _session_factory
    _engine = None
    _session_factory = None


def get_engine() -> AsyncEngine:
    if _engine is None:
        configure_engine()
    assert _engine is not None
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        configure_engine()
    assert _session_factory is not None
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all() -> None:
    """Create schema directly from ORM metadata (for tests / bootstrapping)."""
    from .models import Base

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ----------------------------------------------------------------------
# Write reliability: retry lock contention, count what we finally abandon.
#
# Every write path in the paper broker and ticket service swallows its
# exception on purpose -- persistence must never break a trade -- but that
# made a lost row indistinguishable from a successful one. WAL (see the
# pragmas above) substantially reduces lock contention but does not
# eliminate it, so a retry still earns its keep.
# ----------------------------------------------------------------------

_WRITE_ATTEMPTS = 3
_WRITE_BACKOFF_S = 0.05

_dropped_writes = 0
_last_dropped_write: str | None = None


def dropped_write_stats() -> dict[str, object]:
    """Counts of persistence writes this process gave up on.

    Non-zero means the ledger is incomplete. Every write path in the paper
    broker and ticket service swallows its exception on purpose -- persistence
    must never break a trade -- so without this a lost row is invisible and the
    audit page presents a partial history as the whole.

    This counts **transactions abandoned, not rows lost**: one dropped
    `discovery.groups` batch is up to `GROUP_WRITE_BATCH` missing groups
    reported as 1, and one dropped `ticket.rejected_legs` write is N missing
    leg rows reported as 1. Treat `dropped_writes` as a lower bound on how
    much data is actually missing, not an exact row count.
    """
    return {"dropped_writes": _dropped_writes, "last_dropped_write": _last_dropped_write}


def reset_dropped_writes() -> None:
    global _dropped_writes, _last_dropped_write
    _dropped_writes = 0
    _last_dropped_write = None


def _record_dropped_write(context: str, exc: BaseException) -> None:
    global _dropped_writes, _last_dropped_write
    _dropped_writes += 1
    _last_dropped_write = f"{context}: {type(exc).__name__}: {exc}"


def _is_transient(exc: BaseException) -> bool:
    """Whether `exc` is worth retrying rather than counting as a dropped write.

    Two shapes, both observed in a single day against this app: a `database
    is locked` `OperationalError` from SQLite lock contention, and a
    `sqlalchemy.exc.TimeoutError` from the QueuePool running out of
    connections under load. `TimeoutError` is **not** a subclass of
    `OperationalError` (verified against SQLAlchemy 2.0.51), so a naive
    `isinstance(exc, OperationalError)` check silently abandons a pool
    timeout on the first attempt -- despite it being the most transient of
    the two: the pool drains as in-flight work finishes, with no lock to
    wait out.

    Anything else is a real bug and must not be retried three times and
    filed as contention -- it is counted once and logged as itself.
    """
    if isinstance(exc, SATimeoutError):
        return True
    return isinstance(exc, OperationalError) and "database is locked" in str(exc).lower()


async def run_write(
    context: str, work: Callable[[AsyncSession], Awaitable[None]]
) -> bool:
    """Run one write transaction, retrying transient failures. Never raises.

    Returns True if it committed. Returns False -- and counts a dropped write
    -- if it gave up. `context` names the write for the counter, e.g.
    "ticket.status" or "sink.on_fill".

    Only a `database is locked` `OperationalError` or a QueuePool
    `sqlalchemy.exc.TimeoutError` is retried (see `_is_transient`). Anything
    else is a real bug and is counted once rather than masked as contention.
    """
    for attempt in range(1, _WRITE_ATTEMPTS + 1):
        try:
            async with session_scope() as session:
                await work(session)
            return True
        except Exception as exc:  # callers must never see this -- see run_write's docstring
            if _is_transient(exc) and attempt < _WRITE_ATTEMPTS:
                await asyncio.sleep(_WRITE_BACKOFF_S * attempt)
                continue
            log.exception("persistence write abandoned (%s)", context)
            _record_dropped_write(context, exc)
            return False
    return False

