"""Polymarket US discovery.

``GET /v2/leagues/{slug}/events`` returns every market type for a league in
one call, so team sports, totals and tennis all come from the same request and
differ only in which ``sportsMarketType`` values they keep.

Two traps that the international integration had do not exist here:

* **No 100-row cap.** International's flat ``/markets`` capped at 100 rows
  ordered by 24h volume, where league games never outranked politics. This
  endpoint is league-scoped, so there is nothing to outrank.
* **No question parsing.** International only identified teams in prose.
  Polymarket US returns ``teams[].name`` ("Arizona Diamondbacks") structured,
  which the existing resolvers accept directly.

``startTime`` is a clean UTC instant, so no date heuristics are needed on this
side at all - the matcher's start-time comparison works directly.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .kalshi_sports import VenueGame, _parse_utc
from .matcher import OVER, UNDER
from .players import Player, last_name_code
from .teams import Team, TeamResolver

GATEWAY_BASE = "https://gateway.polymarket.us"

log = logging.getLogger(__name__)

# Our sport label -> Polymarket US league slug.
LEAGUE_SLUGS = {
    "mlb": "mlb",
    "nfl": "nfl",
    "nba": "nba",
    # Polymarket US calls college football "cfb" where Kalshi's series is
    # NCAAF. The two registries are separate dicts precisely so a league can
    # be named differently per venue.
    "wnba": "wnba",
    "ncaaf": "cfb",
}

TENNIS_LEAGUES = ("atp", "wta")

# NBA is unverified: /v2/leagues/nba/events returned zero events on
# 2026-08-11 (offseason), the same condition that leaves KXNBAGAME unverified
# on the Kalshi side. The basketball type string below was confirmed against
# WNBA, which had open events.
MONEYLINE_TYPES = frozenset(
    {
        "baseball_team_full_game_winner",
        "football_team_full_game_winner",
        "basketball_team_full_game_winner",
    }
)
TENNIS_WINNER_TYPES = frozenset({"tennis_match_winner"})

# UFC. Verified live 2026-08-24: 54 events, 49 typed ufc_fight_winner and 5
# still on the older generic "moneyline" label, so both are accepted.
UFC_LEAGUES = ("ufc",)
UFC_WINNER_TYPES = frozenset({"ufc_fight_winner", "moneyline"})

# One shared set, like MONEYLINE_TYPES above: `_fetch_events` is league-scoped,
# so a baseball type can never surface in the nfl league response and there is
# nothing to key per sport. MLB and WNBA totals were wired on 2026-08-24 once
# the Polymarket US port was proven live; NBA stays out until its season opens,
# for the same reason its moneyline is unverified.
TOTAL_TYPES = frozenset(
    {
        "football_team_full_game_total",
        "baseball_team_full_game_total",
        "basketball_team_full_game_total",
    }
)

# Full-game spreads. Period spreads (`baseball_team_first_five_spread`,
# `football_team_first_half_spread`, the four quarters) are distinct types and
# are Phase 3. Verified live 2026-09-06 on mlb, nfl and cfb.
SPREAD_TYPES = frozenset(
    {
        "baseball_team_full_game_spread",
        "football_team_full_game_spread",
        # Presumed by analogy with the winner and total types; unverified,
        # both basketball leagues were out of season on 2026-09-06.
        "basketball_team_full_game_spread",
    }
)

# "Seattle Seahawks wins by over 3.5 points" / "Milwaukee Brewers wins by over
# 2.5 runs" — the venue's own statement of which team the line is for.
_SPREAD_TITLE_RE = re.compile(
    r"^(?P<team>.+?) wins by over (?P<line>\d+(?:\.\d+)?) (?:runs|points)$"
)


async def _fetch_events(
    league: str, http_client: httpx.AsyncClient | None, limit: int
) -> list[dict[str, Any]]:
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.get(
            f"{GATEWAY_BASE}/v2/leagues/{league}/events", params={"limit": limit}
        )
        resp.raise_for_status()
        events = resp.json().get("events") or []
        return [e for e in events if isinstance(e, dict)]
    finally:
        if owns_client:
            await client.aclose()


def _sides(market: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The (long, short) pair, or None when the market is not binary."""
    sides = market.get("marketSides") or []
    longs = [s for s in sides if isinstance(s, dict) and s.get("long")]
    shorts = [s for s in sides if isinstance(s, dict) and not s.get("long")]
    if len(longs) != 1 or len(shorts) != 1:
        return None
    return longs[0], shorts[0]


def _line(market: dict[str, Any]) -> Decimal | None:
    """The strike, as Decimal. The API sends a JSON float (21.5)."""
    raw = market.get("line")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError):
        return None


