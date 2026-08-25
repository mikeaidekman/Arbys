"""Match Kalshi + Polymarket games by (sport, date, team pair) and build EventGroups."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from ..shared.types import EventGroup, EventGroupLeg
from .kalshi_sports import Participant, VenueGame

OVER = "OVER"
UNDER = "UNDER"


@dataclass(frozen=True)
class CrossVenueMatch:
    sport: str
    game_date: str
    team_a: Participant  # canonical side (alphabetically first code)
    team_b: Participant
    per_venue: dict[str, VenueGame]
    market_type: str = "moneyline"
    line: Decimal | None = None
    anchor: str | None = None

    def yes_key(self) -> str:
        """Which ``outcome_ids`` key represents the group's TRUE proposition.

        Moneyline groups are canonically "team_a wins"; totals are "the total
        goes over the line"; spreads are "the anchored participant covers".

        Written as a dispatch rather than a conditional so Phase 2 registers a
        market type here instead of rewriting the method.
        """
        if self.market_type == "total":
            return OVER
        if self.market_type == "spread":
            return self.anchor or self.team_a.code
        return self.team_a.code

    def start_time(self) -> datetime | None:
        """Earliest start time any venue reports for this game.

        Venues agree closely in practice; taking the earliest keeps the value
        deterministic regardless of dict ordering.
        """
        times = [g.start_time for g in self.per_venue.values() if g.start_time is not None]
        return min(times) if times else None

    def in_play(self) -> bool | None:
        """Whether any venue says this event is under way.

        A venue that reports nothing (Kalshi publishes no live state) must not
        be read as "not playing", so only venues that actually answered are
        consulted. ``ended`` wins over ``live`` on the same venue, and a
        finished report from one venue beats a stale ``live`` from another -
        the safe direction, since treating a finished game as live only costs
        pointless polling while the reverse under-polls a moving book.
        """
        seen = False
        playing = False
        for game in self.per_venue.values():
            if game.ended is None and game.live is None:
                continue
            seen = True
            if game.ended:
                return False
            if game.live:
                playing = True
        return playing if seen else None

    def event_group_id(self) -> str:
        base = f"{self.sport}-{self.team_a.code}-{self.team_b.code}-{self.game_date}"
        if self.market_type != "moneyline":
            return f"{base}-{self.market_type}-{_fmt_line(self.line)}"
        return base

    def event_group_title(self) -> str:
        matchup = f"{self.team_a.full_name} vs {self.team_b.full_name}"
        if self.market_type == "total":
            return f"{matchup} — Over {_fmt_line(self.line)} ({self.game_date})"
        return f"{matchup} ({self.game_date})"


def _fmt_line(line: Decimal | None) -> str:
    """Stable string for a line, so ids don't drift on 8.5 vs 8.50."""
    if line is None:
        return "na"
    return format(line.normalize(), "f")


# Two venues quoting the same fixture report the same scheduled start, give or
# take rounding. Anything further apart is a different game — a doubleheader's
# second leg is ~3h later, the next game in a series ~24h.
START_TIME_TOLERANCE = timedelta(minutes=90)


def _same_fixture(a: VenueGame, b: VenueGame, date_tol: timedelta) -> bool:
    """Are these two the same real-world game?

    Prefer exact start times. ``game_date`` is not comparable across venues:
    Kalshi's ticker carries a *local trading day* while Polymarket reports UTC,
    so a 10pm ET game is Aug 11 on one and Aug 12 on the other — and, worse,
    Kalshi's Aug 11 night game and Polymarket's Aug 10 night game both reduce
    to Aug 11. That collision paired Monday's game against Tuesday's and
    invented an arb between two different fixtures.
    """
    if a.start_time is not None and b.start_time is not None:
        return abs(a.start_time - b.start_time) <= START_TIME_TOLERANCE
    return abs(a.game_date - b.game_date) <= date_tol


def _order_key(game: VenueGame) -> tuple[int, float, str]:
    """Sort games chronologically, preferring the exact start when known."""
    if game.start_time is not None:
        return (0, game.start_time.timestamp(), game.venue_id)
    return (1, float(game.game_date.toordinal()), game.venue_id)


