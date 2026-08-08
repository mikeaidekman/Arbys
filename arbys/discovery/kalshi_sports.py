"""Kalshi sports discovery — fetch and parse game events into VenueGame objects."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from typing import Protocol

import httpx

from .teams import Team, TeamResolver

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

log = logging.getLogger(__name__)

_REQUEST_SPACING_S = 0.15  # ~6 req/s; Kalshi public tier tolerates this
_MAX_429_RETRIES = 4


class Participant(Protocol):
    code: str
    full_name: str


@dataclass(frozen=True)
class VenueGame:
    """A single game/match as seen on one venue.

    ``participants`` is a 2-tuple; each item exposes ``.code`` (canonical short
    ID used for cross-venue matching) and ``.full_name``. For team sports the
    tuple contains ``Team`` instances; for tennis it contains ``Player``
    instances. We record the pair without asserting home/away so the matcher
    can rely on the unordered set of codes.

    ``outcome_ids[participant.code]`` is the venue-specific ID to reference
    that side's YES-side market on that venue.
    """

    sport: str
    venue_id: str
    game_date: date
    teams: tuple[Participant, Participant]
    outcome_ids: dict[str, str]
    ref: str  # venue-specific identifier (event ticker, market slug, etc.)


# Kalshi series ticker per team sport. All share the same event/market shape:
# "<SERIES>-<yyMONdd[hhmm]><CODES>" with one market per side whose
# ``yes_sub_title`` is the team's city (or "City X" where a city has two teams).
SERIES_TICKERS = {
    "mlb": "KXMLBGAME",
    "nfl": "KXNFLGAME",
    "nba": "KXNBAGAME",
}


async def fetch_kalshi_team_games(
    *,
    resolver: TeamResolver,
    sport: str,
    series_ticker: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 100,
) -> list[VenueGame]:
    """Fetch open game events for a team sport and return a VenueGame per game."""
    series = series_ticker or SERIES_TICKERS.get(sport)
    if series is None:
        raise ValueError(f"no Kalshi series ticker known for sport {sport!r}")
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=15.0, base_url=KALSHI_BASE)
    try:
        events_resp = await _get_with_retry(
            client, "/events", {"series_ticker": series, "status": "open", "limit": limit}
        )
        events_resp.raise_for_status()
        events = events_resp.json().get("events", [])

        games: list[VenueGame] = []
        for ev in events:
            game = await _parse_kalshi_event(client, ev, resolver, sport=sport)
            if game is not None:
                games.append(game)
            await asyncio.sleep(_REQUEST_SPACING_S)
        return games
    finally:
        if owns_client:
            await client.aclose()


async def fetch_kalshi_mlb_games(
    *,
    resolver: TeamResolver,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 100,
) -> list[VenueGame]:
    """Fetch open MLB game events from Kalshi and return VenueGame per game."""
    return await fetch_kalshi_team_games(
        resolver=resolver, sport="mlb", http_client=http_client, limit=limit
    )


async def _parse_kalshi_event(
    client: httpx.AsyncClient, event: dict, resolver: TeamResolver, *, sport: str = "mlb"
) -> VenueGame | None:
    event_ticker = event.get("event_ticker") or ""
    if not event_ticker:
        return None

    game_date = _parse_ticker_date(event_ticker)
    if game_date is None:
        return None

    markets_resp = await _get_with_retry(
        client, "/markets", {"event_ticker": event_ticker, "limit": 20}
    )
    markets_resp.raise_for_status()
    markets = markets_resp.json().get("markets", [])
    if not markets:
        return None

    outcome_ids: dict[str, str] = {}
    teams_found: dict[str, Team] = {}
    for m in markets:
        ticker = m.get("ticker")
        yes_sub_title = m.get("yes_sub_title") or ""
        if not ticker or not yes_sub_title:
            continue
        team = resolver.by_kalshi_title(yes_sub_title)
        if team is None:
            continue
        outcome_ids[team.code] = f"{ticker}:YES"
        teams_found[team.code] = team

    if len(teams_found) != 2:
        return None

    team_list = tuple(teams_found.values())
    return VenueGame(
        sport=sport,
        venue_id="kalshi",
        game_date=game_date,
        teams=(team_list[0], team_list[1]),
        outcome_ids=outcome_ids,
        ref=event_ticker,
    )


_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


async def _get_with_retry(
    client: httpx.AsyncClient, path: str, params: dict
) -> httpx.Response:
    """GET with exponential backoff on 429 responses."""
    delay = 0.5
    for attempt in range(_MAX_429_RETRIES + 1):
        resp = await client.get(path, params=params)
        if resp.status_code != 429 or attempt == _MAX_429_RETRIES:
            return resp
        retry_after = resp.headers.get("Retry-After")
        wait = float(retry_after) if retry_after else delay
        log.info("kalshi 429; retry in %.2fs (attempt %d)", wait, attempt + 1)
        await asyncio.sleep(wait)
        delay = min(delay * 2, 8.0)
    return resp


def _parse_ticker_date(event_ticker: str) -> date | None:
    """Extract the game date from tickers like ``KXMLBGAME-26AUG051420LADCHC``."""
    try:
        _, tail = event_ticker.split("-", 1)
    except ValueError:
        return None
    if len(tail) < 7:
        return None
    yy = tail[0:2]
    mon = tail[2:5]
    dd = tail[5:7]
    if mon not in _MONTHS:
        return None
    try:
        year = 2000 + int(yy)
        day = int(dd)
    except ValueError:
        return None
    try:
        return date(year, _MONTHS[mon], day)
    except ValueError:
        return None
