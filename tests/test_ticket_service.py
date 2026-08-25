"""submit_arb_ticket is the only way a ticket is submitted.

The position cap used to live in the HTTP endpoint, so any non-HTTP caller
bypassed it silently and stacked without bound. These tests pin it to the
shared path instead.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from arbys.backend import state as state_module
from arbys.backend.state import get_state
from arbys.backend.ticket_service import submit_arb_ticket
from arbys.db import models as m
from arbys.db import repositories as repo
from arbys.db import session as db_session
from arbys.db.session import create_all, session_scope
from arbys.shared.types import EventGroup, EventGroupLeg, Quote


@pytest.fixture(autouse=True)
async def _fresh_state(tmp_path: Path):
    os.environ["ARBYS_DB_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'tickets.db'}"
    db_session.reset_engine()
    state_module.reset_state()
    await create_all()
    yield
    db_session.reset_engine()
    state_module.reset_state()
    os.environ.pop("ARBYS_DB_URL", None)


async def _arb_group(*, ask_size: Decimal | None = None):
    """An eg-1 group quoted 0.40 / 0.50 — a live 10c gross edge."""
    s = get_state()
    group = EventGroup(
        id="eg-1",
        title="MLB: ATL @ LAD",
        legs=(
            EventGroupLeg(outcome_id="p-yes", venue_id="polymarket_us", is_yes_side=True),
            EventGroupLeg(outcome_id="k-no", venue_id="kalshi", is_yes_side=False),
        ),
    )
    s.event_groups[group.id] = group
    s.engine.register_group(group)
    async with session_scope() as session:
        await repo.ensure_paper_account(session, s.default_account_id)
    for oid, px in (("p-yes", Decimal("0.40")), ("k-no", Decimal("0.50"))):
        s.quotebook.upsert(
            Quote(
                outcome_id=oid,
                bid=px,
                ask=px,
                bid_size=ask_size,
                ask_size=ask_size,
            )
        )
    for broker in s.paper_brokers.values():
        broker.deposit(s.default_account_id, Decimal("10000"))
    return s, group


async def test_filled_ticket_groups_its_legs():
    s, _ = await _arb_group()
    opp = s.engine.evaluate_now("eg-1")[0]
    result = await submit_arb_ticket(s, opp, source="manual")
    assert result.status == "filled"
    assert len(result.order_ids) == 2
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert len(tickets) == 1
    assert tickets[0]["status"] == "filled"
    assert tickets[0]["title_snapshot"] == "MLB: ATL @ LAD"
    assert tickets[0]["source"] == "manual"
    assert len(tickets[0]["legs"]) == 2


async def test_position_cap_is_enforced_here_not_in_the_endpoint(monkeypatch):
    monkeypatch.setenv("ARBYS_MAX_OUTCOME_QTY", "1")
    s, _ = await _arb_group()
    opp = s.engine.evaluate_now("eg-1")[0]
    result = await submit_arb_ticket(s, opp, source="auto")
    assert result.status == "rejected"
    assert result.reason is not None
    assert result.reason.startswith("position_cap:")
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert tickets[0]["status"] == "rejected"


async def test_vanished_edge_writes_a_missed_ticket_and_no_orders():
    s, _ = await _arb_group()
    opp = s.engine.evaluate_now("eg-1")[0]
    # The edge disappears before submission: both sides reprice to 0.60.
    for oid in ("p-yes", "k-no"):
        s.quotebook.upsert(
            Quote(outcome_id=oid, bid=Decimal("0.60"), ask=Decimal("0.60"))
        )
    result = await submit_arb_ticket(s, opp, source="auto")
    assert result.status == "missed"
    assert result.order_ids == ()
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert tickets[0]["status"] == "missed"
    assert tickets[0]["legs"] == []
    assert tickets[0]["total_stake"] is None


async def test_router_rejection_writes_per_leg_order_rows(monkeypatch):
    """A preview rejection never builds an Order, so before this nothing about
    it reached the database at all.

    The router is stubbed rather than provoked with a thin book: sizing is
    depth-aware, so re-quoting one side with `ask_size=1` makes the detector
    publish a qty-1 opportunity that fills perfectly well. No quote reliably
    produces a preview rejection *and* still detects.
    """
    from arbys.shared.execution_router import InsufficientLegsError, LegRejection

    s, _ = await _arb_group()
    opp = s.engine.evaluate_now("eg-1")[0]

    async def _refuse(_intent):
        raise InsufficientLegsError(
            (LegRejection("kalshi", "k-no", "insufficient_liquidity"),)
        )

    monkeypatch.setattr(s.router, "submit", _refuse)

    result = await submit_arb_ticket(s, opp, source="auto")
    assert result.status == "rejected"
    assert result.order_ids == ()
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert tickets[0]["status"] == "rejected"
    reasons = {leg["outcome_id"]: leg["rejection_reason"] for leg in tickets[0]["legs"]}
    assert reasons["k-no"] == "insufficient_liquidity"
    # A leg that previewed fine still gets a row: the ticket failed as a whole
    # and no leg was submitted.
    assert reasons["p-yes"] == "ticket_rejected"


async def test_atomic_commit_failure_writes_per_leg_order_rows():
    """Drives the *real* router into `_commit_atomically`'s post-preview
    failure branch -- no stubbing of `router.submit` -- to prove that path
    also gets its rejected legs written, not just the preview path above.

    The preview loop checks each leg's cash requirement independently
    against the unmutated balance, so a same-venue two-leg ticket whose
    *combined* cost exceeds available cash passes preview on both legs (each
    leg's cost alone is affordable) and only fails when the second leg's
    `apply_fill` sees the balance already reduced by the first. That failure
    unwinds completely (`restore_account` + `forget_order`) before raising a
    plain string with `legs_persisted=False`, so nothing about the two legs
    is in the database unless `ticket_service` writes it here.

    Both legs sit on Kalshi, which routes through `detect_complementary_set`
    (buy every outcome of a single-venue crossed book) rather than the
    cross-venue detector, so a real two-leg same-adapter ticket can be
    produced without a second venue.
    """
    s = get_state()
    group = EventGroup(
        id="eg-2",
        title="Kalshi crossed book",
        legs=(
            EventGroupLeg(outcome_id="k-yes", venue_id="kalshi", is_yes_side=True),
            EventGroupLeg(outcome_id="k-no", venue_id="kalshi", is_yes_side=False),
        ),
    )
    s.event_groups[group.id] = group
    s.engine.register_group(group)
    async with session_scope() as session:
        await repo.ensure_paper_account(session, s.default_account_id)
    for oid in ("k-yes", "k-no"):
        s.quotebook.upsert(
            Quote(
                outcome_id=oid,
                bid=Decimal("0.47"),
                ask=Decimal("0.47"),
                ask_size=Decimal("100"),
            )
        )
    # Each leg costs ~48.74 (100 contracts at 0.47 + Kalshi's 7%*p*(1-p) fee).
    # $60 covers either leg alone but not both, so preview passes for both
    # (checked independently) and the second leg's apply_fill is what fails.
    s.paper_brokers["kalshi"].deposit(s.default_account_id, Decimal("60"))

    found = s.engine.evaluate_now("eg-2")
    comp = next(o for o in found if o.event_group_id == "eg-2:kalshi")
    assert {leg.outcome_id for leg in comp.legs} == {"k-yes", "k-no"}

    result = await submit_arb_ticket(s, comp, source="manual")

    assert result.status == "rejected"
    assert result.order_ids == ()
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert tickets[0]["status"] == "rejected"
    legs = tickets[0]["legs"]
    assert len(legs) == 2
    reasons = {leg["outcome_id"]: leg["rejection_reason"] for leg in legs}
    # e.rejections is empty for this string-raise, so both legs fall back to
    # the generic reason rather than naming which one actually failed.
    assert reasons == {"k-yes": "ticket_rejected", "k-no": "ticket_rejected"}
    statuses = {leg["status"] for leg in legs}
    assert statuses == {"rejected"}
    # The unwind must have left no trace in the broker's own books either.
    cash, positions = s.paper_brokers["kalshi"].account_snapshot(s.default_account_id)
    assert cash == Decimal("60")
    assert positions == {}


async def test_string_raise_rejection_does_not_duplicate_leg_rows(monkeypatch):
    """`_commit_sequentially`'s post-preview failures raise a plain string
    (`InsufficientLegsError.rejections == ()`) with `legs_persisted=True`,
    *after* `place_order` already persisted a `paper_order` row for each
    attempted leg via `emit_order_events` -- unlike the preview/atomic paths,
    which raise having persisted nothing. `_write_rejected_legs` must not run
    for this case: it would add a second row for a leg that already has one
    (once `filled`, once `rejected`), corrupting `_score_ticket` and the
    frontend's per-leg React key.
    """
    from arbys.shared.execution_router import InsufficientLegsError

    s, _ = await _arb_group()
    opp = s.engine.evaluate_now("eg-1")[0]

    async def _refuse(_intent):
        raise InsufficientLegsError(
            "post-preview rejection on kalshi: rejected", legs_persisted=True
        )

    monkeypatch.setattr(s.router, "submit", _refuse)

    result = await submit_arb_ticket(s, opp, source="auto")
    assert result.status == "rejected"
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert tickets[0]["status"] == "rejected"
    # No leg rows written here -- whatever the router already persisted (or
    # didn't, in this stubbed case) is the only leg history for this ticket.
    assert tickets[0]["legs"] == []


async def test_ticket_row_exists_as_pending_before_the_router_runs(monkeypatch):
    """paper_order.ticket_id is an FK to paper_ticket.id and the sink writes
    order rows from inside router.submit, so the ticket must already exist.
    SQLite does not enforce FKs, so only this test stands between that
    ordering and a Postgres failure.
    """
    s, _ = await _arb_group()
    opp = s.engine.evaluate_now("eg-1")[0]
    seen: list[str | None] = []

    real_submit = s.router.submit

    async def _observe(intent):
        async with session_scope() as session:
            row = await session.get(m.PaperTicket, intent.ticket_id)
            seen.append(None if row is None else row.status)
        return await real_submit(intent)

    monkeypatch.setattr(s.router, "submit", _observe)

    result = await submit_arb_ticket(s, opp, source="manual")
    assert result.status == "filled"
    assert seen == ["pending"]


async def test_descriptor_with_no_live_edge_writes_a_missed_ticket():
    """The gap this closes.

    /paper/execute used to resolve the opportunity itself and raise 409 before
    ever reaching the ticket service, so the most common real failure — the
    edge dying between the row rendering and the click landing — wrote nothing
    at all. Six manual fills and several visible failures on 2026-08-24
    produced zero `missed` tickets.
    """
    from arbys.backend.ticket_service import submit_arb_ticket_for_descriptor
    from arbys.db import repositories as repo

    s, _ = await _arb_group()
    # Reprice both sides so no edge exists, then describe the group anyway —
    # exactly what a click on a stale row does.
    for oid in ("p-yes", "k-no"):
        s.quotebook.upsert(
            Quote(outcome_id=oid, bid=Decimal("0.60"), ask=Decimal("0.60"))
        )

    result = await submit_arb_ticket_for_descriptor(
        s, event_group_id="eg-1", outcome_ids={"p-yes", "k-no"}, source="manual"
    )
    assert result.status == "missed"
    assert result.order_ids == ()
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert len(tickets) == 1
    assert tickets[0]["status"] == "missed"
    assert tickets[0]["title_snapshot"] == "MLB: ATL @ LAD"
    assert tickets[0]["total_stake"] is None
    assert tickets[0]["legs"] == []


async def test_descriptor_with_a_live_edge_still_fills():
    from arbys.backend.ticket_service import submit_arb_ticket_for_descriptor
    from arbys.db import repositories as repo

    s, _ = await _arb_group()
    result = await submit_arb_ticket_for_descriptor(
        s, event_group_id="eg-1", outcome_ids={"p-yes", "k-no"}, source="manual"
    )
    assert result.status == "filled"
    assert len(result.order_ids) == 2
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert tickets[0]["status"] == "filled"


async def test_descriptor_missed_ticket_names_an_unknown_group():
    """A descriptor for a group AppState has never heard of still gets a row,
    titled with the id rather than crashing on the lookup."""
    from arbys.backend.ticket_service import submit_arb_ticket_for_descriptor
    from arbys.db import repositories as repo

    s, _ = await _arb_group()
    result = await submit_arb_ticket_for_descriptor(
        s, event_group_id="eg-nonexistent", outcome_ids=None, source="manual"
    )
    assert result.status == "missed"
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert tickets[0]["title_snapshot"] == "eg-nonexistent"
