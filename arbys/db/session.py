"""Database configuration and session management."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DEFAULT_DB_URL = "sqlite+aiosqlite:///./arbys-local.db"


def _get_db_url() -> str:
    return os.environ.get("ARBYS_DB_URL", DEFAULT_DB_URL)


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# journal_mode is persisted in the database file; the other two are
# per-connection and so must be re-issued on every checkout.
_SQLITE_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("busy_timeout", "15000"),
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


def configure_engine(url: str | None = None) -> AsyncEngine:
    """(Re)configure the global engine. Call this from tests to swap DB URL."""
    global _engine, _session_factory
    resolved = url or _get_db_url()
    kwargs: dict[str, object] = {"pool_pre_ping": True, "future": True}
    backend = make_url(resolved).get_backend_name()
    # An in-memory SQLite database uses a pool class that rejects these, and
    # sizing it would be meaningless anyway.
    if ":memory:" not in resolved:
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

