from datetime import date

from arbys.discovery.kalshi_sports import VenueGame
from arbys.discovery.matcher import match_games, match_to_event_group
from arbys.discovery.teams import MLB_RESOLVER


def _game(venue: str, teams: tuple[str, str], date_str: str, ids: dict[str, str]) -> VenueGame:
    t1 = MLB_RESOLVER.by_code(teams[0])
    t2 = MLB_RESOLVER.by_code(teams[1])
    assert t1 is not None and t2 is not None
    y, m, d = map(int, date_str.split("-"))
    return VenueGame(
        sport="mlb",
        venue_id=venue,
        game_date=date(y, m, d),
        teams=(t1, t2),
        outcome_ids=ids,
        ref=f"{venue}-{teams[0]}-{teams[1]}-{date_str}",
    )


def test_match_games_finds_cross_venue_pair():
    kalshi = [
        _game(
            "kalshi",
            ("LAD", "CHC"),
            "2026-08-05",
            {"LAD": "K-LAD:YES", "CHC": "K-CHC:YES"},
        )
    ]
    poly = [
        _game(
            "polymarket",
            ("CHC", "LAD"),  # different team order — should still match
            "2026-08-05",
            {"LAD": "P-LAD", "CHC": "P-CHC"},
        )
    ]
    matches = match_games(kalshi, poly)
    assert len(matches) == 1
    m = matches[0]
    # Canonical alphabetical: CHC < LAD
    assert m.team_a.code == "CHC"
    assert m.team_b.code == "LAD"
    assert set(m.per_venue.keys()) == {"kalshi", "polymarket"}


def test_match_games_ignores_unpaired_games():
    kalshi = [
        _game(
            "kalshi",
            ("NYY", "BOS"),
            "2026-08-05",
            {"NYY": "K1", "BOS": "K2"},
        )
    ]
    poly = [
        _game(
            "polymarket",
            ("LAD", "CHC"),
            "2026-08-05",
            {"LAD": "P1", "CHC": "P2"},
        )
    ]
    assert match_games(kalshi, poly) == []


def test_match_games_date_mismatch_does_not_match():
    kalshi = [_game("kalshi", ("LAD", "CHC"), "2026-08-05", {"LAD": "K1", "CHC": "K2"})]
    poly = [_game("polymarket", ("LAD", "CHC"), "2026-08-06", {"LAD": "P1", "CHC": "P2"})]
    assert match_games(kalshi, poly) == []


def test_match_to_event_group_builds_four_legs():
    kalshi = _game("kalshi", ("LAD", "CHC"), "2026-08-05", {"LAD": "K-LAD:YES", "CHC": "K-CHC:YES"})
    poly = _game("polymarket", ("LAD", "CHC"), "2026-08-05", {"LAD": "P-LAD", "CHC": "P-CHC"})
    matches = match_games([kalshi], [poly])
    group = match_to_event_group(matches[0])
    assert group.id == "mlb-CHC-LAD-2026-08-05"
    assert len(group.legs) == 4
    # CHC is team_a → its legs are is_yes_side=True
    chc_legs = [leg for leg in group.legs if leg.outcome_id in {"K-CHC:YES", "P-CHC"}]
    lad_legs = [leg for leg in group.legs if leg.outcome_id in {"K-LAD:YES", "P-LAD"}]
    assert all(leg.is_yes_side is True for leg in chc_legs)
    assert all(leg.is_yes_side is False for leg in lad_legs)
    venues = {leg.venue_id for leg in group.legs}
    assert venues == {"kalshi", "polymarket"}
