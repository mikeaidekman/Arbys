"""Cursor paging, counting, and the activity rollup.

The defect these cover: the account page fetched a flat 1000 tickets and said
nothing about the remainder, which at the auto-trader's ~1,500/day covered
under ten hours while the page still offered a 90-day selector.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, update

from arbys.db import models as m
from arbys.db import repositories as repo
from arbys.db import session as db_session
from arbys.db.session import create_all, session_scope


@pytest.fixture(autouse=True)
def _sqlite_db(tmp_path: Path):
    os.environ["ARBYS_DB_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'paging.db'}"
    db_session.reset_engine()
    yield
    db_session.reset_engine()
    os.environ.pop("ARBYS_DB_URL", None)


BASE = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


async def _seed(spec: list[tuple[str, str, datetime]]) -> None:
    """(ticket_id, status, submitted_at) rows, plus the account they need."""
    await create_all()
    async with session_scope() as session:
        await repo.ensure_paper_account(session, "default")
        for ticket_id, status, _ts in spec:
            await repo.insert_paper_ticket(
                session,
                ticket_id=ticket_id,
                account_id="default",
                event_group_id="nfl-ARI-LAC-2026-09-13",
                title_snapshot="NFL: ARI @ LAC (2026-09-13)",
                source="auto",
                status=status,
                rejection_reason=(
                    "kalshi:insufficient_funds" if status == "rejected" else None
                ),
                expected_edge_bps=Decimal("40"),
            )
    # submitted_at is a server default, so it is set after the fact -- the
    # point of these rows is the exact ordering, ties included.
    async with session_scope() as session:
        for ticket_id, _status, ts in spec:
            await session.execute(
                update(m.PaperTicket)
                .where(m.PaperTicket.id == ticket_id)
                .values(submitted_at=ts)
            )


async def _walk(page_size: int, **kwargs) -> list[str]:
    """Every ticket id reachable by following the cursor to exhaustion."""
    seen: list[str] = []
    cursor: tuple[datetime, str] | None = None
    for _ in range(50):  # bounded so a paging bug fails rather than hangs
        async with session_scope() as session:
            page = await repo.list_paper_tickets(
                session, "default", limit=page_size, cursor=cursor, **kwargs
            )
        if not page:
            return seen
        seen.extend(row["id"] for row in page)
        cursor = (page[-1]["submitted_at"], page[-1]["id"])
    raise AssertionError("cursor never exhausted the ledger")


async def test_paging_visits_every_ticket_exactly_once_across_ties():
    """The cursor is `(submitted_at, id)`, and the id half is load-bearing.

    The auto-trader writes bursts inside one second -- 74% of one day's repeat
    tickets landed in the same second -- so a cursor keyed on the timestamp
    alone either skips rows (`<`) or repeats them forever (`<=`) at every page
    boundary that falls inside a tie.
    """
    tied = BASE - timedelta(minutes=1)
    spec = [
        ("t-09", "filled", BASE),
        # Five rows sharing one timestamp, straddling two page boundaries.
        ("t-08", "rejected", tied),
        ("t-07", "rejected", tied),
        ("t-06", "rejected", tied),
        ("t-05", "rejected", tied),
        ("t-04", "rejected", tied),
        ("t-03", "filled", BASE - timedelta(minutes=2)),
        ("t-02", "missed", BASE - timedelta(minutes=3)),
        ("t-01", "filled", BASE - timedelta(minutes=4)),
    ]
    await _seed(spec)

    walked = await _walk(page_size=2)
    assert walked == sorted((t[0] for t in spec), reverse=True)
    assert len(set(walked)) == len(walked), "a ticket was returned twice"


async def test_paging_survives_the_real_write_path():
    """Rows written the way production writes them, not the way tests do.

    Regression for a duplicate-row bug that every other test here missed. The
    tests above set `submitted_at` through SQLAlchemy, which writes SQLite's
    text datetime *with* a `.000000` fraction. Real rows come from
    `server_default=func.now()` -- SQLite's `CURRENT_TIMESTAMP` -- which writes
    whole seconds and no fraction at all. Since SQLite compares datetimes as
    text and SQLAlchemy always binds the fractional form,
    `'…:34' < '…:34.000000'` held for every row in the cursor's own second, so
    each page re-served its predecessor's tie group.

    The auto-trader writes in bursts, so ties are the common case rather than
    an edge: 950 seconds in the local database hold more than one ticket.
    Postgres, having a real timestamp type, was never affected -- which is
    exactly why this had to be caught here.
    """
    await create_all()
    async with session_scope() as session:
        await repo.ensure_paper_account(session, "default")
        for i in range(12):
            await repo.insert_paper_ticket(
                session,
                ticket_id=f"burst-{i:02d}",
                account_id="default",
                event_group_id="nfl-ARI-LAC-2026-09-13",
                title_snapshot="NFL: ARI @ LAC (2026-09-13)",
                source="auto",
                status="rejected",
                rejection_reason="kalshi:insufficient_funds",
            )

    async with session_scope() as session:
        stamps = (
            await session.execute(select(m.PaperTicket.submitted_at))
        ).scalars().all()
    assert len({s.replace(microsecond=0) for s in stamps}) <= 2, (
        "expected the burst to land inside a second or two, which is the "
        "condition this regression needs"
    )

    walked = await _walk(page_size=5)
    assert len(walked) == 12
    assert len(set(walked)) == 12, "a page re-served its predecessor's tie group"


async def test_page_size_does_not_change_what_is_reachable():
    spec = [
        (f"t-{i:02d}", "filled", BASE - timedelta(seconds=i)) for i in range(11)
    ]
    await _seed(spec)
    assert await _walk(page_size=1) == await _walk(page_size=4)
    assert len(await _walk(page_size=1)) == 11


async def test_count_reports_the_whole_ledger_not_the_page():
    """`total` is what makes truncation visible instead of silent."""
    spec = [(f"t-{i:02d}", "rejected", BASE - timedelta(seconds=i)) for i in range(7)]
    await _seed(spec)
    async with session_scope() as session:
        page = await repo.list_paper_tickets(session, "default", limit=3)
        total = await repo.count_paper_tickets(session, "default")
    assert len(page) == 3
    assert total == 7


async def test_since_scopes_page_and_count_identically():
    """A footer reading "3 of 40" is a lie if the two are scoped differently."""
    spec = [
        (f"t-{i:02d}", "filled", BASE - timedelta(hours=i)) for i in range(10)
    ]
    await _seed(spec)
    cutoff = BASE - timedelta(hours=4, minutes=30)
    async with session_scope() as session:
        rows = await repo.list_paper_tickets(session, "default", limit=100, since=cutoff)
        total = await repo.count_paper_tickets(session, "default", since=cutoff)
    assert total == len(rows) == 5


async def test_never_traded_filter_stays_in_sql():
    """`outcome="none"` is the 76% case and must not hydrate a single fill."""
    spec = [
        ("t-a", "filled", BASE),
        ("t-b", "rejected", BASE - timedelta(seconds=1)),
        ("t-c", "missed", BASE - timedelta(seconds=2)),
    ]
    await _seed(spec)
    async with session_scope() as session:
        rows = await repo.list_paper_tickets(
            session, "default", limit=100, outcome="none"
        )
        total = await repo.count_paper_tickets(session, "default", outcome="none")
    assert {r["id"] for r in rows} == {"t-b", "t-c"}
    assert total == 2


async def test_scalars_cover_every_ticket_and_touch_no_join():
    """The single scan the whole dashboard is built on.

    It must include the rejections -- they carry the edge the engine believed
    and the reason it went unfilled -- while never reaching an order or a fill.
    """
    spec = [
        ("t-a", "filled", BASE),
        ("t-b", "rejected", BASE - timedelta(seconds=1)),
        ("t-c", "rejected", BASE - timedelta(seconds=2)),
        ("t-d", "missed", BASE - timedelta(seconds=3)),
    ]
    await _seed(spec)
    async with session_scope() as session:
        scalars = await repo.paper_ticket_scalars(session, "default")
    assert len(scalars) == 4
    assert {s["status"] for s in scalars} == {"filled", "rejected", "missed"}
    assert sum(1 for s in scalars if s["rejection_reason"] is not None) == 2
    assert all(s["expected_edge_bps"] == Decimal("40") for s in scalars)
    assert "legs" not in scalars[0]


async def test_scalars_honour_the_window():
    spec = [(f"t-{i:02d}", "rejected", BASE - timedelta(hours=i)) for i in range(6)]
    await _seed(spec)
    async with session_scope() as session:
        scoped = await repo.paper_ticket_scalars(
            session, "default", since=BASE - timedelta(hours=2, minutes=30)
        )
    assert len(scoped) == 3


async def test_outcomes_score_only_filled_tickets():
    """A rejected ticket has no settlement outcome, so it is never scored."""
    await _seed(
        [
            ("t-win", "filled", BASE),
            ("t-open", "filled", BASE - timedelta(seconds=1)),
            ("t-rej", "rejected", BASE - timedelta(seconds=2)),
        ]
    )
    async with session_scope() as session:
        await repo.ensure_venue(session, "kalshi", name="Kalshi", kind="exchange")
        # Settled winner: bought at 0.40, resolved at 1.
        await repo.insert_paper_order(
            session, order_id="o-win", account_id="default", venue_id="kalshi",
            outcome_id="k-win:YES", is_buy=True, qty=Decimal("10"),
            limit_price=Decimal("0.40"), status="filled", ticket_id="t-win",
        )
        await repo.insert_paper_fill(
            session, order_id="o-win", qty=Decimal("10"),
            price=Decimal("0.40"), fee=Decimal("0.10"),
        )
        await repo.insert_paper_settlement(
            session, outcome_id="k-win:YES", venue_id="kalshi",
            resolved_value=Decimal("1"),
        )
        # Filled but its outcome carries no settlement row yet.
        await repo.insert_paper_order(
            session, order_id="o-open", account_id="default", venue_id="kalshi",
            outcome_id="k-open:YES", is_buy=True, qty=Decimal("5"),
            limit_price=Decimal("0.50"), status="filled", ticket_id="t-open",
        )
        await repo.insert_paper_fill(
            session, order_id="o-open", qty=Decimal("5"),
            price=Decimal("0.50"), fee=Decimal("0.05"),
        )

    async with session_scope() as session:
        outcomes = await repo.ticket_outcomes(session, "default")
    assert outcomes == {"t-win": "won", "t-open": "open"}
    assert "t-rej" not in outcomes

    async with session_scope() as session:
        won = await repo.list_paper_tickets(
            session, "default", limit=10, outcome="won"
        )
        open_total = await repo.count_paper_tickets(session, "default", outcome="open")
    assert [r["id"] for r in won] == ["t-win"]
    assert open_total == 1
