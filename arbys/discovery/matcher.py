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

    def yes_key(self) -> str:
        """Which ``outcome_ids`` key represents the group's TRUE proposition.

        Moneyline groups are canonically "team_a wins"; totals are "the total
        goes over the line".
        """
        return OVER if self.market_type == "total" else self.team_a.code

    def start_time(self) -> datetime | None:
        """Earliest start time any venue reports for this game.

        Venues agree closely in practice; taking the earliest keeps the value
        deterministic regardless of dict ordering.
        """
        times = [g.start_time for g in self.per_venue.values() if g.start_time is not None]
        return min(times) if times else None

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


def _pair_key(game: VenueGame) -> tuple[str, str, str, frozenset[str]]:
    """Bucket key. Market type and line are part of identity: an Over 44.5 and
    an Over 47.5 on the same game are different bets, not the same one."""
    return (
        game.sport,
        game.market_type,
        _fmt_line(game.line),
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
        games.sort(key=lambda g: g.game_date)
        claimed = [False] * len(games)

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
                if abs(g.game_date - anchor.game_date) > tol:
                    continue
                existing = per_venue.get(g.venue_id)
                if existing is None or abs(g.game_date - anchor.game_date) < abs(
                    existing.game_date - anchor.game_date
                ):
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
                )
            )
    matches.sort(
        key=lambda m: (
            m.game_date,
            m.team_a.code,
            m.team_b.code,
            m.market_type,
            _fmt_line(m.line),
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
    )
