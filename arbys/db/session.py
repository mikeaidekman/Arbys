"""Database configuration and session management."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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


def configure_engine(url: str | None = None) -> AsyncEngine:
    """(Re)configure the global engine. Call this from tests to swap DB URL."""
    global _engine, _session_factory
    _engine = create_async_engine(url or _get_db_url(), pool_pre_ping=True, future=True)
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

