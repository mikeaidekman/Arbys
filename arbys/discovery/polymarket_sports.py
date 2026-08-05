"""Polymarket sports discovery — fetch active sports markets and parse into VenueGame objects."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import httpx

from .kalshi_sports import VenueGame
from .teams import TeamResolver

POLY_GAMMA_URL = "https://gamma-api.polymarket.com/markets"


async def fetch_polymarket_sports_games(
    *,
    resolver: TeamResolver,
    sport: str,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 500,
) -> list[VenueGame]:
    """Fetch active Polymarket markets, keep those that parse as a
    two-team ``vs`` game using ``resolver``.

    Only the ``sport`` label is attached to results — the resolver is
    the actual filter (unknown team names are dropped).
    """
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.get(
            POLY_GAMMA_URL,
            params={
                "closed": "false",
                "active": "true",
                "limit": limit,
                "order": "volume24hr",
                "ascending": "false",
            },
        )
        resp.raise_for_status()
        markets = resp.json()
        games: list[VenueGame] = []
        for m in markets:
            game = _parse_market(m, resolver, sport)
            if game is not None:
                games.append(game)
        return games
    finally:
        if owns_client:
            await client.aclose()


def _parse_market(market: dict[str, Any], resolver: TeamResolver, sport: str) -> VenueGame | None:
    question = market.get("question") or ""
    parsed = resolver.parse_vs_question(question)
    if parsed is None:
        return None
    team_a, team_b = parsed

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

    outcome_ids: dict[str, str] = {}
    for label, tid in zip(outcome_labels, token_ids, strict=False):
        team = resolver.by_polymarket_name(label)
        if team is None:
            return None
        outcome_ids[team.code] = str(tid)
    if set(outcome_ids.keys()) != {team_a.code, team_b.code}:
        return None

    game_date = _extract_game_date(market)
    if game_date is None:
        return None

    return VenueGame(
        sport=sport,
        venue_id="polymarket",
        game_date=game_date,
        teams=(team_a, team_b),
        outcome_ids=outcome_ids,
        ref=str(market.get("slug") or market.get("id") or ""),
    )


def _extract_game_date(market: dict[str, Any]) -> date | None:
    """Prefer ``gameStartTime`` (UTC), fall back to a date embedded in ``slug``."""
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
