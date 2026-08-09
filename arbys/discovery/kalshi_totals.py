"""Kalshi over/under (total) discovery.

A totals event mirrors the moneyline event for the same game — same
``<yyMONdd[hhmm]><CODES>`` stem, different series — but the market shape
differs in two ways that matter:

* The participants are **not** in ``yes_sub_title``; that field holds the
  strike ("Over 8.5 runs scored"). The team codes are concatenated in the
  event ticker instead, and have to be split back apart.
* Each strike is its own market, and the line is a structured field
  (``floor_strike``), not prose — so lines match exactly across venues
  rather than by string comparison.

Every strike becomes its own ``VenueGame`` with ``market_type="total"``,
because Over 44.5 and Over 47.5 are different bets on the same game.
"""

from __future__ import annotations

import asyncio
import logging
import re
from decimal import Decimal, InvalidOperation

import httpx

from .kalshi_sports import (
    _REQUEST_SPACING_S,
    KALSHI_BASE,
    VenueGame,
    _get_with_retry,
    _parse_ticker_date,
    parse_ticker_start,
)
from .matcher import OVER, UNDER
from .teams import TeamResolver

log = logging.getLogger(__name__)

# Kalshi totals series per sport.
TOTALS_SERIES = {
    "mlb": "KXMLBTOTAL",
    "nfl": "KXNFLTOTAL",
    "nba": "KXNBATOTAL",
}

# "KXNFLTOTAL-26AUG13DETCIN" -> ("26AUG13", "DETCIN"); the time, when present,
# is part of the date chunk ("26AUG091215").
_TICKER_RE = re.compile(r"^[A-Z0-9]+-(\d{2}[A-Z]{3}\d{2}(?:\d{4})?)([A-Z]+)$")


def split_team_codes(codes: str, resolver: TeamResolver) -> tuple[str, str] | None:
    """Split a concatenated code pair like "DETCIN" into ("DET", "CIN").

    Codes vary in length (GB, KC, NYJ, WAS), so try every split and accept the
    first where *both* halves resolve. Ambiguity is possible in principle but
    no real pair in the current tables produces two valid splits.
    """
    for i in range(2, len(codes) - 1):
        a, b = codes[:i], codes[i:]
        if resolver.by_code(a) and resolver.by_code(b):
            return a, b
    return None


async def fetch_kalshi_totals(
    *,
    resolver: TeamResolver,
    sport: str,
    series_ticker: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 100,
) -> list[VenueGame]:
    """One VenueGame per (game, line) with OVER/UNDER outcome ids."""
    series = series_ticker or TOTALS_SERIES.get(sport)
    if series is None:
        raise ValueError(f"no Kalshi totals series known for sport {sport!r}")
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=15.0, base_url=KALSHI_BASE)
    try:
        resp = await _get_with_retry(
            client, "/events", {"series_ticker": series, "status": "open", "limit": limit}
        )
        resp.raise_for_status()
        events = resp.json().get("events", [])

        games: list[VenueGame] = []
        for ev in events:
            games.extend(await _parse_totals_event(client, ev, resolver, sport=sport))
            await asyncio.sleep(_REQUEST_SPACING_S)
        return games
    finally:
        if owns_client:
            await client.aclose()


async def _parse_totals_event(
    client: httpx.AsyncClient, event: dict, resolver: TeamResolver, *, sport: str
) -> list[VenueGame]:
    ticker = event.get("event_ticker") or ""
    m = _TICKER_RE.match(ticker)
    if not m:
        return []
    _datepart, codes = m.groups()

    game_date = _parse_ticker_date(ticker)
    if game_date is None:
        return []

    pair = split_team_codes(codes, resolver)
    if pair is None:
        log.debug("kalshi totals: unsplittable codes %r in %s", codes, ticker)
        return []
    team_a, team_b = resolver.by_code(pair[0]), resolver.by_code(pair[1])
    if team_a is None or team_b is None:
        return []

    resp = await _get_with_retry(client, "/markets", {"event_ticker": ticker, "limit": 60})
    if resp.status_code != 200:
        return []

    out: list[VenueGame] = []
    for mk in resp.json().get("markets", []):
        mkt_ticker = mk.get("ticker")
        strike = mk.get("floor_strike")
        if not mkt_ticker or strike is None:
            continue
        try:
            line = Decimal(str(strike))
        except (InvalidOperation, ValueError):
            continue
        # A Kalshi total is one binary market: YES is over, NO is under.
        out.append(
            VenueGame(
                sport=sport,
                venue_id="kalshi",
                game_date=game_date,
                teams=(team_a, team_b),
                outcome_ids={
                    OVER: f"{mkt_ticker}:YES",
                    UNDER: f"{mkt_ticker}:NO",
                },
                ref=mkt_ticker,
                market_type="total",
                line=line,
                start_time=parse_ticker_start(ticker),
            )
        )
    return out
