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
