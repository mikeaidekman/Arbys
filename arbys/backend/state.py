"""Application state — DB-hydrated, DB-persisted.

Hot-path reads (opportunities, current book, positions) still come from
in-memory structures for latency, but every mutation is mirrored to Postgres
via the persistence sink so a restart replays cleanly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections import defaultdict, deque
from collections.abc import Callable
from decimal import Decimal

from ..adapters.base import MarketDataAdapter
from ..adapters.draftkings import DraftKingsAdapter, draftkings_enabled
from ..adapters.kalshi import KalshiAdapter
from ..adapters.kalshi_ws import KalshiWebSocketAdapter, kalshi_ws_creds_from_env
from ..adapters.polymarket import PolymarketAdapter
from ..db import repositories as repo
from ..db.session import session_scope
from ..ingest.engine_runtime import EngineRuntime
from ..ingest.pnl_service import PnlSnapshotService
from ..ingest.worker import IngestWorker
from ..shared.arb_engine import ArbOpportunity
from ..shared.execution_router import ExecutionRouter
from ..shared.fees import (
    FeeModelRegistry,
    KalshiFeeModel,
    PolymarketFeeModel,
    SportsbookFeeModel,
)
from ..shared.paper_broker import PaperExecutionAdapter
from ..shared.persistence import AccountScopedSink, DbPaperPersistenceSink
from ..shared.quotebook import QuoteBook
from ..shared.types import EventGroup

log = logging.getLogger(__name__)

MAX_RECENT_OPPS = 500

DEFAULT_STARTING_BALANCE = Decimal("1000")


def _ingest_enabled() -> bool:
    """Live-ingest master switch. Off by default so tests never touch the network."""
    return os.environ.get("ARBYS_ENABLE_INGEST", "0") == "1"


def _discovery_enabled() -> bool:
    """Auto-discovery master switch. Off by default so tests never touch the network."""
    return os.environ.get("ARBYS_ENABLE_DISCOVERY", "0") == "1"


def _discovery_interval_s() -> float:
    raw = os.environ.get("ARBYS_DISCOVERY_INTERVAL_S", "600")
    try:
        return max(30.0, float(raw))
    except ValueError:
        return 600.0


# venue_id -> factory(outcome_ids) -> MarketDataAdapter
AdapterFactory = Callable[[list[str]], MarketDataAdapter]


def _default_adapter_factories() -> dict[str, AdapterFactory]:
    kalshi_creds = kalshi_ws_creds_from_env()

    def _kalshi_factory(oids: list[str]) -> MarketDataAdapter:
        if kalshi_creds is not None:
            key_id, private_key = kalshi_creds
            log.info("using Kalshi WebSocket adapter (authenticated, real-time)")
            return KalshiWebSocketAdapter(
                outcome_ids=oids, api_key_id=key_id, private_key=private_key
            )
        log.info("using Kalshi REST poll adapter (no KALSHI_API_KEY_ID/PATH set)")
        return KalshiAdapter(outcome_ids=oids)

    factories: dict[str, AdapterFactory] = {
        "polymarket": lambda oids: PolymarketAdapter(outcome_ids=oids),
        "kalshi": _kalshi_factory,
    }
    if draftkings_enabled():
        factories["draftkings"] = lambda oids: DraftKingsAdapter(outcome_ids=oids)
    return factories


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

        self.default_account_id = "default"
        self._sink_inner = DbPaperPersistenceSink()
        self.paper_brokers: dict[str, PaperExecutionAdapter] = {
            venue: PaperExecutionAdapter(
                venue_id=venue,
                quotebook=self.quotebook,
                fee_model=fee,
                sink=AccountScopedSink(self._sink_inner, self.default_account_id),
            )
            for venue, fee in self.fees.items()
        }
        self.router = ExecutionRouter(dict(self.paper_brokers))
        self.engine = EngineRuntime(
            quotebook=self.quotebook,
            fees=self.fees,
            on_opportunity=self._record_opportunity,
        )
        self.pnl_service = PnlSnapshotService(
            brokers=self.paper_brokers,
            quotebook=self.quotebook,
            account_ids=[self.default_account_id],
        )

        self.adapter_factories: dict[str, AdapterFactory] = _default_adapter_factories()
        self._adapters: list[MarketDataAdapter] = []
        self._ingest_worker: IngestWorker | None = None
        self._discovery_service = None

    async def bootstrap(self) -> None:
        """Ensure schema, seed reference data, and hydrate in-memory state."""
        # Ensure schema exists (idempotent). Alembic remains the source of truth
        # for prod; this is a convenience for local dev + tests.
        from ..db.session import create_all
        await create_all()

        async with session_scope() as session:
            for venue_id in self.fees:
                await repo.ensure_venue(session, venue_id, name=venue_id.title(), kind="exchange")
            await repo.ensure_paper_account(
                session, self.default_account_id, name=self.default_account_id
            )

        # Hydrate event groups.
        async with session_scope() as session:
            groups = await repo.list_event_groups(session)
        for g in groups:
            self.event_groups[g.id] = g
            self.engine.register_group(g)

        # Hydrate balances + positions (or seed if first run).
        async with session_scope() as session:
            from sqlalchemy import select

            from ..db import models as m

            balances = (
                await session.execute(
                    select(m.PaperBalance).where(
                        m.PaperBalance.account_id == self.default_account_id
                    )
                )
            ).scalars().all()
            positions = (
                await session.execute(
                    select(m.PaperPosition).where(
                        m.PaperPosition.account_id == self.default_account_id
                    )
                )
            ).scalars().all()

        seeded_balance_venues: set[str] = set()
        for row in balances:
            broker = self.paper_brokers.get(row.venue_id)
            if broker is not None:
                broker.hydrate_balance(self.default_account_id, row.amount)
                seeded_balance_venues.add(row.venue_id)

        for row in positions:
            for broker in self.paper_brokers.values():
                broker.hydrate_position(
                    self.default_account_id,
                    row.outcome_id,
                    qty=row.qty,
                    avg_price=row.avg_price,
                    realized_pnl=row.realized_pnl,
                )

        # Seed starting balances for venues that have never been funded.
        for venue_id, broker in self.paper_brokers.items():
            if venue_id in seeded_balance_venues:
                continue
            broker.hydrate_balance(self.default_account_id, DEFAULT_STARTING_BALANCE)
            async with session_scope() as session:
                await repo.upsert_paper_balance(
                    session,
                    account_id=self.default_account_id,
                    venue_id=venue_id,
                    amount=DEFAULT_STARTING_BALANCE,
                )

        await self.pnl_service.start()

        if _ingest_enabled():
            await self._start_ingest()
        else:
            log.info("ARBYS_ENABLE_INGEST != 1; live market data ingest is disabled")

        if _discovery_enabled():
            from ..discovery.service import DiscoveryService

            self._discovery_service = DiscoveryService(self, interval_s=_discovery_interval_s())
            await self._discovery_service.start()
            log.info("discovery service started (interval=%.0fs)", _discovery_interval_s())
        else:
            log.info("ARBYS_ENABLE_DISCOVERY != 1; auto-discovery is disabled")

    async def _start_ingest(self) -> None:
        """Build one adapter per venue with registered outcomes; start worker."""
        outcomes_by_venue: dict[str, list[str]] = defaultdict(list)
        for group in self.event_groups.values():
            for leg in group.legs:
                outcomes_by_venue[leg.venue_id].append(leg.outcome_id)

        adapters: list[MarketDataAdapter] = []
        for venue_id, outcome_ids in outcomes_by_venue.items():
            factory = self.adapter_factories.get(venue_id)
            if factory is None:
                log.warning(
                    "no adapter factory registered for venue %s; skipping %d outcomes",
                    venue_id,
                    len(outcome_ids),
                )
                continue
            dedup = sorted(set(outcome_ids))
            adapters.append(factory(dedup))
            log.info("ingest: %s adapter built for %d outcomes", venue_id, len(dedup))

        if not adapters:
            log.info("ingest: no adapters to start (no event groups registered)")
            return

        self._adapters = adapters
        self._ingest_worker = IngestWorker(
            adapters=adapters,
            quotebook=self.quotebook,
            on_quote=self.engine.on_quote,
        )
        await self._ingest_worker.start()
        log.info("ingest worker started with %d adapters", len(adapters))

    async def _stop_ingest(self) -> None:
        if self._ingest_worker is not None:
            await self._ingest_worker.stop()
            self._ingest_worker = None
        for adapter in self._adapters:
            close = getattr(adapter, "close", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()
        self._adapters = []

    async def restart_ingest(self) -> None:
        """Public: stop and restart ingest to pick up new outcome subscriptions.

        No-op when ``ARBYS_ENABLE_INGEST`` is off.
        """
        if not _ingest_enabled():
            return
        await self._stop_ingest()
        await self._start_ingest()

    async def shutdown(self) -> None:
        if self._discovery_service is not None:
            await self._discovery_service.stop()
            self._discovery_service = None
        await self._stop_ingest()
        await self.pnl_service.stop()

    def _record_opportunity(self, opp: ArbOpportunity) -> None:
        self.opportunities.appendleft(opp)
        # Fire-and-forget persistence.
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(self._persist_opp(opp))
        for q in list(self._opp_subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(opp)

    async def _persist_opp(self, opp: ArbOpportunity) -> None:
        try:
            async with session_scope() as session:
                await repo.insert_opportunity(session, opp)
        except Exception:
            pass

    def subscribe_opportunities(self) -> asyncio.Queue[ArbOpportunity]:
        q: asyncio.Queue[ArbOpportunity] = asyncio.Queue(maxsize=100)
        self._opp_subscribers.append(q)
        return q

    def unsubscribe_opportunities(self, q: asyncio.Queue[ArbOpportunity]) -> None:
        if q in self._opp_subscribers:
            self._opp_subscribers.remove(q)


STATE: AppState | None = None


def get_state() -> AppState:
    global STATE
    if STATE is None:
        STATE = AppState()
    return STATE


def reset_state() -> None:
    global STATE
    STATE = None

