"""Kalshi tennis discovery — fetch ATP/WTA match events into VenueGame objects."""

from __future__ import annotations

import asyncio
import logging
from datetime import date

import httpx

from .kalshi_sports import (
    _REQUEST_SPACING_S,
    KALSHI_BASE,
    VenueGame,
    _get_with_retry,
    _parse_ticker_date,
    parse_ticker_start,
)
from .players import parse_vs_title

log = logging.getLogger(__name__)

# (sport, kalshi series_ticker) pairs to scan.
TENNIS_SERIES: tuple[tuple[str, str], ...] = (
    ("atp", "KXATPMATCH"),
    ("wta", "KXWTAMATCH"),
)


async def fetch_kalshi_tennis_matches(
    *,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 100,
) -> list[VenueGame]:
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=15.0, base_url=KALSHI_BASE)
    try:
        out: list[VenueGame] = []
        for sport, series in TENNIS_SERIES:
            events_resp = await _get_with_retry(
                client, "/events", {"series_ticker": series, "status": "open", "limit": limit}
            )
            if events_resp.status_code != 200:
                log.warning("kalshi %s events fetch failed: %s", series, events_resp.status_code)
                continue
            events = events_resp.json().get("events", [])
            for ev in events:
                game = await _parse_kalshi_tennis_event(client, ev, sport=sport)
                if game is not None:
                    out.append(game)
                await asyncio.sleep(_REQUEST_SPACING_S)
        return out
    finally:
        if owns_client:
            await client.aclose()


async def _parse_kalshi_tennis_event(
    client: httpx.AsyncClient, event: dict, *, sport: str
) -> VenueGame | None:
    event_ticker = event.get("event_ticker") or ""
    title = event.get("title") or ""
    if not event_ticker:
        return None

    game_date = _parse_ticker_date(event_ticker) or _fallback_date_today()

    parsed = parse_vs_title(title)
    if parsed is None:
        return None
    p_a, p_b = parsed

    markets_resp = await _get_with_retry(
        client, "/markets", {"event_ticker": event_ticker, "limit": 20}
    )
    if markets_resp.status_code != 200:
        return None
    markets = markets_resp.json().get("markets", [])
    if not markets:
        return None

    outcome_ids: dict[str, str] = {}
    start_time = parse_ticker_start(event_ticker)
    for m in markets:
        ticker = m.get("ticker")
        yes_sub_title = (m.get("yes_sub_title") or "").strip()
        if not ticker or not yes_sub_title:
            continue
        # Kalshi's yes_sub_title for tennis is just the player's last name.
        code = yes_sub_title.split()[-1].upper()
        if code == p_a.code:
            outcome_ids[p_a.code] = f"{ticker}:YES"
        elif code == p_b.code:
            outcome_ids[p_b.code] = f"{ticker}:YES"

    if len(outcome_ids) != 2:
        return None

    return VenueGame(
        sport=sport,
        venue_id="kalshi",
        game_date=game_date,
        teams=(p_a, p_b),
        outcome_ids=outcome_ids,
        ref=event_ticker,
        start_time=start_time,
    )


def _fallback_date_today() -> date:
    from datetime import UTC, datetime

    return datetime.now(UTC).date()
