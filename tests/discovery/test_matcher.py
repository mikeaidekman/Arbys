from dataclasses import replace
from datetime import UTC, date, datetime

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
            "polymarket_us",
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
    assert set(m.per_venue.keys()) == {"kalshi", "polymarket_us"}


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
            "polymarket_us",
            ("LAD", "CHC"),
            "2026-08-05",
            {"LAD": "P1", "CHC": "P2"},
        )
    ]
    assert match_games(kalshi, poly) == []


def test_match_games_date_mismatch_does_not_match():
    kalshi = [_game("kalshi", ("LAD", "CHC"), "2026-08-05", {"LAD": "K1", "CHC": "K2"})]
    poly = [_game("polymarket_us", ("LAD", "CHC"), "2026-08-06", {"LAD": "P1", "CHC": "P2"})]
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
        _game("polymarket_us", ("NYM", "ATL"), d, {"NYM": f"P-NYM-{d}", "ATL": f"P-ATL-{d}"})
        for d in ("2026-08-10", "2026-08-11", "2026-08-12")
    ]
    matches = match_games(kalshi, poly)
    assert len(matches) == 3
    assert [m.game_date for m in matches] == ["2026-08-10", "2026-08-11", "2026-08-12"]
    for m in matches:
        assert set(m.per_venue.keys()) == {"kalshi", "polymarket_us"}
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
        _game("polymarket_us", ("NYM", "ATL"), d, {"NYM": f"P-NYM-{d}", "ATL": f"P-ATL-{d}"})
        for d in ("2026-08-10", "2026-08-11")
    ]
    matches = match_games(kalshi, poly, date_tolerance_days=1)
    for m in matches:
        k = m.per_venue["kalshi"].game_date.isoformat()
        p = m.per_venue["polymarket_us"].game_date.isoformat()
        assert k == p, f"fused different games: kalshi={k} polymarket={p}"


def test_match_games_tolerance_still_bridges_offset_dates():
    """Tennis relies on tolerance: Kalshi's trading day can trail the UTC date."""
    kalshi = [_game("kalshi", ("LAD", "CHC"), "2026-08-05", {"LAD": "K1", "CHC": "K2"})]
    poly = [_game("polymarket_us", ("LAD", "CHC"), "2026-08-06", {"LAD": "P1", "CHC": "P2"})]
    matches = match_games(kalshi, poly, date_tolerance_days=1)
    assert len(matches) == 1
    assert set(matches[0].per_venue.keys()) == {"kalshi", "polymarket_us"}


def test_event_group_carries_earliest_venue_start_time():
    """The group's start_time comes from the venues, deterministically."""
    k = _game("kalshi", ("LAD", "CHC"), "2026-08-05", {"LAD": "K1", "CHC": "K2"})
    p = _game("polymarket_us", ("LAD", "CHC"), "2026-08-05", {"LAD": "P1", "CHC": "P2"})
    k = replace(k, start_time=datetime(2026, 8, 5, 18, 20, tzinfo=UTC))
    p = replace(p, start_time=datetime(2026, 8, 5, 18, 25, tzinfo=UTC))

    group = match_to_event_group(match_games([k], [p])[0])
    assert group.start_time == datetime(2026, 8, 5, 18, 20, tzinfo=UTC)


def test_event_group_start_time_none_when_no_venue_reports_one():
    k = _game("kalshi", ("LAD", "CHC"), "2026-08-05", {"LAD": "K1", "CHC": "K2"})
    p = _game("polymarket_us", ("LAD", "CHC"), "2026-08-05", {"LAD": "P1", "CHC": "P2"})
    group = match_to_event_group(match_games([k], [p])[0])
    assert group.start_time is None


def test_event_group_start_time_uses_the_venue_that_has_one():
    k = _game("kalshi", ("LAD", "CHC"), "2026-08-05", {"LAD": "K1", "CHC": "K2"})
    p = _game("polymarket_us", ("LAD", "CHC"), "2026-08-05", {"LAD": "P1", "CHC": "P2"})
    p = replace(p, start_time=datetime(2026, 8, 5, 23, 10, tzinfo=UTC))
    group = match_to_event_group(match_games([k], [p])[0])
    assert group.start_time == datetime(2026, 8, 5, 23, 10, tzinfo=UTC)


def test_match_to_event_group_builds_four_legs():
    kalshi = _game("kalshi", ("LAD", "CHC"), "2026-08-05", {"LAD": "K-LAD:YES", "CHC": "K-CHC:YES"})
    poly = _game("polymarket_us", ("LAD", "CHC"), "2026-08-05", {"LAD": "P-LAD", "CHC": "P-CHC"})
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
    assert venues == {"kalshi", "polymarket_us"}


