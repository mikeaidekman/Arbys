"""Async repository functions.

Thin data-access layer over SQLAlchemy sessions. Each function takes an
`AsyncSession` (or an async session factory) and returns plain domain objects
or dicts — never ORM instances leaking out of this module.

This keeps `AppState`, the FastAPI handlers, and the paper broker independent
of SQLAlchemy specifics; if we ever swap Postgres for something else, only
this module changes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..shared.arb_engine import ArbLeg, ArbOpportunity
from ..shared.types import EventGroup, EventGroupLeg
from . import models as m

# ---------------------------------------------------------------------------
# Venues
# ---------------------------------------------------------------------------

async def ensure_venue(session: AsyncSession, venue_id: str, *, name: str, kind: str) -> None:
    existing = await session.get(m.Venue, venue_id)
    if existing is None:
        session.add(m.Venue(id=venue_id, name=name, kind=kind))


# ---------------------------------------------------------------------------
# Outcomes (auto-created when we first see an outcome_id via a quote or event group)
# ---------------------------------------------------------------------------

async def ensure_outcome_placeholder(
    session: AsyncSession, outcome_id: str, *, venue_id: str
) -> None:
    """Auto-create a placeholder market+outcome the first time we see an outcome_id.

    v1 uses hand-curated event groups whose outcome_ids come from adapters
    before we've formally listed markets. This keeps FKs happy without forcing
    the caller to persist a full market row up-front.
    """
    existing = await session.get(m.Outcome, outcome_id)
    if existing is not None:
        return
    placeholder_market_id = f"placeholder:{outcome_id}"
    market = await session.get(m.Market, placeholder_market_id)
    if market is None:
        session.add(
            m.Market(
                id=placeholder_market_id,
                venue_id=venue_id,
                venue_market_id=outcome_id,
                title=f"auto-placeholder for {outcome_id}",
                kind="binary",
            )
        )
        await session.flush()
    session.add(m.Outcome(id=outcome_id, market_id=placeholder_market_id, label=outcome_id))
    # Flush before returning, so the row exists by the time the caller adds a
    # leg pointing at it. `EventGroupLeg.outcome_id` carries a ForeignKey but no
    # `relationship()`, and SQLAlchemy's unit of work orders inserts by
    # relationship dependencies rather than by raw FKs -- so with both pending it
    # is free to write the leg first. The next caller's `session.get` then
    # autoflushes exactly that order and Postgres rejects it.
    #
    # This was invisible for the life of the project: SQLite does not enforce
    # foreign keys unless asked, so dev happily wrote legs referencing outcomes
    # that did not exist yet. It surfaced on the first hosted deploy as
    # `discovery.groups` dropping every batch -- no groups, so no adapters, so
    # no venue was ever contacted.
    await session.flush()


# ---------------------------------------------------------------------------
# Event groups
# ---------------------------------------------------------------------------

async def upsert_event_group(session: AsyncSession, group: EventGroup) -> None:
    existing = await session.get(m.EventGroup, group.id)
    if existing is None:
        session.add(
            m.EventGroup(
                id=group.id,
                title=group.title,
                start_time=group.start_time,
                source=group.source,
            )
        )
    else:
        existing.title = group.title
        existing.source = group.source
        # Don't clobber a known start time with None when a later pass, or a
        # venue that reports no time, re-registers the same group.
        if group.start_time is not None:
            existing.start_time = group.start_time
    # Replace legs wholesale.
    await session.execute(
        delete(m.EventGroupLeg).where(m.EventGroupLeg.event_group_id == group.id)
    )
    await session.flush()
    for leg in group.legs:
        await ensure_outcome_placeholder(session, leg.outcome_id, venue_id=leg.venue_id)
        session.add(
            m.EventGroupLeg(
                event_group_id=group.id,
                outcome_id=leg.outcome_id,
                is_yes_side=leg.is_yes_side,
                resolution_source=group.resolution_source_by_venue.get(leg.venue_id),
            )
        )


async def delete_event_group(session: AsyncSession, group_id: str) -> None:
    await session.execute(
        delete(m.EventGroupLeg).where(m.EventGroupLeg.event_group_id == group_id)
    )
    await session.execute(delete(m.EventGroup).where(m.EventGroup.id == group_id))


async def list_event_groups(session: AsyncSession) -> list[EventGroup]:
    rows = (await session.execute(select(m.EventGroup))).scalars().all()
    result: list[EventGroup] = []
    for row in rows:
        legs = (
            await session.execute(
                select(m.EventGroupLeg, m.Outcome.market_id, m.Market.venue_id)
                .join(m.Outcome, m.EventGroupLeg.outcome_id == m.Outcome.id)
                .join(m.Market, m.Outcome.market_id == m.Market.id)
                .where(m.EventGroupLeg.event_group_id == row.id)
            )
        ).all()
        result.append(
            EventGroup(
                id=row.id,
                title=row.title,
                start_time=row.start_time,
                source=row.source,
                legs=tuple(
                    EventGroupLeg(
                        outcome_id=leg.outcome_id,
                        venue_id=venue_id,
                        is_yes_side=leg.is_yes_side,
                    )
                    for (leg, _market_id, venue_id) in legs
                ),
            )
        )
    return result


# ---------------------------------------------------------------------------
# Quotes (append-only)
# ---------------------------------------------------------------------------

async def insert_quote(
    session: AsyncSession, *, outcome_id: str, bid: Decimal, ask: Decimal,
    bid_size: Decimal | None = None, ask_size: Decimal | None = None,
) -> None:
    session.add(
        m.Quote(
            outcome_id=outcome_id,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
        )
    )


# ---------------------------------------------------------------------------
# Arb opportunities
# ---------------------------------------------------------------------------

async def insert_opportunity(session: AsyncSession, opp: ArbOpportunity) -> None:
    session.add(
        m.ArbOpportunity(
            event_group_id=opp.event_group_id,
            legs=[_leg_to_json(leg) for leg in opp.legs],
            total_stake=opp.total_stake,
            guaranteed_profit=opp.guaranteed_profit,
            guaranteed_profit_bps=opp.guaranteed_profit_bps,
        )
    )


def _leg_to_json(leg: ArbLeg) -> dict:
    return {
        "outcome_id": leg.outcome_id,
        "venue_id": leg.venue_id,
        "is_buy": leg.is_buy,
        "price": str(leg.price),
        "qty": str(leg.qty),
        "fee": str(leg.fee),
    }


# ---------------------------------------------------------------------------
# Paper trading
# ---------------------------------------------------------------------------

async def ensure_paper_account(
    session: AsyncSession, account_id: str, *, name: str | None = None
) -> None:
    existing = await session.get(m.PaperAccount, account_id)
    if existing is None:
        session.add(m.PaperAccount(id=account_id, name=name or account_id))


async def upsert_paper_balance(
    session: AsyncSession, *, account_id: str, venue_id: str, amount: Decimal
) -> None:
    row = (
        await session.execute(
            select(m.PaperBalance).where(
                m.PaperBalance.account_id == account_id,
                m.PaperBalance.venue_id == venue_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(
            m.PaperBalance(account_id=account_id, venue_id=venue_id, amount=amount)
        )
    else:
        row.amount = amount


async def insert_paper_order(
    session: AsyncSession, *, order_id: str, account_id: str, venue_id: str,
    outcome_id: str, is_buy: bool, qty: Decimal, limit_price: Decimal, status: str,
    arb_opportunity_id: str | None = None, rejection_reason: str | None = None,
    ticket_id: str | None = None,
) -> None:
    await ensure_outcome_placeholder(session, outcome_id, venue_id=venue_id)
    session.add(
        m.PaperOrder(
            id=order_id,
            account_id=account_id,
            venue_id=venue_id,
            outcome_id=outcome_id,
            is_buy=is_buy,
            qty=qty,
            limit_price=limit_price,
            status=status,
            arb_opportunity_id=arb_opportunity_id,
            rejection_reason=rejection_reason,
            ticket_id=ticket_id,
        )
    )
    # Flush for the same reason ensure_outcome_placeholder does: `paper_fill`
    # references this row by FK with no `relationship()` between them, so a
    # caller that records an order and its fill in one transaction is otherwise
    # at the mercy of the unit of work's insert order. Production happens to be
    # safe -- DbPaperPersistenceSink commits on_order before on_fill -- which is
    # precisely why nothing would have caught it here.
    await session.flush()


async def insert_paper_fill(
    session: AsyncSession, *, order_id: str, qty: Decimal, price: Decimal, fee: Decimal
) -> None:
    session.add(m.PaperFill(order_id=order_id, qty=qty, price=price, fee=fee))


async def insert_paper_ticket(
    session: AsyncSession, *, ticket_id: str, account_id: str, event_group_id: str,
    title_snapshot: str, source: str, status: str,
    rejection_reason: str | None = None, total_stake: Decimal | None = None,
    expected_profit: Decimal | None = None, expected_edge_bps: Decimal | None = None,
    starts_at: datetime | None = None,
) -> None:
    session.add(
        m.PaperTicket(
            id=ticket_id,
            account_id=account_id,
            event_group_id=event_group_id,
            title_snapshot=title_snapshot,
            starts_at=starts_at,
            source=source,
            status=status,
            rejection_reason=rejection_reason,
            total_stake=total_stake,
            expected_profit=expected_profit,
            expected_edge_bps=expected_edge_bps,
        )
    )


async def insert_paper_settlement(
    session: AsyncSession, *, outcome_id: str, venue_id: str,
    resolved_value: Decimal, source: str = "heuristic",
) -> None:
    await ensure_outcome_placeholder(session, outcome_id, venue_id=venue_id)
    session.add(
        m.PaperSettlement(
            outcome_id=outcome_id, resolved_value=resolved_value, source=source
        )
    )


async def list_paper_settlements(session: AsyncSession) -> list[dict]:
    """Latest settlement per outcome, newest first."""
    rows = (
        await session.execute(
            select(m.PaperSettlement).order_by(m.PaperSettlement.ts.desc())
        )
    ).scalars().all()
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        if r.outcome_id in seen:
            continue
        seen.add(r.outcome_id)
        out.append(
            {
                "outcome_id": r.outcome_id,
                "resolved_value": r.resolved_value,
                "ts": r.ts,
                "source": r.source,
            }
        )
    return out


async def upsert_paper_position(
    session: AsyncSession, *, account_id: str, venue_id: str, outcome_id: str,
    qty: Decimal, avg_price: Decimal, realized_pnl: Decimal,
    open_fees: Decimal = Decimal("0"),
) -> None:
    await ensure_outcome_placeholder(session, outcome_id, venue_id=venue_id)
    row = (
        await session.execute(
            select(m.PaperPosition).where(
                m.PaperPosition.account_id == account_id,
                m.PaperPosition.venue_id == venue_id,
                m.PaperPosition.outcome_id == outcome_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(
            m.PaperPosition(
                account_id=account_id,
                venue_id=venue_id,
                outcome_id=outcome_id,
                qty=qty,
                avg_price=avg_price,
                realized_pnl=realized_pnl,
                open_fees=open_fees,
            )
        )
    else:
        row.qty = qty
        row.avg_price = avg_price
        row.realized_pnl = realized_pnl
        row.open_fees = open_fees


async def insert_paper_pnl_snapshot(
    session: AsyncSession, *, account_id: str,
    cash: Decimal, mtm_positions: Decimal, total_equity: Decimal,
    ts: datetime | None = None,
) -> None:
    session.add(
        m.PaperPnlSnapshot(
            account_id=account_id,
            ts=ts or datetime.now(UTC),
            cash=cash,
            mtm_positions=mtm_positions,
            total_equity=total_equity,
        )
    )


async def delete_paper_history(session: AsyncSession, account_id: str) -> None:
    """Wipe all paper trading history for an account.

    Deletes pnl snapshots, fills (via their orders), orders, tickets,
    positions, and balances, and wipes every `paper_settlement` row (see
    below). Leaves the paper_account row itself intact.
    """
    order_ids = (
        await session.execute(
            select(m.PaperOrder.id).where(m.PaperOrder.account_id == account_id)
        )
    ).scalars().all()
    if order_ids:
        await session.execute(
            delete(m.PaperFill).where(m.PaperFill.order_id.in_(order_ids))
        )
    await session.execute(
        delete(m.PaperOrder).where(m.PaperOrder.account_id == account_id)
    )
    # Orders are already gone, so no paper_order.ticket_id still points at
    # these rows. Leaving tickets behind after their orders are deleted is
    # what used to make count_open_paper_tickets' outer join see a ticket
    # with no matching order (outcome_id is None) and count it as open
    # forever, and list_paper_tickets kept showing it as "filled" with an
    # empty legs list.
    await session.execute(
        delete(m.PaperTicket).where(m.PaperTicket.account_id == account_id)
    )
    # paper_settlement has no account_id column: it's keyed by outcome_id,
    # because on a real exchange settlement is global, not per-account. This
    # is a single-account paper simulator though, so "reset the account" and
    # "clear every settlement this simulator has recorded" are the same
    # operation in practice — delete them all. Leaving stale rows behind
    # would score a brand-new ticket on a previously-settled outcome
    # immediately against that old resolution, before the ticket's own legs
    # ever settle.
    await session.execute(delete(m.PaperSettlement))
    await session.execute(
        delete(m.PaperPnlSnapshot).where(m.PaperPnlSnapshot.account_id == account_id)
    )
    await session.execute(
        delete(m.PaperPosition).where(m.PaperPosition.account_id == account_id)
    )
    await session.execute(
        delete(m.PaperBalance).where(m.PaperBalance.account_id == account_id)
    )


async def list_recent_opportunities(session: AsyncSession, *, limit: int = 50) -> list[dict]:
    rows = (
        await session.execute(
            select(m.ArbOpportunity)
            .order_by(m.ArbOpportunity.detected_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "id": r.id,
            "event_group_id": r.event_group_id,
            "detected_at": r.detected_at,
            "legs": r.legs,
            "total_stake": r.total_stake,
            "guaranteed_profit": r.guaranteed_profit,
            "guaranteed_profit_bps": r.guaranteed_profit_bps,
            "status": r.status,
        }
        for r in rows
    ]


async def list_paper_orders(session: AsyncSession, account_id: str) -> list[dict]:
    rows = (
        await session.execute(
            select(m.PaperOrder)
            .where(m.PaperOrder.account_id == account_id)
            .order_by(m.PaperOrder.submitted_at.desc())
        )
    ).scalars().all()
    return [
        {
            "id": r.id,
            "ticket_id": r.ticket_id,
            "venue_id": r.venue_id,
            "outcome_id": r.outcome_id,
            "is_buy": r.is_buy,
            "qty": r.qty,
            "limit_price": r.limit_price,
            "status": r.status,
            "rejection_reason": r.rejection_reason,
            "submitted_at": r.submitted_at,
        }
        for r in rows
    ]


async def list_pnl_snapshots(
    session: AsyncSession, account_id: str, *, limit: int = 500
) -> list[dict]:
    rows = (
        await session.execute(
            select(m.PaperPnlSnapshot)
            .where(m.PaperPnlSnapshot.account_id == account_id)
            .order_by(m.PaperPnlSnapshot.ts.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "ts": r.ts,
            "cash": r.cash,
            "mtm_positions": r.mtm_positions,
            "total_equity": r.total_equity,
        }
        for r in rows
    ]


async def list_paper_tickets(
    session: AsyncSession, account_id: str, *, limit: int = 200,
    status: str | None = None, source: str | None = None,
) -> list[dict]:
    """Ticket-level history, newest first, with fills joined and scoring.

    `realized_profit` is computed here rather than stored, from the ticket's
    **own** fills. Broker state cannot answer this: settlement uses an
    `avg_price` blended across every ticket on that outcome, and
    ARBYS_MAX_OUTCOME_QTY permits roughly 2.5 tickets on one.
    """
    stmt = select(m.PaperTicket).where(m.PaperTicket.account_id == account_id)
    if status is not None:
        stmt = stmt.where(m.PaperTicket.status == status)
    if source is not None:
        stmt = stmt.where(m.PaperTicket.source == source)
    tickets = (
        await session.execute(
            stmt.order_by(m.PaperTicket.submitted_at.desc()).limit(limit)
        )
    ).scalars().all()
    if not tickets:
        return []

    ticket_ids = [t.id for t in tickets]
    orders = (
        await session.execute(
            select(m.PaperOrder)
            .where(m.PaperOrder.ticket_id.in_(ticket_ids))
            .order_by(m.PaperOrder.submitted_at, m.PaperOrder.id)
        )
    ).scalars().all()

    fills_by_order: dict[str, tuple[Decimal, Decimal]] = {}
    if orders:
        fill_rows = (
            await session.execute(
                select(m.PaperFill).where(
                    m.PaperFill.order_id.in_([o.id for o in orders])
                )
            )
        ).scalars().all()
        for f in fill_rows:
            _price, fee = fills_by_order.get(f.order_id, (Decimal("0"), Decimal("0")))
            fills_by_order[f.order_id] = (f.price, fee + f.fee)

    settled = {
        row["outcome_id"]: row["resolved_value"]
        for row in await list_paper_settlements(session)
    }

    legs_by_ticket: dict[str, list[dict]] = {tid: [] for tid in ticket_ids}
    for o in orders:
        fill_price, fee = fills_by_order.get(o.id, (None, Decimal("0")))
        legs_by_ticket[o.ticket_id].append(
            {
                "venue_id": o.venue_id,
                "outcome_id": o.outcome_id,
                "is_buy": o.is_buy,
                "qty": o.qty,
                "limit_price": o.limit_price,
                "fill_price": fill_price,
                "fee": fee,
                "resolved_value": settled.get(o.outcome_id),
                "status": o.status,
                "rejection_reason": o.rejection_reason,
            }
        )

    out: list[dict] = []
    for t in tickets:
        legs = legs_by_ticket[t.id]
        out.append(
            {
                "id": t.id,
                "event_group_id": t.event_group_id,
                "title_snapshot": t.title_snapshot,
                "source": t.source,
                "status": t.status,
                "rejection_reason": t.rejection_reason,
                "total_stake": t.total_stake,
                "expected_profit": t.expected_profit,
                "expected_edge_bps": t.expected_edge_bps,
                "submitted_at": t.submitted_at,
                "starts_at": t.starts_at,
                "realized_profit": _score_ticket(legs, settled),
                "legs": legs,
            }
        )
    return out


def _score_ticket(
    legs: list[dict], settled: dict[str, Decimal]
) -> Decimal | None:
    """Realized profit, or None while any leg is unsettled.

    A sold leg inverts: the direction factor keeps the sign right even though
    every detector currently emits buys only.
    """
    if not legs:
        return None
    total = Decimal("0")
    for leg in legs:
        if leg["fill_price"] is None:
            return None
        resolved = settled.get(leg["outcome_id"])
        if resolved is None:
            return None
        direction = Decimal("1") if leg["is_buy"] else Decimal("-1")
        total += direction * (resolved - leg["fill_price"]) * leg["qty"] - leg["fee"]
    return total


async def count_open_paper_tickets(session: AsyncSession, account_id: str) -> int:
    """Filled tickets with at least one leg still unsettled.

    Counted in SQL rather than by hydrating `list_paper_tickets`, which is
    capped at 200 rows and would silently undercount an active account — and
    which the summary endpoint polls every few seconds. Mirrors
    `_score_ticket`'s open/settled semantics exactly (no fill yet, or the
    leg's outcome carries no settlement row, means open) via a two-query
    approach rather than one gnarly SQL boolean, so the two can never
    disagree and neither is bounded by a row limit.
    """
    rows = (
        await session.execute(
            select(m.PaperTicket.id, m.PaperOrder.outcome_id, m.PaperFill.price)
            .select_from(m.PaperTicket)
            .outerjoin(m.PaperOrder, m.PaperOrder.ticket_id == m.PaperTicket.id)
            .outerjoin(m.PaperFill, m.PaperFill.order_id == m.PaperOrder.id)
            .where(
                m.PaperTicket.account_id == account_id,
                m.PaperTicket.status == "filled",
            )
        )
    ).all()
    if not rows:
        return 0
    settled_outcomes = {
        row[0]
        for row in (
            await session.execute(select(m.PaperSettlement.outcome_id).distinct())
        ).all()
    }
    open_ids: set[str] = set()
    for ticket_id, outcome_id, fill_price in rows:
        if outcome_id is None or fill_price is None or outcome_id not in settled_outcomes:
            open_ids.add(ticket_id)
    return len(open_ids)


async def paper_position_meta(
    session: AsyncSession, account_id: str
) -> dict[str, tuple[str, str | None]]:
    """outcome_id -> (best available human title, owning event_group_id).

    Prefers the most recent ticket that traded the outcome, because its
    `title_snapshot` survives group retirement. Falls back to the live
    event_group join for outcomes only ever quoted, never traded.

    The group id rides along so a caller can group a game's legs together
    exactly. Grouping on the title *string* would split one game into two
    rows whenever its legs resolve their titles from different sources — a
    ticket snapshot on one leg, the live join on the other — which is
    precisely what a renamed group produces.
    """
    rows = (
        await session.execute(
            select(
                m.PaperOrder.outcome_id,
                m.PaperTicket.title_snapshot,
                m.PaperTicket.event_group_id,
            )
            .join(m.PaperTicket, m.PaperTicket.id == m.PaperOrder.ticket_id)
            .where(m.PaperOrder.account_id == account_id)
            .order_by(m.PaperTicket.submitted_at.desc())
        )
    ).all()
    meta: dict[str, tuple[str, str | None]] = {}
    for outcome_id, title, group_id in rows:
        meta.setdefault(outcome_id, (title, group_id))

    live = (
        await session.execute(
            select(
                m.EventGroupLeg.outcome_id, m.EventGroup.title, m.EventGroup.id
            ).join(m.EventGroup, m.EventGroup.id == m.EventGroupLeg.event_group_id)
        )
    ).all()
    for outcome_id, title, group_id in live:
        meta.setdefault(outcome_id, (title, group_id))
    return meta


async def paper_position_titles(
    session: AsyncSession, account_id: str
) -> dict[str, str]:
    """outcome_id -> best available human title. See `paper_position_meta`."""
    meta = await paper_position_meta(session, account_id)
    return {outcome_id: title for outcome_id, (title, _gid) in meta.items()}


async def update_paper_ticket_status(
    session: AsyncSession, ticket_id: str, *, status: str,
    rejection_reason: str | None = None,
) -> None:
    row = await session.get(m.PaperTicket, ticket_id)
    if row is None:
        return
    row.status = status
    row.rejection_reason = rejection_reason


__all__ = [
    "count_open_paper_tickets",
    "delete_event_group",
    "ensure_outcome_placeholder",
    "ensure_paper_account",
    "ensure_venue",
    "insert_opportunity",
    "insert_paper_fill",
    "insert_paper_order",
    "insert_paper_pnl_snapshot",
    "insert_paper_settlement",
    "insert_paper_ticket",
    "insert_quote",
    "list_event_groups",
    "list_paper_orders",
    "list_paper_settlements",
    "list_paper_tickets",
    "list_pnl_snapshots",
    "list_recent_opportunities",
    "paper_position_meta",
    "paper_position_titles",
    "update_paper_ticket_status",
    "upsert_event_group",
    "upsert_paper_balance",
    "upsert_paper_position",
]
