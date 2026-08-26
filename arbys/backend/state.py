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
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from ..adapters.base import MarketDataAdapter
from ..adapters.draftkings import DraftKingsAdapter, draftkings_enabled
from ..adapters.kalshi import KalshiAdapter
from ..adapters.kalshi_ws import KalshiWebSocketAdapter, kalshi_ws_creds_from_env
from ..adapters.polymarket_us import PolymarketUsAdapter
from ..adapters.polymarket_us_auth import creds_from_env as polymarket_us_creds_from_env
from ..adapters.polymarket_us_ws import PolymarketUsWebSocketAdapter
from ..db import repositories as repo
from ..db.session import run_write, session_scope
from ..ingest.auto_settle_service import AutoSettleService
from ..ingest.engine_runtime import EngineRuntime
from ..ingest.pnl_service import PnlSnapshotService
from ..ingest.worker import IngestWorker
from ..shared.arb_engine import ArbOpportunity
from ..shared.execution_router import ExecutionRouter
from ..shared.fees import (
    FeeModelRegistry,
    KalshiFeeModel,
    PolymarketUsFeeModel,
    SportsbookFeeModel,
)
from ..shared.paper_broker import PaperExecutionAdapter
from ..shared.persistence import AccountScopedSink, DbPaperPersistenceSink
from ..shared.quotebook import DEFAULT_MAX_AGE_S as QUOTEBOOK_DEFAULT_MAX_AGE_S
from ..shared.quotebook import QuoteBook
from ..shared.types import EventGroup

log = logging.getLogger(__name__)


def _opp_fingerprint(opp: ArbOpportunity) -> tuple:
    """Identity of an opportunity for change detection.

    Two detections with the same group and the same priced legs are the same
    edge seen twice, not news.
    """
    return (
        opp.event_group_id,
        tuple(
            (leg.venue_id, leg.outcome_id, leg.is_buy, leg.price, leg.qty)
            for leg in opp.legs
        ),
    )

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


def quote_max_age_s() -> float | None:
    """How old a quote may be before it stops counting as tradeable.

    ``ARBYS_QUOTE_MAX_AGE_S=0`` disables expiry. A venue that stops publishing
    an outcome otherwise looks identical to a quiet one, and its last price
    quotes forever — which is how a delisted Polymarket market kept showing an
    8c arb against a live Kalshi leg.
    """
    raw = os.environ.get("ARBYS_QUOTE_MAX_AGE_S")
    if raw is None:
        return QUOTEBOOK_DEFAULT_MAX_AGE_S
    try:
        value = float(raw)
    except ValueError:
        return QUOTEBOOK_DEFAULT_MAX_AGE_S
    return None if value <= 0 else value


DEFAULT_POLYMARKET_US_POLL_S = 5.0


def polymarket_us_poll_s() -> float:
    """Seconds between Polymarket US ``/bbo`` sweeps.

    Measured 2026-08-11: 53 concurrent ``/bbo`` calls returned in 1.46s with
    no rate limiting, so 5s is comfortable. The 1s floor stops a typo from
    hammering the gateway.
    """
    raw = os.environ.get("ARBYS_POLYMARKET_US_POLL_S")
    if raw is None:
        return DEFAULT_POLYMARKET_US_POLL_S
    try:
        return max(1.0, float(raw))
    except ValueError:
        return DEFAULT_POLYMARKET_US_POLL_S


DEFAULT_POLYMARKET_US_WS_SHARD_SIZE = 100


def polymarket_us_ws_shard_size() -> int:
    """Markets per Polymarket US WebSocket connection.

    One connection silently stops streaming a large fraction of its markets
    past some ceiling - measured 2026-08-25, 573 slugs on one socket shed
    eight of nine slugs that were provably live - so the subscription is split
    across connections. Lower this if a shard's ``live`` count in the feed
    report sags; raise it to trade that safety margin for fewer sockets.
    """
    raw = os.environ.get("ARBYS_POLYMARKET_US_WS_SHARD_SIZE")
    if raw is None:
        return DEFAULT_POLYMARKET_US_WS_SHARD_SIZE
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_POLYMARKET_US_WS_SHARD_SIZE


