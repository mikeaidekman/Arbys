"""Revision 0011 voids the 2026-09-05 outage window and moves the cash with it.

Flagging the rows alone would not be enough. In the simulator the phantom
profit is *real* money: the broker filled at the frozen price and settlement
genuinely paid out $1, so leaving it as buying power would inflate every later
position and the denominator of every return. These tests pin both halves --
the flag and the arithmetic -- and that the evidence survives either way.

SQLite here, like the migration tests beside it. Both statements are plain SQL
and the Python arithmetic is dialect-independent; the Postgres CI branch
replays the same chain.
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
PREVIOUS = "0010_fund_trading_venues"

# 18:00Z sits inside the voided 17:00-23:00Z window; 12:00Z sits before it.
IN_WINDOW = "2026-09-05 18:00:00"
BEFORE_WINDOW = "2026-09-05 12:00:00"
SETTLED_AT = "2026-09-05 20:00:00"


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


def _reference_rows(conn) -> None:
    # OR IGNORE because migration 0005 already creates the venue rows on its
    # way past, so by 0010 they exist. SQLite-specific, and this file is a
    # SQLite test; the Postgres branch replays the chain, not these fixtures.
    for venue in ("kalshi", "polymarket_us"):
        conn.execute(
            sa.text(
                "INSERT OR IGNORE INTO venue (id, name, kind) "
                "VALUES (:i, :n, 'exchange')"
            ),
            {"i": venue, "n": venue.title()},
        )
        conn.execute(
            sa.text(
                "INSERT INTO market (id, venue_id, venue_market_id, title, kind) "
                "VALUES (:i, :v, :i, 'T', 'binary')"
            ),
            {"i": f"mkt-{venue}", "v": venue},
        )
    conn.execute(
        sa.text(
            "INSERT INTO paper_account (id, name, base_currency) "
            "VALUES ('default', 'default', 'USD')"
        )
    )


def _outcome(conn, outcome_id: str, venue: str) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO outcome (id, market_id, label, side) "
            "VALUES (:i, :m, :i, 'YES')"
        ),
        {"i": outcome_id, "m": f"mkt-{venue}"},
    )


def _filled_leg(conn, *, ticket, order, venue, outcome, qty, price, fee, ts) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO paper_order (id, account_id, venue_id, outcome_id, is_buy, "
            "qty, limit_price, status, submitted_at, ticket_id) VALUES "
            "(:o, 'default', :v, :oc, 1, :q, :p, 'filled', :ts, :t)"
        ),
        {"o": order, "v": venue, "oc": outcome, "q": qty, "p": price, "ts": ts, "t": ticket},
    )
    conn.execute(
        sa.text(
            "INSERT INTO paper_fill (order_id, qty, price, fee, ts) "
            "VALUES (:o, :q, :p, :f, :ts)"
        ),
        {"o": order, "q": qty, "p": price, "f": fee, "ts": ts},
    )


def _ticket(conn, ticket_id: str, ts: str) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO paper_ticket (id, account_id, event_group_id, title_snapshot, "
            "source, status, submitted_at) VALUES "
            "(:i, 'default', 'ncaaf-AUB-BAY-2026-09-05', 'AUB @ BAY', 'auto', "
            "'filled', :ts)"
        ),
        {"i": ticket_id, "ts": ts},
    )


def _balances(conn, kalshi: str, poly: str) -> None:
    for venue, amount in (("kalshi", kalshi), ("polymarket_us", poly)):
        conn.execute(
            sa.text(
                "INSERT INTO paper_balance (account_id, venue_id, currency, amount) "
                "VALUES ('default', :v, 'USD', :a)"
            ),
            {"v": venue, "a": amount},
        )


def _read(url: str, query: str) -> list:
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(sa.text(query)).all()
    finally:
        engine.dispose()


def _seed_settled_phantom(url: str) -> None:
    """A voided pair costing 39c against a $1 payout, plus a control outside it.

    Kalshi holds the winning leg at 0.20 and Polymarket the loser at 0.17, both
    100 contracts with a $1 fee a side. Cost is $39, payout $100, so $61 of
    phantom profit sits in cash: kalshi +$79 and polymarket -$18.
    """
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            _reference_rows(conn)
            for oid, venue in (("k-yes", "kalshi"), ("p-short", "polymarket_us")):
                _outcome(conn, oid, venue)
            _outcome(conn, "k-old", "kalshi")
            _balances(conn, "1079", "982")

            _ticket(conn, "tkt-void", IN_WINDOW)
            _filled_leg(conn, ticket="tkt-void", order="ord-a", venue="kalshi",
                        outcome="k-yes", qty="100", price="0.20", fee="1.00", ts=IN_WINDOW)
            _filled_leg(conn, ticket="tkt-void", order="ord-b", venue="polymarket_us",
                        outcome="p-short", qty="100", price="0.17", fee="1.00", ts=IN_WINDOW)
            for oid, value in (("k-yes", "1"), ("p-short", "0")):
                conn.execute(
                    sa.text(
                        "INSERT INTO paper_settlement (outcome_id, resolved_value, ts, source) "
                        "VALUES (:o, :v, :ts, 'ended')"
                    ),
                    {"o": oid, "v": value, "ts": SETTLED_AT},
                )

            # Control: a morning ticket, already settled, outside the window.
            _ticket(conn, "tkt-keep", BEFORE_WINDOW)
            _filled_leg(conn, ticket="tkt-keep", order="ord-c", venue="kalshi",
                        outcome="k-old", qty="10", price="0.95", fee="0.10",
                        ts=BEFORE_WINDOW)
    finally:
        engine.dispose()


def test_0011_flags_only_the_window_and_deletes_nothing(tmp_path):
    url = f"sqlite:///{tmp_path / 'void.db'}"
    _alembic(url, PREVIOUS)
    _seed_settled_phantom(url)

    _alembic(url, "head")

    flags = dict(_read(url, "SELECT id, excluded_reason FROM paper_ticket"))
    assert flags == {
        "tkt-void": "polymarket_outage_2026-09-05",
        "tkt-keep": None,
    }
    # Excluding is not deleting: the fills are the evidence of the incident.
    assert _read(url, "SELECT COUNT(*) FROM paper_fill")[0][0] == 3


def test_0011_takes_the_phantom_profit_back_out_of_cash(tmp_path):
    """$61 of profit that a real venue would never have offered."""
    url = f"sqlite:///{tmp_path / 'cash.db'}"
    _alembic(url, PREVIOUS)
    _seed_settled_phantom(url)

    _alembic(url, "head")

    balances = {
        venue: Decimal(str(amount))
        for venue, amount in _read(url, "SELECT venue_id, amount FROM paper_balance")
    }
    assert balances == {"kalshi": Decimal("1000"), "polymarket_us": Decimal("1000")}


def test_0011_removes_a_position_nothing_legitimate_bought(tmp_path):
    """Refunding an unsettled ticket while keeping its position pays twice."""
    url = f"sqlite:///{tmp_path / 'pos.db'}"
    _alembic(url, PREVIOUS)
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            _reference_rows(conn)
            _outcome(conn, "k-open", "kalshi")
            _balances(conn, "985", "1000")
            _ticket(conn, "tkt-open", IN_WINDOW)
            _filled_leg(conn, ticket="tkt-open", order="ord-d", venue="kalshi",
                        outcome="k-open", qty="50", price="0.30", fee="0.00",
                        ts=IN_WINDOW)
            conn.execute(
                sa.text(
                    "INSERT INTO paper_position (account_id, venue_id, outcome_id, qty, "
                    "avg_price, realized_pnl, open_fees) VALUES "
                    "('default', 'kalshi', 'k-open', 50, 0.30, 0, 0)"
                )
            )
    finally:
        engine.dispose()

    _alembic(url, "head")

    assert _read(url, "SELECT COUNT(*) FROM paper_position")[0][0] == 0
    kalshi = dict(_read(url, "SELECT venue_id, amount FROM paper_balance"))["kalshi"]
    # The $15 it cost comes back, and the contracts it bought go away.
    assert Decimal(str(kalshi)) == Decimal("1000")
