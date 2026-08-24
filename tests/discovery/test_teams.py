from arbys.discovery.teams import MLB_RESOLVER


def test_by_code_case_insensitive():
    assert MLB_RESOLVER.by_code("lad").full_name == "Los Angeles Dodgers"
    assert MLB_RESOLVER.by_code("LAD").code == "LAD"


def test_by_kalshi_title_los_angeles_dodgers():
    t = MLB_RESOLVER.by_kalshi_title("Los Angeles D")
    assert t is not None and t.code == "LAD"


def test_by_kalshi_title_los_angeles_angels():
    t = MLB_RESOLVER.by_kalshi_title("Los Angeles A")
    assert t is not None and t.code == "LAA"


def test_by_kalshi_title_new_york_yankees_vs_mets():
    assert MLB_RESOLVER.by_kalshi_title("New York Y").code == "NYY"
    assert MLB_RESOLVER.by_kalshi_title("New York M").code == "NYM"


def test_by_kalshi_title_chicago_cubs_vs_white_sox():
    assert MLB_RESOLVER.by_kalshi_title("Chicago C").code == "CHC"
    assert MLB_RESOLVER.by_kalshi_title("Chicago W").code == "CWS"


def test_by_kalshi_title_unknown_returns_none():
    assert MLB_RESOLVER.by_kalshi_title("Nowhere X") is None
    assert MLB_RESOLVER.by_kalshi_title("") is None


# Kalshi only appends the truncated-nickname letter when a city fields more
# than one team. For the other 24 cities it sends the bare city name, which
# used to resolve to None and silently drop the whole game from discovery.
def test_by_kalshi_title_bare_city_single_word():
    assert MLB_RESOLVER.by_kalshi_title("Atlanta").code == "ATL"
    assert MLB_RESOLVER.by_kalshi_title("Boston").code == "BOS"
    assert MLB_RESOLVER.by_kalshi_title("Miami").code == "MIA"
    assert MLB_RESOLVER.by_kalshi_title("Toronto").code == "TOR"


def test_by_kalshi_title_bare_city_multi_word():
    # "Kansas City" must not be read as city="Kansas" + nickname="C".
    assert MLB_RESOLVER.by_kalshi_title("Kansas City").code == "KC"
    assert MLB_RESOLVER.by_kalshi_title("San Diego").code == "SD"
    assert MLB_RESOLVER.by_kalshi_title("Tampa Bay").code == "TB"
    assert MLB_RESOLVER.by_kalshi_title("St. Louis").code == "STL"


def test_by_kalshi_title_athletics_alias():
    # Kalshi labels the Athletics "A's"; they have no city-qualified form.
    assert MLB_RESOLVER.by_kalshi_title("A's").code == "ATH"
    assert MLB_RESOLVER.by_kalshi_title("Athletics").code == "ATH"


def test_by_kalshi_title_shared_city_stays_ambiguous():
    # A bare shared city is genuinely ambiguous and must not guess a team.
    assert MLB_RESOLVER.by_kalshi_title("Chicago") is None
    assert MLB_RESOLVER.by_kalshi_title("New York") is None
    assert MLB_RESOLVER.by_kalshi_title("Los Angeles") is None


def test_by_polymarket_name_full_form():
    assert MLB_RESOLVER.by_polymarket_name("Los Angeles Dodgers").code == "LAD"
    assert MLB_RESOLVER.by_polymarket_name("chicago cubs").code == "CHC"


def test_parse_vs_question_dot_form():
    result = MLB_RESOLVER.parse_vs_question("Los Angeles Dodgers vs. Chicago Cubs")
    assert result is not None
    a, b = result
    assert {a.code, b.code} == {"LAD", "CHC"}


def test_parse_vs_question_no_dot_form():
    result = MLB_RESOLVER.parse_vs_question("Atlanta Braves vs New York Yankees")
    assert result is not None
    assert {t.code for t in result} == {"ATL", "NYY"}


def test_parse_vs_question_unknown_team_returns_none():
    assert MLB_RESOLVER.parse_vs_question("Springfield Isotopes vs New York Yankees") is None


def test_parse_vs_question_no_separator_returns_none():
    assert MLB_RESOLVER.parse_vs_question("Los Angeles Dodgers") is None


# ---------------------------------------------------------------------------
# Polymarket US name resolution: the field's contents vary by league
# ---------------------------------------------------------------------------

def test_polymarket_name_resolves_a_bare_city():
    """Polymarket US sends a bare city for WNBA ("Golden State"), not the full
    name it sends for NFL and MLB. Without this every WNBA game failed to
    resolve and the league discovered zero groups while both venues quoted it.
    """
    from arbys.discovery.teams import WNBA_RESOLVER

    team = WNBA_RESOLVER.by_polymarket_name("Golden State")
    assert team is not None
    assert team.code == "GS"
    assert WNBA_RESOLVER.by_polymarket_name("Minnesota").code == "MIN"


def test_polymarket_name_still_resolves_a_full_name():
    """The NFL/MLB shape must keep working — full name takes precedence."""
    from arbys.discovery.teams import NFL_RESOLVER

    assert NFL_RESOLVER.by_polymarket_name("Arizona Cardinals").code == "ARI"


def test_polymarket_name_refuses_a_shared_city():
    """The bare-city fallback reuses by_kalshi_title's uniqueness rule, so a
    city fielding two teams stays unresolved rather than collapsing into one.
    NFL has two Los Angeles teams and MLB two in Chicago."""
    from arbys.discovery.teams import MLB_RESOLVER, NFL_RESOLVER

    assert NFL_RESOLVER.by_polymarket_name("Los Angeles") is None
    assert MLB_RESOLVER.by_polymarket_name("Chicago") is None


def test_ambiguous_nickname_is_not_an_identity():
    """A bare nickname only identifies a team where it is unique in the league.

    Pro nicknames are distinct, but college mascots are not: 28 repeat across
    the 88 Polymarket US CFB games observed on 2026-08-24. Indexing them
    unconditionally let the last-inserted school win, which resolves one
    school's market to another's code and invents an arb between two
    different fixtures.
    """
    from arbys.discovery.teams import Team, TeamResolver

    resolver = TeamResolver(
        (
            Team("MEM", "Memphis Tigers", "Memphis", "Tigers"),
            Team("AUB", "Auburn Tigers", "Auburn", "Tigers"),
            Team("UNC", "North Carolina Tar Heels", "North Carolina", "Tar Heels"),
        )
    )
    # Shared mascot: unresolvable rather than an arbitrary winner.
    assert resolver.by_polymarket_name("Tigers") is None
    # A unique mascot still resolves.
    assert resolver.by_polymarket_name("Tar Heels").code == "UNC"
    # Qualified forms stay available for the ambiguous ones.
    assert resolver.by_polymarket_name("Memphis Tigers").code == "MEM"
    assert resolver.by_polymarket_name("Auburn Tigers").code == "AUB"


def test_unique_nickname_still_resolves_for_pro_leagues():
    from arbys.discovery.teams import MLB_RESOLVER

    assert MLB_RESOLVER.by_polymarket_name("Dodgers").code == "LAD"
