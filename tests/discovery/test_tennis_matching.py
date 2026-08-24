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


# ---------------------------------------------------------------------------
# UFC rides the same individual-sport path
# ---------------------------------------------------------------------------

def test_ufc_registries_are_distinct_from_tennis():
    """UFC reuses the tennis fetches by parameter, not by copy. The defaults
    must stay tennis so nothing about the existing path shifts."""
    from arbys.discovery.kalshi_tennis import TENNIS_SERIES, UFC_SERIES
    from arbys.discovery.polymarket_us import (
        TENNIS_LEAGUES,
        TENNIS_WINNER_TYPES,
        UFC_LEAGUES,
        UFC_WINNER_TYPES,
    )

    assert UFC_SERIES == (("ufc", "KXUFCFIGHT"),)
    assert UFC_LEAGUES == ("ufc",)
    # Both type labels are live: 49 events typed ufc_fight_winner and 5 still
    # on the older generic "moneyline" on 2026-08-24.
    assert "ufc_fight_winner" in UFC_WINNER_TYPES
    assert "moneyline" in UFC_WINNER_TYPES
    # Tennis untouched.
    assert TENNIS_SERIES == (("atp", "KXATPMATCH"), ("wta", "KXWTAMATCH"))
    assert TENNIS_LEAGUES == ("atp", "wta")
    assert "tennis_match_winner" in TENNIS_WINNER_TYPES
    assert len(TENNIS_WINNER_TYPES) == 1


def test_kalshi_title_prefix_does_not_break_competitor_codes():
    """UFC event titles carry a card prefix that tennis titles do not:
    "Fight Night: Meng vs Nelson". Competitor codes are the ASCII uppercase
    last token, so the prefix is absorbed without a special case — which is
    the reason UFC needed no new parser."""
    from arbys.discovery.players import parse_vs_title

    parsed = parse_vs_title("Fight Night: Meng vs Nelson")
    assert parsed is not None
    a, b = parsed
    assert a.code == "MENG"
    assert b.code == "NELSON"


def test_venue_disagreement_on_a_name_drops_rather_than_mispairs():
    """The known UFC limitation, pinned so it stays a *drop*.

    Kalshi rendered a fighter "Xiong Jing Nan" where Polymarket sent
    "Xiong Jingnan" (2026-08-24). Last-token coding gives NAN and JINGNAN, so
    the fight does not match and is skipped. That costs an opportunity but
    cannot invent one, which is what makes the limitation acceptable to ship.
    """
    from arbys.discovery.players import last_name_code

    assert last_name_code("Xiong Jing Nan") != last_name_code("Xiong Jingnan")
    # A name both venues render alike still matches.
    assert last_name_code("Jack Jenkins") == last_name_code("Jack Jenkins")
    # And the multi-surname case does match, because both reduce to the tail.
    assert last_name_code("Hector de Sousa Santiago") == last_name_code("Hector Santiago")