DEFAULT_POLYMARKET_US_DARK_AFTER_S = 120.0
DEFAULT_POLYMARKET_US_PRIORITY_DARK_AFTER_S = 6.0


def polymarket_us_dark_after_s() -> float:
    """Socket silence before a subscribed market is treated as dark.

    Dark is a *diagnosis*, not a data source. The adapter responds by
    re-subscribing the market, and by rebuilding the connection if that fails
    for a game already under way - never by fetching the price from somewhere
    else. A REST read lags the WebSocket ladder and carries no size at all,
    and a sizeless quote is worse than none: `tradeable_qty` treats unknown
    depth as *no ceiling*, so one sized a live ticket at 200 contracts where
    the legs built from real ladder depth were capped at 25.
    """
    raw = os.environ.get("ARBYS_POLYMARKET_US_DARK_AFTER_S")
    if raw is None:
        return DEFAULT_POLYMARKET_US_DARK_AFTER_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_POLYMARKET_US_DARK_AFTER_S


def polymarket_us_priority_dark_after_s() -> float:
    """The same, for a market whose game is already under way.

    Far tighter, because an in-play book reprices on every point: the silence
    that is normal for next week's game is a fault here, and a stale in-play
    leg is exactly what invents an arbitrage against the other venue.
    """
    raw = os.environ.get("ARBYS_POLYMARKET_US_PRIORITY_DARK_AFTER_S")
    if raw is None:
        return DEFAULT_POLYMARKET_US_PRIORITY_DARK_AFTER_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_POLYMARKET_US_PRIORITY_DARK_AFTER_S


DEFAULT_MAX_OUTCOME_QTY = Decimal("500")


def max_outcome_qty() -> Decimal | None:
    """Cap on open units per outcome for one account. ``None`` disables it.

    The engine republishes an edge for as long as it exists, and nothing
    stopped a caller taking the same ticket over and over — five clicks put on
    five times the intended size. Units are the natural measure here: a binary
    ticket that buys N units pays off N, so this caps guaranteed payoff
    exposure per outcome. Set ARBYS_MAX_OUTCOME_QTY=0 to turn it off.
    """
    raw = os.environ.get("ARBYS_MAX_OUTCOME_QTY")
    if raw is None:
        return DEFAULT_MAX_OUTCOME_QTY
    try:
        value = Decimal(raw)
    except (ArithmeticError, ValueError):
        return DEFAULT_MAX_OUTCOME_QTY
    return None if value <= 0 else value


DEFAULT_MAX_TICKET_STAKE = Decimal("200")


def max_ticket_stake() -> Decimal | None:
    """Cap on total capital in one arb ticket. ``None`` disables it.

    Sizing is depth-driven, and some books are enormous — a single Polymarket
    US level has shown 419,882 contracts resting. Without a budget cap one
    ticket would consume the whole book. At ~$1.00 all-in per contract pair,
    the $200 default is roughly 198 contracts.

    This does **not** replace ``ARBYS_MAX_OUTCOME_QTY``: that caps cumulative
    open units per outcome per account and is enforced at execute time in
    ``app.py``, whereas this caps one ticket at detection time. Both apply.

    Set ARBYS_MAX_TICKET_STAKE=0 to turn it off.
    """
    raw = os.environ.get("ARBYS_MAX_TICKET_STAKE")
    if raw is None:
        return DEFAULT_MAX_TICKET_STAKE
    try:
        value = Decimal(raw)
    except (ArithmeticError, ValueError):
        return DEFAULT_MAX_TICKET_STAKE
    return None if value <= 0 else value


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

    def _polymarket_us_factory(oids: list[str]) -> MarketDataAdapter:
        creds = polymarket_us_creds_from_env()
        if creds is not None:
            log.info("using Polymarket US WebSocket adapter (authenticated, real-time)")
            return PolymarketUsWebSocketAdapter(
                outcome_ids=oids,
                creds=creds,
                shard_size=polymarket_us_ws_shard_size(),
                dark_after_s=polymarket_us_dark_after_s(),
                priority_dark_after_s=polymarket_us_priority_dark_after_s(),
            )
        # The REST path is the only one that works without KYC, so it stays.
        log.info("using Polymarket US REST poll adapter (no credentials set)")
        return PolymarketUsAdapter(
            outcome_ids=oids, poll_interval_s=polymarket_us_poll_s()
        )

    factories: dict[str, AdapterFactory] = {
        "polymarket_us": _polymarket_us_factory,
        "kalshi": _kalshi_factory,
    }
    if draftkings_enabled():
        factories["draftkings"] = lambda oids: DraftKingsAdapter(outcome_ids=oids)
    return factories


