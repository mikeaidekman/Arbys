"""Kalshi spread discovery.

A spread event mirrors the totals event for the same game — same
``<yyMONdd[hhmm]><CODES>`` stem, different series — and lists **one market per
(team, line)**: ``KXNFLSPREAD-26SEP09NESEA-SEA4`` is "Seattle wins by over 3.5
points", with the line in ``floor_strike`` (3.5) and the team in the ticker
suffix. The integer in the suffix was ``floor_strike + 0.5`` on all 1,480
markets observed on 2026-09-06; it is not used.

Every market becomes a ``VenueGame`` with ``market_type="spread"``, a positive
``line`` and ``anchor`` set to the team that must win by more than it. YES is
the anchor covering, so ``outcome_ids[anchor]`` is ``"<ticker>:YES"`` and the
other team's key is ``":NO"``. ``fetch_polymarket_us_spreads`` produces the
same canonical form, which is what lets the matcher pair the two venues on
``(anchor, line)`` alone — see the spec for the price evidence.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation

import httpx

from .horizon import discovery_horizon_days, within_horizon
from .kalshi_sports import (
    _REQUEST_SPACING_S,
    KALSHI_BASE,
    VenueGame,
    _get_with_retry,
    _parse_ticker_date,
    parse_ticker_start,
)
from .kalshi_totals import _TICKER_RE, split_team_codes
from .teams import TeamResolver

log = logging.getLogger(__name__)

# Kalshi spread series per sport.
SPREADS_SERIES = {
    "mlb": "KXMLBSPREAD",
    "nfl": "KXNFLSPREAD",
    "ncaaf": "KXNCAAFSPREAD",
    # Both exist and returned zero open events on 2026-09-06 (off-season), so
    # they are registered here but neither sport is in SPREADS_SPORTS.
    "nba": "KXNBASPREAD",
    "wnba": "KXWNBASPREAD",
}

# The only strike type whose YES reads "anchor wins by MORE than the line".
# Any other would invert the proposition, so it is refused rather than guessed.
_EXPECTED_STRIKE_TYPE = "greater"


def anchor_code_from_ticker(
    market_ticker: str, event_ticker: str, codes: tuple[str, str]
) -> str | None:
    """Which of the event's two (raw Kalshi) codes a spread market is for.

    The suffix is ``<CODE><N>`` and codes vary in width, so rather than a
    regex the suffix is tested against each known code. Exactly one must fit,
    which also enforces that the anchor is one of the event's own teams.
    Returns the code as Kalshi wrote it (``"AZ"``); the caller resolves it.
    """
    prefix = f"{event_ticker}-"
    if not market_ticker.startswith(prefix):
        return None
    rest = market_ticker[len(prefix):]
    hits = [c for c in codes if rest.startswith(c) and rest[len(c):].isdigit()]
    return hits[0] if len(hits) == 1 else None


async def fetch_kalshi_spreads(
    *,
    resolver: TeamResolver,
    sport: str,
    series_ticker: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 100,
    horizon_days: int | None = None,
) -> list[VenueGame]:
    """One VenueGame per (game, anchor, line).

    ``horizon_days`` bounds how far ahead a game may be; ``None`` reads
    ``ARBYS_DISCOVERY_HORIZON_DAYS``. Applied before the per-event market call.
    """
    series = series_ticker or SPREADS_SERIES.get(sport)
    if series is None:
        raise ValueError(f"no Kalshi spreads series known for sport {sport!r}")
    days = discovery_horizon_days() if horizon_days is None else horizon_days
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
            games.extend(
                await _parse_spread_event(client, ev, resolver, sport=sport, horizon_days=days)
            )
            await asyncio.sleep(_REQUEST_SPACING_S)
        return games
    finally:
        if owns_client:
            await client.aclose()


async def _parse_spread_event(
    client: httpx.AsyncClient,
    event: dict,
    resolver: TeamResolver,
    *,
    sport: str,
    horizon_days: int,
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
        log.debug("kalshi spreads: unsplittable codes %r in %s", codes, ticker)
        return []
    team_a, team_b = resolver.by_code(pair[0]), resolver.by_code(pair[1])
    if team_a is None or team_b is None:
        return []

    # The market call is what costs a request; a game past the horizon is
    # skipped before it is made.
    if not within_horizon(game_date, days=horizon_days):
        return []

    resp = await _get_with_retry(client, "/markets", {"event_ticker": ticker, "limit": 60})
    if resp.status_code != 200:
        return []

    out: list[VenueGame] = []
    for mk in resp.json().get("markets", []):
        mkt_ticker = mk.get("ticker")
        if not mkt_ticker:
            continue
        raw_anchor = anchor_code_from_ticker(mkt_ticker, ticker, pair)
        if raw_anchor is None:
            log.debug("kalshi spreads: suffix names neither team in %s", mkt_ticker)
            continue
        strike_type = mk.get("strike_type")
        if strike_type != _EXPECTED_STRIKE_TYPE:
            log.warning(
                "kalshi spreads: %s has strike_type %r, not %r; skipped",
                mkt_ticker, strike_type, _EXPECTED_STRIKE_TYPE,
            )
            continue
        strike = mk.get("floor_strike")
        if strike is None:
            continue
        try:
            line = Decimal(str(strike))
        except (InvalidOperation, ValueError):
            continue
        if line <= 0:
            continue
        anchor, other = (team_a, team_b) if raw_anchor == pair[0] else (team_b, team_a)
        out.append(
            VenueGame(
                sport=sport,
                venue_id="kalshi",
                game_date=game_date,
                teams=(team_a, team_b),
                outcome_ids={
                    anchor.code: f"{mkt_ticker}:YES",
                    other.code: f"{mkt_ticker}:NO",
                },
                ref=mkt_ticker,
                market_type="spread",
                line=line,
                anchor=anchor.code,
                start_time=parse_ticker_start(ticker),
            )
        )
    return out
