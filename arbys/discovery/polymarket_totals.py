"""Polymarket over/under (total) discovery.

Polymarket labels these with ``sportsMarketType == "totals"`` and encodes the
line in the slug: ``nfl-dal-sea-2026-08-16-total-37pt5`` -> 37.5. Outcomes are
``["Over", "Under"]``, so each market is already the binary shape the engine
wants — no model change, just a line to parse and match on.

Team codes in the slug are Polymarket's own abbreviations, which happen to
agree with Kalshi's for the leagues wired so far; they are resolved through
the same ``TeamResolver`` rather than trusted.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from .kalshi_sports import VenueGame, _parse_utc
from .matcher import OVER, UNDER
from .polymarket_sports import POLY_EVENTS_URL, SPORT_TAG_SLUGS
from .teams import TeamResolver

log = logging.getLogger(__name__)

# "nfl-dal-sea-2026-08-16-total-37pt5" (also plain "-total-44" for whole lines)
_SLUG_RE = re.compile(
    r"^(?P<league>[a-z]+)-(?P<a>[a-z]+)-(?P<b>[a-z]+)-"
    r"(?P<date>\d{4}-\d{2}-\d{2})-total-(?P<whole>\d+)(?:pt(?P<frac>\d+))?$"
)


def parse_total_slug(slug: str) -> tuple[str, str, str, Decimal] | None:
    """``(code_a, code_b, iso_date, line)`` from a totals slug, or None."""
    m = _SLUG_RE.match(slug or "")
    if not m:
        return None
    frac = m.group("frac") or "0"
    try:
        line = Decimal(f"{m.group('whole')}.{frac}")
    except (InvalidOperation, ValueError):
        return None
    return m.group("a").upper(), m.group("b").upper(), m.group("date"), line


async def fetch_polymarket_totals(
    *,
    resolver: TeamResolver,
    sport: str,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 500,
    tag_slug: str | None = None,
) -> list[VenueGame]:
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=15.0)
    slug = tag_slug or SPORT_TAG_SLUGS.get(sport, sport)
    try:
        resp = await client.get(
            POLY_EVENTS_URL,
            params={
                "closed": "false",
                "active": "true",
                "tag_slug": slug,
                "limit": limit,
                "order": "startDate",
                "ascending": "false",
            },
        )
        resp.raise_for_status()
        events = resp.json() or []
    except (httpx.HTTPError, json.JSONDecodeError):
        return []
    finally:
        if owns_client:
            await client.aclose()

    games: list[VenueGame] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            game = _parse_totals_market(market, resolver, sport)
            if game is not None:
                games.append(game)
    return games


def _parse_totals_market(
    market: dict[str, Any], resolver: TeamResolver, sport: str
) -> VenueGame | None:
    if (market.get("sportsMarketType") or "") != "totals":
        return None
    parsed = parse_total_slug(market.get("slug") or "")
    if parsed is None:
        return None
    code_a, code_b, date_str, line = parsed

    team_a, team_b = resolver.by_code(code_a), resolver.by_code(code_b)
    if team_a is None or team_b is None:
        return None

    token_ids = market.get("clobTokenIds") or []
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except json.JSONDecodeError:
            token_ids = []
    labels = market.get("outcomes") or []
    if isinstance(labels, str):
        try:
            labels = json.loads(labels)
        except json.JSONDecodeError:
            labels = []
    if len(token_ids) != 2 or len(labels) != 2:
        return None

    outcome_ids: dict[str, str] = {}
    for label, tid in zip(labels, token_ids, strict=False):
        key = str(label).strip().upper()
        if key in (OVER, UNDER):
            outcome_ids[key] = str(tid)
    if set(outcome_ids) != {OVER, UNDER}:
        return None

    start = _parse_utc(market.get("gameStartTime"))
    try:
        y, mo, d = (int(x) for x in date_str.split("-"))
        from datetime import date as _date

        game_date = start.date() if start is not None else _date(y, mo, d)
    except (ValueError, AttributeError):
        return None

    return VenueGame(
        sport=sport,
        venue_id="polymarket",
        game_date=game_date,
        teams=(team_a, team_b),
        outcome_ids=outcome_ids,
        ref=str(market.get("slug") or market.get("id") or ""),
        market_type="total",
        line=line,
        start_time=start,
    )
