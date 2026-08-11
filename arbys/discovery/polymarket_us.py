"""Polymarket US discovery.

``GET /v2/leagues/{slug}/events`` returns every market type for a league in
one call, so team sports, totals and tennis all come from the same request and
differ only in which ``sportsMarketType`` values they keep.

Two traps that the international integration had do not exist here:

* **No 100-row cap.** International's flat ``/markets`` capped at 100 rows
  ordered by 24h volume, where league games never outranked politics. This
  endpoint is league-scoped, so there is nothing to outrank.
* **No question parsing.** International only identified teams in prose.
  Polymarket US returns ``teams[].name`` ("Arizona Diamondbacks") structured,
  which the existing resolvers accept directly.

``startTime`` is a clean UTC instant, so no date heuristics are needed on this
side at all - the matcher's start-time comparison works directly.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from .kalshi_sports import VenueGame, _parse_utc
from .matcher import OVER, UNDER
from .players import Player, last_name_code
from .teams import TeamResolver

GATEWAY_BASE = "https://gateway.polymarket.us"

log = logging.getLogger(__name__)

# Our sport label -> Polymarket US league slug.
LEAGUE_SLUGS = {
    "mlb": "mlb",
    "nfl": "nfl",
    "nba": "nba",
}

TENNIS_LEAGUES = ("atp", "wta")

# NBA is unverified: /v2/leagues/nba/events returned zero events on
# 2026-08-11 (offseason), the same condition that leaves KXNBAGAME unverified
# on the Kalshi side. The basketball type string below was confirmed against
# WNBA, which had open events.
MONEYLINE_TYPES = frozenset(
    {
        "baseball_team_full_game_winner",
        "football_team_full_game_winner",
        "basketball_team_full_game_winner",
    }
)
TENNIS_WINNER_TYPES = frozenset({"tennis_match_winner"})

# NFL only in Phase 1. Polymarket US also carries MLB and NBA totals; those
# are held back so the venue port has exactly one behavioural variable.
TOTAL_TYPES = frozenset({"football_team_full_game_total"})


async def _fetch_events(
    league: str, http_client: httpx.AsyncClient | None, limit: int
) -> list[dict[str, Any]]:
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.get(
            f"{GATEWAY_BASE}/v2/leagues/{league}/events", params={"limit": limit}
        )
        resp.raise_for_status()
        events = resp.json().get("events") or []
        return [e for e in events if isinstance(e, dict)]
    finally:
        if owns_client:
            await client.aclose()


def _sides(market: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The (long, short) pair, or None when the market is not binary."""
    sides = market.get("marketSides") or []
    longs = [s for s in sides if isinstance(s, dict) and s.get("long")]
    shorts = [s for s in sides if isinstance(s, dict) and not s.get("long")]
    if len(longs) != 1 or len(shorts) != 1:
        return None
    return longs[0], shorts[0]


def _line(market: dict[str, Any]) -> Decimal | None:
    """The strike, as Decimal. The API sends a JSON float (21.5)."""
    raw = market.get("line")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError):
        return None


def _team_name(side: dict[str, Any]) -> str | None:
    team = side.get("team")
    if not isinstance(team, dict):
        return None
    name = team.get("name")
    return name if isinstance(name, str) else None


