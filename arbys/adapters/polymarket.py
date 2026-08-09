"""Polymarket market-data adapter.

Uses Polymarket's Gamma markets API for discovery and the CLOB WebSocket
(``wss://ws-subscriptions-clob.polymarket.com/ws/market``) for streaming
quotes. If the WebSocket is unhealthy (repeated failures), the adapter falls
back to REST polling of the CLOB ``/price`` endpoint until the next WS
success. Market discovery via Gamma always uses REST.

The public WS market channel requires no credentials.

The adapter deliberately does *not* attempt to be exhaustive — it fetches only
active binary markets, since those are what the arb engine currently supports.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from ..shared.types import Outcome, Quote, Side
from .base import MarketDataAdapter

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_PRICE_URL = "https://clob.polymarket.com/price"
DEFAULT_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

log = logging.getLogger(__name__)


class _BookState:
    """Per-asset limit-order book, price -> size, one side at a time.

    We keep it as a plain dict and recompute best bid/ask on demand — the book
    is small enough (top-of-book only really matters) that this is fine.
    """

    __slots__ = ("asks", "bids")

    def __init__(self) -> None:
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}

    def replace_side(self, side: str, levels: list[dict[str, Any]]) -> None:
        target = self._side(side)
        target.clear()
        for lvl in levels:
            price = _dec(lvl.get("price"))
            size = _dec(lvl.get("size"))
            if price is None or size is None or size <= 0:
                continue
            target[price] = size

    def apply_change(self, side: str, price: Any, size: Any) -> None:
        target = self._side(side)
        p = _dec(price)
        s = _dec(size)
        if p is None:
            return
        if s is None or s <= 0:
            target.pop(p, None)
        else:
            target[p] = s

    def best_bid(self) -> Decimal | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Decimal | None:
        return min(self.asks) if self.asks else None

    def best_bid_level(self) -> tuple[Decimal, Decimal] | None:
        """Best bid and the size resting at it."""
        if not self.bids:
            return None
        p = max(self.bids)
        return p, self.bids[p]

    def best_ask_level(self) -> tuple[Decimal, Decimal] | None:
        if not self.asks:
            return None
        p = min(self.asks)
        return p, self.asks[p]

    def _side(self, side: str) -> dict[Decimal, Decimal]:
        s = side.upper()
        if s == "BUY":
            return self.bids
        if s == "SELL":
            return self.asks
        raise ValueError(f"unknown side: {side!r}")


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _quote_from_book(outcome_id: str, book: _BookState) -> Quote | None:
    """Top of book plus the depth resting there.

    Sizes matter because a quoted price only holds for as much as is actually
    on that level; a one-sided book reports the size it has and 0 for the side
    it is standing in for.
    """
    bid_level = book.best_bid_level()
    ask_level = book.best_ask_level()
    if bid_level is None and ask_level is None:
        return None

    zero = Decimal("0")
    if bid_level is None:
        bid, bid_size = ask_level[0], zero  # type: ignore[index]
    else:
        bid, bid_size = bid_level
    if ask_level is None:
        ask, ask_size = bid, zero
    else:
        ask, ask_size = ask_level
    if bid > ask:
        bid = ask
    return Quote(
        outcome_id=outcome_id, bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size
    )


def _iter_events(payload: Any) -> list[dict[str, Any]]:
    """Polymarket sends either a single event dict or a list of events."""
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


class PolymarketAdapter(MarketDataAdapter):
    venue_id = "polymarket"

    def __init__(
        self,
        *,
        poll_interval_s: float = 5.0,
        outcome_ids: list[str] | None = None,
        http_client: httpx.AsyncClient | None = None,
        use_websocket: bool = True,
        ws_url: str = DEFAULT_WS_URL,
        ws_backoff_initial_s: float = 1.0,
        ws_backoff_max_s: float = 30.0,
        ws_fallback_failure_threshold: int = 3,
        ws_fallback_window_s: float = 60.0,
    ) -> None:
        self._poll_interval_s = poll_interval_s
        self._outcome_ids = outcome_ids or []
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=10.0)
        self._use_ws = use_websocket
        self._ws_url = ws_url
        self._backoff_initial = ws_backoff_initial_s
        self._backoff_max = ws_backoff_max_s
        self._fail_threshold = ws_fallback_failure_threshold
        self._fail_window = ws_fallback_window_s
        self._books: dict[str, _BookState] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def list_markets(self, *, limit: int = 100, active_only: bool = True) -> list[Outcome]:
        params: dict[str, Any] = {"limit": limit, "closed": "false" if active_only else "true"}
        resp = await self._http.get(GAMMA_MARKETS_URL, params=params)
        resp.raise_for_status()
        markets = resp.json()

        outcomes: list[Outcome] = []
        for m in markets:
            token_ids = m.get("clobTokenIds") or []
            if isinstance(token_ids, str):
                try:
                    token_ids = json.loads(token_ids)
                except json.JSONDecodeError:
                    token_ids = []
            outcome_labels = m.get("outcomes") or ["Yes", "No"]
            if isinstance(outcome_labels, str):
                try:
                    outcome_labels = json.loads(outcome_labels)
                except json.JSONDecodeError:
                    outcome_labels = ["Yes", "No"]

            for i, tid in enumerate(token_ids):
                label = outcome_labels[i] if i < len(outcome_labels) else f"outcome{i}"
                side = Side.YES if label.lower().startswith("y") else Side.NO
                outcomes.append(
                    Outcome(
                        id=str(tid),
                        venue_id=self.venue_id,
                        market_id=str(m.get("id", "")),
                        label=label,
                        side=side,
                    )
                )
        return outcomes

    async def _fetch_quote(self, token_id: str) -> Quote | None:
        try:
            buy_resp, sell_resp = await asyncio.gather(
                self._http.get(CLOB_PRICE_URL, params={"token_id": token_id, "side": "buy"}),
                self._http.get(CLOB_PRICE_URL, params={"token_id": token_id, "side": "sell"}),
            )
            buy_resp.raise_for_status()
            sell_resp.raise_for_status()
            ask = Decimal(str(buy_resp.json().get("price", "0")))
            bid = Decimal(str(sell_resp.json().get("price", "0")))
            if bid > ask:
                bid = ask
            return Quote(outcome_id=token_id, bid=bid, ask=ask)
        except (httpx.HTTPError, ValueError, KeyError):
            return None

    def _apply_event(self, event: dict[str, Any]) -> Quote | None:
        etype = event.get("event_type") or event.get("type")
        asset_id = event.get("asset_id") or event.get("market") or event.get("token_id")
        if not asset_id or not isinstance(asset_id, str):
            return None
        book = self._books.setdefault(asset_id, _BookState())

        if etype == "book":
            book.replace_side("BUY", event.get("bids") or event.get("buys") or [])
            book.replace_side("SELL", event.get("asks") or event.get("sells") or [])
        elif etype == "price_change":
            changes = event.get("changes") or []
            if not changes:
                side = event.get("side")
                if side:
                    book.apply_change(side, event.get("price"), event.get("size"))
            else:
                for ch in changes:
                    if not isinstance(ch, dict):
                        continue
                    book.apply_change(
                        ch.get("side", ""), ch.get("price"), ch.get("size")
                    )
        else:
            return None

        return _quote_from_book(asset_id, book)

    async def _ws_once(self, queue: asyncio.Queue[Quote]) -> int:
        """Connect once, subscribe, pump events. Returns number of quotes emitted."""
        emitted = 0
        async with websockets.connect(self._ws_url, open_timeout=10, close_timeout=5) as ws:
            sub = {"assets_ids": list(self._outcome_ids), "type": "market"}
            await ws.send(json.dumps(sub))
            log.info("polymarket ws connected, subscribed to %d assets", len(self._outcome_ids))
            async for raw in ws:
                try:
                    payload = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                for event in _iter_events(payload):
                    quote = self._apply_event(event)
                    if quote is not None:
                        await queue.put(quote)
                        emitted += 1
        return emitted

    async def _ws_producer(self, queue: asyncio.Queue[Quote]) -> None:
        """Manage WS lifecycle: connect, reconnect with backoff, signal fallback.

        Puts a sentinel ``None`` into the queue when the failure threshold is
        exceeded so the consumer can start REST fallback polling.
        """
        backoff = self._backoff_initial
        failure_times: list[float] = []
        while True:
            failed = False
            try:
                emitted = await self._ws_once(queue)
                if emitted == 0:
                    failed = True
                    log.warning("polymarket ws closed without delivering any quotes")
                else:
                    backoff = self._backoff_initial
                    failure_times.clear()
            except asyncio.CancelledError:
                raise
            except (TimeoutError, WebSocketException, ConnectionClosed, OSError) as exc:
                log.warning("polymarket ws error: %s", exc)
                failed = True
            if failed:
                now = time.monotonic()
                failure_times.append(now)
                cutoff = now - self._fail_window
                failure_times[:] = [t for t in failure_times if t >= cutoff]
                if len(failure_times) >= self._fail_threshold:
                    log.warning(
                        "polymarket ws unhealthy (%d failures in %.0fs); requesting fallback",
                        len(failure_times),
                        self._fail_window,
                    )
                    await queue.put(None)  # type: ignore[arg-type]
                    failure_times.clear()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._backoff_max)

    async def _rest_poll_once(self, queue: asyncio.Queue[Quote]) -> None:
        for tid in self._outcome_ids:
            q = await self._fetch_quote(tid)
            if q is not None:
                await queue.put(q)

    async def stream_quotes(self) -> AsyncIterator[Quote]:
        if not self._outcome_ids:
            return

        if not self._use_ws:
            async for q in self._rest_stream():
                yield q
            return

        queue: asyncio.Queue[Quote | None] = asyncio.Queue(maxsize=1024)
        ws_task = asyncio.create_task(self._ws_producer(queue))  # type: ignore[arg-type]
        fallback_task: asyncio.Task[None] | None = None
        try:
            while True:
                item = await queue.get()
                if item is None:
                    if fallback_task is None or fallback_task.done():
                        fallback_task = asyncio.create_task(self._rest_fallback_loop(queue))  # type: ignore[arg-type]
                    continue
                # Any WS quote means WS is healthy — stop fallback polling.
                if fallback_task is not None and not fallback_task.done():
                    fallback_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await fallback_task
                    fallback_task = None
                yield item
        finally:
            ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await ws_task
            if fallback_task is not None:
                fallback_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await fallback_task

    async def _rest_stream(self) -> AsyncIterator[Quote]:
        while True:
            for tid in self._outcome_ids:
                q = await self._fetch_quote(tid)
                if q is not None:
                    yield q
            await asyncio.sleep(self._poll_interval_s)

    async def _rest_fallback_loop(self, queue: asyncio.Queue[Quote]) -> None:
        log.info("polymarket REST fallback polling started")
        try:
            while True:
                await self._rest_poll_once(queue)
                await asyncio.sleep(self._poll_interval_s)
        finally:
            log.info("polymarket REST fallback polling stopped")
