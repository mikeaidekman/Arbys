"""Polymarket US market-data adapter.

Polymarket US is a separate CFTC-regulated exchange from Polymarket
international, with its own order book. Shares are not fungible between them
and prices diverge, so this is a distinct venue, not a different endpoint for
the same one.

Uses the public gateway (``gateway.polymarket.us``), which needs no API key,
no KYC and no wallet. The authenticated WebSocket at ``api.polymarket.us``
requires Ed25519 credentials and identity verification; it is deliberately not
used here. When it is added, follow the ``_kalshi_factory`` pattern in
``backend/state.py``.

A Polymarket US market is a single binary contract with a long and a short
side - structurally like a Kalshi market rather than like Polymarket
international's two-token pair. So ``outcome_id`` follows the Kalshi
convention: ``{market_slug}:LONG`` / ``{market_slug}:SHORT``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ..shared.types import Outcome, Quote, Side
from .base import MarketDataAdapter

GATEWAY_BASE = "https://gateway.polymarket.us"

LONG = "LONG"
SHORT = "SHORT"

log = logging.getLogger(__name__)


def split_outcome_id(outcome_id: str) -> tuple[str, str]:
    """``"slug:LONG"`` -> ``("slug", "LONG")``.

    Slugs contain hyphens but never colons, so rpartition is unambiguous.
    """
    slug, _, side = outcome_id.rpartition(":")
    return slug, side.upper()


def _price(node: Any) -> Decimal | None:
    """Pull a Decimal out of a ``{"value": "0.4550", "currency": "USD"}`` node."""
    if not isinstance(node, dict):
        return None
    try:
        return Decimal(str(node["value"]))
    except (KeyError, TypeError, InvalidOperation):
        return None


def _size(value: Any) -> Decimal:
    """Resting depth at top of book. Unknown or malformed reads as 0."""
    try:
        return Decimal(str(value))
    except (TypeError, InvalidOperation):
        return Decimal("0")


def _pair_to_quotes(
    slug: str,
    bid: Decimal | None,
    ask: Decimal | None,
    bid_size: Decimal | None,
    ask_size: Decimal | None,
) -> list[Quote]:
    """Build the LONG/SHORT pair from one market's top of book.

    The long side is the book as reported. The short side of a binary is its
    complement: buying SHORT at its ask means lifting the LONG bid, so prices
    invert *and* the sizes swap along with them::

        short.bid  = 1 - long.ask      short.bid_size = long.ask_size
        short.ask  = 1 - long.bid      short.ask_size = long.bid_size

    A **one-sided** book keeps its live side and synthesises the missing one
    at the same price with **size 0**. Dropping the market instead would hide
    a genuinely tradeable ask - live example, 419,882 contracts offered at
    0.0050 with no bids at all.

    Size ``0`` there is load-bearing rather than cosmetic, and distinct from
    ``None``: ``0`` asserts nothing is resting, which is what lets
    ``paper_broker`` refuse the fill; ``None`` merely means the venue did not
    say, and must still be fillable.

    Returns an empty list rather than raising when the book is empty,
    malformed, or crossed - a bad tick must not kill the feed.
    """
    if bid is None and ask is None:
        return []
    # The missing side is *known* empty - the venue reported nothing resting
    # there - which is different from the present side, whose size may still
    # be unknown.
    if bid is None:
        bid, bid_size = ask, Decimal("0")
    if ask is None:
        ask, ask_size = bid, Decimal("0")
    if bid is None or ask is None:  # unreachable; keeps the type checker honest
        return []

    one = Decimal("1")
    try:
        return [
            Quote(
                outcome_id=f"{slug}:{LONG}",
                bid=bid,
                ask=ask,
                bid_size=bid_size,
                ask_size=ask_size,
            ),
            Quote(
                outcome_id=f"{slug}:{SHORT}",
                bid=one - ask,
                ask=one - bid,
                bid_size=ask_size,
                ask_size=bid_size,
            ),
        ]
    except ValueError:
        # Quote.__post_init__ rejects out-of-range or crossed books.
        log.debug("dropping malformed book for %s: bid=%s ask=%s", slug, bid, ask)
        return []


def quotes_from_bbo(slug: str, market_data: dict[str, Any]) -> list[Quote]:
    """Top of book from a ``/bbo`` payload.

    **Sizes are reported as ``None``, meaning unknown.** ``bidDepth``/``askDepth``
    count price *levels*, not contracts: measured on
    ``aec-mlb-tb-ath-2026-08-12``, ``bidDepth`` was 49 while ``/book`` showed
    49 levels whose best held 287,926.98 contracts. This endpoint cannot
    report size at all, and a level count that reads as a size is worse than
    an explicit unknown. The WebSocket path carries real quantities.
    """
    return _pair_to_quotes(
        slug,
        _price(market_data.get("bestBid")),
        _price(market_data.get("bestAsk")),
        None,
        None,
    )


def quotes_from_levels(
    slug: str, bids: list[dict[str, Any]], offers: list[dict[str, Any]]
) -> list[Quote]:
    """Top of book from a full ladder - ``[{"px": {"value": ...}, "qty": ...}]``.

    Only level 0 is read. Carrying the whole ladder is the cost of the full
    market-data subscription, and the ``qty`` on level 0 is what that cost
    buys - the lite subscription reports only depth counters.
    """

    def top(levels: list[dict[str, Any]]) -> tuple[Decimal | None, Decimal | None]:
        if not levels or not isinstance(levels[0], dict):
            return None, None
        return _price(levels[0].get("px")), _size(levels[0].get("qty"))

    bid, bid_size = top(bids)
    ask, ask_size = top(offers)
    return _pair_to_quotes(slug, bid, ask, bid_size, ask_size)


class PolymarketUsAdapter(MarketDataAdapter):
    venue_id = "polymarket_us"

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

    def _slugs(self) -> list[str]:
        """Distinct market slugs behind the subscribed outcome ids.

        LONG and SHORT share one market and therefore one HTTP call. Sorted
        so the poll order is deterministic.
        """
        return sorted({split_outcome_id(oid)[0] for oid in self._outcome_ids})

    async def list_markets(self, *, league: str = "mlb", limit: int = 200) -> list[Outcome]:
        resp = await self._http.get(
            f"{GATEWAY_BASE}/v2/leagues/{league}/events", params={"limit": limit}
        )
        resp.raise_for_status()
        outcomes: list[Outcome] = []
        for event in resp.json().get("events") or []:
            if not isinstance(event, dict):
                continue
            for market in event.get("markets") or []:
                if not isinstance(market, dict):
                    continue
                slug = market.get("slug")
                if not slug:
                    continue
                title = market.get("question") or slug
                for side_label, side_enum in ((LONG, Side.YES), (SHORT, Side.NO)):
                    outcomes.append(
                        Outcome(
                            id=f"{slug}:{side_label}",
                            venue_id=self.venue_id,
                            market_id=str(slug),
                            label=f"{title} ({side_label})",
                            side=side_enum,
                        )
                    )
        return outcomes

    async def _fetch_quotes(self, slug: str) -> list[Quote]:
        try:
            resp = await self._http.get(f"{GATEWAY_BASE}/v1/markets/{slug}/bbo")
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError):
            return []
        market_data = body.get("marketData")
        if not isinstance(market_data, dict):
            return []
        return quotes_from_bbo(slug, market_data)

    async def stream_quotes(self) -> AsyncIterator[Quote]:
        slugs = self._slugs()
        if not slugs:
            return
        while True:
            for slug in slugs:
                for quote in await self._fetch_quotes(slug):
                    yield quote
            await asyncio.sleep(self._poll_interval_s)
