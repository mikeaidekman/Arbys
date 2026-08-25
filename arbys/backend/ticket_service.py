"""Submitting an arb ticket — the one path that writes trade history.

Everything that submits goes through here: the HTTP endpoint and the
auto-trader alike. Three reasons it is a single function rather than logic in
the endpoint:

* The ARBYS_MAX_OUTCOME_QTY check used to live in `app.py`, so any non-HTTP
  caller bypassed it silently and stacked positions without bound.
* A ticket's identity has to be minted before the intent is built, so the
  legs can be grouped.
* Rejected and missed tickets are the most valuable rows in the audit log and
  they have to be written somewhere both callers share.

An attempt is logged only once it reaches this function. "The detector found
nothing" is not an attempt and is never written — otherwise a bot writes
thousands of rows a night saying nothing happened.
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
from .state import max_outcome_qty

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


def _cap_breach(state: AppState, live: ArbOpportunity, account_id: str) -> str | None:
    """The outcome that would exceed ARBYS_MAX_OUTCOME_QTY, or None."""
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
) -> TicketResult:
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
        await _write_ticket(
            ticket_id=ticket_id, account_id=account_id, opp=opp, title=title,
            source=source, status="missed", reason=reason, economics=None,
        )
        return TicketResult(ticket_id, "missed", (), reason)

    breach = _cap_breach(state, live, account_id)
    if breach is not None:
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
