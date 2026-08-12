from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from arbys.discovery.polymarket_us import (
    fetch_polymarket_us_games,
    fetch_polymarket_us_tennis,
    fetch_polymarket_us_totals,
)
from arbys.discovery.teams import MLB_RESOLVER, NFL_RESOLVER

MLB_EVENTS = {
    "events": [
        {
            "slug": "mlb-cle-det-2026-08-11",
            "title": "Cleveland Guardians vs. Detroit Tigers",
            "startTime": "2026-08-11T22:40:00Z",
            "teams": [
                {"name": "Cleveland Guardians", "abbreviation": "cle"},
                {"name": "Detroit Tigers", "abbreviation": "det"},
            ],
            "markets": [
                {
                    "slug": "aec-mlb-cle-det-2026-08-11",
                    "sportsMarketType": "baseball_team_full_game_winner",
                    "marketSides": [
                        {"long": True, "team": {"name": "Cleveland Guardians"}},
                        {"long": False, "team": {"name": "Detroit Tigers"}},
                    ],
                },
                {
                    "slug": "asc-mlb-cle-det-2026-08-11-pos-2pt5",
                    "sportsMarketType": "baseball_team_full_game_spread",
                    "line": 2.5,
                    "marketSides": [
                        {"long": True, "team": {"name": "Cleveland Guardians"}},
                        {"long": False, "team": {"name": "Detroit Tigers"}},
                    ],
                },
            ],
        }
    ]
}

NFL_TOTALS = {
    "events": [
        {
            "slug": "nfl-gb-pit-2026-08-13",
            "title": "Green Bay Packers vs. Pittsburgh Steelers",
            "startTime": "2026-08-13T23:00:00Z",
            "teams": [
                {"name": "Green Bay Packers", "abbreviation": "gb"},
                {"name": "Pittsburgh Steelers", "abbreviation": "pit"},
            ],
            "markets": [
                {
                    "slug": "tsc-nfl-gb-pit-2026-08-13-total-21pt5",
                    "sportsMarketType": "football_team_full_game_total",
                    "line": 21.5,
                    "marketSides": [
                        {"long": True, "team": None, "description": "Over"},
                        {"long": False, "team": None, "description": "Under"},
                    ],
                }
            ],
        }
    ]
}

TENNIS_EVENTS = {
    "events": [
        {
            "slug": "wta-naoosa-eleryb-2026-08-11",
            "title": "Naomi Osaka vs. Elena Rybakina",
            "startTime": "2026-08-11T14:00:00Z",
            "teams": [
                {"name": "Naomi Osaka", "abbreviation": "naoosa"},
                {"name": "Elena Rybakina", "abbreviation": "eleryb"},
            ],
            "markets": [
                {
                    "slug": "aec-wta-naoosa-eleryb-2026-08-11",
                    "sportsMarketType": "tennis_match_winner",
                    "marketSides": [
                        {"long": True, "team": {"name": "Naomi Osaka"}},
                        {"long": False, "team": {"name": "Elena Rybakina"}},
                    ],
                }
            ],
        }
    ]
}


