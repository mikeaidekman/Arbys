"""Revision 0010 is the delivery mechanism for a real deposit, so it is tested
as one.

The hosted account is in Neon Postgres, whose connection string exists only in
Fly's secret store; the deploy's `release_command` runs `alembic upgrade head`,
and that is the only path into the database. So "add $2,000 to each trading
venue" *is* this migration, and a migration that ran but did the wrong
arithmetic would look exactly like a successful deploy.

SQLite here, like the replay test beside it; the Postgres CI branch replays
the same chain and both statements are plain SQL on either dialect. The
starting amounts are deliberately not the seed value -- a live account holds
whatever its fills left, and the request was "add", not "set".
"""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]
PREVIOUS = "0009_paper_position_open_fees"


def _alembic(url: str, target: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=REPO_ROOT,
        env=dict(os.environ, ARBYS_DB_URL=url),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"alembic upgrade {target} failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
        )


def _fund_at_previous_revision(url: str) -> None:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            for venue in ("kalshi", "polymarket_us", "draftkings"):
                # 0005 already seeds a `polymarket_us` venue row on the way to
                # 0009, so this has to be idempotent -- same WHERE NOT EXISTS
                # idiom 0005 itself uses, for the same portability reason.
                conn.execute(
                    sa.text(
                        "INSERT INTO venue (id, name, kind) "
                        "SELECT :id, :name, 'exchange' "
                        "WHERE NOT EXISTS (SELECT 1 FROM venue WHERE id = :id)"
                    ),
                    {"id": venue, "name": venue.title()},
                )
            conn.execute(
                sa.text(
                    "INSERT INTO paper_account (id, name, base_currency) "
                    "VALUES ('default', 'default', 'USD')"
                )
            )
            for venue, amount in (
                ("kalshi", "1177.50"),
                ("polymarket_us", "0.25"),
                ("draftkings", "2000"),
            ):
                conn.execute(
                    sa.text(
                        "INSERT INTO paper_balance (account_id, venue_id, currency, amount) "
                        "VALUES ('default', :venue, 'USD', :amount)"
                    ),
                    {"venue": venue, "amount": amount},
                )
    finally:
        engine.dispose()


def _balances(url: str) -> dict[str, Decimal]:
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT venue_id, amount FROM paper_balance ORDER BY venue_id")
            ).all()
    finally:
        engine.dispose()
    return {venue: Decimal(str(amount)) for venue, amount in rows}


def test_0010_adds_2000_to_each_trading_venue_and_drops_draftkings(tmp_path):
    url = f"sqlite:///{tmp_path / 'fund.db'}"
    _alembic(url, PREVIOUS)
    _fund_at_previous_revision(url)

    _alembic(url, "head")

    assert _balances(url) == {
        "kalshi": Decimal("3177.50"),
        "polymarket_us": Decimal("2000.25"),
    }


def test_0010_is_a_no_op_on_an_unfunded_database(tmp_path):
    """A fresh deploy, the CI replay and the SQLite replay test all run this
    against empty tables; bootstrap() then seeds the new default."""
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    _alembic(url, "head")
    assert _balances(url) == {}
