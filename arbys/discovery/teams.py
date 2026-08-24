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


# Kalshi CFB codes with Polymarket US mascots, generated from live payloads on
# 2026-08-24: `city` is Kalshi's `yes_sub_title` (the school), `nickname` is
# Polymarket's `teams[].name` (the mascot), and `code` is the ticker code both
# venues agree on for these teams.
#
# Only the teams both venues quoted are here. Kalshi listed 259 CFB codes and
# Polymarket US 168; 154 codes appear on both, and a team absent from either
# can never produce a cross-venue group, so listing it would be dead weight.
# Fourteen Polymarket codes (BAMA, NCSU, TAMU, MAINE, MERR, M-OH ...) had no
# open Kalshi game in the window and are deliberately omitted rather than
# guessed at — `discover_team_sport_event_groups` simply finds no match for
# them until both venues quote the same fixture.
#
# Mascots are NOT unique here (28 repeat, "Tigers" seven times), which is why
# TeamResolver refuses a bare ambiguous nickname. Resolution for CFB runs
# through `displayAbbreviation` -> code and `safeName` -> school instead.
CFB_TEAMS: tuple[Team, ...] = (
    Team('AFA', 'Air Force Falcons', 'Air Force', 'Falcons'),
    Team('AKR', 'Akron Zips', 'Akron', 'Zips'),
    Team('ALBY', 'University at Albany Great Danes', 'University at Albany', 'Great Danes'),
    Team('ALCN', 'Alcorn St. Braves', 'Alcorn St.', 'Braves'),
    Team('APP', 'Appalachian St. Mountaineers', 'Appalachian St.', 'Mountaineers'),
    Team('ARIZ', 'Arizona Wildcats', 'Arizona', 'Wildcats'),
    Team('ARK', 'Arkansas Razorbacks', 'Arkansas', 'Razorbacks'),
    Team('ARMY', 'Army Black Knights', 'Army', 'Black Knights'),
    Team('ARPB', 'Arkansas-Pine Bluff Golden Lions', 'Arkansas-Pine Bluff', 'Golden Lions'),
    Team('ARST', 'Arkansas St. Red Wolves', 'Arkansas St.', 'Red Wolves'),
    Team('ASU', 'Arizona St. Sun Devils', 'Arizona St.', 'Sun Devils'),
    Team('AUB', 'Auburn Tigers', 'Auburn', 'Tigers'),
    Team('BALL', 'Ball St. Cardinals', 'Ball St.', 'Cardinals'),
    Team('BAY', 'Baylor Bears', 'Baylor', 'Bears'),
    Team('BC', 'Boston College Eagles', 'Boston College', 'Eagles'),
    Team('BGSU', 'Bowling Green Falcons', 'Bowling Green', 'Falcons'),
    Team('BRY', 'Bryant Bulldogs', 'Bryant', 'Bulldogs'),
    Team('BSU', 'Boise St. Broncos', 'Boise St.', 'Broncos'),
    Team('BUFF', 'Buffalo Bulls', 'Buffalo', 'Bulls'),
    Team('CAL', 'California Golden Bears', 'California', 'Golden Bears'),
    Team('CCAR', 'Coastal Carolina Chanticleers', 'Coastal Carolina', 'Chanticleers'),
    Team('CHAR', 'Charlotte 49ers', 'Charlotte', '49ers'),
    Team('CHSO', 'Charleston Southern Buccaneers', 'Charleston Southern', 'Buccaneers'),
    Team('CIN', 'Cincinnati Bearcats', 'Cincinnati', 'Bearcats'),
    Team('CIT', 'The Citadel Bulldogs', 'The Citadel', 'Bulldogs'),
    Team('CLEM', 'Clemson Tigers', 'Clemson', 'Tigers'),
    Team('CMU', 'Central Michigan Chippewas', 'Central Michigan', 'Chippewas'),
    Team('COLO', 'Colorado Buffaloes', 'Colorado', 'Buffaloes'),
    Team('COOK', 'Bethune-Cookman Wildcats', 'Bethune-Cookman', 'Wildcats'),
    Team('CSU', 'Colorado St. Rams', 'Colorado St.', 'Rams'),
    Team('DEL', "Delaware Fightin' Blue Hens", 'Delaware', "Fightin' Blue Hens"),
    Team('DUKE', 'Duke Blue Devils', 'Duke', 'Blue Devils'),
    Team('DUQ', 'Duquesne Dukes', 'Duquesne', 'Dukes'),
    Team('ECU', 'East Carolina Pirates', 'East Carolina', 'Pirates'),
    Team('EIU', 'Eastern Illinois Panthers', 'Eastern Illinois', 'Panthers'),
    Team('EKY', 'Eastern Kentucky Colonels', 'Eastern Kentucky', 'Colonels'),
    Team('EMU', 'Eastern Michigan Eagles', 'Eastern Michigan', 'Eagles'),
    Team('FAU', 'Florida Atlantic Owls', 'Florida Atlantic', 'Owls'),
    Team('FIU', 'Florida International Panthers', 'Florida International', 'Panthers'),
    Team('FLA', 'Florida Gators', 'Florida', 'Gators'),
    Team('FRES', 'Fresno St. Bulldogs', 'Fresno St.', 'Bulldogs'),
    Team('FSU', 'Florida St. Seminoles', 'Florida St.', 'Seminoles'),
    Team('FUR', 'Furman Paladins', 'Furman', 'Paladins'),
    Team('GASO', 'Georgia Southern Eagles', 'Georgia Southern', 'Eagles'),
    Team('GT', 'Georgia Tech Yellow Jackets', 'Georgia Tech', 'Yellow Jackets'),
    Team('HAW', "Hawai'i Rainbow Warriors", "Hawai'i", 'Rainbow Warriors'),
    Team('HCU', 'Houston Christian Huskies', 'Houston Christian', 'Huskies'),
    Team('HOU', 'Houston Cougars', 'Houston', 'Cougars'),
    Team('IDHO', 'Idaho Vandals', 'Idaho', 'Vandals'),
    Team('IDST', 'Idaho St. Bengals', 'Idaho St.', 'Bengals'),
    Team('ILL', 'Illinois Fighting Illini', 'Illinois', 'Fighting Illini'),
    Team('IND', 'Indiana Hoosiers', 'Indiana', 'Hoosiers'),
    Team('INST', 'Indiana St. Sycamores', 'Indiana St.', 'Sycamores'),
    Team('IOWA', 'Iowa Hawkeyes', 'Iowa', 'Hawkeyes'),
    Team('ISU', 'Iowa St. Cyclones', 'Iowa St.', 'Cyclones'),
    Team('JMU', 'James Madison Dukes', 'James Madison', 'Dukes'),
    Team('JVST', 'Jacksonville St. Gamecocks', 'Jacksonville St.', 'Gamecocks'),
    Team('KENT', 'Kent St. Golden Flashes', 'Kent St.', 'Golden Flashes'),
    Team('KSU', 'Kansas St. Wildcats', 'Kansas St.', 'Wildcats'),
    Team('KU', 'Kansas Jayhawks', 'Kansas', 'Jayhawks'),
    Team('LAF', 'Lafayette Leopards', 'Lafayette', 'Leopards'),
    Team('LIB', 'Liberty Flames', 'Liberty', 'Flames'),
    Team('LIU', 'LIU Sharks', 'LIU', 'Sharks'),
    Team('LOU', 'Louisville Cardinals', 'Louisville', 'Cardinals'),
    Team('LSU', 'LSU Tigers', 'LSU', 'Tigers'),
    Team('LT', 'Louisiana Tech Bulldogs', 'Louisiana Tech', 'Bulldogs'),
    Team('MEM', 'Memphis Tigers', 'Memphis', 'Tigers'),
    Team('MIA', 'Miami (FL) Hurricanes', 'Miami (FL)', 'Hurricanes'),
    Team('MICH', 'Michigan Wolverines', 'Michigan', 'Wolverines'),
    Team('MINN', 'Minnesota Golden Gophers', 'Minnesota', 'Golden Gophers'),
    Team('MISS', 'Ole Miss Rebels', 'Ole Miss', 'Rebels'),
    Team('MIZZ', 'Missouri Tigers', 'Missouri', 'Tigers'),
    Team('MORG', 'Morgan St. Bears', 'Morgan St.', 'Bears'),
    Team('MRSH', 'Marshall Thundering Herd', 'Marshall', 'Thundering Herd'),
    Team('MSST', 'Mississippi St. Bulldogs', 'Mississippi St.', 'Bulldogs'),
    Team('MSU', 'Michigan St. Spartans', 'Michigan St.', 'Spartans'),
    Team('MURR', 'Murray St. Racers', 'Murray St.', 'Racers'),
    Team('NAU', 'Northern Arizona Lumberjacks', 'Northern Arizona', 'Lumberjacks'),
    Team('NAVY', 'Navy Midshipmen', 'Navy', 'Midshipmen'),
    Team('NCAT', 'North Carolina A&T Aggies', 'North Carolina A&T', 'Aggies'),
    Team('ND', 'Notre Dame Fighting Irish', 'Notre Dame', 'Fighting Irish'),
    Team('NEB', 'Nebraska Cornhuskers', 'Nebraska', 'Cornhuskers'),
    Team('NEV', 'Nevada Wolf Pack', 'Nevada', 'Wolf Pack'),
    Team('NICH', 'Nicholls St. Colonels', 'Nicholls St.', 'Colonels'),
    Team('NIU', 'Northern Illinois Huskies', 'Northern Illinois', 'Huskies'),
    Team('NMSU', 'New Mexico St. Aggies', 'New Mexico St.', 'Aggies'),
    Team('NORF', 'Norfolk St. Spartans', 'Norfolk St.', 'Spartans'),
    Team('NWST', 'Northwestern St. Demons', 'Northwestern St.', 'Demons'),
    Team('ODU', 'Old Dominion Monarchs', 'Old Dominion', 'Monarchs'),
    Team('OHIO', 'Ohio Bobcats', 'Ohio', 'Bobcats'),
    Team('OKLA', 'Oklahoma Sooners', 'Oklahoma', 'Sooners'),
    Team('OKST', 'Oklahoma St. Cowboys', 'Oklahoma St.', 'Cowboys'),
    Team('ORE', 'Oregon Ducks', 'Oregon', 'Ducks'),
    Team('ORST', 'Oregon St. Beavers', 'Oregon St.', 'Beavers'),
    Team('OSU', 'Ohio St. Buckeyes', 'Ohio St.', 'Buckeyes'),
    Team('PEAY', 'Austin Peay Governors', 'Austin Peay', 'Governors'),
    Team('PITT', 'Pittsburgh Panthers', 'Pittsburgh', 'Panthers'),
    Team('PRST', 'Portland St. Vikings', 'Portland St.', 'Vikings'),
    Team('PSU', 'Penn St. Nittany Lions', 'Penn St.', 'Nittany Lions'),
    Team('PUR', 'Purdue Boilermakers', 'Purdue', 'Boilermakers'),
    Team('RICE', 'Rice Owls', 'Rice', 'Owls'),
    Team('RUTG', 'Rutgers Scarlet Knights', 'Rutgers', 'Scarlet Knights'),
    Team('SAC', 'Sacramento St. Hornets', 'Sacramento St.', 'Hornets'),
    Team('SCAR', 'South Carolina Gamecocks', 'South Carolina', 'Gamecocks'),
    Team('SDSU', 'San Diego St. Aztecs', 'San Diego St.', 'Aztecs'),
    Team('SELA', 'Southeastern Louisiana Lions', 'Southeastern Louisiana', 'Lions'),
    Team('SEMO', 'Southeast Missouri St. Redhawks', 'Southeast Missouri St.', 'Redhawks'),
    Team('SJSU', 'San Jose St. Spartans', 'San Jose St.', 'Spartans'),
    Team('SMU', 'SMU Mustangs', 'SMU', 'Mustangs'),
    Team('STAN', 'Stanford Cardinal', 'Stanford', 'Cardinal'),
    Team('SYR', 'Syracuse Orange', 'Syracuse', 'Orange'),
    Team('TCU', 'TCU Horned Frogs', 'TCU', 'Horned Frogs'),
    Team('TEM', 'Temple Owls', 'Temple', 'Owls'),
    Team('TENN', 'Tennessee Volunteers', 'Tennessee', 'Volunteers'),
    Team('TEX', 'Texas Longhorns', 'Texas', 'Longhorns'),
    Team('TLSA', 'Tulsa Golden Hurricane', 'Tulsa', 'Golden Hurricane'),
    Team('TOL', 'Toledo Rockets', 'Toledo', 'Rockets'),
    Team('TOWS', 'Towson Tigers', 'Towson', 'Tigers'),
    Team('TROY', 'Troy Trojans', 'Troy', 'Trojans'),
    Team('TTU', 'Texas Tech Red Raiders', 'Texas Tech', 'Red Raiders'),
    Team('TULN', 'Tulane Green Wave', 'Tulane', 'Green Wave'),
    Team('TXST', 'Texas St. Bobcats', 'Texas St.', 'Bobcats'),
    Team('UAB', 'UAB Blazers', 'UAB', 'Blazers'),
    Team('UCF', 'UCF Knights', 'UCF', 'Knights'),
    Team('UCLA', 'UCLA Bruins', 'UCLA', 'Bruins'),
    Team('UK', 'Kentucky Wildcats', 'Kentucky', 'Wildcats'),
    Team('ULM', 'Louisiana-Monroe Warhawks', 'Louisiana-Monroe', 'Warhawks'),
    Team('UNA', 'North Alabama Lions', 'North Alabama', 'Lions'),
    Team('UNC', 'North Carolina Tar Heels', 'North Carolina', 'Tar Heels'),
    Team('UNH', 'New Hampshire Wildcats', 'New Hampshire', 'Wildcats'),
    Team('UNLV', "UNLV Runnin' Rebels", 'UNLV', "Runnin' Rebels"),
    Team('UNM', 'New Mexico Lobos', 'New Mexico', 'Lobos'),
    Team('UNT', 'North Texas Mean Green', 'North Texas', 'Mean Green'),
    Team('URI', 'Rhode Island Rams', 'Rhode Island', 'Rams'),
    Team('USA', 'South Alabama Jaguars', 'South Alabama', 'Jaguars'),
    Team('USC', 'USC Trojans', 'USC', 'Trojans'),
    Team('USF', 'South Florida Bulls', 'South Florida', 'Bulls'),
    Team('USM', 'Southern Miss Golden Eagles', 'Southern Miss', 'Golden Eagles'),
    Team('USU', 'Utah St. Aggies', 'Utah St.', 'Aggies'),
    Team('UTAH', 'Utah Utes', 'Utah', 'Utes'),
    Team('UTEP', 'UTEP Miners', 'UTEP', 'Miners'),
    Team('UVA', 'Virginia Cavaliers', 'Virginia', 'Cavaliers'),
    Team('VAN', 'Vanderbilt Commodores', 'Vanderbilt', 'Commodores'),
    Team('VMI', 'VMI Keydets', 'VMI', 'Keydets'),
    Team('VT', 'Virginia Tech Hokies', 'Virginia Tech', 'Hokies'),
    Team('WAKE', 'Wake Forest Demon Deacons', 'Wake Forest', 'Demon Deacons'),
    Team('WASH', 'Washington Huskies', 'Washington', 'Huskies'),
    Team('WIS', 'Wisconsin Badgers', 'Wisconsin', 'Badgers'),
    Team('WKU', 'Western Kentucky Hilltoppers', 'Western Kentucky', 'Hilltoppers'),
    Team('WMU', 'Western Michigan Broncos', 'Western Michigan', 'Broncos'),
    Team('WSU', 'Washington St. Cougars', 'Washington St.', 'Cougars'),
    Team('WVU', 'West Virginia Mountaineers', 'West Virginia', 'Mountaineers'),
    Team('WYO', 'Wyoming Cowboys', 'Wyoming', 'Cowboys'),
    Team('YSU', 'Youngstown St. Penguins', 'Youngstown St.', 'Penguins'),
)

