"""Team-name mapping for auto-discovery matching.

Maps between the different team representations used by each venue:
- Kalshi ticker codes (2-4 letters): "LAD", "CHC", "ATH"
- Kalshi human titles: "Los Angeles D", "Chicago C"
- Polymarket questions: "Los Angeles Dodgers", "Chicago Cubs"

All normalizations produce the same canonical short code (Kalshi's, since
they are the most compact and unambiguous).

Extending to another sport: add another mapping dict + register it in
``TEAM_MAPS``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Team:
    code: str
    full_name: str
    city: str
    nickname: str


# Kalshi MLB team codes and canonical full names, verified against
# api.elections.kalshi.com game events on 2026-08-05.
MLB_TEAMS: tuple[Team, ...] = (
    Team("ARI", "Arizona Diamondbacks", "Arizona", "Diamondbacks"),
    Team("ATL", "Atlanta Braves", "Atlanta", "Braves"),
    Team("BAL", "Baltimore Orioles", "Baltimore", "Orioles"),
    Team("BOS", "Boston Red Sox", "Boston", "Red Sox"),
    Team("CHC", "Chicago Cubs", "Chicago", "Cubs"),
    Team("CIN", "Cincinnati Reds", "Cincinnati", "Reds"),
    Team("CLE", "Cleveland Guardians", "Cleveland", "Guardians"),
    Team("COL", "Colorado Rockies", "Colorado", "Rockies"),
    Team("CWS", "Chicago White Sox", "Chicago", "White Sox"),
    Team("DET", "Detroit Tigers", "Detroit", "Tigers"),
    Team("HOU", "Houston Astros", "Houston", "Astros"),
    Team("KC", "Kansas City Royals", "Kansas City", "Royals"),
    Team("LAA", "Los Angeles Angels", "Los Angeles", "Angels"),
    Team("LAD", "Los Angeles Dodgers", "Los Angeles", "Dodgers"),
    Team("MIA", "Miami Marlins", "Miami", "Marlins"),
    Team("MIL", "Milwaukee Brewers", "Milwaukee", "Brewers"),
    Team("MIN", "Minnesota Twins", "Minnesota", "Twins"),
    Team("NYM", "New York Mets", "New York", "Mets"),
    Team("NYY", "New York Yankees", "New York", "Yankees"),
    Team("ATH", "Athletics", "Oakland", "Athletics"),  # Kalshi uses "ATH" post-2024
    Team("PHI", "Philadelphia Phillies", "Philadelphia", "Phillies"),
    Team("PIT", "Pittsburgh Pirates", "Pittsburgh", "Pirates"),
    Team("SD", "San Diego Padres", "San Diego", "Padres"),
    Team("SEA", "Seattle Mariners", "Seattle", "Mariners"),
    Team("SF", "San Francisco Giants", "San Francisco", "Giants"),
    Team("STL", "St. Louis Cardinals", "St. Louis", "Cardinals"),
    Team("TB", "Tampa Bay Rays", "Tampa Bay", "Rays"),
    Team("TEX", "Texas Rangers", "Texas", "Rangers"),
    Team("TOR", "Toronto Blue Jays", "Toronto", "Blue Jays"),
    Team("WSH", "Washington Nationals", "Washington", "Nationals"),
)


class TeamResolver:
    """Bidirectional team code <-> name resolver, case- and punctuation-tolerant.

    Kalshi's abbreviated ``title`` field looks like "Los Angeles D" or
    "Chicago C" — the last word is a truncated nickname. We match on
    (city, first_letter_of_nickname) to recover the code, which is more
    robust than trying to expand the truncation.
    """

    def __init__(self, teams: tuple[Team, ...]) -> None:
        self._by_code: dict[str, Team] = {t.code.upper(): t for t in teams}
        # (city_lower, nickname_first_letter) -> code. Handles collisions
        # (Chicago C/W, Los Angeles D/A, New York M/Y) via nickname prefix.
        self._by_city_and_prefix: dict[tuple[str, str], Team] = {}
        for t in teams:
            key = (t.city.lower(), t.nickname[0].upper())
            self._by_city_and_prefix[key] = t
        # Full name lower -> team, plus common alias forms.
        self._by_full: dict[str, Team] = {t.full_name.lower(): t for t in teams}
        for t in teams:
            self._by_full[f"{t.city} {t.nickname}".lower()] = t
            self._by_full[t.nickname.lower()] = t

    def by_code(self, code: str) -> Team | None:
        return self._by_code.get(code.upper())

    def by_kalshi_title(self, title: str) -> Team | None:
        """Resolve a title like "Los Angeles D" or "New York Y" to a Team."""
        t = title.strip()
        if not t:
            return None
        # Direct full-name / nickname match first.
        team = self._by_full.get(t.lower())
        if team is not None:
            return team
        # Split into city and truncated nickname.
        parts = t.rsplit(" ", 1)
        if len(parts) != 2:
            return None
        city_part, nick_part = parts
        return self._by_city_and_prefix.get((city_part.lower(), nick_part[0].upper()))

    def by_polymarket_name(self, name: str) -> Team | None:
        """Resolve a Polymarket outcome or question fragment like
        "Los Angeles Dodgers" or "Chicago Cubs"."""
        n = name.strip().lower()
        return self._by_full.get(n)

    def parse_vs_question(self, question: str) -> tuple[Team, Team] | None:
        """Parse a question like "Los Angeles Dodgers vs. Chicago Cubs"."""
        q = question.strip()
        for sep in (" vs. ", " vs ", " @ ", " at "):
            if sep in q:
                left, right = q.split(sep, 1)
                a = self.by_polymarket_name(left)
                b = self.by_polymarket_name(right)
                if a is not None and b is not None:
                    return a, b
                return None
        return None


MLB_RESOLVER = TeamResolver(MLB_TEAMS)


TEAM_MAPS = {"mlb": MLB_RESOLVER}
