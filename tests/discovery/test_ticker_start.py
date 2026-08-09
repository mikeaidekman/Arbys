"""Kalshi's ticker carries the true first pitch, in Eastern.

The market's ``occurrence_datetime`` is the expected *settlement* time, about
three hours later. Using it made every Kalshi game look three hours offset
from its Polymarket counterpart -- so cross-venue matching on start time found
nothing, and card countdowns ran three hours late.

Verified against live data on 2026-08-09:
  KXMLBGAME-26AUG102210KCLAD  ticker -> 02:10Z, polymarket gameStartTime 02:10Z
                              occurrence_datetime was 05:10Z
  KXMLBGAME-26AUG081505ATLNYY ticker -> 19:05Z, rules say "3:05 PM EDT"
                              occurrence_datetime was 22:05Z
"""

from datetime import UTC, datetime

from arbys.discovery.kalshi_sports import parse_ticker_start


def test_evening_game_crosses_into_the_next_utc_day():
    # Aug 10, 22:10 ET == Aug 11, 02:10 UTC — matches Polymarket exactly.
    assert parse_ticker_start("KXMLBGAME-26AUG102210KCLAD") == datetime(
        2026, 8, 11, 2, 10, tzinfo=UTC
    )


def test_afternoon_game_stays_on_the_same_utc_day():
    # Kalshi's rules text for this market says "3:05 PM EDT".
    assert parse_ticker_start("KXMLBGAME-26AUG081505ATLNYY") == datetime(
        2026, 8, 8, 19, 5, tzinfo=UTC
    )


def test_consecutive_series_games_stay_a_day_apart():
    a = parse_ticker_start("KXMLBGAME-26AUG102210KCLAD")
    b = parse_ticker_start("KXMLBGAME-26AUG112210KCLAD")
    assert (b - a).total_seconds() == 86400


def test_totals_ticker_uses_the_same_stem():
    assert parse_ticker_start("KXMLBTOTAL-26AUG091215CINWSH") == datetime(
        2026, 8, 9, 16, 15, tzinfo=UTC
    )


def test_ticker_without_a_time_component_yields_none():
    # NFL game tickers carry the date only; fall back to date matching there.
    assert parse_ticker_start("KXNFLGAME-26AUG13DETCIN") is None


def test_malformed_tickers_are_rejected():
    assert parse_ticker_start("") is None
    assert parse_ticker_start("NODASH") is None
    assert parse_ticker_start("KXMLBGAME-26XXX102210KCLAD") is None