# Polymarket spells out "State" where Kalshi abbreviates "St." on 39 of the 154
# shared codes. Indexing both spellings costs nothing and makes `safeName` a
# real fallback when `displayAbbreviation` is missing or unrecognised.
CFB_ALIASES: dict[str, str] = {

    'alcorn state': 'ALCN',
    'appalachian state': 'APP',
    'arizona state': 'ASU',
    'arkansas state': 'ARST',
    'ball state': 'BALL',
    'boise state': 'BSU',
    'colorado state': 'CSU',
    'florida state': 'FSU',
    'fresno state': 'FRES',
    'idaho state': 'IDST',
    'indiana state': 'INST',
    'iowa state': 'ISU',
    'jacksonville state': 'JVST',
    'kansas state': 'KSU',
    'kent state': 'KENT',
    'michigan state': 'MSU',
    'mississippi state': 'MSST',
    'morgan state': 'MORG',
    'murray state': 'MURR',
    'new mexico state': 'NMSU',
    'nicholls state': 'NICH',
    'norfolk state': 'NORF',
    'northwestern state': 'NWST',
    'ohio state': 'OSU',
    'oklahoma state': 'OKST',
    'oregon state': 'ORST',
    'penn state': 'PSU',
    'portland state': 'PRST',
    'sacramento state': 'SAC',
    'san diego state': 'SDSU',
    'san jose state': 'SJSU',
    'southeast missouri state': 'SEMO',
    'texas state': 'TXST',
    'utah state': 'USU',
    'washington state': 'WSU',
    'youngstown state': 'YSU',
}

CFB_RESOLVER = TeamResolver(CFB_TEAMS, aliases=CFB_ALIASES)
