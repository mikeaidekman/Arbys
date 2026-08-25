"""Polymarket US authenticated market-data WebSocket.

With this in use both legs of a cross-venue group push: Kalshi already did,
and polling was the reason the two venues could describe different moments
during a live game.

Protocol details the published documentation omits, established by probing the
live venue on 2026-08-12:

* The handshake signature must cover **/v1/ws/markets**. Signing ``/`` is
  rejected with HTTP 401.
* Subscribing yields an immediate snapshot per market, then live deltas.
* Every frame is ``{requestId, subscriptionType, marketData: {...}}``.

We subscribe to the **full** ``SUBSCRIPTION_TYPE_MARKET_DATA`` rather than the
lite variant for one specific reason: lite reports ``bidDepth``/``askDepth``,
which count price *levels* rather than contracts - measured 49 against a true
best-bid size of 287,926.98 - and only the full ladder carries real ``qty``.
Only level 0 is read; the rest of the ladder is the price of that correctness.

**One connection will not stream an unlimited number of markets, and it fails
silently.** Measured 2026-08-25 with 573 markets on a single socket: the venue
kept the connection healthy - no error, no disconnect, ~250 frames/s arriving -
while delivering deltas for only ~400 of them in any 30s window, and what it
dropped included the live in-play markets that reprice fastest. An A/B/A test
isolated this from "the market simply went quiet": of nine slugs proven to be
streaming both immediately before and immediately after, eight were shed while
subscribed alongside 564 others, and all nine streamed when subscribed alone. A
second connection on the same API key had full service throughout, so the
ceiling is per **connection**, not per key.

So the subscription is **sharded across connections**, ``shard_size`` slugs
each, and every shard reports how many of its markets actually delivered. That
count is the only symptom this failure has: quotes merely get older, which
reads as a quiet market right up until a stale leg invents an arbitrage against
a live one on the other venue.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import websockets

from ..shared.types import Outcome, Quote, Side
from .base import MarketDataAdapter
from .polymarket_us import LONG, SHORT, quotes_from_levels, split_outcome_id
from .polymarket_us_auth import PolymarketUsCredentials, auth_headers

DEFAULT_WS_URL = "wss://api.polymarket.us/v1/ws/markets"
WS_SIGN_PATH = "/v1/ws/markets"
SUBSCRIPTION_TYPE = "SUBSCRIPTION_TYPE_MARKET_DATA"
MAX_SLUGS_PER_SUBSCRIPTION = 100

# Slugs per connection. 21 on one socket streamed completely and 573 shed ~90%;
# the sizes in between could not be measured cleanly, because in-play matches
# kept ending mid-sweep and a market that stops trading is indistinguishable
# from one the venue dropped. So this is deliberately conservative rather than
# fitted to a measured ceiling: the per-shard ``live`` count in the feed report
# is what confirms it, and lowering this is the response if that count sags.
DEFAULT_SHARD_SIZE = 100

# How often each shard reports feed health. A market-data socket that stops
# delivering looks exactly like a quiet market: no error, no disconnect, quotes
# simply ageing. The only way to tell them apart is to count what arrives, so
# this reports every interval whether or not anything did.
FEED_REPORT_INTERVAL_S = 30.0

# A subscribed market that has not been delivered in this long is treated as
# dark. This is a *diagnosis*, not a data source: the response is to get the
# socket delivering it again, never to substitute a price from somewhere else.
DEFAULT_DARK_AFTER_S = 120.0

# A market whose game is under way earns a far tighter deadline - an in-play
# book reprices on every point, so 120s of silence there is a fault where the
# same silence on next week's game is normal.
DEFAULT_PRIORITY_DARK_AFTER_S = 6.0

# Re-subscribing markets the socket is not delivering. Subscribing is additive
# and needs no unsubscribe, so a repair costs one small message on the existing
# connection - no reconnect, and nothing else on that socket is disturbed.
#
# Why this is needed at all: the venue acknowledges a subscription as a whole,
# never per market. If it registers 97 of the 100 names we send, nothing says
# so, and those three stay dark for the life of the connection because we
# subscribe exactly once at connect and never check.
REPAIR_SWEEP_S = 15.0

# Per market, per connection, before giving up on repairing in place and
# rebuilding the connection instead.
MAX_REPAIR_ATTEMPTS = 5

# A shard reconnects for dark markets at most this often. Reconnecting costs a
# burst of replayed snapshots, so it is the escalation of last resort and must
# not become a loop when a market is simply finished.
RECONNECT_COOLDOWN_S = 120.0

# Bounded so a stalled consumer applies backpressure to the sockets rather than
# growing without limit. Generous enough that a normal burst never reaches it:
# the whole venue runs ~500 quotes/s and the consumer costs ~34us per quote.
QUEUE_MAXSIZE = 20_000

log = logging.getLogger(__name__)


class DarkMarkets(Exception):
    """Raised to drop a shard whose in-play markets will not come back.

    Not an error condition on the wire - the socket is healthy and busy. It is
    the deliberate escalation when re-subscribing in place has failed for a
    market whose game is under way: rebuild the connection and get a fresh
    subscription rather than leave a live book unquoted.
    """

# The venue stamps nanoseconds; datetime handles at most microseconds.
_FRACTION = re.compile(r"\.(\d{1,9})")


def frame_age_s(transact_time: Any, *, now: float | None = None) -> float | None:
    """Seconds between a frame's own ``transactTime`` and now, or None.

    This is the only field that distinguishes a live book from a replayed
    snapshot. Both arrive identically; only this says the prices in one of
    them are hours old.

    Negative results clamp to 0: a venue slightly ahead of our clock is skew,
    not a quote from the future, and treating it as one would make a stale
    quote look fresh - the exact failure this exists to prevent.
    """
    if not isinstance(transact_time, str) or not transact_time:
        return None
    text = transact_time.strip().replace("Z", "+00:00")
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    current = datetime.now(UTC).timestamp() if now is None else now
    return max(0.0, current - stamp.timestamp())


@dataclass
class _ShardStats:
    """One report window's counters for a single connection."""

    frames: int = 0
    quotes: int = 0
    unknown_slug: int = 0
    no_market_data: int = 0
    replayed: int = 0  # frames whose own transactTime was already stale
    seen: set[str] = field(default_factory=set)
    since: float = 0.0

    def reset(self, now: float) -> None:
        self.frames = 0
        self.quotes = 0
        self.unknown_slug = 0
        self.no_market_data = 0
        self.replayed = 0
        self.seen = set()
        self.since = now