def _team_name(side: dict[str, Any]) -> str | None:
    """The competitor's display name. Used for *individual* sports, where this
    is a person and there is no roster to resolve against."""
    team = side.get("team")
    if not isinstance(team, dict):
        return None
    name = team.get("name")
    return name if isinstance(name, str) else None


def _resolve_team(team: Any, resolver: TeamResolver) -> Team | None:
    """Resolve one Polymarket US team object against a league roster.

    `name` alone is not enough. What it holds varies by league — a full name
    for NFL/MLB, a bare city for WNBA, and for CFB only the *mascot*, which is
    not an identity: 28 mascots repeat across the 88 CFB games observed on
    2026-08-24, covering 81 of 176 team-slots, so nearly half of college games
    would resolve to nothing (or, before the uniqueness guard, to the wrong
    school).

    The payload carries two better fields, verified live the same day:

        name                 "Tar Heels"       <- mascot, ambiguous
        safeName             "North Carolina"  <- what Kalshi's title says
        displayAbbreviation  "UNC"             <- what Kalshi's ticker uses

    So try the precise ones first and keep `name` as the last resort, which
    is what every league relied on before.
    """
    if not isinstance(team, dict):
        return None
    abbrev = team.get("displayAbbreviation")
    if isinstance(abbrev, str) and abbrev:
        found = resolver.by_code(abbrev)
        if found is not None:
            return found
    for key in ("safeName", "name"):
        value = team.get(key)
        if isinstance(value, str) and value:
            found = resolver.by_polymarket_name(value)
            if found is not None:
                return found
    return None


def _live_flags(event: dict[str, Any]) -> tuple[bool | None, bool | None]:
    """``(live, ended)`` as the venue reports them, or ``None`` where it did not.

    Both are genuinely tri-state on this payload and the distinction matters.
    Verified 2026-08-25 against the ATP feed: a match in its third set reads
    ``live=True, ended=False, period="S3"``; a finished one reads
    ``live=False, ended=True, period="FT"``; a scheduled one reads
    ``live=False, ended=False, score="0-0"``; and one the venue is not yet
    tracking reads ``None`` for both with ``period="NS"``. Coercing that last
    case to False would assert a game is not being played when the venue has
    simply not said - and Kalshi never says at all, so ``None`` has to survive
    all the way to the matcher.
    """
    live = event.get("live")
    ended = event.get("ended")
    return (
        live if isinstance(live, bool) else None,
        ended if isinstance(ended, bool) else None,
    )


def _eastern_date(start: datetime) -> date:
    """The game's date **in Eastern time**, not UTC.

    `game_date` is only ever compared against Kalshi's, and Kalshi's comes
    from its event ticker, which carries a local *Eastern trading day*. An
    evening game therefore lands on different calendar dates on the two
    venues: a WNBA tip at 00:00Z is 8pm ET the previous day, so Kalshi says
    Aug 24 while a naive UTC read says Aug 25. `_same_fixture` falls back to
    comparing dates whenever either side has no exact start — which is every
    sport whose Kalshi ticker omits HHMM (WNBA, NFL, CFB) — so a UTC date
    silently dropped every evening fixture in those leagues.

    Converting here rather than widening the date tolerance is deliberate:
    tolerance would also fuse Monday's game with Tuesday's and invent an arb
    between two different fixtures.

    Falls back to the UTC date if the tz database is unavailable, which is the
    behaviour this replaced — wrong for evening games, but not a crash.
    """
    try:
        return start.astimezone(ZoneInfo("America/New_York")).date()
    except (ValueError, ZoneInfoNotFoundError):  # pragma: no cover - no tzdata
        return start.date()


async def fetch_polymarket_us_games(
    *,
    resolver: TeamResolver,
    sport: str,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 200,
) -> list[VenueGame]:
    """Moneyline games for a team sport."""
    league = LEAGUE_SLUGS.get(sport, sport)
    events = await _fetch_events(league, http_client, limit)

    games: list[VenueGame] = []
    for event in events:
        start_time = _parse_utc(event.get("startTime"))
        live, ended = _live_flags(event)
        if start_time is None:
            continue
        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            if market.get("sportsMarketType") not in MONEYLINE_TYPES:
                continue
            pair = _sides(market)
            slug = market.get("slug")
            if pair is None or not slug:
                continue
            long_side, short_side = pair

            team_long = _resolve_team(long_side.get("team"), resolver)
            team_short = _resolve_team(short_side.get("team"), resolver)
            if team_long is None or team_short is None:
                continue

            games.append(
                VenueGame(
                    sport=sport,
                    venue_id="polymarket_us",
                    game_date=_eastern_date(start_time),
                    teams=(team_long, team_short),
                    outcome_ids={
                        team_long.code: f"{slug}:LONG",
                        team_short.code: f"{slug}:SHORT",
                    },
                    ref=str(slug),
                    market_type="moneyline",
                    start_time=start_time,
                    live=live,
                    ended=ended,
                )
            )
    return games


