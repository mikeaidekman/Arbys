from datetime import date
from decimal import Decimal

import httpx
import pytest

from arbys.discovery.kalshi_sports import VenueGame
from arbys.discovery.kalshi_totals import fetch_kalshi_totals, split_team_codes
from arbys.discovery.matcher import OVER, UNDER, match_games, match_to_event_group
from arbys.discovery.polymarket_totals import fetch_polymarket_totals, parse_total_slug
from arbys.discovery.teams import NFL_RESOLVER


def _total(venue, line, date_str="2026-09-13", ids=None):
    a = NFL_RESOLVER.by_code("DET")
    b = NFL_RESOLVER.by_code("CIN")
    y, m, d = map(int, date_str.split("-"))
    return VenueGame(
        sport="nfl",
        venue_id=venue,
        game_date=date(y, m, d),
        teams=(a, b),
        outcome_ids=ids or {OVER: f"{venue}-o-{line}", UNDER: f"{venue}-u-{line}"},
        ref=f"{venue}-{line}",
        market_type="total",
        line=Decimal(str(line)),
    )


# --- slug / ticker parsing -------------------------------------------------

def test_parse_total_slug_fractional_and_whole():
    assert parse_total_slug("nfl-dal-sea-2026-08-16-total-37pt5") == (
        "DAL", "SEA", "2026-08-16", Decimal("37.5")
    )
    assert parse_total_slug("nfl-gb-min-2026-09-13-total-44") == (
        "GB", "MIN", "2026-09-13", Decimal("44.0")
    )


def test_parse_total_slug_rejects_moneyline_and_junk():
    assert parse_total_slug("nfl-dal-sea-2026-08-16") is None
    assert parse_total_slug("nfl-mia-lv-2026-09-13-spread-3pt5") is None
    assert parse_total_slug("") is None


def test_split_team_codes_handles_uneven_lengths():
    # 3+3, 2+3, 3+2 — codes are not fixed width.
    assert split_team_codes("DETCIN", NFL_RESOLVER) == ("DET", "CIN")
    assert split_team_codes("GBMIN", NFL_RESOLVER) == ("GB", "MIN")
    assert split_team_codes("NYJTEN", NFL_RESOLVER) == ("NYJ", "TEN")
    assert split_team_codes("ZZZQQQ", NFL_RESOLVER) is None


# --- the line must be part of identity ------------------------------------

def test_different_lines_do_not_match_each_other():
    """Over 44.5 and Over 47.5 are different bets; fusing them invents an arb."""
    k = _total("kalshi", "44.5")
    p = _total("polymarket", "47.5")
    assert match_games([k], [p]) == []


def test_same_line_matches_across_venues():
    k = _total("kalshi", "44.5")
    p = _total("polymarket", "44.5")
    matches = match_games([k], [p])
    assert len(matches) == 1
    assert matches[0].market_type == "total"
    assert matches[0].line == Decimal("44.5")


def test_each_shared_line_becomes_its_own_group():
    kalshi = [_total("kalshi", x) for x in ("41.5", "44.5", "47.5")]
    poly = [_total("polymarket", x) for x in ("44.5", "47.5", "50.5")]
    matches = match_games(kalshi, poly)
    assert len(matches) == 2  # only 44.5 and 47.5 are on both
    ids = sorted(m.event_group_id() for m in matches)
    assert ids == [
        "nfl-CIN-DET-2026-09-13-total-44.5",
        "nfl-CIN-DET-2026-09-13-total-47.5",
    ]


def test_moneyline_group_id_is_unchanged():
    """Totals must not perturb existing moneyline ids."""
    a, b = NFL_RESOLVER.by_code("DET"), NFL_RESOLVER.by_code("CIN")
    ml = [
        VenueGame(sport="nfl", venue_id=v, game_date=date(2026, 9, 13),
                  teams=(a, b), outcome_ids={"DET": f"{v}-d", "CIN": f"{v}-c"},
                  ref=v)
        for v in ("kalshi", "polymarket")
    ]
    m = match_games([ml[0]], [ml[1]])[0]
    assert m.event_group_id() == "nfl-CIN-DET-2026-09-13"


def test_totals_group_marks_over_as_yes_side():
    k, p = _total("kalshi", "44.5"), _total("polymarket", "44.5")
    group = match_to_event_group(match_games([k], [p])[0])
    assert len(group.legs) == 4
    yes = {leg.outcome_id for leg in group.legs if leg.is_yes_side}
    no = {leg.outcome_id for leg in group.legs if not leg.is_yes_side}
    assert yes == {"kalshi-o-44.5", "polymarket-o-44.5"}
    assert no == {"kalshi-u-44.5", "polymarket-u-44.5"}
    assert "Over 44.5" in group.title


# --- fetchers --------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_kalshi_totals_one_game_per_strike():
    events = {"events": [{"event_ticker": "KXNFLTOTAL-26SEP13DETCIN"}]}
    markets = {
        "markets": [
            {"ticker": "KXNFLTOTAL-26SEP13DETCIN-45", "floor_strike": 44.5,
             "occurrence_datetime": "2026-09-13T17:00:00Z"},
            {"ticker": "KXNFLTOTAL-26SEP13DETCIN-48", "floor_strike": 47.5},
            {"ticker": "KXNFLTOTAL-26SEP13DETCIN-X", "floor_strike": None},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=events)
        return httpx.Response(200, json=markets)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=5.0,
        base_url="https://api.elections.kalshi.com/trade-api/v2",
    )
    games = await fetch_kalshi_totals(resolver=NFL_RESOLVER, sport="nfl", http_client=client)
    await client.aclose()

    assert len(games) == 2  # the strike-less market is skipped
    assert {g.line for g in games} == {Decimal("44.5"), Decimal("47.5")}
    g = next(g for g in games if g.line == Decimal("44.5"))
    assert g.market_type == "total"
    assert g.outcome_ids == {
        OVER: "KXNFLTOTAL-26SEP13DETCIN-45:YES",
        UNDER: "KXNFLTOTAL-26SEP13DETCIN-45:NO",
    }
    assert {t.code for t in g.teams} == {"DET", "CIN"}
    assert g.start_time is not None


@pytest.mark.asyncio
async def test_fetch_polymarket_totals_parses_over_under():
    payload = [
        {
            "markets": [
                {
                    "sportsMarketType": "totals",
                    "slug": "nfl-det-cin-2026-09-13-total-44pt5",
                    "outcomes": ["Over", "Under"],
                    "clobTokenIds": ["tok_over", "tok_under"],
                    "gameStartTime": "2026-09-13 17:00:00+00",
                },
                {  # moneyline on the same game must be ignored here
                    "sportsMarketType": "moneyline",
                    "slug": "nfl-det-cin-2026-09-13",
                    "outcomes": ["Lions", "Bengals"],
                    "clobTokenIds": ["a", "b"],
                },
            ]
        }
    ]

    def handler(_):
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    games = await fetch_polymarket_totals(
        resolver=NFL_RESOLVER, sport="nfl", http_client=client
    )
    await client.aclose()

    assert len(games) == 1
    g = games[0]
    assert g.market_type == "total"
    assert g.line == Decimal("44.5")
    assert g.outcome_ids == {OVER: "tok_over", UNDER: "tok_under"}
