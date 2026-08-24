"""FastAPI app exposing scanner + paper trading over REST + WebSocket."""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from typing import NamedTuple

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

# Load .env from the working directory so ARBYS_* flags are honored when
# uvicorn is launched with no explicit --env-file.
load_dotenv()

from ..db import repositories as repo  # noqa: E402
from ..db.session import session_scope  # noqa: E402
from ..shared.arb_engine import (  # noqa: E402
    DEFAULT_QTY_TICK,
    leg_unit_cost,
    net_edge_per_contract,
)
from ..shared.equity import account_equity  # noqa: E402
from ..shared.qty import tradeable_qty  # noqa: E402
from ..shared.types import EventGroup, EventGroupLeg, Quote  # noqa: E402
from .schemas import (  # noqa: E402
    ArbLegOut,
    ArbOpportunityOut,
    EventGroupIn,
    EventGroupOut,
    ExecuteArbIn,
    MonitoredGroupOut,
    MonitoredLegOut,
    PaperAccountSummary,
    PositionOut,
    QuoteIn,
    TicketOut,
)
from .state import get_state, max_ticket_stake, reset_state  # noqa: E402
from .ticket_service import submit_arb_ticket  # noqa: E402


class _PairCandidate(NamedTuple):
    """One (yes leg, no leg) combination of an event group, priced and sized."""

    net_edge: Decimal
    """Guaranteed profit per contract after fees. May be negative."""
    qty: Decimal
    """Contracts tradeable at those prices. 0 means a leg is known empty."""
    unit_cost: Decimal
    """All-in cost of one contract across both legs (asks plus per-unit fees)."""
    yes_outcome_id: str
    no_outcome_id: str


