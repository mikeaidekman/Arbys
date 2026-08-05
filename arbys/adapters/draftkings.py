"""DraftKings odds adapter (READ-ONLY, ISOLATED, FEATURE-FLAGGED).

DraftKings has no public trading API and the odds JSON we consume is intended
for their own frontend. Only enable this adapter for personal research and
respect DraftKings' Terms of Service. If disabled by feature flag, the ingest
worker skips it entirely.

Odds are converted to *raw* implied probabilities (i.e. the vig is NOT
removed) because the arb engine needs to see the actual price you'd trade
against. `SportsbookFeeModel` returns 0 to reflect this — the vig is already
embedded in the price.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import httpx

from ..shared.odds import american_to_implied_prob
from ..shared.types import Outcome, Quote, Side
from .base import MarketDataAdapter

DKS_EVENT_URL_TEMPLATE = (
    "https://sportsbook-nash.draftkings.com/api/sportscontent/dkusva/v1/leagues/{league_id}"
)


def draftkings_enabled() -> bool:
    return os.environ.get("ARBYS_ENABLE_DRAFTKINGS", "0") == "1"


class DraftKingsAdapter(MarketDataAdapter):
    venue_id = "draftkings"

    def __init__(
        self,
        *,
        league_ids: list[str] | None = None,
        poll_interval_s: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._league_ids = league_ids or []
        self._poll_interval_s = poll_interval_s
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": "arbys-research/0.1"},
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def list_markets(self) -> list[Outcome]:
        outcomes: list[Outcome] = []
        for league_id in self._league_ids:
            try:
                resp = await self._http.get(DKS_EVENT_URL_TEMPLATE.format(league_id=league_id))
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPError:
                continue
            for event in data.get("events", []):
                event_id = str(event.get("id"))
                event_name = event.get("name", "")
                for market in event.get("displayGroups", []):
                    for m in market.get("markets", []):
                        mid = str(m.get("id"))
                        for i, sel in enumerate(m.get("outcomes", [])):
                            label = sel.get("label", f"outcome{i}")
                            outcomes.append(
                                Outcome(
                                    id=f"dks:{event_id}:{mid}:{i}",
                                    venue_id=self.venue_id,
                                    market_id=f"{event_id}:{mid}",
                                    label=f"{event_name}: {label}",
                                    side=Side.YES if i == 0 else Side.NO,
                                )
                            )
        return outcomes

    async def stream_quotes(self) -> AsyncIterator[Quote]:
        """Poll each configured league, emitting one Quote per outcome per cycle."""
        if not self._league_ids:
            return
        while True:
            for league_id in self._league_ids:
                try:
                    resp = await self._http.get(
                        DKS_EVENT_URL_TEMPLATE.format(league_id=league_id)
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except httpx.HTTPError:
                    continue
                for event in data.get("events", []):
                    event_id = str(event.get("id"))
                    for market in event.get("displayGroups", []):
                        for m in market.get("markets", []):
                            mid = str(m.get("id"))
                            for i, sel in enumerate(m.get("outcomes", [])):
                                american = sel.get("oddsAmerican")
                                if american is None:
                                    continue
                                try:
                                    prob = american_to_implied_prob(int(american))
                                except ValueError:
                                    continue
                                oid = f"dks:{event_id}:{mid}:{i}"
                                yield Quote(outcome_id=oid, bid=prob, ask=prob)
            await asyncio.sleep(self._poll_interval_s)
