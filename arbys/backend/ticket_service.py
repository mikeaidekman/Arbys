"""Submitting an arb ticket — the one module that writes trade history.

Everything that submits goes through here rather than through logic living in
the endpoint, for three reasons:

* The ARBYS_MAX_OUTCOME_QTY check used to live in `app.py`, so any non-HTTP
  caller bypassed it silently and stacked positions without bound.
* A ticket's identity has to be minted before the intent is built, so the
  legs can be grouped.
* Rejected and missed tickets are the most valuable rows in the audit log and
  they have to be written somewhere both callers share.

There are two entry points, not one: `submit_arb_ticket` (what the
auto-trader calls, with an opportunity already in hand) and
`submit_arb_ticket_for_descriptor` (what `POST /paper/execute` calls, so a
click can describe *what* to submit without having resolved a live
opportunity itself). An attempt is logged once it reaches either one, even
when that call finds no live opportunity to submit — `missed` is still a
logged outcome, and `submit_arb_ticket_for_descriptor`'s own no-candidate
branch writes one directly, without ever calling `submit_arb_ticket`. What is
never logged is a background detector tick that finds nothing and never
calls into this module at all: "the detector found nothing" during routine
evaluation is not an attempt, and logging it would fill the ticket log with
thousands of rows a night saying nothing happened. A human pressing a button
always reaches one of these two functions and is always logged, whether or
not an edge is still there when it does.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.base import ExecutionIntent, IntentLeg
from ..db import repositories as repo
from ..db.session import run_write
from ..shared.arb_engine import ArbOpportunity
from ..shared.execution_router import InsufficientLegsError
from .state import max_leg_age_skew_s, max_outcome_qty

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .state import AppState


@dataclass(frozen=True)
class TicketResult:
    ticket_id: str
    status: str
    order_ids: tuple[str, ...]
    reason: str | None


def _buy_legs(opp: ArbOpportunity) -> frozenset[str]:
    return frozenset(leg.outcome_id for leg in opp.legs if leg.is_buy)


def _match_live(state: AppState, opp: ArbOpportunity) -> ArbOpportunity | None:
    """Re-detect and return the same edge at live prices, or None.

    Replaying a recorded opportunity is what produced `limit_exceeded` once the
    market moved, so the ticket is always priced against the current book.
    """
    wanted = _buy_legs(opp)
    for candidate in state.live_opportunities_for(opp.event_group_id):
        if candidate.event_group_id != opp.event_group_id:
            continue
        if _buy_legs(candidate) == wanted:
            return candidate
    return None


def _title(state: AppState, event_group_id: str) -> str:
    """Freeze the group's title. Discovery deletes groups; history outlives
    them, so the name is snapshotted rather than joined at read time."""
    base_id = event_group_id.split(":", 1)[0]
    group = state.event_groups.get(base_id)
    return group.title if group is not None else event_group_id


async def _write_ticket(
    *, ticket_id: str, account_id: str, opp: ArbOpportunity, title: str,
    source: str, status: str, reason: str | None,
    economics: ArbOpportunity | None,
) -> None:
    """Persist the ticket.

    Never raises: an unrecorded ticket is acceptable, a broken trade is not.
    A write abandoned here is counted in `dropped_write_stats()`.
    """

    async def work(session: AsyncSession) -> None:
        # A non-default account_id may never have had a paper_account row
        # created for it. On Postgres that makes the ticket's FK fail, which
        # run_write's retry-then-count handles -- so with no ticket row, the
        # sink's paper_order.ticket_id FK then fails too (also counted), and a
        # trade executes in memory with zero database rows. ensure_paper_account
        # is idempotent, so this is free on the common "default" path.
        await repo.ensure_paper_account(session, account_id)
        await repo.insert_paper_ticket(
            session,
            ticket_id=ticket_id,
            account_id=account_id,
            event_group_id=opp.event_group_id,
            title_snapshot=title,
            source=source,
            status=status,
            rejection_reason=reason,
            total_stake=None if economics is None else economics.total_stake,
            expected_profit=(
                None if economics is None else economics.guaranteed_profit
            ),
            expected_edge_bps=(
                None if economics is None else economics.guaranteed_profit_bps
            ),
        )

    await run_write("ticket.insert", work)


async def _set_status(ticket_id: str, *, status: str, reason: str | None) -> None:
    """Move a pending ticket to its final status.

    Never raises. A write abandoned here is counted in
    `dropped_write_stats()` -- this is the path that left a ticket stuck at
    `pending` on 2026-08-25, because the lock error was swallowed with no
    trace.
    """
    await run_write(
        "ticket.status",
        lambda s: repo.update_paper_ticket_status(
            s, ticket_id, status=status, rejection_reason=reason
        ),
    )


async def _write_rejected_legs(
    *, ticket_id: str, account_id: str, live: ArbOpportunity,
    reasons: dict[str, str],
) -> None:
    """One row per attempted leg, so the attempted prices are recorded.

    A leg that previewed fine still gets a row: the ticket failed as a whole
    and no leg was submitted. All legs are written in a single `run_write`
    call so the batch stays atomic -- one transaction, all legs or none.
    """

    async def work(session: AsyncSession) -> None:
        for leg in live.legs:
            await repo.insert_paper_order(
                session,
                order_id=uuid.uuid4().hex,
                account_id=account_id,
                venue_id=leg.venue_id,
                outcome_id=leg.outcome_id,
                is_buy=leg.is_buy,
                qty=leg.qty,
                limit_price=leg.price,
                status="rejected",
                rejection_reason=reasons.get(leg.outcome_id, "ticket_rejected"),
                ticket_id=ticket_id,
            )

    await run_write("ticket.rejected_legs", work)


def stale_leg_skew(state: AppState, live: ArbOpportunity) -> str | None:
    """The reason this ticket's legs describe different moments, or None.

    An arbitrage is a claim that two venues disagree *right now*. If one leg's
    quote arrived seconds ago and the other's is minutes old, the "divergence"
    may just be the market having moved while one feed was not answering --
    and a paper broker fills against the stale price as happily as the live
    one, booking profit that was never there.

    Measured over 2026-08-27: 23 of 260 auto fills (8.8%) had one leg stale
    and the other live, and those 23 carried **$34.55 of the day's $97.71**
    expected profit -- 35%. The clearest was `atp-BONZI-ZANDSCHULP`, where our
    Polymarket leg sat pinned at 0.86 for seven minutes while Kalshi ran
    0.15 -> 0.02 toward resolution; it produced the single richest fill of the
    day and five more besides. The skew distribution over those fills is
    bimodal with a clean gap -- 0, 1, 2, 4, 22, 28 seconds, then nothing until
    36 -- so the 30s default sits in observed empty space rather than on a
    guess.

    Skew, not absolute age, is the discriminating signal. Two legs both quiet
    for ten minutes are a pre-game market whose price genuinely has not moved
    (28 such fills, $1.63 between them); one quiet leg against one busy leg is
    a feed that stopped answering. `ARBYS_QUOTE_MAX_AGE_S` already handles the
    case where *everything* is too old.

    Ages come from `get_with_age`, which counts from the back-dated arrival
    stamp -- so a snapshot the venue replayed hours late is old the moment it
    lands, which is exactly the leg this must catch.
    """
    limit = max_leg_age_skew_s()
    if limit is None:
        return None
    ages: dict[str, float] = {}
    for leg in live.legs:
        if not leg.is_buy:
            continue
        entry = state.quotebook.get_with_age(leg.outcome_id)
        if entry is None:
            # No quote at all for a leg we are about to buy. Detection cannot
            # have used one either, so there is nothing to compare; the
            # execution path rejects this on its own merits.
            return None
        ages[leg.outcome_id] = entry[1]
    if len(ages) < 2:
        return None
    skew = max(ages.values()) - min(ages.values())
    if skew <= limit:
        return None
    detail = ", ".join(f"{oid} {age:.1f}s" for oid, age in sorted(ages.items()))
    # `stale_leg_skew:` prefix is a machine-matchable marker, matching how
    # `position_cap:` and `edge_no_longer_available:` mark their own reasons,
    # so the ticket log can be grouped on it. The per-leg ages ride along in
    # the prose because nothing else records them -- `paper_order` has no age
    # column, so without this the evidence for a phantom fill exists only for
    # as long as the process that saw it.
    return (
        f"stale_leg_skew:{skew:.1f}s between legs exceeds {limit:.0f}s "
        f"({detail})"
    )[:256]


def cap_breach(state: AppState, live: ArbOpportunity, account_id: str) -> str | None:
    """The outcome that would exceed ARBYS_MAX_OUTCOME_QTY, or None.

    Public because the auto-trader pre-checks the same condition before
    submitting. Duplicating this logic there would mean two implementations of
    one safety rule, free to drift apart; this stays the single source and
    `submit_arb_ticket` stays the authoritative enforcement point.
    """
    cap = max_outcome_qty()
    if cap is None:
        return None
    for leg in live.legs:
        if not leg.is_buy:
            continue
        broker = state.paper_brokers.get(leg.venue_id)
        if broker is None:
            continue
        _cash, positions = broker.account_snapshot(account_id)
        held = positions.get(leg.outcome_id, (Decimal("0"),))[0]
        if held + leg.qty > cap:
            # `position_cap:` prefix is a machine-matchable marker (see
            # test_position_cap_is_enforced_here_not_in_the_endpoint); the
            # words "position cap" also have to appear for the HTTP endpoint,
            # which surfaces this string verbatim as the 409 detail (see
            # test_repeat_fills_stop_at_the_position_cap).
            return (
                f"position_cap:{leg.outcome_id} would exceed the position cap "
                f"of {cap}: holds {held}, ticket adds {leg.qty}. Raise "
                f"ARBYS_MAX_OUTCOME_QTY or reset the account."
            )
    return None


async def submit_arb_ticket(
    state: AppState,
    opp: ArbOpportunity,
    *,
    source: str,
    account_id: str | None = None,
    record_nonfill: bool = True,
) -> TicketResult:
    """Submit one opportunity, recording the attempt.

    `record_nonfill=False` runs the attempt identically but skips the audit
    row when the outcome is a miss or a *pre-execution* rejection. Only the
    auto-trader passes it, and only for a group it has already logged a
    non-fill for inside its window -- a live book republishes on every depth
    tick, so without it one dying edge writes a row per tick (1,149 missed
    tickets across 116 groups on 2026-08-27, 74% of the repeats in the same
    second). A human click is always an attempt and always recorded.

    It cannot suppress a rejection raised *after* execution begins: that
    ticket's row is already written, because `paper_order.ticket_id` is an FK
    to it. That is the right split anyway -- a ticket that reached the router
    and failed there is a real event, not a repeat of a pre-flight refusal.
    """
    account_id = account_id or state.default_account_id
    ticket_id = uuid.uuid4().hex
    title = _title(state, opp.event_group_id)

    live = _match_live(state, opp)
    if live is None:
        # Same `edge_no_longer_available:<group>` prefix as the descriptor
        # path below, so both land in the same shape in `rejection_reason`
        # and a query can group on it to answer "how often does an edge die
        # between publication and submission" across both entry points.
        reason = f"edge_no_longer_available:{opp.event_group_id}"
        if record_nonfill:
            await _write_ticket(
                ticket_id=ticket_id, account_id=account_id, opp=opp, title=title,
                source=source, status="missed", reason=reason, economics=None,
            )
        return TicketResult(ticket_id, "missed", (), reason)

    # Before the cap, because a ticket priced off a leg the venue abandoned
    # should not be judged on whether we could afford it.
    skew = stale_leg_skew(state, live)
    if skew is not None:
        if record_nonfill:
            await _write_ticket(
                ticket_id=ticket_id, account_id=account_id, opp=live, title=title,
                source=source, status="rejected", reason=skew, economics=live,
            )
        return TicketResult(ticket_id, "rejected", (), skew)

    breach = cap_breach(state, live, account_id)
    if breach is not None:
        if record_nonfill:
            await _write_ticket(
                ticket_id=ticket_id, account_id=account_id, opp=live, title=title,
                source=source, status="rejected", reason=breach, economics=live,
            )
        return TicketResult(ticket_id, "rejected", (), breach)

    intent = ExecutionIntent(
        event_group_id=live.event_group_id,
        account_id=account_id,
        ticket_id=ticket_id,
        legs=tuple(
            IntentLeg(
                venue_id=leg.venue_id,
                outcome_id=leg.outcome_id,
                is_buy=leg.is_buy,
                qty=leg.qty,
                limit_price=leg.price,
            )
            for leg in live.legs
        ),
    )
    # The ticket row must exist before the router runs: paper_order.ticket_id
    # is an FK to it, and the sink writes order rows from inside submit().
    await _write_ticket(
        ticket_id=ticket_id, account_id=account_id, opp=live, title=title,
        source=source, status="pending", reason=None, economics=live,
    )

    try:
        orders = await state.router.submit(intent)
    except InsufficientLegsError as e:
        reason = str(e)
        await _set_status(ticket_id, status="rejected", reason=reason)
        # e.legs_persisted is the discriminator, not whether e.rejections is
        # structured: only `_commit_sequentially` has already persisted a
        # paper_order row for each attempted leg (via `place_order` ->
        # `emit_order_events`, including the one that filled) by the time it
        # raises, so writing rejected-leg rows here for that case would
        # duplicate the filled leg -- once `filled` with a real fill, once
        # `rejected` -- which corrupts `_score_ticket` (a phantom rejected
        # leg makes it return None forever) and duplicates the frontend's
        # `${venue_id}:${outcome_id}` React key. The preview phase and
        # `_commit_atomically`'s post-preview failure both raise having
        # persisted nothing -- the latter unwinds every applied leg before
        # raising -- so their legs must be written here or the audit record
        # is lost entirely (a rejected ticket with `legs: []`).
        if not e.legs_persisted:
            await _write_rejected_legs(
                ticket_id=ticket_id,
                account_id=account_id,
                live=live,
                reasons={r.outcome_id: r.reason for r in e.rejections},
            )
        return TicketResult(ticket_id, "rejected", (), reason)

    await _set_status(ticket_id, status="filled", reason=None)
    return TicketResult(
        ticket_id, "filled", tuple(o.id for o in orders), None
    )


async def submit_arb_ticket_for_descriptor(
    state: AppState,
    *,
    event_group_id: str,
    outcome_ids: set[str] | None,
    source: str,
    account_id: str | None = None,
) -> TicketResult:
    """Submit the ticket a caller *described*, recording it even if it cannot.

    `POST /paper/execute` used to resolve the opportunity itself and raise 409
    before reaching this module, so a click on a row whose edge had just died
    left no record — the single most common real failure, and the one
    measurement that tells you whether latency work is worth anything.

    The "a detector finding nothing is not an attempt" rule still holds for
    `submit_arb_ticket` itself, which is what the auto-trader calls: a bot
    evaluating every tick would otherwise write thousands of rows a night
    saying nothing happened. A human pressing a button is unambiguously an
    attempt.
    """
    account_id = account_id or state.default_account_id
    for candidate in state.live_opportunities_for(event_group_id):
        if candidate.event_group_id != event_group_id:
            continue
        if outcome_ids is not None and _buy_legs(candidate) != outcome_ids:
            continue
        return await submit_arb_ticket(
            state, candidate, source=source, account_id=account_id
        )

    ticket_id = uuid.uuid4().hex
    # Same `edge_no_longer_available:<group>` prefix `submit_arb_ticket` uses
    # for the same conceptual state, plus prose that keeps the "live quotes"
    # substring the endpoint's 409 detail is pinned on
    # (test_execute_prices_against_live_quotes_not_the_recorded_opportunity).
    # No `!r` here -- this lands in a column a human reads in the ticket log,
    # not a repr a developer reads in a traceback.
    reason = (
        f"edge_no_longer_available:{event_group_id} "
        "(no edge at live quotes for this pair)"
    )
    await _write_missed_descriptor(
        ticket_id=ticket_id,
        account_id=account_id,
        event_group_id=event_group_id,
        title=_title(state, event_group_id),
        source=source,
        reason=reason,
    )
    return TicketResult(ticket_id, "missed", (), reason)


async def _write_missed_descriptor(
    *, ticket_id: str, account_id: str, event_group_id: str, title: str,
    source: str, reason: str,
) -> None:
    """A missed ticket with no opportunity object behind it, so no economics.

    Null rather than zero: a `missed` ticket has nothing to record, and zero
    would read as a free ticket that made nothing.
    """
    async def _work(session: AsyncSession) -> None:
        await repo.ensure_paper_account(session, account_id)
        await repo.insert_paper_ticket(
            session,
            ticket_id=ticket_id,
            account_id=account_id,
            event_group_id=event_group_id,
            title_snapshot=title,
            source=source,
            status="missed",
            rejection_reason=reason,
        )

    await run_write("ticket.missed", _work)