class PolymarketUsWebSocketAdapter(MarketDataAdapter):
    venue_id = "polymarket_us"

    def __init__(
        self,
        *,
        outcome_ids: list[str],
        creds: PolymarketUsCredentials,
        url: str = DEFAULT_WS_URL,
        initial_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        shard_size: int = DEFAULT_SHARD_SIZE,
        dark_after_s: float = DEFAULT_DARK_AFTER_S,
        priority_dark_after_s: float = DEFAULT_PRIORITY_DARK_AFTER_S,
        repair_sweep_s: float = REPAIR_SWEEP_S,
        priority_slugs: Callable[[], set[str]] | None = None,
    ) -> None:
        self._outcome_ids = outcome_ids or []
        self._creds = creds
        self._url = url
        self._initial_backoff_s = initial_backoff_s
        self._max_backoff_s = max_backoff_s
        self._shard_size = max(1, shard_size)
        self._dark_after_s = dark_after_s
        self._priority_dark_after_s = priority_dark_after_s
        self._repair_sweep_s = repair_sweep_s
        # Supplied by AppState, which knows each group's start time and the
        # venue's live/ended flags. Re-read every sweep, so a game going
        # in-play tightens its own deadline with no restart.
        self._priority_slugs = priority_slugs
        self._slugs = sorted({split_outcome_id(o)[0] for o in self._outcome_ids})
        self._slug_set = set(self._slugs)
        self._shard_tasks: list[asyncio.Task[None]] = []
        # Monotonic time each slug last arrived on the socket. This is the
        # only delivery clock there is, deliberately: nothing else may write
        # a price, so nothing else may make a market look healthy.
        self._last_ws_at: dict[str, float] = {}
        # Per shard, when it last reconnected because of dark markets.
        self._last_reconnect_at: dict[int, float] = {}

    def set_priority_slugs(self, fn: Callable[[], set[str]] | None) -> None:
        """Tell the adapter which markets need the tighter deadline.

        Called by ``AppState`` at wiring time; the callable is re-invoked every
        sweep, so a game that goes in-play starts being watched more closely
        without anything being rebuilt.
        """
        self._priority_slugs = fn

    @property
    def shards(self) -> list[list[str]]:
        """The slug groups, one per connection."""
        size = self._shard_size
        return [self._slugs[i : i + size] for i in range(0, len(self._slugs), size)]

    async def close(self) -> None:
        """Shut every shard connection down, now.

        This has to be deterministic rather than left to the garbage
        collector. A shard runs in its own task, and a task is referenced by
        the event loop, so dropping the ``stream_quotes`` generator does not
        end it - the socket would keep streaming, keep its markets subscribed,
        and keep counting against the per-connection ceiling that sharding
        exists to stay under. ``restart_ingest`` rebuilds the adapters on every
        discovery pass that changes anything, so a leak here compounds.
        """
        await self._cancel_shards()

    async def _cancel_shards(self) -> None:
        tasks, self._shard_tasks = self._shard_tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def list_markets(self) -> list[Outcome]:
        return [
            Outcome(
                id=f"{slug}:{side}",
                venue_id=self.venue_id,
                market_id=slug,
                label=f"{slug} ({side})",
                side=Side.YES if side == LONG else Side.NO,
            )
            for slug in self._slugs
            for side in (LONG, SHORT)
        ]

    async def stream_quotes(self) -> AsyncIterator[Quote]:
        """Merge every shard's connection into one quote stream.

        Each shard owns its own reconnect loop, so one socket dropping neither
        stalls the others nor makes them resubscribe.
        """
        if not self._slugs:
            log.info("PolymarketUsWebSocketAdapter: no slugs to subscribe to")
            return
        shards = self.shards
        queue: asyncio.Queue[Quote] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        log.info(
            "polymarket_us WS: %d slug(s) across %d connection(s) of up to %d",
            len(self._slugs),
            len(shards),
            self._shard_size,
        )
        await self._cancel_shards()  # never run two generations at once
        started = time.monotonic()
        self._last_ws_at = dict.fromkeys(self._slugs, started)
        self._shard_tasks = [
            asyncio.create_task(
                self._run_shard(i, shard, queue), name=f"polymarket_us-ws-{i}"
            )
            for i, shard in enumerate(shards)
        ]
        try:
            while True:
                yield await queue.get()
        finally:
            # Covers the generator being closed or collected. `close()` is the
            # path that actually runs on shutdown, because cancelling the
            # consumer task does not close a suspended async generator.
            await self._cancel_shards()

    async def _run_shard(
        self, index: int, slugs: list[str], queue: asyncio.Queue[Quote]
    ) -> None:
        """Keep one connection alive for one shard, indefinitely."""
        backoff = self._initial_backoff_s
        stats = _ShardStats()
        while True:
            try:
                await self._connect_and_pump(index, slugs, queue, stats)
                backoff = self._initial_backoff_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "polymarket_us WS shard %d disconnect: %s; retry in %.1fs",
                    index,
                    exc,
                    backoff,
                )
            # No fallback to REST here on purpose: silently downgrading would
            # hide a revoked credential indefinitely, visible only as degraded
            # fill quality.
            await asyncio.sleep(backoff * (0.5 + random.random()))
            backoff = min(backoff * 2, self._max_backoff_s)

    async def _connect_and_pump(
        self,
        index: int,
        slugs: list[str],
        queue: asyncio.Queue[Quote],
        stats: _ShardStats,
    ) -> None:
        headers = auth_headers(self._creds, "GET", WS_SIGN_PATH)
        log.info("polymarket_us WS shard %d connecting: %d slug(s)", index, len(slugs))
        async with websockets.connect(
            self._url,
            additional_headers=headers,
            max_size=2**22,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            # Subscribing from scratch on every connect re-requests a snapshot
            # for each market, so the quote book self-heals after a drop rather
            # than serving pre-disconnect prices until they age out.
            for i in range(0, len(slugs), MAX_SLUGS_PER_SUBSCRIPTION):
                request_id = f"arbys-{index}-{i // MAX_SLUGS_PER_SUBSCRIPTION}"
                await ws.send(
                    json.dumps(
                        {
                            "subscribe": {
                                "requestId": request_id,
                                "subscriptionType": SUBSCRIPTION_TYPE,
                                "marketSlugs": slugs[
                                    i : i + MAX_SLUGS_PER_SUBSCRIPTION
                                ],
                            }
                        }
                    )
                )
            connected_at = time.monotonic()
            stats.reset(connected_at)
            # Seed from the subscribe, so a market that never answers becomes a
            # repair candidate once its deadline passes rather than looking fresh
            # forever on an absent entry.
            for slug in slugs:
                self._last_ws_at[slug] = connected_at
            delivered: set[str] = set()
            attempts: dict[str, int] = {}
            next_repair = connected_at + self._repair_sweep_s
            while True:
                # A timeout rather than `async for`, so the repair sweep below
                # runs on a clock instead of only when traffic arrives. Frame-
                # driven, a shard whose markets had *all* gone dark could never
                # repair itself - the one case that most needs repairing.
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=self._repair_sweep_s
                    )
                except TimeoutError:
                    now = time.monotonic()
                    if now >= next_repair:
                        next_repair = now + self._repair_sweep_s
                        await self._repair_subscriptions(
                            ws, index, slugs, delivered, attempts
                        )
                    continue
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue  # one bad frame must not drop the connection
                stats.frames += 1
                for quote in self._quotes_from_message(msg, stats):
                    stats.quotes += 1
                    slug = split_outcome_id(quote.outcome_id)[0]
                    delivered.add(slug)
                    attempts.pop(slug, None)  # it answered; restore its budget
                    await queue.put(quote)
                self._maybe_report(index, len(slugs), stats)
                now = time.monotonic()
                if now >= next_repair:
                    next_repair = now + self._repair_sweep_s
                    await self._repair_subscriptions(
                        ws, index, slugs, delivered, attempts
                    )

    async def _repair_subscriptions(
        self,
        ws: Any,
        index: int,
        slugs: list[str],
        delivered: set[str],
        attempts: dict[str, int],
    ) -> None:
        """Re-send a subscribe for markets this socket is not delivering.

        Two kinds of market qualify, and the distinction keeps this from
        spamming a healthy feed:

        A market qualifies once it has sent *nothing at all* for longer than
        its deadline - 6s for a game under way, 120s otherwise. Note the
        deadline is on **delivery**, not on how current the book is: a market
        that is quiet because nothing has traded is subscribed correctly and
        keeps answering, so it never becomes a candidate. Whether its prices
        are still worth anything is a separate question, settled by
        `source_age_s` back-dating and the quote book's age check.

        `MAX_REPAIR_ATTEMPTS` bounds the cost per connection, and the counter
        resets the moment a market delivers, so a market that answers a repair
        gets its full budget back and only a permanently silent one is
        abandoned.

        Safe to do at any time because the quote book refuses to go backwards:
        the snapshot a fresh subscribe replays can be hours old, and on a
        market that is streaming it would otherwise clobber live prices.

        **Raises ``DarkMarkets`` when repairing in place has failed** for a
        market whose game is under way, which drops the connection so the
        shard rebuilds it from scratch. That escalation is the whole design:
        the answer to a socket that is not delivering is to get the socket
        delivering, never to fill the gap from a slower endpoint. A REST read
        would be both behind the ladder and sizeless, and a sizeless quote is
        worse than no quote - `tradeable_qty` treats unknown depth as *no
        ceiling*, so it sized a real ticket at 200 contracts where the two
        built from ladder depth were capped at 25.
        """
        now = time.monotonic()
        priority: set[str] = set()
        if self._priority_slugs is not None:
            with contextlib.suppress(Exception):
                priority = self._priority_slugs()
        dark: list[str] = []
        never = 0
        for slug in slugs:
            if attempts.get(slug, 0) >= MAX_REPAIR_ATTEMPTS:
                continue
            threshold = (
                self._priority_dark_after_s if slug in priority else self._dark_after_s
            )
            if now - self._last_ws_at.get(slug, now) <= threshold:
                continue
            dark.append(slug)
            if slug not in delivered:
                never += 1
        if not dark:
            return
        for i in range(0, len(dark), MAX_SLUGS_PER_SUBSCRIPTION):
            batch = dark[i : i + MAX_SLUGS_PER_SUBSCRIPTION]
            await ws.send(
                json.dumps(
                    {
                        "subscribe": {
                            "requestId": f"arbys-repair-{index}-{i}",
                            "subscriptionType": SUBSCRIPTION_TYPE,
                            "marketSlugs": batch,
                        }
                    }
                )
            )
        for slug in dark:
            attempts[slug] = attempts.get(slug, 0) + 1
        self._maybe_escalate(index, slugs, priority, attempts, now)
        log.info(
            "polymarket_us WS shard %d: re-subscribing %d market(s) "
            "(%d never delivered on this connection, %d in-play gone quiet)",
            index,
            len(dark),
            never,
            len(dark) - never,
        )

    def _quotes_from_message(self, msg: Any, stats: _ShardStats) -> list[Quote]:
        if not isinstance(msg, dict):
            return []
        data = msg.get("marketData")
        if not isinstance(data, dict):
            stats.no_market_data += 1
            return []  # subscription acks, errors, other subscription types
        slug = data.get("marketSlug")
        if not slug:
            stats.no_market_data += 1
            return []
        if slug not in self._slug_set:
            # The venue is streaming a market we never asked for, which means
            # our subscription and our quote book disagree about what we hold.
            stats.unknown_slug += 1
            return []
        stats.seen.add(str(slug))
        bids = data.get("bids") or []
        offers = data.get("offers") or []
        if not isinstance(bids, list) or not isinstance(offers, list):
            return []
        # The frame's own clock, not ours. A subscribe replays a cached book
        # whose transactTime can be hours behind; stamped on arrival it would
        # enter the book as a fresh quote and no age check could withhold it.
        age = frame_age_s(data.get("transactTime"))
        if age is not None and age > self._priority_dark_after_s:
            stats.replayed += 1
        # Any frame counts as the subscription working, however old the book it
        # describes. These are two different questions and conflating them
        # breaks both: a market that is quiet because nothing has traded is
        # *subscribed correctly* and must not be re-subscribed forever, while a
        # book that is genuinely out of date is handled where it belongs - by
        # `source_age_s` back-dating it, so the quote book withholds it. Old
        # and withheld is the safe answer; re-subscribing cannot make a market
        # trade.
        self._last_ws_at[str(slug)] = time.monotonic()
        return quotes_from_levels(str(slug), bids, offers, age)

    def _maybe_escalate(
        self,
        index: int,
        slugs: list[str],
        priority: set[str],
        attempts: dict[str, int],
        now: float,
    ) -> None:
        """Drop the connection when in-play markets stay dark despite repairs.

        Only in-play markets justify this. A pre-game book that never answers
        is usually finished or delisted, and reconnecting for it would thrash
        the socket forever; the age check withholds it, which is the correct
        and safe outcome. A live book is different - it is exactly the leg
        that invents an arbitrage against the other venue when it goes stale.

        Cooldown-limited because a reconnect makes the venue replay cached
        snapshots for the whole shard, so it must stay the last resort.
        """
        stuck = [
            slug
            for slug in slugs
            if slug in priority
            and attempts.get(slug, 0) >= MAX_REPAIR_ATTEMPTS
            and now - self._last_ws_at.get(slug, now) > self._priority_dark_after_s
        ]
        if not stuck:
            return
        last = self._last_reconnect_at.get(index)
        if last is not None and now - last < RECONNECT_COOLDOWN_S:
            return
        self._last_reconnect_at[index] = now
        raise DarkMarkets(
            f"{len(stuck)} in-play market(s) still dark after "
            f"{MAX_REPAIR_ATTEMPTS} repairs: {stuck[:3]}"
        )

    def _maybe_report(self, index: int, subscribed: int, stats: _ShardStats) -> None:
        """Log one shard's feed health once per interval.

        ``live`` - distinct slugs that sent anything this window - is the
        number that matters. A socket can be connected, error-free and busy
        while delivering only a fraction of what it was asked for, and the gap
        between ``live`` and ``subscribed`` is the only visible symptom of
        that. Some markets are legitimately quiet, so read it as a trend
        rather than as an alarm at any single value.
        """
        now = time.monotonic()
        elapsed = now - stats.since
        if elapsed < FEED_REPORT_INTERVAL_S:
            return
        log.info(
            "polymarket_us WS shard %d: %.0f frames/s, %.0f quotes/s, "
            "live %d/%d slug(s), %d stale-on-arrival, %d unknown-slug, "
            "%d non-market frame(s)",
            index,
            stats.frames / elapsed,
            stats.quotes / elapsed,
            len(stats.seen),
            subscribed,
            stats.replayed,
            stats.unknown_slug,
            stats.no_market_data,
        )
        stats.reset(now)
