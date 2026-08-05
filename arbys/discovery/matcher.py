"""Match Kalshi + Polymarket games by (sport, date, team pair) and build EventGroups."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from ..shared.types import EventGroup, EventGroupLeg
from .kalshi_sports import Participant, VenueGame


@dataclass(frozen=True)
class CrossVenueMatch:
    sport: str
    game_date: str
    team_a: Participant  # canonical side (alphabetically first code)
    team_b: Participant
    per_venue: dict[str, VenueGame]

    def event_group_id(self) -> str:
        return f"{self.sport}-{self.team_a.code}-{self.team_b.code}-{self.game_date}"

    def event_group_title(self) -> str:
        return f"{self.team_a.full_name} vs {self.team_b.full_name} ({self.game_date})"


def _pair_key(game: VenueGame) -> tuple[str, frozenset[str]]:
    return (game.sport, frozenset(t.code for t in game.teams))


def match_games(
    *venue_games: list[VenueGame],
    date_tolerance_days: int = 0,
) -> list[CrossVenueMatch]:
    """Group games across venues that share ``(sport, team-pair)`` and whose
    dates are within ``date_tolerance_days`` of each other.

    Only returns matches with games from >= 2 distinct venues. Order of
    the input lists doesn't matter; games within a venue that don't
    match another venue are dropped. When multiple games on the same
    venue could plausibly match (rare), the one closest in date to
    the other venue's game wins.
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
        # Take the earliest game as the anchor for canonical date + participants.
        games.sort(key=lambda g: g.game_date)
        anchor = games[0]
        anchor_teams_sorted = sorted(anchor.teams, key=lambda t: t.code)
        per_venue: dict[str, VenueGame] = {}
        for g in games:
            if abs(g.game_date - anchor.game_date) > tol:
                continue
            # Prefer the game closest in date if a venue already has one.
            existing = per_venue.get(g.venue_id)
            if existing is None or abs(g.game_date - anchor.game_date) < abs(
                existing.game_date - anchor.game_date
            ):
                per_venue[g.venue_id] = g
        if len(per_venue) < 2:
            continue
        matches.append(
            CrossVenueMatch(
                sport=anchor.sport,
                game_date=anchor.game_date.isoformat(),
                team_a=anchor_teams_sorted[0],
                team_b=anchor_teams_sorted[1],
                per_venue=per_venue,
            )
        )
    matches.sort(key=lambda m: (m.game_date, m.team_a.code, m.team_b.code))
    return matches


def match_to_event_group(match: CrossVenueMatch) -> EventGroup:
    """Turn a cross-venue match into an EventGroup with 4 legs.

    Convention: the group's canonical proposition is "team_a wins" (alphabetically
    first team code). Legs where team_a wins are ``is_yes_side=True``.
    """
    legs: list[EventGroupLeg] = []
    for venue_id, game in sorted(match.per_venue.items()):
        for team_code, outcome_id in game.outcome_ids.items():
            legs.append(
                EventGroupLeg(
                    outcome_id=outcome_id,
                    venue_id=venue_id,
                    is_yes_side=(team_code == match.team_a.code),
                )
            )
    return EventGroup(
        id=match.event_group_id(),
        title=match.event_group_title(),
        legs=tuple(legs),
    )
