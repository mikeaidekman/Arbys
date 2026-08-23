"""Ticket and settlement persistence."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from arbys.db import models as m
from arbys.db import repositories as repo
from arbys.db import session as db_session
from arbys.db.session import create_all, session_scope


@pytest.fixture(autouse=True)
def _sqlite_db(tmp_path: Path):
    os.environ["ARBYS_DB_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'tickets.db'}"
    db_session.reset_engine()
    yield
    db_session.reset_engine()
    os.environ.pop("ARBYS_DB_URL", None)


async def test_ticket_and_settlement_round_trip():
    await create_all()
    async with session_scope() as session:
        await repo.ensure_venue(session, "kalshi", name="Kalshi", kind="exchange")
        await repo.ensure_paper_account(session, "default")
        await repo.insert_paper_ticket(
            session,
            ticket_id="tkt-1",
            account_id="default",
            event_group_id="eg-1",
            title_snapshot="MLB: ATL @ LAD",
            source="manual",
            status="filled",
            total_stake=Decimal("90.00"),
            expected_profit=Decimal("10.00"),
            expected_edge_bps=Decimal("1111"),
        )
        await repo.insert_paper_order(
            session,
            order_id="ord-1",
            account_id="default",
            venue_id="kalshi",
            outcome_id="k-yes",
            is_buy=True,
            qty=Decimal("100"),
            limit_price=Decimal("0.40"),
            status="filled",
            ticket_id="tkt-1",
        )
        await repo.insert_paper_settlement(
            session,
            outcome_id="k-yes",
            venue_id="kalshi",
            resolved_value=Decimal("1"),
            source="heuristic",
        )

    async with session_scope() as session:
        ticket = await session.get(m.PaperTicket, "tkt-1")
        assert ticket.title_snapshot == "MLB: ATL @ LAD"
        assert ticket.source == "manual"
        assert ticket.status == "filled"
        assert ticket.total_stake == Decimal("90.00")
        assert ticket.expected_profit == Decimal("10.00")
        assert ticket.expected_edge_bps == Decimal("1111")

        orders = await repo.list_paper_orders(session, "default")
        assert orders[0]["ticket_id"] == "tkt-1"

        settlements = await repo.list_paper_settlements(session)
        assert settlements[0]["outcome_id"] == "k-yes"
        assert settlements[0]["resolved_value"] == Decimal("1")


async def test_missed_ticket_has_null_economics():
    """A missed ticket has no stake or expectation. Zero would read as a free
    ticket that made nothing."""
    await create_all()
    async with session_scope() as session:
        await repo.ensure_paper_account(session, "default")
        await repo.insert_paper_ticket(
            session,
            ticket_id="tkt-miss",
            account_id="default",
            event_group_id="eg-9",
            title_snapshot="NFL: ARI @ LAC",
            source="auto",
            status="missed",
            rejection_reason="edge_no_longer_available",
        )

    async with session_scope() as session:
        ticket = await session.get(m.PaperTicket, "tkt-miss")
        assert ticket.status == "missed"
        assert ticket.rejection_reason == "edge_no_longer_available"
        assert ticket.total_stake is None
        assert ticket.expected_profit is None
        assert ticket.expected_edge_bps is None


async def _two_leg_filled_ticket(session, *, ticket_id: str) -> None:
    """A 100-unit arb: buy YES at 0.40 and NO at 0.50, 1c fee each side."""
    await repo.ensure_venue(session, "kalshi", name="Kalshi", kind="exchange")
    await repo.ensure_venue(
        session, "polymarket_us", name="Polymarket US", kind="exchange"
    )
    await repo.ensure_paper_account(session, "default")
    await repo.insert_paper_ticket(
        session,
        ticket_id=ticket_id,
        account_id="default",
        event_group_id="eg-1",
        title_snapshot="MLB: ATL @ LAD",
        source="manual",
        status="filled",
        total_stake=Decimal("92.00"),
        expected_profit=Decimal("8.00"),
        expected_edge_bps=Decimal("869"),
    )
    for oid, venue, px, order_id in (
        ("k-yes", "kalshi", Decimal("0.40"), f"{ticket_id}-a"),
        ("p-no", "polymarket_us", Decimal("0.50"), f"{ticket_id}-b"),
    ):
        await repo.insert_paper_order(
            session,
            order_id=order_id,
            account_id="default",
            venue_id=venue,
            outcome_id=oid,
            is_buy=True,
            qty=Decimal("100"),
            limit_price=px,
            status="filled",
            ticket_id=ticket_id,
        )
        await repo.insert_paper_fill(
            session, order_id=order_id, qty=Decimal("100"), price=px, fee=Decimal("1.00")
        )


async def test_ticket_reports_fills_not_just_limits():
    await create_all()
    async with session_scope() as session:
        await _two_leg_filled_ticket(session, ticket_id="tkt-1")
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, "default")
    assert len(tickets) == 1
    legs = sorted(tickets[0]["legs"], key=lambda leg: leg["outcome_id"])
    assert [leg["fill_price"] for leg in legs] == [Decimal("0.40"), Decimal("0.50")]
    assert [leg["fee"] for leg in legs] == [Decimal("1.00"), Decimal("1.00")]


async def test_unsettled_ticket_scores_null():
    await create_all()
    async with session_scope() as session:
        await _two_leg_filled_ticket(session, ticket_id="tkt-1")
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, "default")
    assert tickets[0]["realized_profit"] is None


async def test_settled_ticket_scores_from_its_own_fills():
    """One leg resolves to 1, the other to 0.

    Winner: (1 - 0.40) * 100 - 1 = +59. Loser: (0 - 0.50) * 100 - 1 = -51.
    Net +8, which is the stake-implied edge: 100 payout on 92 spent.
    """
    await create_all()
    async with session_scope() as session:
        await _two_leg_filled_ticket(session, ticket_id="tkt-1")
        await repo.insert_paper_settlement(
            session, outcome_id="k-yes", venue_id="kalshi",
            resolved_value=Decimal("1"), source="heuristic",
        )
        await repo.insert_paper_settlement(
            session, outcome_id="p-no", venue_id="polymarket_us",
            resolved_value=Decimal("0"), source="heuristic",
        )
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, "default")
    assert tickets[0]["realized_profit"] == Decimal("8.00")


async def test_partially_settled_ticket_still_scores_null():
    await create_all()
    async with session_scope() as session:
        await _two_leg_filled_ticket(session, ticket_id="tkt-1")
        await repo.insert_paper_settlement(
            session, outcome_id="k-yes", venue_id="kalshi",
            resolved_value=Decimal("1"), source="heuristic",
        )
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, "default")
    assert tickets[0]["realized_profit"] is None


async def test_title_survives_event_group_deletion():
    """Discovery retires groups routinely. The snapshot is why history keeps
    its name for exactly the games that have finished."""
    from arbys.shared.types import EventGroup, EventGroupLeg

    await create_all()
    async with session_scope() as session:
        await repo.upsert_event_group(
            session,
            EventGroup(
                id="eg-1",
                title="MLB: ATL @ LAD",
                legs=(
                    EventGroupLeg(outcome_id="k-yes", venue_id="kalshi", is_yes_side=True),
                ),
            ),
        )
        await _two_leg_filled_ticket(session, ticket_id="tkt-1")
    async with session_scope() as session:
        await repo.delete_event_group(session, "eg-1")
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, "default")
    assert tickets[0]["title_snapshot"] == "MLB: ATL @ LAD"


async def test_tickets_filter_by_status_and_source():
    await create_all()
    async with session_scope() as session:
        await _two_leg_filled_ticket(session, ticket_id="tkt-filled")
        await repo.insert_paper_ticket(
            session,
            ticket_id="tkt-missed",
            account_id="default",
            event_group_id="eg-2",
            title_snapshot="NFL: ARI @ LAC",
            source="auto",
            status="missed",
            rejection_reason="edge_no_longer_available",
        )
    async with session_scope() as session:
        assert len(await repo.list_paper_tickets(session, "default")) == 2
        only_missed = await repo.list_paper_tickets(session, "default", status="missed")
        assert [t["id"] for t in only_missed] == ["tkt-missed"]
        only_auto = await repo.list_paper_tickets(session, "default", source="auto")
        assert [t["id"] for t in only_auto] == ["tkt-missed"]
        assert only_missed[0]["legs"] == []
