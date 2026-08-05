"""Discovery: cross-venue auto-matching of sports markets."""

from .matcher import CrossVenueMatch, match_games, match_to_event_group
from .players import Player, parse_vs_title
from .teams import MLB_RESOLVER, MLB_TEAMS, Team, TeamResolver

__all__ = [
    "MLB_RESOLVER",
    "MLB_TEAMS",
    "CrossVenueMatch",
    "Player",
    "Team",
    "TeamResolver",
    "match_games",
    "match_to_event_group",
    "parse_vs_title",
]
