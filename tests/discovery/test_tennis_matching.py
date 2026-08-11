from datetime import date

from arbys.discovery.kalshi_sports import VenueGame
from arbys.discovery.matcher import match_games, match_to_event_group
from arbys.discovery.players import Player


def _game(venue: str, players: tuple[str, str], date_str: str, ids: dict[str, str]) -> VenueGame:
    p_a = Player(code=players[0], full_name=players[0].title())
    p_b = Player(code=players[1], full_name=players[1].title())
    y, m, d = map(int, date_str.split("-"))
    return VenueGame(
        sport="atp",
        venue_id=venue,
        game_date=date(y, m, d),
        teams=(p_a, p_b),
        outcome_ids=ids,
        ref=f"{venue}-{players[0]}-{players[1]}-{date_str}",
    )


def test_match_games_with_date_tolerance_matches_off_by_one_day():
    """Kalshi tennis tickers may be off by 1 day from Polymarket's UTC date."""
    kalshi = [
        _game("kalshi", ("KECMANOVIC", "RINDERKNECH"), "2026-08-04", {"KECMANOVIC": "K1:YES", "RINDERKNECH": "K2:YES"})
    ]
    poly = [
        _game("polymarket_us", ("KECMANOVIC", "RINDERKNECH"), "2026-08-05", {"KECMANOVIC": "P1", "RINDERKNECH": "P2"})
    ]

    strict = match_games(kalshi, poly, date_tolerance_days=0)
    assert strict == []

    lenient = match_games(kalshi, poly, date_tolerance_days=1)
    assert len(lenient) == 1
    m = lenient[0]
    assert set(m.per_venue.keys()) == {"kalshi", "polymarket_us"}


def test_match_games_tolerance_still_rejects_two_day_gap():
    kalshi = [_game("kalshi", ("KECMANOVIC", "RINDERKNECH"), "2026-08-04", {"KECMANOVIC": "K1", "RINDERKNECH": "K2"})]
    poly = [_game("polymarket_us", ("KECMANOVIC", "RINDERKNECH"), "2026-08-06", {"KECMANOVIC": "P1", "RINDERKNECH": "P2"})]
    assert match_games(kalshi, poly, date_tolerance_days=1) == []


def test_tennis_match_to_event_group_uses_alphabetical_canonical():
    kalshi = _game("kalshi", ("KECMANOVIC", "RINDERKNECH"), "2026-08-04", {"KECMANOVIC": "K1:YES", "RINDERKNECH": "K2:YES"})
    poly = _game("polymarket_us", ("KECMANOVIC", "RINDERKNECH"), "2026-08-05", {"KECMANOVIC": "P1", "RINDERKNECH": "P2"})
    match = match_games([kalshi], [poly], date_tolerance_days=1)[0]
    group = match_to_event_group(match)
    assert group.id.startswith("atp-KECMANOVIC-RINDERKNECH-")
    assert len(group.legs) == 4
    kec_legs = [leg for leg in group.legs if leg.outcome_id in {"K1:YES", "P1"}]
    assert all(leg.is_yes_side for leg in kec_legs)
