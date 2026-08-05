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


def _best_price_dollars(levels: object) -> Decimal | None:
    """Return the highest bid price from a list of ``[price_str, size_str]``."""
    if not isinstance(levels, list) or not levels:
        return None
    best: Decimal | None = None
    for lvl in levels:
        try:
            p = Decimal(str(lvl[0]))
        except (ValueError, IndexError, TypeError):
            continue
        if best is None or p > best:
            best = p
    return best


def _best_price_cents(levels: object) -> Decimal | None:
    if not isinstance(levels, list) or not levels:
        return None
    best_c: int | None = None
    for lvl in levels:
        try:
            c = int(lvl[0])
        except (ValueError, IndexError, TypeError):
            continue
        if best_c is None or c > best_c:
            best_c = c
    if best_c is None:
        return None
    return Decimal(best_c) / Decimal(100)


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
            body = resp.json()
        except (httpx.HTTPError, ValueError, KeyError, IndexError):
            return None
        return self._parse_orderbook(outcome_id, side, body)

    @staticmethod
    def _parse_orderbook(outcome_id: str, side: Side, body: dict) -> Quote | None:
        """Parse Kalshi's orderbook payload into a top-of-book Quote.

        Kalshi returns one of two schemas depending on API version:

        * Current (2025+): ``orderbook_fp`` with ``yes_dollars`` / ``no_dollars``
          arrays of ``[price_dollars_str, size_str]``.
        * Legacy: ``orderbook`` with ``yes`` / ``no`` arrays of
          ``[price_cents_int, size_int]``.

        For a YES outcome:
          bid = best (highest) YES bid
          ask = 1 - best (highest) NO bid  (the price to buy YES = 1 - what someone pays for NO)
        For a NO outcome: mirror.
        """
        try:
            fp = body.get("orderbook_fp")
            if fp is not None:
                yes_side = _best_price_dollars(fp.get("yes_dollars"))
                no_side = _best_price_dollars(fp.get("no_dollars"))
            else:
                ob = body.get("orderbook") or {}
                yes_side = _best_price_cents(ob.get("yes"))
                no_side = _best_price_cents(ob.get("no"))
        except (ValueError, TypeError, KeyError, IndexError):
            return None

        if side is Side.YES:
            bid = yes_side if yes_side is not None else Decimal("0")
            ask = (Decimal("1") - no_side) if no_side is not None else Decimal("1")
        else:
            bid = no_side if no_side is not None else Decimal("0")
            ask = (Decimal("1") - yes_side) if yes_side is not None else Decimal("1")
        if bid > ask:
            bid = ask
        try:
            return Quote(outcome_id=outcome_id, bid=bid, ask=ask)
        except ValueError:
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