def _client(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "gateway.polymarket.us" in str(request.url)
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


@pytest.mark.asyncio
async def test_moneyline_maps_teams_to_long_and_short_outcome_ids():
    client = _client(MLB_EVENTS)
    games = await fetch_polymarket_us_games(
        resolver=MLB_RESOLVER, sport="mlb", http_client=client
    )
    assert len(games) == 1
    game = games[0]
    assert game.venue_id == "polymarket_us"
    assert game.market_type == "moneyline"
    assert game.outcome_ids == {
        "CLE": "aec-mlb-cle-det-2026-08-11:LONG",
        "DET": "aec-mlb-cle-det-2026-08-11:SHORT",
    }
    assert game.start_time == datetime(2026, 8, 11, 22, 40, tzinfo=UTC)
    await client.aclose()


@pytest.mark.asyncio
async def test_spread_markets_are_skipped_in_phase_1():
    """Spreads are Phase 2. The payload contains one; it must not appear."""
    client = _client(MLB_EVENTS)
    games = await fetch_polymarket_us_games(
        resolver=MLB_RESOLVER, sport="mlb", http_client=client
    )
    assert all(g.market_type == "moneyline" for g in games)
    await client.aclose()


@pytest.mark.asyncio
async def test_totals_key_by_over_under_and_carry_a_decimal_line():
    client = _client(NFL_TOTALS)
    games = await fetch_polymarket_us_totals(
        resolver=NFL_RESOLVER, sport="nfl", http_client=client
    )
    assert len(games) == 1
    game = games[0]
    assert game.market_type == "total"
    assert game.line == Decimal("21.5")
    assert isinstance(game.line, Decimal)
    assert game.outcome_ids == {
        "OVER": "tsc-nfl-gb-pit-2026-08-13-total-21pt5:LONG",
        "UNDER": "tsc-nfl-gb-pit-2026-08-13-total-21pt5:SHORT",
    }
    await client.aclose()


@pytest.mark.asyncio
async def test_tennis_resolves_players_from_structured_names():
    client = _client(TENNIS_EVENTS)
    matches = await fetch_polymarket_us_tennis(http_client=client)
    # Both ATP and WTA are fetched from the same stub, so the same match
    # comes back twice; identity is what matters here.
    assert matches
    match = matches[0]
    assert set(match.outcome_ids) == {"OSAKA", "RYBAKINA"}
    assert match.outcome_ids["OSAKA"].endswith(":LONG")
    assert match.outcome_ids["RYBAKINA"].endswith(":SHORT")
    assert match.venue_id == "polymarket_us"
    await client.aclose()


@pytest.mark.asyncio
async def test_unknown_team_is_dropped_not_raised():
    payload = {
        "events": [
            {
                "slug": "mlb-xxx-yyy-2026-08-11",
                "startTime": "2026-08-11T22:40:00Z",
                "teams": [{"name": "Springfield Isotopes"}, {"name": "Shelbyville"}],
                "markets": [
                    {
                        "slug": "aec-mlb-xxx-yyy-2026-08-11",
                        "sportsMarketType": "baseball_team_full_game_winner",
                        "marketSides": [
                            {"long": True, "team": {"name": "Springfield Isotopes"}},
                            {"long": False, "team": {"name": "Shelbyville"}},
                        ],
                    }
                ],
            }
        ]
    }
    client = _client(payload)
    games = await fetch_polymarket_us_games(
        resolver=MLB_RESOLVER, sport="mlb", http_client=client
    )
    assert games == []
    await client.aclose()


@pytest.mark.asyncio
async def test_event_without_start_time_is_dropped():
    payload = {
        "events": [
            {
                "slug": "mlb-cle-det-2026-08-11",
                "teams": [
                    {"name": "Cleveland Guardians"},
                    {"name": "Detroit Tigers"},
                ],
                "markets": [
                    {
                        "slug": "aec-mlb-cle-det-2026-08-11",
                        "sportsMarketType": "baseball_team_full_game_winner",
                        "marketSides": [
                            {"long": True, "team": {"name": "Cleveland Guardians"}},
                            {"long": False, "team": {"name": "Detroit Tigers"}},
                        ],
                    }
                ],
            }
        ]
    }
    client = _client(payload)
    games = await fetch_polymarket_us_games(
        resolver=MLB_RESOLVER, sport="mlb", http_client=client
    )
    assert games == []
    await client.aclose()


@pytest.mark.asyncio
async def test_tennis_sport_is_the_league_not_the_word_tennis():
    """Kalshi labels tennis per-tour ("atp" / "wta") and  is part of
    the matcher's bucket key, so labelling everything "tennis" pairs with
    nothing. That shipped once and silently dropped 72 matching player pairs."""
    client = _client(TENNIS_EVENTS)
    matches = await fetch_polymarket_us_tennis(http_client=client)
    await client.aclose()
    assert matches
    assert {m.sport for m in matches} <= {"atp", "wta"}
    assert "tennis" not in {m.sport for m in matches}


@pytest.mark.asyncio
async def test_tennis_matches_a_kalshi_game_end_to_end():
    """The bucket key must actually let the two venues pair up."""
    from datetime import UTC, datetime

    from arbys.discovery.kalshi_sports import VenueGame
    from arbys.discovery.matcher import match_games
    from arbys.discovery.players import Player

    client = _client(TENNIS_EVENTS)
    poly = await fetch_polymarket_us_tennis(http_client=client)
    await client.aclose()
    wta = [m for m in poly if m.sport == "wta"]
    assert wta, "expected a wta match from the stub"
    p = wta[0]

    kalshi = VenueGame(
        sport="wta",
        venue_id="kalshi",
        game_date=p.game_date,
        teams=(Player(code="OSAKA", full_name="Osaka"),
               Player(code="RYBAKINA", full_name="Rybakina")),
        outcome_ids={"OSAKA": "K1:YES", "RYBAKINA": "K1:NO"},
        ref="K1",
    )
    assert len(match_games([kalshi], [p], date_tolerance_days=1)) == 1
