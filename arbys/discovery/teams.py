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

    Kalshi only disambiguates when it has to. A city fielding two teams comes
    through as "Los Angeles D" / "Chicago C" — city plus the nickname's first
    letter. Every other city arrives bare: "Atlanta", "Kansas City". Both forms
    have to resolve, and a bare *shared* city ("Chicago") must stay unresolved
    rather than guess.
    """

    def __init__(
        self, teams: tuple[Team, ...], aliases: dict[str, str] | None = None
    ) -> None:
        self._by_code: dict[str, Team] = {t.code.upper(): t for t in teams}
        # (city_lower, nickname_first_letter) -> code. Handles collisions
        # (Chicago C/W, Los Angeles D/A, New York M/Y) via nickname prefix.
        self._by_city_and_prefix: dict[tuple[str, str], Team] = {}
        for t in teams:
            key = (t.city.lower(), t.nickname[0].upper())
            self._by_city_and_prefix[key] = t
        # Full name lower -> team, plus common alias forms.
        self._by_full: dict[str, Team] = {t.full_name.lower(): t for t in teams}
        # A bare nickname is only an identity where it is unique in the league.
        # Pro leagues have distinct nicknames, but college does not: across the
        # 88 Polymarket US CFB games observed on 2026-08-24, 28 mascots repeat —
        # "Tigers" seven times, "Wildcats" six, "Bulldogs" five. Indexing those
        # unconditionally made the last-inserted school win, silently resolving
        # one school's market to another's code, which is how a matcher invents
        # an arb between two different fixtures.
        nickname_counts: dict[str, int] = {}
        for t in teams:
            key = t.nickname.lower()
            nickname_counts[key] = nickname_counts.get(key, 0) + 1
        for t in teams:
            self._by_full[f"{t.city} {t.nickname}".lower()] = t
            if nickname_counts[t.nickname.lower()] == 1:
                self._by_full[t.nickname.lower()] = t
        # Bare city -> team, but only where the city fields exactly one team.
        # Shared cities are deliberately absent so they resolve to None.
        city_counts: dict[str, int] = {}
        for t in teams:
            city_counts[t.city.lower()] = city_counts.get(t.city.lower(), 0) + 1
        self._by_city_unique: dict[str, Team] = {
            t.city.lower(): t for t in teams if city_counts[t.city.lower()] == 1
        }
        for alias, code in (aliases or {}).items():
            team = self._by_code.get(code.upper())
            if team is not None:
                self._by_full[alias.strip().lower()] = team

    def by_code(self, code: str) -> Team | None:
        return self._by_code.get(code.upper())

    def by_kalshi_title(self, title: str) -> Team | None:
        """Resolve a title like "Los Angeles D", "New York Y", or "Atlanta"."""
        t = title.strip()
        if not t:
            return None
        # Direct full-name / nickname / alias match first.
        team = self._by_full.get(t.lower())
        if team is not None:
            return team
        # City plus truncated nickname, e.g. "New York Y".
        parts = t.rsplit(" ", 1)
        if len(parts) == 2:
            city_part, nick_part = parts
            team = self._by_city_and_prefix.get((city_part.lower(), nick_part[0].upper()))
            if team is not None:
                return team
        # Bare city, e.g. "Atlanta" or "Kansas City". Only resolves when that
        # city fields a single team, so "Chicago" alone stays None.
        return self._by_city_unique.get(t.lower())

    def by_polymarket_name(self, name: str) -> Team | None:
        """Resolve a Polymarket ``teams[].name`` like "Los Angeles Dodgers",
        "Chicago Cubs", or a bare city.

        What that field holds varies by league, which is not obvious from the
        NFL and MLB cases the first port was built against. Verified live on
        2026-08-24 against Polymarket US:

        * NFL / MLB -> full name, "Arizona Cardinals"
        * WNBA      -> bare city, "Golden State"
        * CFB       -> mascot only, "Tar Heels"

        Without the bare-city fallback below, every WNBA game failed to
        resolve and the league discovered zero groups while both venues were
        quoting it.

        The fallback reuses the same uniqueness rule as ``by_kalshi_title``: a
        shared city stays unresolved, so NFL's two "Los Angeles" teams cannot
        be silently collapsed into one.
        """
        n = name.strip().lower()
        team = self._by_full.get(n)
        if team is not None:
            return team
        return self._by_city_unique.get(n)

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


# Venue-specific labels that match no city/nickname rule.
MLB_ALIASES = {
    "a's": "ATH",
    "as": "ATH",
    "oakland athletics": "ATH",
}

MLB_RESOLVER = TeamResolver(MLB_TEAMS, aliases=MLB_ALIASES)


# Kalshi NFL codes and titles, verified against KXNFLGAME events on 2026-08-08.
# Kalshi sends the bare city ("Detroit", "Green Bay") except for the two shared
# cities, which arrive as "Los Angeles C" / "New York G".
NFL_TEAMS: tuple[Team, ...] = (
    Team("ARI", "Arizona Cardinals", "Arizona", "Cardinals"),
    Team("ATL", "Atlanta Falcons", "Atlanta", "Falcons"),
    Team("BAL", "Baltimore Ravens", "Baltimore", "Ravens"),
    Team("BUF", "Buffalo Bills", "Buffalo", "Bills"),
    Team("CAR", "Carolina Panthers", "Carolina", "Panthers"),
    Team("CHI", "Chicago Bears", "Chicago", "Bears"),
    Team("CIN", "Cincinnati Bengals", "Cincinnati", "Bengals"),
    Team("CLE", "Cleveland Browns", "Cleveland", "Browns"),
    Team("DAL", "Dallas Cowboys", "Dallas", "Cowboys"),
    Team("DEN", "Denver Broncos", "Denver", "Broncos"),
    Team("DET", "Detroit Lions", "Detroit", "Lions"),
    Team("GB", "Green Bay Packers", "Green Bay", "Packers"),
    Team("HOU", "Houston Texans", "Houston", "Texans"),
    Team("IND", "Indianapolis Colts", "Indianapolis", "Colts"),
    Team("JAX", "Jacksonville Jaguars", "Jacksonville", "Jaguars"),
    Team("KC", "Kansas City Chiefs", "Kansas City", "Chiefs"),
    Team("LV", "Las Vegas Raiders", "Las Vegas", "Raiders"),
    Team("LAC", "Los Angeles Chargers", "Los Angeles", "Chargers"),
    Team("LAR", "Los Angeles Rams", "Los Angeles", "Rams"),
    Team("MIA", "Miami Dolphins", "Miami", "Dolphins"),
    Team("MIN", "Minnesota Vikings", "Minnesota", "Vikings"),
    Team("NE", "New England Patriots", "New England", "Patriots"),
    Team("NO", "New Orleans Saints", "New Orleans", "Saints"),
    Team("NYG", "New York Giants", "New York", "Giants"),
    Team("NYJ", "New York Jets", "New York", "Jets"),
    Team("PHI", "Philadelphia Eagles", "Philadelphia", "Eagles"),
    Team("PIT", "Pittsburgh Steelers", "Pittsburgh", "Steelers"),
    Team("SF", "San Francisco 49ers", "San Francisco", "49ers"),
    Team("SEA", "Seattle Seahawks", "Seattle", "Seahawks"),
    Team("TB", "Tampa Bay Buccaneers", "Tampa Bay", "Buccaneers"),
    Team("TEN", "Tennessee Titans", "Tennessee", "Titans"),
    Team("WAS", "Washington Commanders", "Washington", "Commanders"),
)

NFL_RESOLVER = TeamResolver(NFL_TEAMS)


# Kalshi WNBA codes and Polymarket US names, verified against KXWNBAGAME
# events and /v2/leagues/wnba/events on 2026-08-24. Unusually easy: both
# venues send the bare city and the two strings are byte-identical for every
# team observed, so nothing here needs an alias. No city fields two WNBA
# teams, so no "Los Angeles C" style disambiguation arises either.
#
# Indiana and Las Vegas had no open game in the observed window and are
# included from the league roster rather than from venue data; their city
# strings follow the same bare-city convention as the twelve confirmed.
WNBA_TEAMS: tuple[Team, ...] = (
    Team("ATL", "Atlanta Dream", "Atlanta", "Dream"),
    Team("CHI", "Chicago Sky", "Chicago", "Sky"),
    Team("CONN", "Connecticut Sun", "Connecticut", "Sun"),
    Team("DAL", "Dallas Wings", "Dallas", "Wings"),
    Team("GS", "Golden State Valkyries", "Golden State", "Valkyries"),
    Team("IND", "Indiana Fever", "Indiana", "Fever"),
    Team("LA", "Los Angeles Sparks", "Los Angeles", "Sparks"),
    Team("LV", "Las Vegas Aces", "Las Vegas", "Aces"),
    Team("MIN", "Minnesota Lynx", "Minnesota", "Lynx"),
    Team("NY", "New York Liberty", "New York", "Liberty"),
    Team("PDX", "Portland Fire", "Portland", "Fire"),
    Team("PHX", "Phoenix Mercury", "Phoenix", "Mercury"),
    Team("SEA", "Seattle Storm", "Seattle", "Storm"),
    Team("TOR", "Toronto Tempo", "Toronto", "Tempo"),
    Team("WSH", "Washington Mystics", "Washington", "Mystics"),
)

WNBA_RESOLVER = TeamResolver(WNBA_TEAMS)


# NBA. Codes follow Kalshi's documented convention, but KXNBAGAME had no open
# events when this was written (deep offseason), so unlike MLB and NFL these
# are NOT verified against live venue data. Recheck the codes and the
# yes_sub_title format once the season opens.
NBA_TEAMS: tuple[Team, ...] = (
    Team("ATL", "Atlanta Hawks", "Atlanta", "Hawks"),
    Team("BOS", "Boston Celtics", "Boston", "Celtics"),
    Team("BKN", "Brooklyn Nets", "Brooklyn", "Nets"),
    Team("CHA", "Charlotte Hornets", "Charlotte", "Hornets"),
    Team("CHI", "Chicago Bulls", "Chicago", "Bulls"),
    Team("CLE", "Cleveland Cavaliers", "Cleveland", "Cavaliers"),
    Team("DAL", "Dallas Mavericks", "Dallas", "Mavericks"),
    Team("DEN", "Denver Nuggets", "Denver", "Nuggets"),
    Team("DET", "Detroit Pistons", "Detroit", "Pistons"),
    Team("GS", "Golden State Warriors", "Golden State", "Warriors"),
    Team("HOU", "Houston Rockets", "Houston", "Rockets"),
    Team("IND", "Indiana Pacers", "Indiana", "Pacers"),
    Team("LAC", "Los Angeles Clippers", "Los Angeles", "Clippers"),
    Team("LAL", "Los Angeles Lakers", "Los Angeles", "Lakers"),
    Team("MEM", "Memphis Grizzlies", "Memphis", "Grizzlies"),
    Team("MIA", "Miami Heat", "Miami", "Heat"),
    Team("MIL", "Milwaukee Bucks", "Milwaukee", "Bucks"),
    Team("MIN", "Minnesota Timberwolves", "Minnesota", "Timberwolves"),
    Team("NO", "New Orleans Pelicans", "New Orleans", "Pelicans"),
    Team("NYK", "New York Knicks", "New York", "Knicks"),
    Team("OKC", "Oklahoma City Thunder", "Oklahoma City", "Thunder"),
    Team("ORL", "Orlando Magic", "Orlando", "Magic"),
    Team("PHI", "Philadelphia 76ers", "Philadelphia", "76ers"),
    Team("PHX", "Phoenix Suns", "Phoenix", "Suns"),
    Team("POR", "Portland Trail Blazers", "Portland", "Trail Blazers"),
    Team("SAC", "Sacramento Kings", "Sacramento", "Kings"),
    Team("SA", "San Antonio Spurs", "San Antonio", "Spurs"),
    Team("TOR", "Toronto Raptors", "Toronto", "Raptors"),
    Team("UTA", "Utah Jazz", "Utah", "Jazz"),
    Team("WAS", "Washington Wizards", "Washington", "Wizards"),
)

NBA_RESOLVER = TeamResolver(NBA_TEAMS)


TEAM_MAPS = {
    "mlb": MLB_RESOLVER,
    "nfl": NFL_RESOLVER,
    "nba": NBA_RESOLVER,
}
