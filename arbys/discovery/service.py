"""Discovery service — periodic auto-registration of cross-venue sports event groups."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from ..db import repositories as repo
from ..db.session import session_scope
from ..shared.types import EventGroup
from .kalshi_sports import fetch_kalshi_team_games
from .kalshi_tennis import fetch_kalshi_tennis_matches
from .kalshi_totals import fetch_kalshi_totals
from .matcher import match_games, match_to_event_group
from .polymarket_us import (
    fetch_polymarket_us_games,
    fetch_polymarket_us_tennis,
    fetch_polymarket_us_totals,
)
from .teams import (
    MLB_RESOLVER,
    NBA_RESOLVER,
    NFL_RESOLVER,
    WNBA_RESOLVER,
    TeamResolver,
)

log = logging.getLogger(__name__)

# Team sports discovered through the shared Kalshi-series / Polymarket-tag path.
TEAM_SPORTS: tuple[tuple[str, TeamResolver], ...] = (
    ("mlb", MLB_RESOLVER),
    ("nfl", NFL_RESOLVER),
    ("nba", NBA_RESOLVER),
    ("wnba", WNBA_RESOLVER),
)

# Sports whose over/under markets both venues quote. MLB and WNBA were wired on
# 2026-08-24, once the Polymarket US port had been proven live — the reason they
# were previously held back was to keep the port to exactly one behavioural
# variable, not any absence of markets. NBA stays out until its season opens,
# for the same reason its moneyline is unverified.
TOTALS_SPORTS: tuple[tuple[str, TeamResolver], ...] = (
    ("nfl", NFL_RESOLVER),
    ("mlb", MLB_RESOLVER),
    ("wnba", WNBA_RESOLVER),
)


async def discover_team_sport_event_groups(
    sport: str, resolver: TeamResolver
) -> list[EventGroup]:
    """Run one discovery pass for a team sport and return EventGroups.

    Date tolerance stays at 0 here. Kalshi's ticker carries a local trading
    day while Polymarket reports UTC, so night games can differ by one — but
    these leagues play the same pair on consecutive days, and widening the
    window risks pairing one venue's game with the other venue's *next* game.
    Matching on exact start time is the real fix.
    """
    kalshi_games, poly_games = await asyncio.gather(
        fetch_kalshi_team_games(resolver=resolver, sport=sport),
        fetch_polymarket_us_games(resolver=resolver, sport=sport),
    )
    matches = match_games(kalshi_games, poly_games)
    log.info(
        "discovery[%s]: kalshi=%d polymarket_us=%d matched=%d",
        sport, len(kalshi_games), len(poly_games), len(matches),
    )
    return [match_to_event_group(m) for m in matches]


async def discover_totals_event_groups(
    sport: str, resolver: TeamResolver
) -> list[EventGroup]:
    """Discover over/under groups, one per (game, line).

    Only lines quoted on *both* venues survive the match, since Over 44.5 and
    Over 47.5 are different bets. Kalshi lists many strikes per game and
    Polymarket a narrower set, so expect a subset of Kalshi's ladder.
    """
    kalshi_games, poly_games = await asyncio.gather(
        fetch_kalshi_totals(resolver=resolver, sport=sport),
        fetch_polymarket_us_totals(resolver=resolver, sport=sport),
    )
    matches = match_games(kalshi_games, poly_games)
    log.info(
        "discovery[%s totals]: kalshi=%d polymarket_us=%d matched=%d",
        sport, len(kalshi_games), len(poly_games), len(matches),
    )
    return [match_to_event_group(m) for m in matches]


async def discover_mlb_event_groups() -> list[EventGroup]:
    """Run one MLB discovery pass and return the EventGroups to register."""
    return await discover_team_sport_event_groups("mlb", MLB_RESOLVER)


async def discover_tennis_event_groups() -> list[EventGroup]:
    """Run one ATP+WTA discovery pass and return the EventGroups to register.

    Kalshi's tennis tickers embed a "trading day" that can differ from the
    match's UTC date, so we allow a 1-day tolerance when matching.
    """
    kalshi_games, poly_games = await asyncio.gather(
        fetch_kalshi_tennis_matches(),
        fetch_polymarket_us_tennis(),
    )
    matches = match_games(kalshi_games, poly_games, date_tolerance_days=1)
    return [match_to_event_group(m) for m in matches]


async def discover_all_event_groups() -> tuple[list[EventGroup], bool]:
    """Aggregate discovery across every sport we currently support.

    Returns ``(groups, complete)``. ``complete`` is False when any sub-pass
    raised, which matters because the caller retires groups missing from the
    result — and a transient venue error must not be read as "these games no
    longer exist".
    """
    results = await asyncio.gather(
        *(discover_team_sport_event_groups(s, r) for s, r in TEAM_SPORTS),
        *(discover_totals_event_groups(s, r) for s, r in TOTALS_SPORTS),
        discover_tennis_event_groups(),
        return_exceptions=True,
    )
    groups: list[EventGroup] = []
    complete = True
    for r in results:
        if isinstance(r, BaseException):
            log.exception("discovery sub-pass failed", exc_info=r)
            complete = False
            continue
        groups.extend(r)
    return groups, complete


class DiscoveryService:
    """Periodically re-runs discovery and syncs event groups into AppState.

    On each pass:
      1. Run discovery (MLB only for now).
      2. Upsert each match as an EventGroup in the DB.
      3. Register with the engine.
      4. Trigger ``AppState.restart_ingest`` if any groups changed.

    Existing groups from other sources are left alone.
    """

    def __init__(
        self,
        app_state,  # forward-declared to avoid circular import; AppState from backend.state
        *,
        interval_s: float = 300.0,
    ) -> None:
        self._state = app_state
        self._interval_s = interval_s
        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_evt.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def run_once(self) -> int:
        """Public: run one discovery pass. Returns count of groups registered/updated."""
        try:
            groups, complete = await discover_all_event_groups()
        except Exception:
            log.exception("discovery pass failed")
            return 0

        changed = False
        for group in groups:
            existing = self._state.event_groups.get(group.id)
            if existing == group:
                continue
            async with session_scope() as session:
                await repo.upsert_event_group(session, group)
            self._state.event_groups[group.id] = group
            if existing is None:
                self._state.engine.register_group(group)
            else:
                self._state.engine.unregister_group(group.id)
                self._state.engine.register_group(group)
            changed = True

        if complete:
            changed |= await self._retire_missing({g.id for g in groups})
        else:
            log.warning(
                "discovery incomplete — skipping retirement so a venue error "
                "is not mistaken for delisted games"
            )

        if changed:
            await self._state.restart_ingest()
        log.info("discovery: registered/updated %d groups (changed=%s)", len(groups), changed)
        return len(groups)

    async def _retire_missing(self, found_ids: set[str]) -> bool:
        """Remove discovery-sourced groups this pass no longer found.

        A game whose markets are delisted or whose venue tokens have rotated
        stops matching, but used to linger forever through restart hydration —
        still displayed, still priced off its last quotes, still capable of
        showing a phantom arb against the one leg that is still live.

        Only ``source="discovery"`` groups are eligible; anything registered by
        hand is left alone.
        """
        stale = [
            gid
            for gid, g in self._state.event_groups.items()
            if gid not in found_ids and g.source == "discovery"
        ]
        if not stale:
            return False
        for gid in stale:
            self._state.engine.unregister_group(gid)
            # Must come after unregistering: once unregistered the engine never
            # re-evaluates the group, so nothing else would ever empty its set.
            self._state.clear_group_opportunities(gid)
            self._state.event_groups.pop(gid, None)
            async with session_scope() as session:
                await repo.delete_event_group(session, gid)
        log.info("discovery: retired %d group(s) no longer offered: %s", len(stale), stale[:5])
        return True

    async def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                await self.run_once()
            except Exception:
                log.exception("discovery loop iteration failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_evt.wait(), timeout=self._interval_s)