async def fetch_polymarket_us_games(
    *,
    resolver: TeamResolver,
    sport: str,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 200,
) -> list[VenueGame]:
    """Moneyline games for a team sport."""
    league = LEAGUE_SLUGS.get(sport, sport)
    events = await _fetch_events(league, http_client, limit)

    games: list[VenueGame] = []
    for event in events:
        start_time = _parse_utc(event.get("startTime"))
        if start_time is None:
            continue
        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            if market.get("sportsMarketType") not in MONEYLINE_TYPES:
                continue
            pair = _sides(market)
            slug = market.get("slug")
            if pair is None or not slug:
                continue
            long_side, short_side = pair

            long_name, short_name = _team_name(long_side), _team_name(short_side)
            if long_name is None or short_name is None:
                continue
            team_long = resolver.by_polymarket_name(long_name)
            team_short = resolver.by_polymarket_name(short_name)
            if team_long is None or team_short is None:
                continue

            games.append(
                VenueGame(
                    sport=sport,
                    venue_id="polymarket_us",
                    game_date=start_time.date(),
                    teams=(team_long, team_short),
                    outcome_ids={
                        team_long.code: f"{slug}:LONG",
                        team_short.code: f"{slug}:SHORT",
                    },
                    ref=str(slug),
                    market_type="moneyline",
                    start_time=start_time,
                )
            )
    return games


async def fetch_polymarket_us_totals(
    *,
    resolver: TeamResolver,
    sport: str,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 200,
) -> list[VenueGame]:
    """Over/under games, one VenueGame per (game, line).

    Totals sides carry ``team: null`` and are labelled Over / Under, so the
    participants come from the event's ``teams`` array rather than from the
    market. The canonical TRUE side is OVER, which is the long side.
    """
    league = LEAGUE_SLUGS.get(sport, sport)
    events = await _fetch_events(league, http_client, limit)

    games: list[VenueGame] = []
    for event in events:
        start_time = _parse_utc(event.get("startTime"))
        if start_time is None:
            continue
        event_teams = [t for t in (event.get("teams") or []) if isinstance(t, dict)]
        if len(event_teams) != 2:
            continue
        resolved = [resolver.by_polymarket_name(t.get("name") or "") for t in event_teams]
        if any(t is None for t in resolved):
            continue
        team_a, team_b = resolved[0], resolved[1]
        assert team_a is not None and team_b is not None  # narrowed above

        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            if market.get("sportsMarketType") not in TOTAL_TYPES:
                continue
            slug = market.get("slug")
            line = _line(market)
            if not slug or line is None or _sides(market) is None:
                continue
            games.append(
                VenueGame(
                    sport=sport,
                    venue_id="polymarket_us",
                    game_date=start_time.date(),
                    teams=(team_a, team_b),
                    outcome_ids={
                        OVER: f"{slug}:LONG",
                        UNDER: f"{slug}:SHORT",
                    },
                    ref=str(slug),
                    market_type="total",
                    line=line,
                    start_time=start_time,
                )
            )
    return games


async def fetch_polymarket_us_tennis(
    *,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 200,
) -> list[VenueGame]:
    """ATP + WTA match winners.

    Players come from the market's own sides, which carry full names, so
    there is no title to split.
    """
    matches: list[VenueGame] = []
    for league in TENNIS_LEAGUES:
        events = await _fetch_events(league, http_client, limit)
        for event in events:
            start_time = _parse_utc(event.get("startTime"))
            if start_time is None:
                continue
            for market in event.get("markets") or []:
                if not isinstance(market, dict):
                    continue
                if market.get("sportsMarketType") not in TENNIS_WINNER_TYPES:
                    continue
                pair = _sides(market)
                slug = market.get("slug")
                if pair is None or not slug:
                    continue
                long_side, short_side = pair
                long_name, short_name = _team_name(long_side), _team_name(short_side)
                if long_name is None or short_name is None:
                    continue
                p_long = Player(code=last_name_code(long_name), full_name=long_name)
                p_short = Player(code=last_name_code(short_name), full_name=short_name)
                if not p_long.code or not p_short.code or p_long.code == p_short.code:
                    continue

                matches.append(
                    VenueGame(
                        sport="tennis",
                        venue_id="polymarket_us",
                        game_date=start_time.date(),
                        teams=(p_long, p_short),
                        outcome_ids={
                            p_long.code: f"{slug}:LONG",
                            p_short.code: f"{slug}:SHORT",
                        },
                        ref=str(slug),
                        market_type="moneyline",
                        start_time=start_time,
                    )
                )
    return matches