def _pair_key(game: VenueGame) -> tuple[str, str, str, str, frozenset[str]]:
    """Bucket key. Market type, line and anchor are all part of identity.

    An Over 44.5 and an Over 47.5 on the same game are different bets. So are
    ``CLE -2.5`` and ``DET -2.5`` — same line, opposite anchor — which is why
    the anchor belongs here even though no Phase 1 market type sets one.
    """
    return (
        game.sport,
        game.market_type,
        _fmt_line(game.line),
        game.anchor or "",
        frozenset(t.code for t in game.teams),
    )


def match_games(
    *venue_games: list[VenueGame],
    date_tolerance_days: int = 0,
) -> list[CrossVenueMatch]:
    """Group games across venues that share ``(sport, team-pair)`` and whose
    dates are within ``date_tolerance_days`` of each other.

    Only returns matches with games from >= 2 distinct venues. Order of
    the input lists doesn't matter; games within a venue that don't
    match another venue are dropped.

    A pair can meet more than once in the discovery window — MLB plays
    three-game series on consecutive days — so each bucket is split into
    date clusters and every cluster yields its own match. Within a cluster a
    venue contributes at most one game, the one nearest the cluster anchor,
    which keeps tolerance from fusing Monday's game on one venue with
    Tuesday's on the other.
    """
    # Bucket by (sport, participant-pair) only — apply date tolerance within
    # each bucket. This is O(n log n) in the number of games per pair.
    buckets: dict[tuple[str, frozenset[str]], list[VenueGame]] = {}
    for lst in venue_games:
        for g in lst:
            buckets.setdefault(_pair_key(g), []).append(g)

    matches: list[CrossVenueMatch] = []
    tol = timedelta(days=date_tolerance_days)
    for _key, games in buckets.items():
        if {g.venue_id for g in games} == {games[0].venue_id}:
            continue  # single venue only
        games.sort(key=_order_key)
        claimed = [False] * len(games)

        def _distance(g: VenueGame, anchor: VenueGame) -> float:
            """How far g sits from the anchor, in seconds where possible."""
            if g.start_time is not None and anchor.start_time is not None:
                return abs((g.start_time - anchor.start_time).total_seconds())
            return abs((g.game_date - anchor.game_date).days) * 86400.0

        for i, anchor in enumerate(games):
            if claimed[i]:
                continue
            # Greedily build one cluster around the earliest unclaimed game.
            per_venue: dict[str, VenueGame] = {}
            chosen_idx: dict[str, int] = {}
            for j in range(i, len(games)):
                if claimed[j]:
                    continue
                g = games[j]
                if not _same_fixture(g, anchor, tol):
                    continue
                existing = per_venue.get(g.venue_id)
                if existing is None or _distance(g, anchor) < _distance(existing, anchor):
                    per_venue[g.venue_id] = g
                    chosen_idx[g.venue_id] = j
            for j in chosen_idx.values():
                claimed[j] = True
            if len(per_venue) < 2:
                continue
            anchor_teams_sorted = sorted(anchor.teams, key=lambda t: t.code)
            matches.append(
                CrossVenueMatch(
                    sport=anchor.sport,
                    game_date=anchor.game_date.isoformat(),
                    team_a=anchor_teams_sorted[0],
                    team_b=anchor_teams_sorted[1],
                    per_venue=per_venue,
                    market_type=anchor.market_type,
                    line=anchor.line,
                    # `anchor` here is the cluster's anchor *game*; its
                    # `.anchor` is the participant its line is stated for.
                    anchor=anchor.anchor,
                )
            )
    matches.sort(
        key=lambda m: (
            m.game_date,
            m.team_a.code,
            m.team_b.code,
            m.market_type,
            _fmt_line(m.line),
            m.anchor or "",
        )
    )
    return matches


def match_to_event_group(match: CrossVenueMatch) -> EventGroup:
    """Turn a cross-venue match into an EventGroup with 4 legs.

    The canonical proposition is "team_a wins" for a moneyline and "the total
    goes over the line" for a total; legs matching it are ``is_yes_side=True``.
    """
    legs: list[EventGroupLeg] = []
    yes_key = match.yes_key()
    for venue_id, game in sorted(match.per_venue.items()):
        for outcome_key, outcome_id in game.outcome_ids.items():
            legs.append(
                EventGroupLeg(
                    outcome_id=outcome_id,
                    venue_id=venue_id,
                    is_yes_side=(outcome_key == yes_key),
                )
            )
    return EventGroup(
        id=match.event_group_id(),
        title=match.event_group_title(),
        legs=tuple(legs),
        start_time=match.start_time(),
        in_play=match.in_play(),
        source="discovery",
    )
