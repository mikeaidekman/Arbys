"""submit_arb_ticket is the only way a ticket is submitted.

The position cap used to live in the HTTP endpoint, so any non-HTTP caller
bypassed it silently and stacked without bound. These tests pin it to the
shared path instead.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
async def _fresh_state(tmp_path: Path, seed_reference_rows):
    os.environ["ARBYS_DB_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'tickets.db'}"
    db_session.reset_engine()
    state_module.reset_state()
    await create_all()
    await seed_reference_rows()
    yield
    db_session.reset_engine()
    state_module.reset_state()
    os.environ.pop("ARBYS_DB_URL", None)


async def _arb_group(
    *, ask_size: Decimal | None = None, start_time: datetime | None = None
):
    """An eg-1 group quoted 0.40 / 0.50 — a live 10c gross edge."""
    s = get_state()
    group = EventGroup(
        id="eg-1",
        title="MLB: ATL @ LAD",
        start_time=start_time,
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
    monkeypatch.setenv("ARBYS_MAX_OUTCOME_STAKE", "1")
    s, _ = await _arb_group()
    opp = s.engine.evaluate_now("eg-1")[0]
    result = await submit_arb_ticket(s, opp, source="auto")
    assert result.status == "rejected"
    assert result.reason is not None
    assert result.reason.startswith("position_cap:")
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert tickets[0]["status"] == "rejected"


async def test_a_settled_group_is_never_traded_again():
    """Capital bought after settlement can never be paid out.

    Settlement zeroes the position and credits the payout once. A fill after
    that point locks its stake for the life of the account -- on the local
    ledger, 274 such fills spent $3,982, 85% of it buying the expensive leg at
    0.90 or better, and none of it came back. The engine keeps publishing the
    edge because the market is still quoting, so the refusal has to live at
    the submission chokepoint.
    """
    s, group = await _arb_group()
    opp = s.engine.evaluate_now("eg-1")[0]
    await s.auto_settle_service._settle_group(group, True, reason="test")
    s.auto_settle_service._settled.add(group.id)

    result = await submit_arb_ticket(s, opp, source="auto")

    assert result.status == "rejected"
    assert result.reason is not None
    assert result.reason.startswith("already_settled:")
    assert result.order_ids == ()
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert [t["status"] for t in tickets] == ["rejected"]


async def test_the_settled_guard_matches_a_synthetic_intra_venue_id():
    """`engine_runtime` publishes `<group>:<venue>` for an intra-venue edge."""
    s, group = await _arb_group()
    opp = s.engine.evaluate_now("eg-1")[0]
    s.auto_settle_service._settled.add(group.id)
    synthetic = replace(opp, event_group_id=f"{group.id}:kalshi")

    result = await submit_arb_ticket(s, synthetic, source="auto")

    assert result.status == "rejected"
    assert result.reason is not None
    assert result.reason.startswith("already_settled:")


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


async def test_atomic_commit_failure_writes_per_leg_order_rows(monkeypatch):
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
    produced without a second venue. That detector is off by default, so this
    test turns it back on -- what is under test is the router's atomic-commit
    unwind, not the venue-pairing policy, and a same-venue group is simply the
    cheapest way to get two legs onto one adapter.
    """
    monkeypatch.setenv("ARBYS_CROSS_VENUE_ONLY", "0")
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
    # Exactly one row -- not just "the newest one is filled". Guards against
    # a regression where the descriptor path logs its own row before
    # delegating to submit_arb_ticket, which would still show tickets[0]
    # filled with a duplicate sitting underneath (list_paper_tickets is
    # newest-first). Matches the pattern in test_filled_ticket_groups_its_legs.
    assert len(tickets) == 1
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


async def _arb_group_with_a_stale_leg(stale_age_s: float = 400.0):
    """The same eg-1 edge, but Polymarket's leg arrived already stale.

    Built via `source_age_s` rather than by re-upserting later, because
    `QuoteBook.upsert` refuses to replace a newer book with an older one — the
    guard that stops a replayed snapshot clobbering live prices. This is the
    real shape of the failure anyway: Polymarket US answers a subscribe with a
    cached book that can be hours old, and it lands looking brand new.
    """
    s = get_state()
    group = EventGroup(
        id="eg-1",
        title="ATP: Bonzi v Zandschulp",
        legs=(
            EventGroupLeg(outcome_id="p-yes", venue_id="polymarket_us", is_yes_side=True),
            EventGroupLeg(outcome_id="k-no", venue_id="kalshi", is_yes_side=False),
        ),
    )
    s.event_groups[group.id] = group
    s.engine.register_group(group)
    async with session_scope() as session:
        await repo.ensure_paper_account(session, s.default_account_id)
    s.quotebook.upsert(
        Quote(
            outcome_id="p-yes",
            bid=Decimal("0.40"),
            ask=Decimal("0.40"),
            source_age_s=stale_age_s,
        )
    )
    s.quotebook.upsert(
        Quote(outcome_id="k-no", bid=Decimal("0.50"), ask=Decimal("0.50"))
    )
    for broker in s.paper_brokers.values():
        broker.deposit(s.default_account_id, Decimal("10000"))
    return s, group


