from datetime import date

from arbys.discovery.horizon import (
    DEFAULT_HORIZON_DAYS,
    discovery_horizon_days,
    filter_horizon,
    within_horizon,
)
from arbys.discovery.kalshi_sports import VenueGame
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
