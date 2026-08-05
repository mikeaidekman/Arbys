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