async def test_a_ticket_whose_legs_describe_different_moments_is_refused():
    """An arb is a claim that two venues disagree *right now*. One leg minutes
    behind the other is not evidence of that, and the paper broker fills
    against the stale price as happily as the live one — which is how 23 of
    260 auto fills on 2026-08-27 came to carry 35% of the day's profit."""
    s, _ = await _arb_group_with_a_stale_leg()
    opps = s.engine.evaluate_now("eg-1")
    assert opps, "the stale leg must still produce an edge — that is the problem"
    result = await submit_arb_ticket(s, opps[0], source="auto")
    assert result.status == "rejected"
    assert result.reason is not None
    assert result.reason.startswith("stale_leg_skew:")
    # The per-leg ages ride along in the reason because nothing else records
    # them; paper_order has no age column.
    assert "p-yes" in result.reason and "k-no" in result.reason


async def test_the_stale_leg_rejection_is_recorded_with_its_economics():
    s, _ = await _arb_group_with_a_stale_leg()
    opp = s.engine.evaluate_now("eg-1")[0]
    await submit_arb_ticket(s, opp, source="auto")
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert tickets[0]["status"] == "rejected"
    assert tickets[0]["rejection_reason"].startswith("stale_leg_skew:")
    assert tickets[0]["total_stake"] is not None, "a refused ticket still says how big it would have been"


async def test_two_equally_quiet_legs_are_not_refused():
    """Skew, not absolute age, is the signal. Two legs both quiet for minutes
    are a pre-game market whose price genuinely has not moved — 28 such fills
    over the measured day, worth $1.63 between them. `ARBYS_QUOTE_MAX_AGE_S`
    is what handles everything being too old."""
    s = get_state()
    group = EventGroup(
        id="eg-1",
        title="NCAAF: ALBY @ BUFF",
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
            Quote(outcome_id=oid, bid=px, ask=px, source_age_s=400.0)
        )
    for broker in s.paper_brokers.values():
        broker.deposit(s.default_account_id, Decimal("10000"))
    result = await submit_arb_ticket(s, s.engine.evaluate_now("eg-1")[0], source="auto")
    assert result.status == "filled"


async def test_the_skew_gate_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ARBYS_MAX_LEG_AGE_SKEW_S", "0")
    s, _ = await _arb_group_with_a_stale_leg()
    result = await submit_arb_ticket(s, s.engine.evaluate_now("eg-1")[0], source="auto")
    assert result.status == "filled"


async def test_record_nonfill_false_suppresses_the_row_but_not_the_attempt():
    """The auto-trader passes this for a group it has already logged a
    non-fill for. The attempt must be identical; only the duplicate audit row
    goes away."""
    s, _ = await _arb_group()
    opp = s.engine.evaluate_now("eg-1")[0]
    for oid in ("p-yes", "k-no"):
        s.quotebook.upsert(
            Quote(outcome_id=oid, bid=Decimal("0.60"), ask=Decimal("0.60"))
        )
    result = await submit_arb_ticket(s, opp, source="auto", record_nonfill=False)
    assert result.status == "missed", "the attempt still runs and still reports"
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert tickets == [], "no duplicate row"


async def test_record_nonfill_false_still_lets_a_fill_through():
    """A fill's row has to exist before the router runs — paper_order.ticket_id
    is an FK to it — so the flag must never reach that path."""
    s, _ = await _arb_group()
    opp = s.engine.evaluate_now("eg-1")[0]
    result = await submit_arb_ticket(s, opp, source="auto", record_nonfill=False)
    assert result.status == "filled"
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert len(tickets) == 1
    assert len(tickets[0]["legs"]) == 2


async def test_a_ticket_is_refused_while_draining():
    """A platform restarts on its own schedule, so shutdown has to stop
    accepting work before it stops doing work.

    The refusal is recorded with a distinct reason rather than silently
    dropped: a drained attempt and a vanished edge are different events, and a
    ticket log that conflated them would make a deploy look like a burst of
    missed opportunities."""
    s, _ = await _arb_group()
    opp = s.engine.evaluate_now("eg-1")[0]
    s.begin_draining()

    result = await submit_arb_ticket(s, opp, source="auto")
    assert result.status == "rejected"
    assert result.reason is not None
    assert result.reason.startswith("draining:")
    assert result.order_ids == ()

    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert tickets[0]["status"] == "rejected"
    assert tickets[0]["rejection_reason"].startswith("draining:")


async def test_draining_refuses_before_the_router_runs():
    """Refusing *after* placing a leg would be the bug wearing a different
    hat. Nothing may reach the broker once draining starts."""
    s, _ = await _arb_group()
    opp = s.engine.evaluate_now("eg-1")[0]
    s.begin_draining()
    await submit_arb_ticket(s, opp, source="auto")

    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert tickets[0]["legs"] == [], "a drained ticket must not have touched the broker"


