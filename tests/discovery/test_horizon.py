from datetime import date

import httpx

from arbys.discovery.horizon import (
    DEFAULT_HORIZON_DAYS,
    discovery_horizon_days,
    filter_horizon,
    within_horizon,
)
from arbys.discovery.kalshi_sports import VenueGame, fetch_kalshi_team_games
from arbys.discovery.kalshi_totals import fetch_kalshi_totals
from arbys.discovery.teams import NFL_RESOLVER

TODAY = date(2026, 9, 6)


def _game(day: date, venue: str = "kalshi") -> VenueGame:
    a = NFL_RESOLVER.by_code("NE")
    b = NFL_RESOLVER.by_code("SEA")
    assert a is not None and b is not None
    return VenueGame(
        sport="nfl",
        venue_id=venue,
        game_date=day,
        teams=(a, b),
        outcome_ids={"NE": f"{venue}-{day}-ne", "SEA": f"{venue}-{day}-sea"},
        ref=f"{venue}-{day}",
    )


def test_last_day_of_the_window_is_inside_and_the_next_is_out():
    assert within_horizon(date(2026, 9, 9), days=3, today=TODAY)
    assert not within_horizon(date(2026, 9, 10), days=3, today=TODAY)


def test_today_and_the_past_are_inside():
    """The horizon bounds the future. Finished games are removed by
    retirement, not by this rule."""
    assert within_horizon(TODAY, days=3, today=TODAY)
    assert within_horizon(date(2026, 9, 1), days=3, today=TODAY)


def test_zero_disables():
    assert within_horizon(date(2030, 1, 1), days=0, today=TODAY)


def test_filter_keeps_near_games_and_drops_far_ones():
    near, far = _game(date(2026, 9, 8)), _game(date(2026, 9, 13))
    assert filter_horizon([near, far], days=3, today=TODAY) == [near]
    assert filter_horizon([near, far], days=0, today=TODAY) == [near, far]


def test_config_default_and_parsing(monkeypatch):
    monkeypatch.delenv("ARBYS_DISCOVERY_HORIZON_DAYS", raising=False)
    assert discovery_horizon_days() == DEFAULT_HORIZON_DAYS == 3
    monkeypatch.setenv("ARBYS_DISCOVERY_HORIZON_DAYS", "7")
    assert discovery_horizon_days() == 7
    monkeypatch.setenv("ARBYS_DISCOVERY_HORIZON_DAYS", "0")
    assert discovery_horizon_days() == 0
    monkeypatch.setenv("ARBYS_DISCOVERY_HORIZON_DAYS", "-2")
    assert discovery_horizon_days() == 0
    monkeypatch.setenv("ARBYS_DISCOVERY_HORIZON_DAYS", "soon")
    assert discovery_horizon_days() == 3


# --- Kalshi fetchers skip the market call past the horizon ---------------------


def _kalshi_client(events, markets, calls: list[str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=events)
        return httpx.Response(200, json=markets)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=5.0,
        base_url="https://api.elections.kalshi.com/trade-api/v2",
    )


NFL_MARKETS = {
    "markets": [
        {"ticker": "KXNFLGAME-30SEP09NESEA-NE", "yes_sub_title": "New England"},
        {"ticker": "KXNFLGAME-30SEP09NESEA-SEA", "yes_sub_title": "Seattle"},
    ]
}


async def test_kalshi_team_games_skip_the_market_call_past_the_horizon():
    """The per-event /markets request is the cost; a 2030 game must not incur
    it. With the horizon off the same event is fetched and parsed."""
    events = {"events": [{"event_ticker": "KXNFLGAME-30SEP09NESEA"}]}
    calls: list[str] = []
    client = _kalshi_client(events, NFL_MARKETS, calls)

    games = await fetch_kalshi_team_games(
        resolver=NFL_RESOLVER, sport="nfl", http_client=client, horizon_days=3
    )
    assert games == []
    assert not any(p.endswith("/markets") for p in calls)

    calls.clear()
    games = await fetch_kalshi_team_games(
        resolver=NFL_RESOLVER, sport="nfl", http_client=client, horizon_days=0
    )
    await client.aclose()
    assert len(games) == 1 and games[0].game_date == date(2030, 9, 9)
    assert any(p.endswith("/markets") for p in calls)


async def test_kalshi_totals_skip_the_market_call_past_the_horizon():
    events = {"events": [{"event_ticker": "KXNFLTOTAL-30SEP09NESEA"}]}
    markets = {"markets": [{"ticker": "KXNFLTOTAL-30SEP09NESEA-45", "floor_strike": 44.5}]}
    calls: list[str] = []
    client = _kalshi_client(events, markets, calls)

    games = await fetch_kalshi_totals(
        resolver=NFL_RESOLVER, sport="nfl", http_client=client, horizon_days=3
    )
    assert games == []
    assert not any(p.endswith("/markets") for p in calls)

    calls.clear()
    games = await fetch_kalshi_totals(
        resolver=NFL_RESOLVER, sport="nfl", http_client=client, horizon_days=0
    )
    await client.aclose()
    assert len(games) == 1
    assert any(p.endswith("/markets") for p in calls)


async def test_kalshi_fetchers_read_the_config_when_no_horizon_is_given(monkeypatch):
    events = {"events": [{"event_ticker": "KXNFLGAME-30SEP09NESEA"}]}
    calls: list[str] = []
    client = _kalshi_client(events, NFL_MARKETS, calls)
    monkeypatch.setenv("ARBYS_DISCOVERY_HORIZON_DAYS", "3")
    assert await fetch_kalshi_team_games(resolver=NFL_RESOLVER, sport="nfl", http_client=client) == []
    monkeypatch.setenv("ARBYS_DISCOVERY_HORIZON_DAYS", "0")
    assert len(await fetch_kalshi_team_games(resolver=NFL_RESOLVER, sport="nfl", http_client=client)) == 1
    await client.aclose()
