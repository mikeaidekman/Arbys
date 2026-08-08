from datetime import date

import httpx
import pytest

from arbys.discovery.polymarket_sports import fetch_polymarket_sports_games
from arbys.discovery.teams import MLB_RESOLVER


@pytest.mark.asyncio
async def test_fetch_polymarket_sports_games_parses_gamestarttime():
    payload = [
        {
            "question": "Los Angeles Dodgers vs. Chicago Cubs",
            "outcomes": ["Los Angeles Dodgers", "Chicago Cubs"],
            "clobTokenIds": ["tok_lad", "tok_chc"],
            "gameStartTime": "2026-08-05 18:20:00+00",
            "slug": "mlb-lad-chc-2026-08-05",
        },
        {
            # Unrelated market — should be dropped.
            "question": "Will Bitcoin hit 200k in 2026?",
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["a", "b"],
            "gameStartTime": None,
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    games = await fetch_polymarket_sports_games(
        resolver=MLB_RESOLVER, sport="mlb", http_client=client
    )
    await client.aclose()

    assert len(games) == 1
    g = games[0]
    assert g.venue_id == "polymarket"
    assert g.game_date == date(2026, 8, 5)
    assert g.outcome_ids == {"LAD": "tok_lad", "CHC": "tok_chc"}


@pytest.mark.asyncio
async def test_fetch_polymarket_sports_games_falls_back_to_slug_date():
    payload = [
        {
            "question": "Atlanta Braves vs. New York Yankees",
            "outcomes": ["Atlanta Braves", "New York Yankees"],
            "clobTokenIds": ["a", "b"],
            "slug": "mlb-atl-nyy-2026-08-05",
        }
    ]

    def handler(_):
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    games = await fetch_polymarket_sports_games(
        resolver=MLB_RESOLVER, sport="mlb", http_client=client
    )
    await client.aclose()
    assert len(games) == 1
    assert games[0].game_date == date(2026, 8, 5)


@pytest.mark.asyncio
async def test_fetch_polymarket_sports_games_uses_tagged_events_endpoint():
    """Games must be found via /events?tag_slug even when /markets misses them.

    The volume-ordered /markets endpoint caps at 100 rows, and in-season MLB
    never outranks politics/esports there — which is why MLB discovery
    silently returned zero games.
    """
    event_payload = [
        {
            "title": "Colorado Rockies vs. San Francisco Giants",
            "markets": [
                {
                    "question": "Colorado Rockies vs. San Francisco Giants",
                    "outcomes": ["Colorado Rockies", "San Francisco Giants"],
                    "clobTokenIds": ["tok_col", "tok_sf"],
                    "gameStartTime": "2026-08-08 20:15:00+00",
                    "slug": "mlb-col-sf-2026-08-08",
                }
            ],
        }
    ]
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=event_payload)
        # /markets is crowded out — returns nothing relevant.
        return httpx.Response(200, json=[])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    games = await fetch_polymarket_sports_games(
        resolver=MLB_RESOLVER, sport="mlb", http_client=client
    )
    await client.aclose()

    assert any(p.endswith("/events") for p in seen_paths), seen_paths
    assert len(games) == 1
    assert games[0].outcome_ids == {"COL": "tok_col", "SF": "tok_sf"}
    assert games[0].game_date == date(2026, 8, 8)


@pytest.mark.asyncio
async def test_fetch_polymarket_sports_games_survives_events_endpoint_failure():
    """A failing /events call must not lose the /markets results."""
    market_payload = [
        {
            "question": "Atlanta Braves vs. New York Yankees",
            "outcomes": ["Atlanta Braves", "New York Yankees"],
            "clobTokenIds": ["a", "b"],
            "slug": "mlb-atl-nyy-2026-08-08",
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json=market_payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    games = await fetch_polymarket_sports_games(
        resolver=MLB_RESOLVER, sport="mlb", http_client=client
    )
    await client.aclose()
    assert len(games) == 1
    assert games[0].outcome_ids == {"ATL": "a", "NYY": "b"}


@pytest.mark.asyncio
async def test_fetch_polymarket_sports_games_handles_stringified_arrays():
    payload = [
        {
            "question": "Los Angeles Dodgers vs. Chicago Cubs",
            "outcomes": '["Los Angeles Dodgers","Chicago Cubs"]',
            "clobTokenIds": '["tok_lad","tok_chc"]',
            "gameStartTime": "2026-08-05 18:20:00+00",
        }
    ]

    def handler(_):
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    games = await fetch_polymarket_sports_games(
        resolver=MLB_RESOLVER, sport="mlb", http_client=client
    )
    await client.aclose()
    assert len(games) == 1
    assert games[0].outcome_ids == {"LAD": "tok_lad", "CHC": "tok_chc"}
