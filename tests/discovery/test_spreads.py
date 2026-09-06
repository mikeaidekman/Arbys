"""Spreads: both venue parsers and the cross-venue convention.

Every fixture is modelled on payloads observed live on 2026-09-06; see
docs/superpowers/specs/2026-09-06-spreads-and-discovery-horizon-design.md.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from arbys.discovery.kalshi_spreads import anchor_code_from_ticker, fetch_kalshi_spreads
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
