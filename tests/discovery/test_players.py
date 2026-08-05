from arbys.discovery.players import Player, parse_vs_title, strip_prefix


def test_parse_vs_title_polymarket_form():
    parsed = parse_vs_title("Miomir Kecmanovic vs Arthur Rinderknech")
    assert parsed is not None
    a, b = parsed
    assert a.code == "KECMANOVIC"
    assert b.code == "RINDERKNECH"
    assert a.full_name == "Miomir Kecmanovic"


def test_parse_vs_title_kalshi_last_name_only():
    parsed = parse_vs_title("Kecmanovic vs Rinderknech")
    assert parsed is not None
    a, b = parsed
    assert {a.code, b.code} == {"KECMANOVIC", "RINDERKNECH"}


def test_parse_vs_title_dot_form():
    parsed = parse_vs_title("Ana Ivanovic vs. Serena Williams")
    assert parsed is not None
    a, b = parsed
    assert {a.code, b.code} == {"IVANOVIC", "WILLIAMS"}


def test_parse_vs_title_strips_diacritics():
    parsed = parse_vs_title("Novak Đoković vs Rafael Nadal")
    assert parsed is not None
    codes = {p.code for p in parsed}
    assert "DOKOVIC" in codes
    assert "NADAL" in codes


def test_parse_vs_title_rejects_same_last_name():
    # Prevents matching a hypothetical "Williams vs Williams" as a valid pair.
    assert parse_vs_title("Venus Williams vs Serena Williams") is None


def test_parse_vs_title_rejects_missing_separator():
    assert parse_vs_title("Just one player") is None
    assert parse_vs_title("") is None


def test_strip_prefix_removes_tournament_context():
    assert strip_prefix("National Bank Open: Kasatkina vs Rybakina") == "Kasatkina vs Rybakina"
    assert strip_prefix("Kasatkina vs Rybakina") == "Kasatkina vs Rybakina"


def test_player_last_name_property():
    p = Player(code="ALCARAZ", full_name="Carlos Alcaraz")
    assert p.last_name == "Alcaraz"