class AppState:
    def __init__(self) -> None:
        self.quotebook = QuoteBook(max_age_s=quote_max_age_s())
        self.fees: FeeModelRegistry = {
            "polymarket_us": PolymarketUsFeeModel(),
            "kalshi": KalshiFeeModel(),
            "draftkings": SportsbookFeeModel("draftkings"),
        }
        self.event_groups: dict[str, EventGroup] = {}
        # source group id -> that group's currently-executable opportunities.
        # Keyed by group so an evaluation can replace the whole set, including
        # removing entries when the edge disappears.
        self._opps_by_group: dict[str, list[ArbOpportunity]] = {}
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
            on_opportunities=self._set_group_opportunities,
            max_ticket_stake=max_ticket_stake(),
        )
        self.pnl_service = PnlSnapshotService(
            brokers=self.paper_brokers,
            quotebook=self.quotebook,
            account_ids=[self.default_account_id],
        )
        self.auto_settle_service = AutoSettleService(
            event_groups=self.event_groups,
            brokers=self.paper_brokers,
            quotebook=self.quotebook,
        )

        self.adapter_factories: dict[str, AdapterFactory] = _default_adapter_factories()
        self._adapters: list[MarketDataAdapter] = []
        # What each venue's live adapter was actually built to subscribe to.
        # `sync_ingest` compares against this to decide whether a rebuild is
        # needed at all.
        self._subscribed: dict[str, set[str]] = {}
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
            # Route to the owning venue only. Fanning a row out to every broker
            # double/triple-counts qty and realized PnL in GET /paper on restart.
            broker = self.paper_brokers.get(row.venue_id)
            if broker is not None:
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
        await self.auto_settle_service.start()

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
        subscribed: dict[str, set[str]] = {}
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
            adapter = factory(dedup)
            # Duck-typed like `close` below: an adapter that can poll harder
            # for in-play markets is told how to recognise them. Only AppState
            # knows each group's start time.
            set_priority = getattr(adapter, "set_priority_slugs", None)
            if set_priority is not None:
                set_priority(self.in_play_slugs)
            set_confirmed = getattr(adapter, "set_confirmed_in_play", None)
            if set_confirmed is not None:
                set_confirmed(self.confirmed_in_play_slugs)
            adapters.append(adapter)
            subscribed[venue_id] = set(dedup)
            log.info("ingest: %s adapter built for %d outcomes", venue_id, len(dedup))

        if not adapters:
            log.info("ingest: no adapters to start (no event groups registered)")
            return

        self._adapters = adapters
        self._subscribed = subscribed
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
        self._subscribed = {}

    # Longest a real event is assumed to still be running. A five-set match
    # can pass five hours; nothing we list runs past six. Without an upper
    # bound, "started" is true forever, so every finished game would be polled
    # at in-play rates and re-subscribed for the life of the process -
    # observed as 13 markets per shard being repaired every 15s, none of which
    # could ever stream again because they had settled.
    MAX_EVENT_DURATION_S = 6 * 60 * 60

    def confirmed_in_play_slugs(self) -> set[str]:
        """Only markets a venue has *positively said* are under way.

        Distinct from `in_play_slugs`, which falls back to a start-time window
        where nobody reported. That guess is fine for tightening a cheap
        deadline, but it must never drive an expensive action: dropping a
        healthy connection on a guess is how last night's finished games came
        to force 19 reconnects across four shards, each one flagged in-play
        only because it had started within `MAX_EVENT_DURATION_S`.

        Escalation therefore requires evidence, not the absence of it.
        """
        return {
            leg.outcome_id.rpartition(":")[0]
            for group in self.event_groups.values()
            if group.in_play
            for leg in group.legs
        }

    def in_play_slugs(self) -> set[str]:
        """Venue market ids whose real-world event is under way now.

        Prefers the venue's own ``live``/``ended`` flags, recorded on the group
        by discovery, and falls back to a start-time window only where no venue
        reported. The flags are what make this exact rather than a guess: a
        tennis match that ended an hour ago looks identical to one in its third
        set if all you have is a kickoff time.

        A market being played reprices constantly, so it earns a much tighter
        freshness deadline than one for a game next week. Recomputed on demand
        rather than cached, so a game going in-play tightens its own deadline
        without an ingest restart - and drops back out once it is over.
        """
        now = datetime.now(UTC)
        live: set[str] = set()
        for group in self.event_groups.values():
            if group.in_play is not None:
                # The venue said. Believe it - it knows whether a match is on
                # court, and the clock-based guess below cannot tell a game in
                # progress from one that finished an hour ago.
                if not group.in_play:
                    continue
            else:
                # Nobody said. Kalshi never does, and a Polymarket event the
                # venue has not started tracking reports None too, so fall
                # back to the clock.
                start = group.start_time
                if start is None:
                    continue
                if start.tzinfo is None:
                    start = start.replace(tzinfo=UTC)
                elapsed = (now - start).total_seconds()
                if not 0 <= elapsed <= self.MAX_EVENT_DURATION_S:
                    continue
            for leg in group.legs:
                live.add(leg.outcome_id.rpartition(":")[0])
        return live

    async def sync_ingest(self) -> None:
        """Make the live subscriptions cover the current event groups.

        **Rebuilds only when some venue needs a market it is not already
        subscribed to.** Everything else - a retirement, a title change, a
        start time moving - leaves the sockets alone.

        That distinction is the whole point. Neither venue supports
        unsubscribing, so the only way to *drop* a market is to hang up every
        connection and redial with a shorter list. But dropping costs nothing
        if we simply stop caring: frames for markets we no longer hold are
        already ignored, and the group is gone from `event_groups` so nothing
        evaluates it. Redialing, by contrast, is expensive - the venue answers
        a fresh subscription by replaying a cached book that can be hours old,
        so every rebuild floods the quote book with stale prices and takes
        minutes to recover.

        Discovery retires a finished match on nearly every pass, so treating
        that as a reason to rebuild meant all 569 subscriptions across both
        venues went stale for ~3 minutes every ~12 minutes, to stop watching
        one tennis game that had ended.
        """
        if not _ingest_enabled():
            return
        desired: dict[str, set[str]] = defaultdict(set)
        for group in self.event_groups.values():
            for leg in group.legs:
                desired[leg.venue_id].add(leg.outcome_id)
        added = {
            venue_id: oids - self._subscribed.get(venue_id, set())
            for venue_id, oids in desired.items()
        }
        added = {v: o for v, o in added.items() if o}
        if not added and self._ingest_worker is not None:
            dropped = sum(
                len(self._subscribed.get(v, set()) - desired.get(v, set()))
                for v in self._subscribed
            )
            log.info(
                "ingest: subscriptions already cover every registered outcome "
                "(%d now-unwanted market(s) left subscribed and ignored); "
                "not rebuilding",
                dropped,
            )
            return
        log.info(
            "ingest: rebuilding — %s",
            ", ".join(f"{v}+{len(o)}" for v, o in sorted(added.items())) or "no worker",
        )
        await self.restart_ingest()

    async def restart_ingest(self) -> None:
        """Public: unconditionally stop and rebuild every venue adapter.

        Prefer `sync_ingest`, which skips the rebuild when nothing new needs
        subscribing — a rebuild costs a multi-minute window of stale prices
        across every market on both venues.

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
        await self.auto_settle_service.stop()
        await self.pnl_service.stop()

    async def reset_paper_account(self, account_id: str) -> None:
        """Wipe all history for a paper account and re-seed starting balances.

        Clears in-memory broker state, deletes DB history, re-seeds
        `DEFAULT_STARTING_BALANCE` per venue in memory and DB. Also clears the
        auto-settle service's memoized "already settled" set so previously
        settled groups can settle again if they re-trigger.
        """
        for broker in self.paper_brokers.values():
            broker.reset_account(account_id)
        async with session_scope() as session:
            await repo.delete_paper_history(session, account_id)
        for venue_id, broker in self.paper_brokers.items():
            broker.hydrate_balance(account_id, DEFAULT_STARTING_BALANCE)
            async with session_scope() as session:
                await repo.upsert_paper_balance(
                    session,
                    account_id=account_id,
                    venue_id=venue_id,
                    amount=DEFAULT_STARTING_BALANCE,
                )
        self.auto_settle_service.clear_settled()
        log.info("paper account %s reset to $%s per venue", account_id, DEFAULT_STARTING_BALANCE)

    def _set_group_opportunities(
        self, group_id: str, opps: list[ArbOpportunity]
    ) -> None:
        """Replace a group's live opportunities with the latest evaluation.

        The set is authoritative: anything the group previously offered and no
        longer does is dropped here. That is what keeps `opportunities` a
        picture of what is executable *now* rather than a log of everything
        ever detected.

        Re-detections that are byte-identical to what we already hold are not
        re-broadcast or re-persisted. A quiet, lopsided market re-triggers the
        detector on every tick, and without this one such game buried every
        other group's opportunity and wrote a DB row per tick.
        """
        previous = {_opp_fingerprint(o): o for o in self._opps_by_group.get(group_id, ())}
        if opps:
            self._opps_by_group[group_id] = list(opps)
        else:
            self._opps_by_group.pop(group_id, None)

        for opp in opps:
            if _opp_fingerprint(opp) in previous:
                continue  # unchanged — already broadcast and persisted
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().create_task(self._persist_opp(opp))
            for q in list(self._opp_subscribers):
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(opp)

    async def _persist_opp(self, opp: ArbOpportunity) -> None:
        await run_write("state.opportunity", lambda s: repo.insert_opportunity(s, opp))

    @property
    def opportunities(self) -> list[ArbOpportunity]:
        """Every currently-executable opportunity, richest edge first.

        This is live state, not history: an entry exists only while the
        detector still finds that edge at current quotes.
        """
        flat = [o for opps in self._opps_by_group.values() for o in opps]
        flat.sort(key=lambda o: (o.guaranteed_profit_bps, o.guaranteed_profit), reverse=True)
        return flat

    def clear_group_opportunities(self, group_id: str) -> None:
        """Forget a group's opportunities outright.

        Normally the set empties when the engine re-evaluates and finds no
        edge, but a retired group is unregistered first, so no evaluation ever
        comes. Without this its last opportunity would outlive the group it
        belongs to — visible in /opportunities after the group had gone.
        """
        self._opps_by_group.pop(group_id, None)

    def live_opportunities_for(self, event_group_id: str) -> list[ArbOpportunity]:
        """Re-run detection now and return what is executable at live quotes.

        Complementary (same-venue) opportunities are published under a
        synthetic ``<group>:<venue>`` id, so strip that suffix to find the
        event group the engine actually knows about.
        """
        base_id = event_group_id.split(":", 1)[0]
        return self.engine.evaluate_now(base_id)

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

