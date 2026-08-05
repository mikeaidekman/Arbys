"""FastAPI app exposing scanner + paper trading over REST + WebSocket."""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from ..adapters.base import ExecutionIntent, IntentLeg
from ..db import repositories as repo
from ..db.session import session_scope
from ..shared.execution_router import InsufficientLegsError
from ..shared.types import EventGroup, EventGroupLeg, Quote
from .schemas import (
    ArbLegOut,
    ArbOpportunityOut,
    EventGroupIn,
    EventGroupOut,
    ExecuteArbIn,
    PaperAccountSummary,
    QuoteIn,
)
from .state import get_state, reset_state


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

    @app.post("/paper/execute", response_model=list[str])
    async def paper_execute(body: ExecuteArbIn) -> list[str]:
        s = get_state()
        if body.opportunity_index < 0 or body.opportunity_index >= len(s.opportunities):
            raise HTTPException(status_code=404, detail="opportunity_index out of range")
        opp = list(s.opportunities)[body.opportunity_index]
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
