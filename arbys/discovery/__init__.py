"""Discovery: cross-venue auto-matching of sports markets."""

from .matcher import CrossVenueMatch, match_games, match_to_event_group
from .teams import MLB_RESOLVER, MLB_TEAMS, Team, TeamResolver

__all__ = [
    "MLB_RESOLVER",
    "MLB_TEAMS",
    "CrossVenueMatch",
    "Team",
    "TeamResolver",
    "match_games",
    "match_to_event_group",
]
