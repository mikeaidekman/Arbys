"""Match Kalshi + Polymarket games by (sport, date, team pair) and build EventGroups."""

from __future__ import annotations

from dataclasses import dataclass

from ..shared.types import EventGroup, EventGroupLeg
from .kalshi_sports import VenueGame
from .teams import Team


@dataclass(frozen=True)
class CrossVenueMatch:
    sport: str
    game_date: str
    team_a: Team  # canonical "home" side for group semantics (first alphabetical)
    team_b: Team
    per_venue: dict[str, VenueGame]  # venue_id -> VenueGame

    def event_group_id(self) -> str:
        return f"{self.sport}-{self.team_a.code}-{self.team_b.code}-{self.game_date}"

    def event_group_title(self) -> str:
        return f"{self.team_a.full_name} vs {self.team_b.full_name} ({self.game_date})"


def _key(game: VenueGame) -> tuple[str, str, frozenset[str]]:
    return (game.sport, game.game_date.isoformat(), frozenset(t.code for t in game.teams))


def match_games(*venue_games: list[VenueGame]) -> list[CrossVenueMatch]:
    """Group games across venues that share (sport, date, team-pair).

    Only returns matches with games from >= 2 distinct venues. Order of
    the input lists doesn't matter; games within a venue that don't
    match another venue are dropped.
    """
    buckets: dict[tuple[str, str, frozenset[str]], dict[str, VenueGame]] = {}
    for lst in venue_games:
        for g in lst:
            key = _key(g)
            buckets.setdefault(key, {})[g.venue_id] = g

    matches: list[CrossVenueMatch] = []
    for (sport, game_date, _codes), per_venue in buckets.items():
        if len(per_venue) < 2:
            continue
        # Team ordering: alphabetical by code for a stable canonical form.
        any_game = next(iter(per_venue.values()))
        sorted_teams = sorted(any_game.teams, key=lambda t: t.code)
        matches.append(
            CrossVenueMatch(
                sport=sport,
                game_date=game_date,
                team_a=sorted_teams[0],
                team_b=sorted_teams[1],
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
