"""Spreads: both venue parsers and the cross-venue convention.

Every fixture is modelled on payloads observed live on 2026-09-06; see
docs/superpowers/specs/2026-09-06-spreads-and-discovery-horizon-design.md.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from arbys.discovery.kalshi_spreads import anchor_code_from_ticker, fetch_kalshi_spreads
from arbys.discovery.matcher import match_games, match_to_event_group
from arbys.discovery.polymarket_us import _title_agrees, fetch_polymarket_us_spreads
from arbys.discovery.teams import MLB_RESOLVER, NFL_RESOLVER

# --- Kalshi -----------------------------------------------------------------

KALSHI_EVENTS = {"events": [{"event_ticker": "KXNFLSPREAD-26SEP09NESEA"}]}
KALSHI_MARKETS = {
    "markets": [
        {"ticker": "KXNFLSPREAD-26SEP09NESEA-SEA4", "floor_strike": 3.5,
         "strike_type": "greater", "yes_sub_title": "Seattle wins by over 3.5 points"},
        {"ticker": "KXNFLSPREAD-26SEP09NESEA-NE4", "floor_strike": 3.5,
         "strike_type": "greater", "yes_sub_title": "New England wins by over 3.5 points"},
        # A strike type other than "greater" would invert the proposition.
        {"ticker": "KXNFLSPREAD-26SEP09NESEA-NE8", "floor_strike": 7.5, "strike_type": "less"},
        # Suffix names a team that is not in this game.
        {"ticker": "KXNFLSPREAD-26SEP09NESEA-KC4", "floor_strike": 3.5, "strike_type": "greater"},
        # No line.
        {"ticker": "KXNFLSPREAD-26SEP09NESEA-SEA8", "floor_strike": None, "strike_type": "greater"},
    ]
}


def _kalshi_client(events, markets, calls: list[str] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=events)
        return httpx.Response(200, json=markets)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=5.0,
        base_url="https://api.elections.kalshi.com/trade-api/v2",
    )


def test_anchor_code_is_whichever_team_code_prefixes_the_suffix():
    ev = "KXNFLSPREAD-26SEP09NESEA"
    assert anchor_code_from_ticker(f"{ev}-SEA4", ev, ("NE", "SEA")) == "SEA"
    assert anchor_code_from_ticker(f"{ev}-NE12", ev, ("NE", "SEA")) == "NE"
    # Neither team, wrong event, or no digits.
    assert anchor_code_from_ticker(f"{ev}-KC4", ev, ("NE", "SEA")) is None
    assert anchor_code_from_ticker("KXNFLSPREAD-26SEP10SFLAR-SF5", ev, ("NE", "SEA")) is None
    assert anchor_code_from_ticker(f"{ev}-SEA", ev, ("NE", "SEA")) is None


async def test_kalshi_one_game_per_team_and_line_anchored_on_the_ticker_team():
    client = _kalshi_client(KALSHI_EVENTS, KALSHI_MARKETS)
    games = await fetch_kalshi_spreads(
        resolver=NFL_RESOLVER, sport="nfl", http_client=client, horizon_days=0
    )
    await client.aclose()

    assert {(g.anchor, g.line) for g in games} == {("SEA", Decimal("3.5")), ("NE", Decimal("3.5"))}
    sea = next(g for g in games if g.anchor == "SEA")
    assert sea.market_type == "spread"
    assert isinstance(sea.line, Decimal)
    # YES is the anchor covering; the other team's key is the complement.
    assert sea.outcome_ids == {
        "SEA": "KXNFLSPREAD-26SEP09NESEA-SEA4:YES",
        "NE": "KXNFLSPREAD-26SEP09NESEA-SEA4:NO",
    }
    assert {t.code for t in sea.teams} == {"NE", "SEA"}
    assert sea.game_date == date(2026, 9, 9)
    assert sea.start_time is None  # NFL tickers carry no HHMM
    assert sea.ref == "KXNFLSPREAD-26SEP09NESEA-SEA4"


async def test_kalshi_alias_code_yields_the_canonical_anchor():
    """KXMLBSPREAD-26SEP061410AZHOU-AZ2 observed live. AZ must come out as ARI
    so it matches Polymarket's ARI, and the MLB ticker's HHMM is the start."""
    events = {"events": [{"event_ticker": "KXMLBSPREAD-26SEP061410AZHOU"}]}
    markets = {"markets": [{"ticker": "KXMLBSPREAD-26SEP061410AZHOU-AZ2",
                            "floor_strike": 1.5, "strike_type": "greater"}]}
    client = _kalshi_client(events, markets)
    games = await fetch_kalshi_spreads(
        resolver=MLB_RESOLVER, sport="mlb", http_client=client, horizon_days=0
    )
    await client.aclose()

    assert len(games) == 1
    g = games[0]
    assert g.anchor == "ARI"
    assert g.outcome_ids == {
        "ARI": "KXMLBSPREAD-26SEP061410AZHOU-AZ2:YES",
        "HOU": "KXMLBSPREAD-26SEP061410AZHOU-AZ2:NO",
    }
    assert g.start_time == datetime(2026, 9, 6, 18, 10, tzinfo=UTC)  # 14:10 EDT


async def test_kalshi_event_past_the_horizon_makes_no_market_call():
    """The per-event /markets request is what costs against Kalshi's rate
    limit; a game beyond the horizon is skipped before it is made."""
    far = {"events": [{"event_ticker": "KXNFLSPREAD-30SEP09NESEA"}]}
    far_markets = {
        "markets": [
            {"ticker": "KXNFLSPREAD-30SEP09NESEA-SEA4", "floor_strike": 3.5, "strike_type": "greater"},
            {"ticker": "KXNFLSPREAD-30SEP09NESEA-NE4", "floor_strike": 3.5, "strike_type": "greater"},
        ]
    }
    calls: list[str] = []
    client = _kalshi_client(far, far_markets, calls)
    games = await fetch_kalshi_spreads(
        resolver=NFL_RESOLVER, sport="nfl", http_client=client, horizon_days=3
    )
    assert games == []
    assert not any(p.endswith("/markets") for p in calls)

    calls.clear()
    games = await fetch_kalshi_spreads(
        resolver=NFL_RESOLVER, sport="nfl", http_client=client, horizon_days=0
    )
    await client.aclose()
    assert len(games) == 2
    assert any(p.endswith("/markets") for p in calls)


async def test_kalshi_unknown_sport_raises():
    with pytest.raises(ValueError):
        await fetch_kalshi_spreads(resolver=NFL_RESOLVER, sport="curling", horizon_days=0)


# --- Polymarket US ------------------------------------------------------------


def _pm_client(payload) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "gateway.polymarket.us" in str(request.url)
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def _pm_event(markets: list[dict]) -> dict:
    return {
        "events": [
            {
                "slug": "mlb-mil-cin-2026-09-06",
                "startTime": "2026-09-06T16:10:00Z",
                "live": False,
                "ended": False,
                "teams": [
                    {"name": "Milwaukee Brewers", "displayAbbreviation": "MIL", "safeName": "Brewers"},
                    {"name": "Cincinnati Reds", "displayAbbreviation": "CIN", "safeName": "Reds"},
                ],
                "markets": markets,
            }
        ]
    }


def _pm_spread(
    slug: str,
    line,
    title: str | None,
    *,
    kind: str = "baseball_team_full_game_spread",
    long_team: str = "Milwaukee Brewers",
    short_team: str = "Cincinnati Reds",
) -> dict:
    market = {
        "slug": slug,
        "sportsMarketType": kind,
        "line": line,
        "marketSides": [
            {"long": True, "team": {"name": long_team}},
            {"long": False, "team": {"name": short_team}},
        ],
    }
    if title is not None:
        market["title"] = title
    return market


# Observed live 2026-09-06. `line` is signed relative to the first-listed
# team; the title names the team that must win by more than |line|.
POS = _pm_spread("asc-mlb-mil-cin-2026-09-06-pos-2pt5", 2.5, "Cincinnati Reds wins by over 2.5 runs")
NEG = _pm_spread("asc-mlb-mil-cin-2026-09-06-neg-2pt5", -2.5, "Milwaukee Brewers wins by over 2.5 runs")


async def _pm_games(markets: list[dict]):
    client = _pm_client(_pm_event(markets))
    try:
        return await fetch_polymarket_us_spreads(
            resolver=MLB_RESOLVER, sport="mlb", http_client=client
        )
    finally:
        await client.aclose()


async def test_polymarket_negative_line_anchors_the_first_team():
    games = await _pm_games([NEG])
    assert len(games) == 1
    g = games[0]
    assert (g.market_type, g.anchor, g.line) == ("spread", "MIL", Decimal("2.5"))
    assert isinstance(g.line, Decimal)
    assert g.outcome_ids == {
        "MIL": "asc-mlb-mil-cin-2026-09-06-neg-2pt5:LONG",
        "CIN": "asc-mlb-mil-cin-2026-09-06-neg-2pt5:SHORT",
    }
    assert g.game_date == date(2026, 9, 6)
    assert g.start_time == datetime(2026, 9, 6, 16, 10, tzinfo=UTC)
    assert (g.live, g.ended) == (False, False)


async def test_polymarket_positive_line_anchors_the_second_team_with_the_same_outcome_ids():
    """Only the anchor flips with the sign. First team is always LONG."""
    games = await _pm_games([POS])
    assert len(games) == 1
    g = games[0]
    assert (g.anchor, g.line) == ("CIN", Decimal("2.5"))
    assert g.outcome_ids == {
        "MIL": "asc-mlb-mil-cin-2026-09-06-pos-2pt5:LONG",
        "CIN": "asc-mlb-mil-cin-2026-09-06-pos-2pt5:SHORT",
    }


async def test_polymarket_title_naming_the_other_team_is_skipped(caplog):
    """Two venue fields contradicting each other is the signal that the sign
    convention moved. A backwards sign near even money is a 2-4c phantom edge
    the plausible-edge ceiling cannot see, so the market is refused."""
    bad = _pm_spread("asc-mlb-mil-cin-2026-09-06-pos-2pt5", 2.5, "Milwaukee Brewers wins by over 2.5 runs")
    with caplog.at_level("WARNING"):
        games = await _pm_games([bad])
    assert games == []
    assert "contradicts" in caplog.text


async def test_polymarket_title_with_a_different_line_is_skipped():
    bad = _pm_spread("asc-mlb-mil-cin-2026-09-06-pos-2pt5", 2.5, "Cincinnati Reds wins by over 1.5 runs")
    assert await _pm_games([bad]) == []


async def test_polymarket_unparseable_title_is_accepted_and_counted(caplog):
    """The structure is the source of truth; the title is a guard. A reworded
    title disables the guard visibly rather than silently zeroing the league."""
    odd = _pm_spread("asc-mlb-mil-cin-2026-09-06-pos-2pt5", 2.5, "Reds to cover the run line")
    with caplog.at_level("WARNING"):
        games = await _pm_games([odd])
    assert len(games) == 1
    assert "matched no known pattern" in caplog.text


async def test_polymarket_missing_title_is_accepted():
    games = await _pm_games([_pm_spread("asc-mlb-mil-cin-2026-09-06-pos-2pt5", 2.5, None)])
    assert len(games) == 1


async def test_polymarket_long_side_that_is_not_the_first_team_is_skipped():
    swapped = _pm_spread(
        "asc-mlb-mil-cin-2026-09-06-neg-2pt5", -2.5, "Cincinnati Reds wins by over 2.5 runs",
        long_team="Cincinnati Reds", short_team="Milwaukee Brewers",
    )
    assert await _pm_games([swapped]) == []


async def test_polymarket_zero_and_missing_lines_are_skipped():
    zero = _pm_spread("asc-mlb-mil-cin-2026-09-06-pos-0", 0, "Cincinnati Reds wins by over 0 runs")
    missing = _pm_spread("asc-mlb-mil-cin-2026-09-06-pos-2pt5", None, "Cincinnati Reds wins by over 2.5 runs")
    assert await _pm_games([zero, missing]) == []


async def test_polymarket_period_spreads_are_ignored():
    """First-five, half and quarter spreads are Phase 3 and distinct types."""
    f5 = _pm_spread(
        "asc-mlb-mil-cin-2026-09-06-f5-pos-1pt5", 1.5,
        "Cincinnati Reds wins by over 1.5 runs in first 5 innings",
        kind="baseball_team_first_five_spread",
    )
    assert await _pm_games([f5]) == []


async def test_polymarket_winner_and_total_markets_are_not_spreads():
    winner = {"slug": "aec-mlb-mil-cin-2026-09-06", "sportsMarketType": "baseball_team_full_game_winner",
              "marketSides": [{"long": True, "team": {"name": "Milwaukee Brewers"}},
                              {"long": False, "team": {"name": "Cincinnati Reds"}}]}
    assert await _pm_games([winner]) == []


def test_title_agrees_is_tri_state():
    assert _title_agrees("Cincinnati Reds wins by over 2.5 runs", "Cincinnati Reds", Decimal("2.5")) is True
    assert _title_agrees("Seattle Seahawks wins by over 21.5 points", "Seattle Seahawks", Decimal("21.5")) is True
    assert _title_agrees("Tar Heels wins by over 20.5 points", "Tar Heels", Decimal("20.5")) is True
    assert _title_agrees("Cincinnati Reds wins by over 2.5 runs", "Milwaukee Brewers", Decimal("2.5")) is False
    assert _title_agrees("Cincinnati Reds wins by over 1.5 runs", "Cincinnati Reds", Decimal("2.5")) is False
    assert _title_agrees("Reds to cover", "Cincinnati Reds", Decimal("2.5")) is None
    assert _title_agrees(None, "Cincinnati Reds", Decimal("2.5")) is None


# --- Cross-venue: THE convention test -------------------------------------------

KALSHI_MILCIN_EVENTS = {"events": [{"event_ticker": "KXMLBSPREAD-26SEP061210MILCIN"}]}


def _kalshi_milcin(*suffixes_and_strikes: tuple[str, float]) -> dict:
    return {
        "markets": [
            {"ticker": f"KXMLBSPREAD-26SEP061210MILCIN-{suffix}",
             "floor_strike": strike, "strike_type": "greater"}
            for suffix, strike in suffixes_and_strikes
        ]
    }


async def _both(kalshi_markets: dict, pm_markets: list[dict]):
    k_client = _kalshi_client(KALSHI_MILCIN_EVENTS, kalshi_markets)
    p_client = _pm_client(_pm_event(pm_markets))
    try:
        kalshi = await fetch_kalshi_spreads(
            resolver=MLB_RESOLVER, sport="mlb", http_client=k_client, horizon_days=0
        )
        poly = await fetch_polymarket_us_spreads(
            resolver=MLB_RESOLVER, sport="mlb", http_client=p_client
        )
    finally:
        await k_client.aclose()
        await p_client.aclose()
    return kalshi, poly


async def test_a_positive_polymarket_line_pairs_with_kalshi_yes_on_the_second_team():
    """Kalshi CIN3 ("Cincinnati wins by over 2.5 runs") and Polymarket
    pos-2pt5 (Brewers +2.5, long = Brewers) are the same binary with opposite
    sides: the Reds covering is Kalshi YES and Polymarket SHORT.

    Pinned live on 2026-09-06 16:20Z, NFL: asc-nfl-ne-sea-…-pos-3pt5 long
    0.51/0.52 vs KXNFLSPREAD-26SEP09NESEA-SEA4 NO 0.51/0.52; pos-10pt5 long
    0.72/0.74 vs SFLAR-LAR11 NO 0.72/0.74. Getting this backwards near even
    money produces a 2-4c phantom edge the plausible-edge ceiling cannot
    see, which is why this test exists.
    """
    kalshi, poly = await _both(_kalshi_milcin(("CIN3", 2.5)), [POS])
    matches = match_games(kalshi, poly)
    assert len(matches) == 1
    group = match_to_event_group(matches[0])

    assert group.id == "mlb-CIN-MIL-2026-09-06-spread-CIN-2.5"
    assert group.title == "Cincinnati Reds vs Milwaukee Brewers — Cincinnati Reds -2.5 (2026-09-06)"
    assert group.start_time == datetime(2026, 9, 6, 16, 10, tzinfo=UTC)
    yes = {leg.outcome_id for leg in group.legs if leg.is_yes_side}
    no = {leg.outcome_id for leg in group.legs if not leg.is_yes_side}
    assert yes == {
        "KXMLBSPREAD-26SEP061210MILCIN-CIN3:YES",
        "asc-mlb-mil-cin-2026-09-06-pos-2pt5:SHORT",
    }
    assert no == {
        "KXMLBSPREAD-26SEP061210MILCIN-CIN3:NO",
        "asc-mlb-mil-cin-2026-09-06-pos-2pt5:LONG",
    }
    assert {leg.venue_id for leg in group.legs} == {"kalshi", "polymarket_us"}


async def test_a_negative_polymarket_line_pairs_with_kalshi_yes_on_the_first_team():
    """neg-3pt5 long 0.26/0.27 vs NESEA-NE4 YES 0.25/0.27; neg-10pt5 long
    0.08/0.11 vs SFLAR-SF11 YES 0.08/0.14 (2026-09-06)."""
    kalshi, poly = await _both(_kalshi_milcin(("MIL3", 2.5)), [NEG])
    matches = match_games(kalshi, poly)
    assert len(matches) == 1
    group = match_to_event_group(matches[0])
    assert group.id == "mlb-CIN-MIL-2026-09-06-spread-MIL-2.5"
    yes = {leg.outcome_id for leg in group.legs if leg.is_yes_side}
    assert yes == {
        "KXMLBSPREAD-26SEP061210MILCIN-MIL3:YES",
        "asc-mlb-mil-cin-2026-09-06-neg-2pt5:LONG",
    }


async def test_opposite_anchors_on_the_same_line_never_pair_across_venues():
    """Kalshi MIL3 (Brewers by more than 2.5) against Polymarket pos-2pt5
    (Reds by more than 2.5) share a line and a game and are different bets."""
    kalshi, poly = await _both(_kalshi_milcin(("MIL3", 2.5)), [POS])
    assert match_games(kalshi, poly) == []


async def test_a_full_ladder_yields_one_group_per_shared_anchor_and_line():
    """Kalshi lists ±1.5/±2.5/±3.5 for MLB, Polymarket only ±1.5/±2.5; the
    intersection is what becomes groups. Observed 4 per MLB game live."""
    pm = [
        _pm_spread("asc-mlb-mil-cin-2026-09-06-neg-1pt5", -1.5, "Milwaukee Brewers wins by over 1.5 runs"),
        _pm_spread("asc-mlb-mil-cin-2026-09-06-neg-2pt5", -2.5, "Milwaukee Brewers wins by over 2.5 runs"),
        _pm_spread("asc-mlb-mil-cin-2026-09-06-pos-1pt5", 1.5, "Cincinnati Reds wins by over 1.5 runs"),
        _pm_spread("asc-mlb-mil-cin-2026-09-06-pos-2pt5", 2.5, "Cincinnati Reds wins by over 2.5 runs"),
    ]
    kalshi, poly = await _both(
        _kalshi_milcin(("MIL2", 1.5), ("MIL3", 2.5), ("MIL4", 3.5),
                       ("CIN2", 1.5), ("CIN3", 2.5), ("CIN4", 3.5)),
        pm,
    )
    ids = sorted(m.event_group_id() for m in match_games(kalshi, poly))
    assert ids == [
        "mlb-CIN-MIL-2026-09-06-spread-CIN-1.5",
        "mlb-CIN-MIL-2026-09-06-spread-CIN-2.5",
        "mlb-CIN-MIL-2026-09-06-spread-MIL-1.5",
        "mlb-CIN-MIL-2026-09-06-spread-MIL-2.5",
    ]
