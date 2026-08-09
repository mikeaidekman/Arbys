"""Alembic environment.

Uses the ORM metadata for autogeneration and reads the DB URL from
ARBYS_DB_URL (falls back to the same default as arbys.db.session). We use the
sync psycopg driver here because Alembic's default runner is synchronous.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from arbys.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url() -> str:
    """Alembic's runner is synchronous, so swap async drivers for sync ones.

    SQLite is the documented dev default, so it has to be handled here too —
    otherwise ``alembic upgrade`` fails on the URL in .env.example.
    """
    url = os.environ.get("ARBYS_DB_URL", "postgresql+asyncpg://arbys:arbys@localhost:5432/arbys")
    for async_driver, sync_driver in (
        ("postgresql+asyncpg://", "postgresql+psycopg://"),
        ("sqlite+aiosqlite://", "sqlite://"),
    ):
        if url.startswith(async_driver):
            return url.replace(async_driver, sync_driver, 1)
    return url


def run_migrations_offline() -> None:
    context.configure(url=_sync_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    ini = config.get_section(config.config_ini_section) or {}
    ini["sqlalchemy.url"] = _sync_url()
    connectable = engine_from_config(ini, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
