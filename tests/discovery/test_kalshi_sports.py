from datetime import date

import httpx
import pytest

from arbys.discovery.kalshi_sports import _parse_ticker_date, fetch_kalshi_mlb_games
from arbys.discovery.teams import MLB_RESOLVER


def test_parse_ticker_date_typical():
    assert _parse_ticker_date("KXMLBGAME-26AUG051420LADCHC") == date(2026, 8, 5)


def test_parse_ticker_date_variable_team_code_length():
    # "ATH" (3-letter code) plus "BOS" — should still parse the date prefix.
    assert _parse_ticker_date("KXMLBGAME-26AUG051910ATHBOS") == date(2026, 8, 5)


def test_parse_ticker_date_bad_month_returns_none():
    assert _parse_ticker_date("KXMLBGAME-26XYZ051420LADCHC") is None


def test_parse_ticker_date_no_dash_returns_none():
    assert _parse_ticker_date("BADTICKER") is None


@pytest.mark.asyncio
async def test_fetch_kalshi_mlb_games_parses_events_and_markets():
    events_payload = {
        "events": [
            {"event_ticker": "KXMLBGAME-26AUG051420LADCHC", "title": "Los Angeles D vs Chicago C"}
        ]
    }
    markets_payload = {
        "markets": [
            {"ticker": "KXMLBGAME-26AUG051420LADCHC-LAD", "yes_sub_title": "Los Angeles D"},
            {"ticker": "KXMLBGAME-26AUG051420LADCHC-CHC", "yes_sub_title": "Chicago C"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=events_payload)
        if request.url.path.endswith("/markets"):
            return httpx.Response(200, json=markets_payload)
        return httpx.Response(404)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=5.0,
        base_url="https://api.elections.kalshi.com/trade-api/v2",
    )
    games = await fetch_kalshi_mlb_games(resolver=MLB_RESOLVER, http_client=client)
    await client.aclose()

    assert len(games) == 1
    g = games[0]
    assert g.sport == "mlb"
    assert g.venue_id == "kalshi"
    assert g.game_date == date(2026, 8, 5)
    assert {t.code for t in g.teams} == {"LAD", "CHC"}
    assert g.outcome_ids["LAD"] == "KXMLBGAME-26AUG051420LADCHC-LAD:YES"
    assert g.outcome_ids["CHC"] == "KXMLBGAME-26AUG051420LADCHC-CHC:YES"


@pytest.mark.asyncio
async def test_fetch_kalshi_mlb_games_skips_events_with_unknown_teams():
    events_payload = {"events": [{"event_ticker": "KXMLBGAME-26AUG051420LADXXX"}]}
    markets_payload = {
        "markets": [
            {"ticker": "KXMLBGAME-26AUG051420LADXXX-LAD", "yes_sub_title": "Los Angeles D"},
            {"ticker": "KXMLBGAME-26AUG051420LADXXX-XXX", "yes_sub_title": "Nowhere X"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=events_payload)
        return httpx.Response(200, json=markets_payload)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=5.0,
        base_url="https://api.elections.kalshi.com/trade-api/v2",
    )
    games = await fetch_kalshi_mlb_games(resolver=MLB_RESOLVER, http_client=client)
    await client.aclose()
    assert games == []