def _dated(venue, date_str, start_iso, teams=("NYM", "ATL")):
    a = MLB_RESOLVER.by_code(teams[0])
    b = MLB_RESOLVER.by_code(teams[1])
    y, m, d = map(int, date_str.split("-"))
    return VenueGame(
        sport="mlb",
        venue_id=venue,
        game_date=date(y, m, d),
        teams=(a, b),
        outcome_ids={teams[0]: f"{venue}-a-{start_iso}", teams[1]: f"{venue}-b-{start_iso}"},
        ref=f"{venue}-{start_iso}",
        start_time=datetime.fromisoformat(start_iso),
    )


def test_matching_venues_disagree_on_date_but_agree_on_start_time():
    """The venues label dates differently; the start time is the truth.

    Kalshi's ticker carries a local trading day, Polymarket reports UTC, so a
    night game is Aug 11 on one and Aug 12 on the other. Same fixture.
    """
    k = _dated("kalshi", "2026-08-11", "2026-08-12T05:10:00+00:00")
    p = _dated("polymarket_us", "2026-08-12", "2026-08-12T05:10:00+00:00")
    matches = match_games([k], [p])
    assert len(matches) == 1
    assert set(matches[0].per_venue) == {"kalshi", "polymarket_us"}


def test_same_date_but_different_games_must_not_match():
    """The KC/LAD phantom: dates collide, fixtures are 27h apart.

    Kalshi's Aug 11 night game (Aug 12 05:10Z) and Polymarket's Aug 10 night
    game (Aug 11 02:10Z) both reduce to game_date 2026-08-11, so date matching
    paired Monday's game with Tuesday's and invented an arb between them.
    """
    k = _dated("kalshi", "2026-08-11", "2026-08-12T05:10:00+00:00")
    p = _dated("polymarket_us", "2026-08-11", "2026-08-11T02:10:00+00:00")
    assert match_games([k], [p]) == [], "paired two different games in a series"


def test_doubleheader_legs_are_kept_apart():
    """Two games the same day, ~3h apart, are still different games."""
    k1 = _dated("kalshi", "2026-08-11", "2026-08-11T17:10:00+00:00")
    k2 = _dated("kalshi", "2026-08-11", "2026-08-11T21:40:00+00:00")
    p1 = _dated("polymarket_us", "2026-08-11", "2026-08-11T17:10:00+00:00")
    p2 = _dated("polymarket_us", "2026-08-11", "2026-08-11T21:40:00+00:00")
    matches = match_games([k1, k2], [p1, p2])
    assert len(matches) == 2
    for m in matches:
        k = m.per_venue["kalshi"].start_time
        p = m.per_venue["polymarket_us"].start_time
        assert k == p, f"fused different halves of a doubleheader: {k} vs {p}"


def test_falls_back_to_dates_when_a_venue_reports_no_start_time():
    """Tennis and hand-built groups may have no start time; still match."""
    k = _dated("kalshi", "2026-08-11", "2026-08-11T17:10:00+00:00")
    k = replace(k, start_time=None)
    p = _dated("polymarket_us", "2026-08-11", "2026-08-11T17:10:00+00:00")
    p = replace(p, start_time=None)
    assert len(match_games([k], [p])) == 1


def test_anchor_defaults_to_none_and_preserves_today_behaviour():
    """Phase 1 ships no market type that sets an anchor, so two games that
    matched before must still match, with anchor left None."""
    kalshi = [_game("kalshi", ("LAD", "CHC"), "2026-08-05", {"LAD": "K1", "CHC": "K2"})]
    poly = [_game("polymarket_us", ("LAD", "CHC"), "2026-08-05", {"LAD": "P1", "CHC": "P2"})]
    matches = match_games(kalshi, poly)
    assert len(matches) == 1
    assert matches[0].anchor is None


def _spread(venue: str, anchor: str, line: str) -> VenueGame:
    """A spread game anchored on one team. Phase 2 shape, exercised now so
    the bucket key is proven before anything depends on it."""
    from decimal import Decimal

    base = _game(venue, ("LAD", "CHC"), "2026-08-05", {"LAD": f"{venue}-L", "CHC": f"{venue}-C"})
    return replace(base, market_type="spread", line=Decimal(line), anchor=anchor)


def test_same_anchor_and_line_still_matches_across_venues():
    matches = match_games([_spread("kalshi", "LAD", "-2.5")], [_spread("polymarket_us", "LAD", "-2.5")])
    assert len(matches) == 1
    assert matches[0].anchor == "LAD"


def test_opposite_anchors_on_the_same_line_do_not_match():
    """The guard Phase 2 depends on. LAD -2.5 and CHC -2.5 are different
    bets; without the anchor in the bucket key they would pair and invent a
    guaranteed profit that does not exist."""
    assert match_games([_spread("kalshi", "LAD", "-2.5")], [_spread("polymarket_us", "CHC", "-2.5")]) == []


def test_same_anchor_different_lines_do_not_match():
    """Already true for totals; asserted here for spreads too."""
    assert match_games([_spread("kalshi", "LAD", "-2.5")], [_spread("polymarket_us", "LAD", "-1.5")]) == []
