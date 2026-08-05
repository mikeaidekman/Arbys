"""Kalshi market-data adapter.

Uses Kalshi's public REST API v2 for market discovery and orderbook top polling.
The Kalshi trade API requires authentication (email+password → token, or an API
key). This adapter accepts an optional `token_provider` callable so the same
class can be used both anonymously (public market listings) and with auth.

Streaming via WS is a follow-up; polling is used in v1.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal

import httpx

from ..shared.types import Outcome, Quote, Side
from .base import MarketDataAdapter

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiAdapter(MarketDataAdapter):
    venue_id = "kalshi"

    def __init__(
        self,
        *,
        poll_interval_s: float = 5.0,
        outcome_ids: list[str] | None = None,
        token_provider: Callable[[], Awaitable[str | None]] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._poll_interval_s = poll_interval_s
        self._outcome_ids = outcome_ids or []
        self._token_provider = token_provider
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=10.0, base_url=BASE_URL)

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def _headers(self) -> dict[str, str]:
        if self._token_provider is None:
            return {}
        token = await self._token_provider()
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def list_markets(self, *, limit: int = 100, status: str = "open") -> list[Outcome]:
        resp = await self._http.get(
            "/markets", params={"limit": limit, "status": status}, headers=await self._headers()
        )
        resp.raise_for_status()
        payload = resp.json()
        outcomes: list[Outcome] = []
        for m in payload.get("markets", []):
            ticker = m.get("ticker")
            if not ticker:
                continue
            title = m.get("title") or m.get("subtitle") or ticker
            # Kalshi markets are binary: YES and NO tradeable sides tied to same ticker.
            for side_label, side_enum in (("YES", Side.YES), ("NO", Side.NO)):
                outcomes.append(
                    Outcome(
                        id=f"{ticker}:{side_label}",
                        venue_id=self.venue_id,
                        market_id=ticker,
                        label=f"{title} ({side_label})",
                        side=side_enum,
                    )
                )
        return outcomes

    def _split_outcome_id(self, outcome_id: str) -> tuple[str, Side]:
        ticker, side_str = outcome_id.rsplit(":", 1)
        return ticker, Side.YES if side_str.upper() == "YES" else Side.NO

    async def _fetch_quote(self, outcome_id: str) -> Quote | None:
        try:
            ticker, side = self._split_outcome_id(outcome_id)
            resp = await self._http.get(
                f"/markets/{ticker}/orderbook",
                params={"depth": 1},
                headers=await self._headers(),
            )
            resp.raise_for_status()
            ob = resp.json().get("orderbook", {})
            # Kalshi quotes prices in cents (0-99). yes/no arrays: [[price, size], ...].
            if side is Side.YES:
                yes_bids = ob.get("yes") or []
                no_bids = ob.get("no") or []
                bid_c = yes_bids[-1][0] if yes_bids else 0
                # Ask on YES = 100 - top NO bid (the price someone is willing to pay for NO).
                ask_c = 100 - no_bids[-1][0] if no_bids else 100
            else:
                no_bids = ob.get("no") or []
                yes_bids = ob.get("yes") or []
                bid_c = no_bids[-1][0] if no_bids else 0
                ask_c = 100 - yes_bids[-1][0] if yes_bids else 100
            bid = Decimal(bid_c) / Decimal(100)
            ask = Decimal(ask_c) / Decimal(100)
            if bid > ask:
                bid = ask
            return Quote(outcome_id=outcome_id, bid=bid, ask=ask)
        except (httpx.HTTPError, ValueError, KeyError, IndexError):
            return None

    async def stream_quotes(self) -> AsyncIterator[Quote]:
        if not self._outcome_ids:
            return
        while True:
            for oid in self._outcome_ids:
                q = await self._fetch_quote(oid)
                if q is not None:
                    yield q
            await asyncio.sleep(self._poll_interval_s)