async def test_shutdown_waits_for_an_in_flight_ticket():
    """An in-flight submission is past the cap check and may already have
    applied fills. Shutdown lets it finish rather than abandoning it."""
    import asyncio

    s, _ = await _arb_group()
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_ticket():
        s.enter_ticket()
        started.set()
        try:
            await release.wait()
        finally:
            s.exit_ticket()

    task = asyncio.create_task(slow_ticket())
    await asyncio.wait_for(started.wait(), timeout=2.0)

    shutdown = asyncio.create_task(s.shutdown())
    await asyncio.sleep(0.05)
    assert not shutdown.done(), "shutdown must wait for the in-flight ticket"

    release.set()
    await asyncio.wait_for(shutdown, timeout=5.0)
    await task


async def test_shutdown_gives_up_at_the_bound_rather_than_hanging(monkeypatch):
    """A wedged submit must not block shutdown forever — the platform will
    hard-kill us, which gives no drain at all."""
    import asyncio

    from arbys.backend import state as state_module

    monkeypatch.setattr(state_module, "DRAIN_TIMEOUT_S", 0.2)
    s, _ = await _arb_group()
    s.enter_ticket()  # never exits
    try:
        await asyncio.wait_for(s.shutdown(), timeout=5.0)
    finally:
        s.exit_ticket()


# --- ARBYS_MAX_DAYS_TO_START: capital is not tied up in far-out games -------


async def test_a_game_more_than_the_window_away_is_refused(monkeypatch):
    """A pre-game edge locks its stake until the game settles. On 2026-09-03
    a slate one to two weeks out had both venues out of buying power."""
    monkeypatch.delenv("ARBYS_MAX_DAYS_TO_START", raising=False)  # default: 7
    s, _ = await _arb_group(start_time=datetime.now(UTC) + timedelta(days=8))
    opp = s.engine.evaluate_now("eg-1")[0]

    result = await submit_arb_ticket(s, opp, source="manual")

    assert result.status == "rejected"
    assert result.reason is not None
    assert result.reason.startswith("starts_too_far_out:eg-1")
    assert "limit 7" in result.reason
    assert result.order_ids == ()
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert [t["status"] for t in tickets] == ["rejected"]


async def test_a_game_inside_the_window_fills(monkeypatch):
    monkeypatch.delenv("ARBYS_MAX_DAYS_TO_START", raising=False)
    s, _ = await _arb_group(start_time=datetime.now(UTC) + timedelta(days=6))
    opp = s.engine.evaluate_now("eg-1")[0]

    result = await submit_arb_ticket(s, opp, source="manual")

    assert result.status == "filled", result.reason


async def test_a_game_already_under_way_is_not_refused(monkeypatch):
    """Negative distance to kickoff is in-play, which is the best case."""
    monkeypatch.delenv("ARBYS_MAX_DAYS_TO_START", raising=False)
    s, _ = await _arb_group(start_time=datetime.now(UTC) - timedelta(hours=1))
    opp = s.engine.evaluate_now("eg-1")[0]

    result = await submit_arb_ticket(s, opp, source="manual")

    assert result.status == "filled", result.reason


async def test_an_unknown_start_time_does_not_block(monkeypatch):
    """None means unknown, never "far away" -- a hand-registered group without
    a start time must stay tradeable, as it does for settlement."""
    monkeypatch.delenv("ARBYS_MAX_DAYS_TO_START", raising=False)
    s, _ = await _arb_group(start_time=None)
    opp = s.engine.evaluate_now("eg-1")[0]

    result = await submit_arb_ticket(s, opp, source="manual")

    assert result.status == "filled", result.reason


async def test_the_start_window_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ARBYS_MAX_DAYS_TO_START", "0")
    s, _ = await _arb_group(start_time=datetime.now(UTC) + timedelta(days=30))
    opp = s.engine.evaluate_now("eg-1")[0]

    result = await submit_arb_ticket(s, opp, source="manual")

    assert result.status == "filled", result.reason


async def test_a_naive_start_time_is_read_as_utc(monkeypatch):
    """Discovery writes aware datetimes, but a hand-registered group may not;
    `in_play_slugs` already reads naive as UTC and this must agree."""
    monkeypatch.delenv("ARBYS_MAX_DAYS_TO_START", raising=False)
    naive = (datetime.now(UTC) + timedelta(days=8)).replace(tzinfo=None)
    s, _ = await _arb_group(start_time=naive)
    opp = s.engine.evaluate_now("eg-1")[0]

    result = await submit_arb_ticket(s, opp, source="manual")

    assert result.status == "rejected"
    assert result.reason is not None
    assert result.reason.startswith("starts_too_far_out:")


async def test_the_far_out_refusal_honours_record_nonfill_false(monkeypatch):
    """The auto-trader's duplicate-row suppression applies here as it does to
    every other pre-execution refusal."""
    monkeypatch.delenv("ARBYS_MAX_DAYS_TO_START", raising=False)
    s, _ = await _arb_group(start_time=datetime.now(UTC) + timedelta(days=8))
    opp = s.engine.evaluate_now("eg-1")[0]

    result = await submit_arb_ticket(s, opp, source="auto", record_nonfill=False)

    assert result.status == "rejected"
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert tickets == []
