"""Settle open positions at the result the venue itself published.

``AutoSettleService`` reads results out of the quote book, and its two
lifecycle routes -- "venue said ended" and "group was retired" -- both depend
on state that lives only in memory. Two ordinary events strand a position
forever:

- **A restart.** Retirement is detected by comparing the registry against
  ``_seen``, which starts empty. A group retired before the restart, or while
  the app was down, is never seen leaving, so nothing settles it.
- **A dropped write.** The broker zeroes the position in memory and the
  ``paper_position`` upsert is lost; the next boot hydrates it open again,
  against a market that has since gone dark and a group discovery has retired.

Either way the position's market is finished and quotes nothing, so no
price-based route can ever reach it. Reported 2026-10-03 as "a large number of
open trades for events that finished well in the past".

This service needs neither the quote book nor the registry to reach them. It
walks the brokers' own open positions and asks each venue what that market
settled at (``adapters/settlement.py``). A venue that has not finalised a
market answers ``None`` and the position is left for the next pass -- this
never guesses, so it is safe to run on every position on a timer and at boot.

One case must **not** be paid. A dropped position write after a landed
settlement leaves a ghost: the cash already moved, and its
``paper_settlement`` row says so, but the position hydrates as held. Paying it
at the venue's result would pay it twice, so a position with a settlement row
on record is closed without cash (``close_already_settled``). Note this rests
on balance writes being absolute: a payout credited in memory reaches the
stored balance with the next balance write of any kind, and the cash sweep
writes one every minute.

The only use of the registry is to skip asking about games that have not
started, which is most open positions on any given day, and to tell
``AutoSettleService`` a group is done so it is neither traded nor settled
again. Like every service here it must not import ``arbys/backend/``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from ..shared.paper_broker import PaperExecutionAdapter
from ..shared.types import EventGroup

log = logging.getLogger(__name__)

Resolver = Callable[[str], Awaitable[Decimal | None]]

SETTLEMENT_SOURCE = "venue_result"


@dataclass
class VenueSettleResult:
    """One pass, for the admin endpoint and the log."""

    checked: int = 0
    settled: list[tuple[str, str, Decimal]] = field(default_factory=list)
    unresolved: int = 0
    skipped_not_started: int = 0
    # Already paid out once; closed without paying again.
    ghosts_closed: list[tuple[str, str]] = field(default_factory=list)


class VenueSettleService:
    def __init__(
        self,
        *,
        brokers: dict[str, PaperExecutionAdapter],
        account_ids: list[str],
        resolvers: dict[str, Resolver],
        event_groups: dict[str, EventGroup],
        mark_group_settled: Callable[[str], None] = lambda _gid: None,
        already_settled: Callable[[str], Awaitable[bool]] | None = None,
        interval_s: float = 900.0,
        request_spacing_s: float = 0.2,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._brokers = brokers
        self._account_ids = list(account_ids)
        self._resolvers = resolvers
        self._event_groups = event_groups
        self._mark_group_settled = mark_group_settled
        self._already_settled = already_settled
        self._interval_s = interval_s
        self._spacing_s = request_spacing_s
        self._now = now
        self._task: asyncio.Task[None] | None = None
        # One pass at a time: the timer and the admin button share this.
        self._lock = asyncio.Lock()
        # Cumulative, for /health.
        self.settled_total = 0
        self.last_unresolved = 0
        self.ghosts_closed_total = 0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None

    def _not_started(self) -> dict[tuple[str, str], str]:
        """(venue, outcome) -> group id, for legs of games still to come."""
        now = self._now()
        out: dict[tuple[str, str], str] = {}
        for group in self._event_groups.values():
            start = group.start_time
            if start is None:
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
            if start > now:
                for leg in group.legs:
                    out[(leg.venue_id, leg.outcome_id)] = group.id
        return out

    def _group_of(self, venue_id: str, outcome_id: str) -> str | None:
        for group in self._event_groups.values():
            for leg in group.legs:
                if leg.venue_id == venue_id and leg.outcome_id == outcome_id:
                    return group.id
        return None

    async def settle_once(self) -> VenueSettleResult:
        async with self._lock:
            return await self._settle_once()

    async def _settle_once(self) -> VenueSettleResult:
        result = VenueSettleResult()
        pending = self._not_started()
        for venue_id, broker in self._brokers.items():
            resolve = self._resolvers.get(venue_id)
            if resolve is None:
                continue
            open_outcomes: set[str] = set()
            for account_id in self._account_ids:
                try:
                    positions = await broker.get_positions(account_id)
                except KeyError:
                    continue  # never funded on this venue
                open_outcomes.update(o for o, q in positions.items() if q != 0)
            for outcome_id in sorted(open_outcomes):
                if (venue_id, outcome_id) in pending:
                    result.skipped_not_started += 1
                    continue
                if self._already_settled is not None and await self._already_settled(
                    outcome_id
                ):
                    log.warning(
                        "venue-settle %s %s already has a settlement row; closing "
                        "without paying again (a dropped position write)",
                        venue_id,
                        outcome_id,
                    )
                    await broker.close_already_settled(outcome_id)
                    result.ghosts_closed.append((venue_id, outcome_id))
                    group_id = self._group_of(venue_id, outcome_id)
                    if group_id is not None:
                        self._mark_group_settled(group_id)
                    continue
                result.checked += 1
                value = await resolve(outcome_id)
                if self._spacing_s:
                    await asyncio.sleep(self._spacing_s)
                if value is None:
                    result.unresolved += 1
                    continue
                log.info(
                    "venue-settle %s %s at %s (venue published result)",
                    venue_id,
                    outcome_id,
                    value,
                )
                await broker.settle_outcome_async(
                    outcome_id, value, source=SETTLEMENT_SOURCE
                )
                result.settled.append((venue_id, outcome_id, value))
                group_id = self._group_of(venue_id, outcome_id)
                if group_id is not None:
                    self._mark_group_settled(group_id)
        self.settled_total += len(result.settled)
        self.ghosts_closed_total += len(result.ghosts_closed)
        self.last_unresolved = result.unresolved
        if result.checked or result.ghosts_closed:
            log.info(
                "venue-settle pass: checked=%d settled=%d unresolved=%d "
                "ghosts_closed=%d not_started=%d",
                result.checked,
                len(result.settled),
                result.unresolved,
                len(result.ghosts_closed),
                result.skipped_not_started,
            )
        return result

    async def _run(self) -> None:
        while True:
            try:
                await self.settle_once()
            except Exception:
                log.exception("venue-settle pass failed")
            await asyncio.sleep(self._interval_s)
