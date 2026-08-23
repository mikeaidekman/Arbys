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
