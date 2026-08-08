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


def test_match_games_matches_every_game_of_a_series():
    """MLB plays the same pair on consecutive days; each game is its own group.

    Bucketing by (sport, pair) alone collapsed a 3-game series to a single
    match anchored on the earliest date, silently dropping the rest.
    """
    kalshi = [
        _game("kalshi", ("NYM", "ATL"), d, {"NYM": f"K-NYM-{d}", "ATL": f"K-ATL-{d}"})
        for d in ("2026-08-10", "2026-08-11", "2026-08-12")
    ]
    poly = [
        _game("polymarket", ("NYM", "ATL"), d, {"NYM": f"P-NYM-{d}", "ATL": f"P-ATL-{d}"})
        for d in ("2026-08-10", "2026-08-11", "2026-08-12")
    ]
    matches = match_games(kalshi, poly)
    assert len(matches) == 3
    assert [m.game_date for m in matches] == ["2026-08-10", "2026-08-11", "2026-08-12"]
    for m in matches:
        assert set(m.per_venue.keys()) == {"kalshi", "polymarket"}
        # Each match must carry that date's own outcome ids, not another game's.
        for venue, g in m.per_venue.items():
            assert all(m.game_date in oid for oid in g.outcome_ids.values()), (venue, g)


def test_match_games_tolerance_does_not_fuse_consecutive_series_games():
    """With tolerance, a venue must still pair with its own-date counterpart.

    Fusing Monday's Kalshi game with Tuesday's Polymarket game would invent an
    arb between two different games.
    """
    kalshi = [
        _game("kalshi", ("NYM", "ATL"), d, {"NYM": f"K-NYM-{d}", "ATL": f"K-ATL-{d}"})
        for d in ("2026-08-10", "2026-08-11")
    ]
    poly = [
        _game("polymarket", ("NYM", "ATL"), d, {"NYM": f"P-NYM-{d}", "ATL": f"P-ATL-{d}"})
        for d in ("2026-08-10", "2026-08-11")
    ]
    matches = match_games(kalshi, poly, date_tolerance_days=1)
    for m in matches:
        k = m.per_venue["kalshi"].game_date.isoformat()
        p = m.per_venue["polymarket"].game_date.isoformat()
        assert k == p, f"fused different games: kalshi={k} polymarket={p}"


def test_match_games_tolerance_still_bridges_offset_dates():
    """Tennis relies on tolerance: Kalshi's trading day can trail the UTC date."""
    kalshi = [_game("kalshi", ("LAD", "CHC"), "2026-08-05", {"LAD": "K1", "CHC": "K2"})]
    poly = [_game("polymarket", ("LAD", "CHC"), "2026-08-06", {"LAD": "P1", "CHC": "P2"})]
    matches = match_games(kalshi, poly, date_tolerance_days=1)
    assert len(matches) == 1
    assert set(matches[0].per_venue.keys()) == {"kalshi", "polymarket"}


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
