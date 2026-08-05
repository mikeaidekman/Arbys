"""Polymarket market-data adapter.

Uses Polymarket's Gamma markets API to discover markets and the CLOB REST
`/prices` endpoint to poll top-of-book. WS streaming is a follow-up (see
plan.md); polling is sufficient for the first working version and keeps
credential requirements to zero.

The adapter deliberately does *not* attempt to be exhaustive — it fetches only
active binary markets, since those are what the arb engine currently supports.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx

from ..shared.types import Outcome, Quote, Side
from .base import MarketDataAdapter

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_PRICE_URL = "https://clob.polymarket.com/price"


class PolymarketAdapter(MarketDataAdapter):
    venue_id = "polymarket"

    def __init__(
        self,
        *,
        poll_interval_s: float = 5.0,
        outcome_ids: list[str] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._poll_interval_s = poll_interval_s
        self._outcome_ids = outcome_ids or []
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=10.0)

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
                # Gamma sometimes returns this as a stringified JSON array.
                import json
                try:
                    token_ids = json.loads(token_ids)
                except json.JSONDecodeError:
                    token_ids = []
            outcome_labels = m.get("outcomes") or ["Yes", "No"]
            if isinstance(outcome_labels, str):
                import json
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

    async def stream_quotes(self) -> AsyncIterator[Quote]:
        if not self._outcome_ids:
            return
        while True:
            for tid in self._outcome_ids:
                q = await self._fetch_quote(tid)
                if q is not None:
                    yield q
            await asyncio.sleep(self._poll_interval_s)