async def fetch_polymarket_us_totals(
    *,
    resolver: TeamResolver,
    sport: str,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 200,
) -> list[VenueGame]:
    """Over/under games, one VenueGame per (game, line).

    Totals sides carry ``team: null`` and are labelled Over / Under, so the
    participants come from the event's ``teams`` array rather than from the
    market. The canonical TRUE side is OVER, which is the long side.
    """
    league = LEAGUE_SLUGS.get(sport, sport)
    events = await _fetch_events(league, http_client, limit)

    games: list[VenueGame] = []
    for event in events:
        start_time = _parse_utc(event.get("startTime"))
        live, ended = _live_flags(event)
        if start_time is None:
            continue
        event_teams = [t for t in (event.get("teams") or []) if isinstance(t, dict)]
        if len(event_teams) != 2:
            continue
        resolved = [_resolve_team(t, resolver) for t in event_teams]
        if any(t is None for t in resolved):
            continue
        team_a, team_b = resolved[0], resolved[1]
        assert team_a is not None and team_b is not None  # narrowed above

        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            if market.get("sportsMarketType") not in TOTAL_TYPES:
                continue
            slug = market.get("slug")
            line = _line(market)
            if not slug or line is None or _sides(market) is None:
                continue
            games.append(
                VenueGame(
                    sport=sport,
                    venue_id="polymarket_us",
                    game_date=_eastern_date(start_time),
                    teams=(team_a, team_b),
                    outcome_ids={
                        OVER: f"{slug}:LONG",
                        UNDER: f"{slug}:SHORT",
                    },
                    ref=str(slug),
                    market_type="total",
                    line=line,
                    start_time=start_time,
                    live=live,
                    ended=ended,
                )
            )
    return games


def _title_agrees(title: object, anchor_name: object, line: Decimal) -> bool | None:
    """Does the venue's title name the derived anchor and line?

    ``None`` when the title matches no known pattern, so the guard cannot run.
    ``False`` when it matches and disagrees — two venue fields contradicting
    each other, which is the signal that the sign convention has moved.

    String comparison on ``teams[].name``, not resolution: the title uses the
    same string the event's ``teams[]`` carries, bare mascot included on CFB
    (``"Tar Heels wins by over 20.5 points"``), and matched on all 5,267
    markets observed on 2026-09-06.
    """
    if not isinstance(title, str):
        return None
    m = _SPREAD_TITLE_RE.match(title.strip())
    if m is None:
        return None
    try:
        title_line = Decimal(m.group("line"))
    except InvalidOperation:
        return None
    return m.group("team") == anchor_name and title_line == line


