"""In-memory application state.

The full app should persist to Postgres, but for the initial working end-to-end
slice we keep state in memory. The persistence layer wraps this state without
changing its API — same pattern as the paper broker.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from decimal import Decimal

from ..ingest.engine_runtime import EngineRuntime
from ..shared.arb_engine import ArbOpportunity
from ..shared.execution_router import ExecutionRouter
from ..shared.fees import (
    FeeModelRegistry,
    KalshiFeeModel,
    PolymarketFeeModel,
    SportsbookFeeModel,
)
from ..shared.paper_broker import PaperExecutionAdapter
from ..shared.quotebook import QuoteBook
from ..shared.types import EventGroup

MAX_RECENT_OPPS = 500


class AppState:
    def __init__(self) -> None:
        self.quotebook = QuoteBook()
        self.fees: FeeModelRegistry = {
            "polymarket": PolymarketFeeModel(),
            "kalshi": KalshiFeeModel(),
            "draftkings": SportsbookFeeModel("draftkings"),
        }
        self.event_groups: dict[str, EventGroup] = {}
        self.opportunities: deque[ArbOpportunity] = deque(maxlen=MAX_RECENT_OPPS)
        self._opp_subscribers: list[asyncio.Queue[ArbOpportunity]] = []

        # Paper brokers per venue.
        self.paper_brokers: dict[str, PaperExecutionAdapter] = {
            venue: PaperExecutionAdapter(
                venue_id=venue, quotebook=self.quotebook, fee_model=fee
            )
            for venue, fee in self.fees.items()
        }
        self.router = ExecutionRouter(dict(self.paper_brokers))
        self.engine = EngineRuntime(
            quotebook=self.quotebook,
            fees=self.fees,
            on_opportunity=self._record_opportunity,
        )

        # Default paper account.
        self.default_account_id = "default"
        for _venue, broker in self.paper_brokers.items():
            broker.deposit(self.default_account_id, Decimal("1000"))

    def _record_opportunity(self, opp: ArbOpportunity) -> None:
        self.opportunities.appendleft(opp)
        for q in list(self._opp_subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(opp)

    def subscribe_opportunities(self) -> asyncio.Queue[ArbOpportunity]:
        q: asyncio.Queue[ArbOpportunity] = asyncio.Queue(maxsize=100)
        self._opp_subscribers.append(q)
        return q

    def unsubscribe_opportunities(self, q: asyncio.Queue[ArbOpportunity]) -> None:
        if q in self._opp_subscribers:
            self._opp_subscribers.remove(q)


# Singleton for the FastAPI app to share.
STATE: AppState | None = None


def get_state() -> AppState:
    global STATE
    if STATE is None:
        STATE = AppState()
    return STATE
