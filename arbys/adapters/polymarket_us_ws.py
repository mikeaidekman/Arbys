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
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator
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

log = logging.getLogger(__name__)


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
    ) -> None:
        self._outcome_ids = outcome_ids or []
        self._creds = creds
        self._url = url
        self._initial_backoff_s = initial_backoff_s
        self._max_backoff_s = max_backoff_s
        self._slugs = sorted({split_outcome_id(o)[0] for o in self._outcome_ids})
        self._slug_set = set(self._slugs)

    async def close(self) -> None:
        """Nothing to release.

        The connection lives inside ``stream_quotes`` and is closed by its
        context manager when the iterator is dropped or cancelled.
        """
        return None

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
        if not self._slugs:
            log.info("PolymarketUsWebSocketAdapter: no slugs to subscribe to")
            return
        backoff = self._initial_backoff_s
        while True:
            try:
                async for quote in self._connect_and_stream():
                    backoff = self._initial_backoff_s
                    yield quote
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "polymarket_us WS disconnect: %s; retry in %.1fs", exc, backoff
                )
            # No fallback to REST here on purpose: silently downgrading would
            # hide a revoked credential indefinitely, visible only as degraded
            # fill quality.
            await asyncio.sleep(backoff * (0.5 + random.random()))
            backoff = min(backoff * 2, self._max_backoff_s)

    async def _connect_and_stream(self) -> AsyncIterator[Quote]:
        headers = auth_headers(self._creds, "GET", WS_SIGN_PATH)
        log.info("polymarket_us WS connecting: %d slug(s)", len(self._slugs))
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
            for i in range(0, len(self._slugs), MAX_SLUGS_PER_SUBSCRIPTION):
                batch = self._slugs[i : i + MAX_SLUGS_PER_SUBSCRIPTION]
                await ws.send(
                    json.dumps(
                        {
                            "subscribe": {
                                "requestId": f"arbys-{i // MAX_SLUGS_PER_SUBSCRIPTION}",
                                "subscriptionType": SUBSCRIPTION_TYPE,
                                "marketSlugs": batch,
                            }
                        }
                    )
                )
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue  # one bad frame must not drop the connection
                for quote in self._quotes_from_message(msg):
                    yield quote

    def _quotes_from_message(self, msg: Any) -> list[Quote]:
        if not isinstance(msg, dict):
            return []
        data = msg.get("marketData")
        if not isinstance(data, dict):
            return []  # subscription acks, errors, other subscription types
        slug = data.get("marketSlug")
        if not slug or slug not in self._slug_set:
            return []
        bids = data.get("bids") or []
        offers = data.get("offers") or []
        if not isinstance(bids, list) or not isinstance(offers, list):
            return []
        return quotes_from_levels(str(slug), bids, offers)