async def fetch_polymarket_us_spreads(
    *,
    resolver: TeamResolver,
    sport: str,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 200,
) -> list[VenueGame]:
    """Full-game spreads, one VenueGame per (game, anchor, line).

    One market per **signed line on the first-listed team**. ``line`` is
    signed (``pos-2pt5`` carries 2.5, ``neg-2pt5`` carries -2.5) and the long
    side is always that first team covering it, so:

    * ``line < 0`` — the first team must win by more than |line|. It is the
      anchor, and LONG is the anchor covering (≡ Kalshi YES).
    * ``line > 0`` — the *second* team must win by more than |line|. It is
      the anchor, and LONG is the complement (≡ Kalshi NO).

    Pinned by live prices on 2026-09-06: ``asc-nfl-ne-sea-…-pos-3pt5`` long
    0.51/0.52 against Kalshi ``NESEA-SEA4`` NO 0.51/0.52; ``neg-3pt5`` long
    0.26/0.27 against ``NESEA-NE4`` YES 0.25/0.27. Outcomes are therefore
    always first team → ``:LONG``, second team → ``:SHORT``; only the anchor
    flips with the sign.

    Two guards, both structural. The long side's team must be the first-listed
    team, or the derivation above does not hold. The title must name the
    derived anchor and line, or the market is skipped — a backwards sign near
    even money is a 2-4c phantom edge that no clock-based guard and not the
    15c plausible-edge ceiling can see. A title matching no pattern is
    accepted and counted once per pass, so a rewording disables the guard
    visibly rather than silently zeroing the league.
    """
    league = LEAGUE_SLUGS.get(sport, sport)
    events = await _fetch_events(league, http_client, limit)

    games: list[VenueGame] = []
    unparsed_titles = 0
    for event in events:
        start_time = _parse_utc(event.get("startTime"))
        live, ended = _live_flags(event)
        if start_time is None:
            continue
        event_teams = [t for t in (event.get("teams") or []) if isinstance(t, dict)]
        if len(event_teams) != 2:
            continue
        resolved = [_resolve_team(t, resolver) for t in event_teams]
        if any(t is None for t in resolved):
            continue
        first, second = resolved[0], resolved[1]
        assert first is not None and second is not None  # narrowed above

        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            if market.get("sportsMarketType") not in SPREAD_TYPES:
                continue
            slug = market.get("slug")
            signed = _line(market)
            pair = _sides(market)
            if not slug or signed is None or signed == 0 or pair is None:
                continue
            long_side, short_side = pair
            long_team = _resolve_team(long_side.get("team"), resolver)
            short_team = _resolve_team(short_side.get("team"), resolver)
            if (
                long_team is None
                or short_team is None
                or long_team.code != first.code
                or short_team.code != second.code
            ):
                log.warning(
                    "polymarket_us spreads: %s sides do not follow teams[] order; skipped",
                    slug,
                )
                continue

            anchor_idx = 0 if signed < 0 else 1
            anchor = first if anchor_idx == 0 else second
            line = abs(signed)
            verdict = _title_agrees(market.get("title"), event_teams[anchor_idx].get("name"), line)
            if verdict is None:
                unparsed_titles += 1
            elif verdict is False:
                log.warning(
                    "polymarket_us spreads: %s title %r contradicts derived anchor %s %s; skipped",
                    slug, market.get("title"), anchor.code, line,
                )
                continue

            games.append(
                VenueGame(
                    sport=sport,
                    venue_id="polymarket_us",
                    game_date=_eastern_date(start_time),
                    teams=(first, second),
                    outcome_ids={
                        first.code: f"{slug}:LONG",
                        second.code: f"{slug}:SHORT",
                    },
                    ref=str(slug),
                    market_type="spread",
                    line=line,
                    anchor=anchor.code,
                    start_time=start_time,
                    live=live,
                    ended=ended,
                )
            )
    if unparsed_titles:
        log.warning(
            "polymarket_us spreads[%s]: %d market title(s) matched no known pattern; "
            "the title cross-check did not run for them",
            sport, unparsed_titles,
        )
    return games


async def fetch_polymarket_us_tennis(
    *,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 200,
    leagues: tuple[str, ...] = TENNIS_LEAGUES,
    winner_types: frozenset[str] = TENNIS_WINNER_TYPES,
) -> list[VenueGame]:
    """ATP + WTA match winners.

    Players come from the market's own sides, which carry full names, so
    there is no title to split.

    ``sport`` must be the league (``"atp"`` / ``"wta"``), **not** ``"tennis"``.
    Kalshi labels its matches per-tour and ``sport`` is part of the matcher's
    bucket key, so a single "tennis" label silently pairs with nothing — 72
    otherwise-matching player pairs went missing that way. The UI agrees:
    ``CATEGORY_LABELS`` in ``frontend/src/lib/combo.ts`` keys on atp/wta.
    """
    matches: list[VenueGame] = []
    for league in leagues:
        events = await _fetch_events(league, http_client, limit)
        for event in events:
            start_time = _parse_utc(event.get("startTime"))
            live, ended = _live_flags(event)
            if start_time is None:
                continue
            for market in event.get("markets") or []:
                if not isinstance(market, dict):
                    continue
                if market.get("sportsMarketType") not in winner_types:
                    continue
                pair = _sides(market)
                slug = market.get("slug")
                if pair is None or not slug:
                    continue
                long_side, short_side = pair
                long_name, short_name = _team_name(long_side), _team_name(short_side)
                if long_name is None or short_name is None:
                    continue
                p_long = Player(code=last_name_code(long_name), full_name=long_name)
                p_short = Player(code=last_name_code(short_name), full_name=short_name)
                if not p_long.code or not p_short.code or p_long.code == p_short.code:
                    continue

                matches.append(
                    VenueGame(
                        sport=league,
                        venue_id="polymarket_us",
                        game_date=_eastern_date(start_time),
                        teams=(p_long, p_short),
                        outcome_ids={
                            p_long.code: f"{slug}:LONG",
                            p_short.code: f"{slug}:SHORT",
                        },
                        ref=str(slug),
                        market_type="moneyline",
                        start_time=start_time,
                        live=live,
                        ended=ended,
                    )
                )
    return matches
