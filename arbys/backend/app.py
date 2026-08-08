"""FastAPI app exposing scanner + paper trading over REST + WebSocket."""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

# Load .env from the working directory so ARBYS_* flags are honored when
# uvicorn is launched with no explicit --env-file.
load_dotenv()

from ..adapters.base import ExecutionIntent, IntentLeg  # noqa: E402
from ..db import repositories as repo  # noqa: E402
from ..db.session import session_scope  # noqa: E402
from ..shared.execution_router import InsufficientLegsError  # noqa: E402
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
    QuoteIn,
)
from .state import get_state, reset_state  # noqa: E402


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
        q = Quote(outcome_id=body.outcome_id, bid=body.bid, ask=body.ask)
        s.quotebook.upsert(q)
        async with session_scope() as session:
            await repo.ensure_outcome_placeholder(
                session, body.outcome_id, venue_id="unknown"
            )
            await repo.insert_quote(
                session, outcome_id=body.outcome_id, bid=body.bid, ask=body.ask
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
            for leg in g.legs:
                q = s.quotebook.get(leg.outcome_id)
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
        return PaperAccountSummary(
            account_id=account_id,
            balances=balances,
            positions=positions,
            realized_pnl=realized,
        )

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
            # Re-detect against the live quote book rather than filling from a
            # stored record. A previously detected opportunity carries the
            # prices it was found at; replaying those is what produced
            # "limit_exceeded" once the market moved.
            fresh = s.live_opportunities_for(body.event_group_id)
            wanted = set(body.outcome_ids) if body.outcome_ids else None
            opp = None
            for candidate in fresh:
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
        account_id = body.account_id or s.default_account_id
        intent = ExecutionIntent(
            event_group_id=opp.event_group_id,
            account_id=account_id,
            legs=tuple(
                IntentLeg(
                    venue_id=leg.venue_id,
                    outcome_id=leg.outcome_id,
                    is_buy=leg.is_buy,
                    qty=leg.qty,
                    limit_price=leg.price,
                )
                for leg in opp.legs
            ),
        )
        try:
            orders = await s.router.submit(intent)
        except InsufficientLegsError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return [o.id for o in orders]

    return app


app = create_app()
