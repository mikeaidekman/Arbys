"""The migration chain and models.py must describe the same schema.

Dev builds its schema with ``create_all()`` straight from ``models.py``;
Postgres/prod builds it by replaying migrations. Nothing else in the suite
runs the migrations at all, so without this a migration can be wrong — or
missing entirely — and every dev environment keeps working while the next
real deploy breaks.

That is not hypothetical: 0001 originally called ``Base.metadata.create_all()``,
which read the *current* models rather than the schema at that revision, so
``alembic upgrade head`` on an empty database failed with "duplicate column
name: venue_id" once 0002 existed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]


def _schema(url: str) -> dict[str, dict[str, tuple[str, bool]]]:
    """{table: {column: (TYPE, notnull)}} — ignoring alembic's bookkeeping."""
    engine = sa.create_engine(url)
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


def _build_from_models(path: Path) -> str:
    import asyncio

    from arbys.db import session as db_session

    prev = os.environ.get("ARBYS_DB_URL")
    os.environ["ARBYS_DB_URL"] = f"sqlite+aiosqlite:///{path}"
    try:
        db_session.reset_engine()
        asyncio.run(db_session.create_all())
    finally:
        db_session.reset_engine()
        if prev is None:
            os.environ.pop("ARBYS_DB_URL", None)
        else:
            os.environ["ARBYS_DB_URL"] = prev
    return f"sqlite:///{path}"


def _build_from_migrations(path: Path) -> str:
    env = dict(os.environ, ARBYS_DB_URL=f"sqlite:///{path}")
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(
            "alembic upgrade head failed on an empty database — the migration "
            f"chain cannot be replayed:\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
        )
    return f"sqlite:///{path}"


def test_migration_chain_replays_from_empty_and_matches_models(tmp_path):
    from_models = _schema(_build_from_models(tmp_path / "models.db"))
    from_migrations = _schema(_build_from_migrations(tmp_path / "migrations.db"))

    assert from_models, "create_all produced no tables"

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
    assert not problems, "schema drift between models.py and migrations:\n  " + "\n  ".join(
        problems
    )


def test_migration_chain_downgrades_cleanly(tmp_path):
    """Every revision's downgrade must undo its upgrade."""
    path = tmp_path / "roundtrip.db"
    _build_from_migrations(path)
    env = dict(os.environ, ARBYS_DB_URL=f"sqlite:///{path}")
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"alembic downgrade base failed:\n{proc.stdout[-1500:]}\n{proc.stderr[-2500:]}"
    )
    remaining = _schema(f"sqlite:///{path}")
    assert remaining == {}, f"downgrade left tables behind: {sorted(remaining)}"
