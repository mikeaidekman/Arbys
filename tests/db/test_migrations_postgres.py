"""The migration chain must replay on **Postgres**, not just on SQLite.

`test_migrations_match_models.py` proves the chain against SQLite, which is
what dev and the whole suite run on. But dev never executes a migration at all
— `bootstrap()` builds the schema with `create_all()` — so the first time this
chain runs against Postgres will be the first hosted deploy, on a database that
matters.

The dialects genuinely differ where it counts: `BigInteger().with_variant(
Integer(), "sqlite")` is BIGINT on one and INTEGER on the other, JSON is a
distinct type, and NUMERIC carries its precision. A chain that replays
perfectly on SQLite can still fail or drift here.

Note what is compared and what is not: **both sides are built on the same
Postgres**, so dialect differences cancel out and any disagreement is real
drift between `models.py` and the migrations — not a SQLite-versus-Postgres
artifact.

Skipped unless `ARBYS_TEST_PG_URL` is set, so the default suite never needs a
Postgres server. In CI a Neon branch supplies it; the branch is created for the
run and thrown away after, so this test is destructive by design and must never
be pointed at a database anyone cares about — it drops the public schema
twice.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]

PG_URL = os.environ.get("ARBYS_TEST_PG_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="ARBYS_TEST_PG_URL not set; needs a throwaway Postgres"
)


def _sync(url: str) -> str:
    """Inspection is synchronous; the app's URL names the async driver."""
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)


def _wipe(url: str) -> None:
    """Drop and recreate `public`, so each build starts from genuinely empty.

    Both schemas are built into the same database in turn rather than into two
    databases: a Neon branch hands you one, and CREATE DATABASE against it is
    not reliably available.
    """
    engine = sa.create_engine(_sync(url), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(sa.text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


def _schema(url: str) -> dict[str, dict[str, tuple[str, bool]]]:
    """{table: {column: (TYPE, notnull)}} — ignoring alembic's bookkeeping."""
    engine = sa.create_engine(_sync(url))
    try:
        insp = sa.inspect(engine)
        out: dict[str, dict[str, tuple[str, bool]]] = {}
        for table in insp.get_table_names():
            if table == "alembic_version":
                continue
            out[table] = {
                col["name"]: (str(col["type"]).upper(), not col["nullable"])
                for col in insp.get_columns(table)
            }
        return out
    finally:
        engine.dispose()


def _build_from_models(url: str) -> None:
    import asyncio

    from arbys.db import session as db_session

    prev = os.environ.get("ARBYS_DB_URL")
    os.environ["ARBYS_DB_URL"] = url
    try:
        db_session.reset_engine()
        asyncio.run(db_session.create_all())
    finally:
        db_session.reset_engine()
        if prev is None:
            os.environ.pop("ARBYS_DB_URL", None)
        else:
            os.environ["ARBYS_DB_URL"] = prev


def _build_from_migrations(url: str) -> None:
    # env.py swaps postgresql+asyncpg for postgresql+psycopg itself, so the
    # same URL serves both paths and there is nothing to translate here.
    env = dict(os.environ, ARBYS_DB_URL=url)
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(
            "alembic upgrade head failed on an empty Postgres database — the "
            "migration chain cannot be replayed on the dialect production "
            f"uses:\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
        )


def test_migration_chain_replays_on_postgres_and_matches_models():
    assert PG_URL is not None  # guarded by pytestmark

    _wipe(PG_URL)
    _build_from_models(PG_URL)
    from_models = _schema(PG_URL)

    _wipe(PG_URL)
    _build_from_migrations(PG_URL)
    from_migrations = _schema(PG_URL)

    assert from_models, "create_all produced no tables on Postgres"

    missing = sorted(set(from_models) - set(from_migrations))
    extra = sorted(set(from_migrations) - set(from_models))
    assert not missing, f"tables in models.py with no migration: {missing}"
    assert not extra, f"tables created by migrations but absent from models.py: {extra}"

    problems: list[str] = []
    for table in sorted(from_models):
        cols_m, cols_a = from_models[table], from_migrations[table]
        for col in sorted(set(cols_m) - set(cols_a)):
            problems.append(f"{table}.{col} is in models.py but no migration adds it")
        for col in sorted(set(cols_a) - set(cols_m)):
            problems.append(f"{table}.{col} is created by migrations but not in models.py")
        for col in sorted(set(cols_m) & set(cols_a)):
            if cols_m[col] != cols_a[col]:
                problems.append(
                    f"{table}.{col}: models.py={cols_m[col]} migrations={cols_a[col]}"
                )
    assert not problems, (
        "schema drift between models.py and migrations on Postgres:\n  "
        + "\n  ".join(problems)
    )


def test_migration_chain_downgrades_cleanly_on_postgres():
    """Every revision's downgrade must undo its upgrade, on Postgres too.

    A downgrade that works on SQLite can still fail here — SQLite silently
    tolerates a good deal that Postgres refuses, dropping a constrained column
    among it.
    """
    assert PG_URL is not None  # guarded by pytestmark

    _wipe(PG_URL)
    _build_from_migrations(PG_URL)

    env = dict(os.environ, ARBYS_DB_URL=PG_URL)
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"alembic downgrade base failed on Postgres:\n{proc.stdout[-1500:]}\n"
        f"{proc.stderr[-2500:]}"
    )
    remaining = _schema(PG_URL)
    assert remaining == {}, f"downgrade left tables behind: {sorted(remaining)}"