def _rank_pairs(candidates: list[_PairCandidate]) -> _PairCandidate | None:
    """Pick the pair /monitored should describe. None when there are no pairs.

    The objective flips with the sign of the edge, so there are two regimes.

    *Some pair clears fees.* Rank by ``net_edge * qty`` -- net absolute profit
    -- among pairs that could actually be filled. That is exactly what
    ``detect_cross_venue_two_leg`` does: it drops any pair with
    ``net_edge <= 0`` or ``qty <= 0`` before sizing, so it only ever ranks
    positive, tradeable pairs. Matching it is load-bearing, because the
    frontend joins a displayed pair to a published opportunity by leg
    outcome_id -- naming a different pair leaves a live arb's Fill button
    disabled. A deep 1c pair beating a thin 10c pair is intended here.

    *No pair clears fees.* This is the normal case near a coin flip (measured
    2026-08-22: 0 of 175 live rows were net-positive). Maximising
    ``net_edge * qty`` is *backwards* here -- every product is negative, so
    the maximum is the thinnest book rather than the best price, and a pair
    with ``qty == 0`` scores exactly 0 and so outranks every real pair. That
    is reachable in production: a one-sided book keeps its live side with the
    missing side synthesised at size 0, so such a pair would win outright and
    the row would render "no size" while another pair had real depth. Rank by
    ``net_edge`` per contract instead -- the best-priced pair -- keeping a
    known-empty pair behind any pair with real depth. The engine publishes
    nothing in this regime, so there is no opportunity to disagree with, and
    the row's job is just to state its position honestly.
    """
    fillable = [c for c in candidates if c.net_edge > 0 and c.qty > 0]
    if fillable:
        return max(fillable, key=lambda c: c.net_edge * c.qty)
    if not candidates:
        return None
    return max(candidates, key=lambda c: (c.qty > 0, c.net_edge))


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        state = get_state()
        await state.bootstrap()
        try:
            yield
        finally:
            await state.shutdown()
            reset_state()

    app = FastAPI(title="Arbys", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Event group management (v1 curated allowlist)
    # ------------------------------------------------------------------

    @app.get("/event-groups", response_model=list[EventGroupOut])
    async def list_groups() -> list[EventGroupOut]:
        s = get_state()
        return [
            EventGroupOut(
                id=g.id,
                title=g.title,
                legs=[
                    {"outcome_id": leg.outcome_id, "venue_id": leg.venue_id, "is_yes_side": leg.is_yes_side}
                    for leg in g.legs
                ],
            )
            for g in s.event_groups.values()
        ]

    @app.post("/event-groups", response_model=EventGroupOut, status_code=201)
    async def create_group(body: EventGroupIn) -> EventGroupOut:
        s = get_state()
        group = EventGroup(
            id=body.id,
            title=body.title,
            legs=tuple(
                EventGroupLeg(
                    outcome_id=leg.outcome_id,
                    venue_id=leg.venue_id,
                    is_yes_side=leg.is_yes_side,
                )
                for leg in body.legs
            ),
        )
        async with session_scope() as session:
            await repo.upsert_event_group(session, group)
        s.event_groups[group.id] = group
        s.engine.register_group(group)
        await s.restart_ingest()
        return body

    @app.delete("/event-groups/{group_id}", status_code=204)
    async def delete_group(group_id: str) -> None:
        s = get_state()
        async with session_scope() as session:
            await repo.delete_event_group(session, group_id)
        s.event_groups.pop(group_id, None)
        s.engine.unregister_group(group_id)
        await s.restart_ingest()

    # ------------------------------------------------------------------
    # Quote ingest (mocked/manual entry for local dev without live adapters)
    # ------------------------------------------------------------------

    @app.post("/quotes", status_code=204)
    async def push_quote(body: QuoteIn) -> None:
        s = get_state()
        q = Quote(
            outcome_id=body.outcome_id,
            bid=body.bid,
            ask=body.ask,
            bid_size=body.bid_size,
            ask_size=body.ask_size,
        )
        s.quotebook.upsert(q)
        async with session_scope() as session:
            await repo.ensure_outcome_placeholder(
                session, body.outcome_id, venue_id="unknown"
            )
            await repo.insert_quote(
                session,
                outcome_id=body.outcome_id,
                bid=body.bid,
                ask=body.ask,
                bid_size=body.bid_size,
                ask_size=body.ask_size,
            )
        s.engine.on_quote(q)

    # ------------------------------------------------------------------
    # Opportunities
    # ------------------------------------------------------------------

    def _opp_out(opp) -> ArbOpportunityOut:
        return ArbOpportunityOut(
            event_group_id=opp.event_group_id,
            total_stake=opp.total_stake,
            guaranteed_profit=opp.guaranteed_profit,
            guaranteed_profit_bps=opp.guaranteed_profit_bps,
            legs=[
                ArbLegOut(
                    outcome_id=leg.outcome_id,
                    venue_id=leg.venue_id,
                    is_buy=leg.is_buy,
                    price=leg.price,
                    qty=leg.qty,
                    fee=leg.fee,
                )
                for leg in opp.legs
            ],
        )

    @app.get("/monitored", response_model=list[MonitoredGroupOut])
    async def list_monitored() -> list[MonitoredGroupOut]:
        """Return every registered event group + current quotes + arb edge.

        For each group, ``arb_edge = 1 - (best_yes_ask + best_no_ask)`` where
        the two asks are the cheapest way to buy each side of the canonical
        proposition across venues. Positive edge means a risk-free arb exists
        (before fees).
        """
        s = get_state()
        out: list[MonitoredGroupOut] = []
        for g in s.event_groups.values():
            legs_out: list[MonitoredLegOut] = []
            best_yes_ask: Decimal | None = None
            best_yes_venue: str | None = None
            best_no_ask: Decimal | None = None
            best_no_venue: str | None = None
            all_quoted = True
            quoted: dict[str, Quote | None] = {}
            for leg in g.legs:
                # get() withholds stale quotes; get_with_age() still reports
                # them so the leg can explain itself rather than just vanishing.
                q = s.quotebook.get(leg.outcome_id)
                quoted[leg.outcome_id] = q
                aged = s.quotebook.get_with_age(leg.outcome_id)
                age = aged[1] if aged is not None else None
                stale = q is None and aged is not None
                bid = q.bid if q else None
                ask = q.ask if q else None
                if q is None or q.ask is None:
                    all_quoted = False
                legs_out.append(
                    MonitoredLegOut(
                        outcome_id=leg.outcome_id,
                        venue_id=leg.venue_id,
                        is_yes_side=leg.is_yes_side,
                        bid=bid,
                        ask=ask,
                        quote_age_s=round(age, 1) if age is not None else None,
                        is_stale=stale,
                        bid_size=q.bid_size if q else None,
                        ask_size=q.ask_size if q else None,
                    )
                )
                if ask is not None:
                    if leg.is_yes_side:
                        if best_yes_ask is None or ask < best_yes_ask:
                            best_yes_ask = ask
                            best_yes_venue = leg.venue_id
                    else:
                        if best_no_ask is None or ask < best_no_ask:
                            best_no_ask = ask
                            best_no_venue = leg.venue_id
            edge: Decimal | None = None
            has_arb = False
            if best_yes_ask is not None and best_no_ask is not None:
                edge = Decimal("1") - (best_yes_ask + best_no_ask)
                has_arb = edge > 0

            # The best *tradeable pair*. best_yes_ask/best_no_ask above are
            # independently cheapest per side and can both come from the same
            # venue, so they cannot be reused to derive this pair; every
            # (yes, no) combination is evaluated explicitly instead.
            candidates: list[_PairCandidate] = []
            for y in (leg for leg in g.legs if leg.is_yes_side):
                yq = quoted.get(y.outcome_id)
                y_fm = s.fees.get(y.venue_id)
                if yq is None or y_fm is None:
                    continue
                for n in (leg for leg in g.legs if not leg.is_yes_side):
                    nq = quoted.get(n.outcome_id)
                    n_fm = s.fees.get(n.venue_id)
                    if nq is None or n_fm is None:
                        continue
                    y_unit = leg_unit_cost(yq.ask, y_fm, is_buy=True)
                    n_unit = leg_unit_cost(nq.ask, n_fm, is_buy=True)
                    candidates.append(
                        _PairCandidate(
                            net_edge=net_edge_per_contract([y_unit, n_unit]),
                            qty=tradeable_qty(
                                unit_cost=y_unit + n_unit,
                                depths=[yq.ask_size, nq.ask_size],
                                max_stake=max_ticket_stake(),
                                tick=DEFAULT_QTY_TICK,
                            ),
                            unit_cost=y_unit + n_unit,
                            yes_outcome_id=y.outcome_id,
                            no_outcome_id=n.outcome_id,
                        )
                    )

            best = _rank_pairs(candidates)
            net_edge: Decimal | None = None
            max_qty: Decimal | None = None
            net_max_profit: Decimal | None = None
            capital_required: Decimal | None = None
            best_pair_yes_id: str | None = None
            best_pair_no_id: str | None = None
            if best is not None:
                net_edge = best.net_edge
                max_qty = best.qty
                net_max_profit = best.net_edge * best.qty
                capital_required = best.unit_cost * best.qty
                best_pair_yes_id = best.yes_outcome_id
                best_pair_no_id = best.no_outcome_id

            out.append(
                MonitoredGroupOut(
                    id=g.id,
                    title=g.title,
                    start_time=g.start_time,
                    legs=legs_out,
                    best_yes_ask=best_yes_ask,
                    best_yes_venue=best_yes_venue,
                    best_no_ask=best_no_ask,
                    best_no_venue=best_no_venue,
                    arb_edge=edge,
                    has_arb=has_arb,
                    fully_quoted=all_quoted,
                    net_edge=net_edge,
                    max_tradeable_qty=max_qty,
                    net_max_profit=net_max_profit,
                    capital_required=capital_required,
                    best_pair_yes_outcome_id=best_pair_yes_id,
                    best_pair_no_outcome_id=best_pair_no_id,
                )
            )
        out.sort(key=lambda m: (not m.has_arb, m.arb_edge is None, -(float(m.arb_edge) if m.arb_edge is not None else 0)))
        return out

    @app.get("/opportunities", response_model=list[ArbOpportunityOut])
    async def list_opps(limit: int = 50) -> list[ArbOpportunityOut]:
        s = get_state()
        return [_opp_out(o) for o in list(s.opportunities)[:limit]]

    @app.websocket("/ws/opportunities")
    async def ws_opps(ws: WebSocket) -> None:
        await ws.accept()
        s = get_state()
        queue = s.subscribe_opportunities()
        try:
            while True:
                opp = await queue.get()
                await ws.send_json(_opp_out(opp).model_dump(mode="json"))
        except WebSocketDisconnect:
            pass
        finally:
            s.unsubscribe_opportunities(queue)

    # ------------------------------------------------------------------
    # Paper trading
    # ------------------------------------------------------------------

    @app.get("/paper/{account_id}", response_model=PaperAccountSummary)
    async def paper_summary(account_id: str) -> PaperAccountSummary:
        s = get_state()
        balances: dict[str, Decimal] = {}
        positions: dict[str, Decimal] = {}
        realized: dict[str, Decimal] = {}
        for venue_id, broker in s.paper_brokers.items():
            bals = await broker.get_balances(account_id)
            balances[venue_id] = bals.get(venue_id, Decimal("0"))
            for oid, qty in (await broker.get_positions(account_id)).items():
                if qty != 0:
                    positions[oid] = positions.get(oid, Decimal("0")) + qty
            realized[venue_id] = broker.realized_pnl(account_id)
        eq = account_equity(s.paper_brokers, s.quotebook, account_id)
        async with session_scope() as session:
            # Counted in SQL, not by hydrating list_paper_tickets: that call
            # defaults to limit=200 newest-first across every status, so an
            # active account (small tickets, plus missed/rejected noise)
            # would silently undercount past the 200th row -- on a field this
            # endpoint is polled for every few seconds.
            open_count = await repo.count_open_paper_tickets(session, account_id)
        return PaperAccountSummary(
            account_id=account_id,
            balances=balances,
            positions=positions,
            realized_pnl=realized,
            cash=eq.cash,
            position_value=eq.position_value,
            equity=eq.equity,
            unrealized_pnl=eq.unrealized,
            open_ticket_count=open_count,
        )

    @app.get("/paper/{account_id}/tickets", response_model=list[TicketOut])
    async def paper_tickets(
        account_id: str,
        limit: int = 200,
        status: str | None = None,
        source: str | None = None,
    ) -> list[dict]:
        async with session_scope() as session:
            return await repo.list_paper_tickets(
                session, account_id, limit=limit, status=status, source=source
            )

    @app.get("/paper/{account_id}/positions", response_model=list[PositionOut])
    async def paper_positions(account_id: str) -> list[PositionOut]:
        s = get_state()
        async with session_scope() as session:
            meta = await repo.paper_position_meta(session, account_id)
        out: list[PositionOut] = []
        for venue_id, broker in s.paper_brokers.items():
            _cash, held = broker.account_snapshot(account_id)
            for outcome_id, (qty, avg_price, _realized) in held.items():
                quote = s.quotebook.get(outcome_id)
                mark = (quote.bid + quote.ask) / Decimal(2) if quote is not None else None
                effective = avg_price if mark is None else mark
                title, event_group_id = meta.get(outcome_id, (outcome_id, None))
                out.append(
                    PositionOut(
                        venue_id=venue_id,
                        outcome_id=outcome_id,
                        title=title,
                        event_group_id=event_group_id,
                        qty=qty,
                        avg_price=avg_price,
                        mark=mark,
                        unrealized=(effective - avg_price) * qty,
                    )
                )
        return out

    @app.get("/paper/{account_id}/orders")
    async def paper_orders(account_id: str) -> list[dict]:
        async with session_scope() as session:
            return await repo.list_paper_orders(session, account_id)

    @app.get("/paper/{account_id}/pnl-snapshots")
    async def paper_pnl(account_id: str, limit: int = 500) -> list[dict]:
        async with session_scope() as session:
            return await repo.list_pnl_snapshots(session, account_id, limit=limit)

    @app.post("/paper/{account_id}/reset", response_model=PaperAccountSummary)
    async def paper_reset(account_id: str) -> PaperAccountSummary:
        s = get_state()
        await s.reset_paper_account(account_id)
        return await paper_summary(account_id)

    @app.post("/paper/execute", response_model=list[str])
    async def paper_execute(body: ExecuteArbIn) -> list[str]:
        s = get_state()
        if body.event_group_id is not None:
            # Pick the published opportunity the caller is describing. The
            # re-detect against live quotes happens inside submit_arb_ticket.
            wanted = set(body.outcome_ids) if body.outcome_ids else None
            opp = None
            for candidate in s.live_opportunities_for(body.event_group_id):
                if candidate.event_group_id != body.event_group_id:
                    continue
                if wanted is not None:
                    buy_legs = {leg.outcome_id for leg in candidate.legs if leg.is_buy}
                    if buy_legs != wanted:
                        continue
                opp = candidate
                break
            if opp is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "edge no longer available at live quotes for "
                        f"event_group_id={body.event_group_id!r}"
                    ),
                )
        else:
            opportunities = list(s.opportunities)
            if body.opportunity_index < 0 or body.opportunity_index >= len(opportunities):
                raise HTTPException(status_code=404, detail="opportunity_index out of range")
            opp = opportunities[body.opportunity_index]

        result = await submit_arb_ticket(
            s, opp, source="manual", account_id=body.account_id
        )
        if result.status != "filled":
            raise HTTPException(status_code=409, detail=result.reason or result.status)
        return list(result.order_ids)

    return app


app = create_app()
