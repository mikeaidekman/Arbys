"""Discovery service — periodic auto-registration of cross-venue sports event groups."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from ..db import repositories as repo
from ..db.session import session_scope
from ..shared.types import EventGroup
from .kalshi_sports import fetch_kalshi_mlb_games
from .kalshi_tennis import fetch_kalshi_tennis_matches
from .matcher import match_games, match_to_event_group
from .polymarket_sports import fetch_polymarket_sports_games
from .polymarket_tennis import fetch_polymarket_tennis_matches
from .teams import MLB_RESOLVER

log = logging.getLogger(__name__)


async def discover_mlb_event_groups() -> list[EventGroup]:
    """Run one MLB discovery pass and return the EventGroups to register."""
    kalshi_games, poly_games = await asyncio.gather(
        fetch_kalshi_mlb_games(resolver=MLB_RESOLVER),
        fetch_polymarket_sports_games(resolver=MLB_RESOLVER, sport="mlb"),
    )
    matches = match_games(kalshi_games, poly_games)
    return [match_to_event_group(m) for m in matches]


async def discover_tennis_event_groups() -> list[EventGroup]:
    """Run one ATP+WTA discovery pass and return the EventGroups to register.

    Kalshi's tennis tickers embed a "trading day" that can differ from the
    match's UTC date, so we allow a 1-day tolerance when matching.
    """
    kalshi_games, poly_games = await asyncio.gather(
        fetch_kalshi_tennis_matches(),
        fetch_polymarket_tennis_matches(),
    )
    matches = match_games(kalshi_games, poly_games, date_tolerance_days=1)
    return [match_to_event_group(m) for m in matches]


async def discover_all_event_groups() -> list[EventGroup]:
    """Aggregate discovery across every sport we currently support."""
    results = await asyncio.gather(
        discover_mlb_event_groups(),
        discover_tennis_event_groups(),
        return_exceptions=True,
    )
    groups: list[EventGroup] = []
    for r in results:
        if isinstance(r, BaseException):
            log.exception("discovery sub-pass failed", exc_info=r)
            continue
        groups.extend(r)
    return groups


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
            groups = await discover_all_event_groups()
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
        if changed:
            await self._state.restart_ingest()
        log.info("discovery: registered/updated %d groups (changed=%s)", len(groups), changed)
        return len(groups)

    async def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                await self.run_once()
            except Exception:
                log.exception("discovery loop iteration failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_evt.wait(), timeout=self._interval_s)
