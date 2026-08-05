"""Deterministic backtest harness.

Feed a chronologically-ordered iterable of quotes into a fresh
`EngineRuntime` + `PaperExecutionAdapter` stack, and collect the resulting
opportunities and (optional) paper trades. Use to validate detector or
fee-model changes without touching a live venue.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from ..adapters.base import ExecutionIntent, IntentLeg
from ..ingest.engine_runtime import EngineRuntime
from ..shared.arb_engine import ArbOpportunity
from ..shared.execution_router import ExecutionRouter, InsufficientLegsError
from ..shared.fees import FeeModelRegistry
from ..shared.paper_broker import PaperExecutionAdapter
from ..shared.quotebook import QuoteBook
from ..shared.types import EventGroup, Quote


@dataclass
class BacktestResult:
    opportunities: list[ArbOpportunity] = field(default_factory=list)
    orders: list[str] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)


async def run_backtest(
    *,
    quotes: Iterable[Quote],
    event_groups: list[EventGroup],
    fees: FeeModelRegistry,
    starting_balances: dict[str, Decimal] | None = None,
    execute: bool = False,
    account_id: str = "bt",
) -> BacktestResult:
    book = QuoteBook()
    result = BacktestResult()
    brokers: dict[str, PaperExecutionAdapter] = {}
    router: ExecutionRouter | None = None
    if execute:
        for venue_id, fee in fees.items():
            broker = PaperExecutionAdapter(
                venue_id=venue_id, quotebook=book, fee_model=fee
            )
            if starting_balances and venue_id in starting_balances:
                broker.deposit(account_id, starting_balances[venue_id])
            brokers[venue_id] = broker
        router = ExecutionRouter(dict(brokers))

    def on_opp(opp: ArbOpportunity) -> None:
        result.opportunities.append(opp)

    engine = EngineRuntime(quotebook=book, fees=fees, on_opportunity=on_opp)
    for group in event_groups:
        engine.register_group(group)

    for q in quotes:
        book.upsert(q)
        prev_count = len(result.opportunities)
        engine.on_quote(q)
        if execute and router is not None and len(result.opportunities) > prev_count:
            for opp in result.opportunities[prev_count:]:
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
                    orders = await router.submit(intent)
                    result.orders.extend(o.id for o in orders)
                except InsufficientLegsError as e:
                    result.rejections.append(str(e))

    return result
