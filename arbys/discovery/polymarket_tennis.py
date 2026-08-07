"""Polymarket tennis discovery — fetch tennis markets and parse into VenueGame objects."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import httpx

from .kalshi_sports import VenueGame
from .players import Player, parse_vs_title, strip_prefix

POLY_GAMMA_URL = "https://gamma-api.polymarket.com/markets"
POLY_EVENTS_URL = "https://gamma-api.polymarket.com/events"

# Substrings we look for in slug or question to gate tennis markets.
TENNIS_MARKERS = ("atp-", "wta-", "tennis")


async def fetch_polymarket_tennis_matches(
    *,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 500,
) -> list[VenueGame]:
    """Fetch tennis matches from Polymarket.

    Prefers the ``/events?tag_slug=tennis`` endpoint because it returns
    tennis events regardless of 24h volume rank. The ``/markets`` endpoint
    ordered by ``volume24hr`` used to miss most matches once politics /
    crypto crowded the top-N. Falls back to the flat markets endpoint if
    the events call fails, and unions results by slug.
    """
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=15.0)
    try:
        markets_by_slug: dict[str, dict[str, Any]] = {}

        try:
            events_resp = await client.get(
                POLY_EVENTS_URL,
                params={
                    "closed": "false",
                    "active": "true",
                    "tag_slug": "tennis",
                    "limit": limit,
                    "order": "startDate",
                    "ascending": "false",
                },
            )
            events_resp.raise_for_status()
            for event in events_resp.json() or []:
                for m in event.get("markets") or []:
                    slug = m.get("slug")
                    if slug:
                        markets_by_slug.setdefault(slug, m)
        except (httpx.HTTPError, json.JSONDecodeError):
            pass

        try:
            markets_resp = await client.get(
                POLY_GAMMA_URL,
                params={
                    "closed": "false",
                    "active": "true",
                    "limit": limit,
                    "order": "volume24hr",
                    "ascending": "false",
                },
            )
            markets_resp.raise_for_status()
            for m in markets_resp.json() or []:
                slug = m.get("slug")
                if slug:
                    markets_by_slug.setdefault(slug, m)
        except (httpx.HTTPError, json.JSONDecodeError):
            pass

        games: list[VenueGame] = []
        for m in markets_by_slug.values():
            game = _parse_tennis_market(m)
            if game is not None:
                games.append(game)
        return games
    finally:
        if owns_client:
            await client.aclose()


def _parse_tennis_market(market: dict[str, Any]) -> VenueGame | None:
    slug = (market.get("slug") or "").lower()
    question = market.get("question") or ""
    if not any(m in slug for m in TENNIS_MARKERS) and "tennis" not in question.lower():
        return None

    # Determine sport from slug prefix; default to atp when ambiguous.
    if slug.startswith("wta-") or "wta" in slug:
        sport = "wta"
    elif slug.startswith("atp-") or "atp" in slug:
        sport = "atp"
    else:
        sport = "atp"

    parsed = parse_vs_title(strip_prefix(question))
    if parsed is None:
        return None
    p_a, p_b = parsed

    token_ids = market.get("clobTokenIds") or []
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except json.JSONDecodeError:
            token_ids = []
    outcome_labels = market.get("outcomes") or []
    if isinstance(outcome_labels, str):
        try:
            outcome_labels = json.loads(outcome_labels)
        except json.JSONDecodeError:
            outcome_labels = []
    if len(token_ids) != 2 or len(outcome_labels) != 2:
        return None

    # Match each Polymarket outcome (full name) to one of the parsed players
    # by last-name code.
    outcome_ids: dict[str, str] = {}
    for label, tid in zip(outcome_labels, token_ids, strict=False):
        code = _last_name_code(str(label))
        if code == p_a.code:
            outcome_ids[p_a.code] = str(tid)
        elif code == p_b.code:
            outcome_ids[p_b.code] = str(tid)
    if set(outcome_ids.keys()) != {p_a.code, p_b.code}:
        return None

    game_date = _extract_game_date(market)
    if game_date is None:
        return None

    return VenueGame(
        sport=sport,
        venue_id="polymarket",
        game_date=game_date,
        teams=(p_a, p_b),
        outcome_ids=outcome_ids,
        ref=str(market.get("slug") or market.get("id") or ""),
    )


def _last_name_code(full_name: str) -> str:
    from .players import _last_name_code as impl

    return impl(full_name)


def _extract_game_date(market: dict[str, Any]) -> date | None:
    gst = market.get("gameStartTime")
    if isinstance(gst, str) and gst:
        s = gst.replace(" ", "T").replace("+00", "+00:00")
        try:
            return datetime.fromisoformat(s).date()
        except ValueError:
            pass
    slug = market.get("slug") or ""
    if isinstance(slug, str):
        parts = slug.split("-")
        if len(parts) >= 3:
            tail = "-".join(parts[-3:])
            try:
                return datetime.strptime(tail, "%Y-%m-%d").date()
            except ValueError:
                pass
    return None


# Re-export Player for callers that want to introspect participants
__all__ = ["Player", "fetch_polymarket_tennis_matches"]
