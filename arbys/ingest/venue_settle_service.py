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

The mirror case is a **second phase**. `/account` reads a ticket as open while
any of its outcomes has no ``paper_settlement`` row, whatever the position
says, so a settlement whose *record* was dropped -- cash paid, position
zeroed -- leaves every ticket on it open forever, and the first phase never
sees it because nothing is held. ``unrecorded_outcomes`` lists traded
outcomes with no record; for any the broker no longer holds, the venue's
result is written as a record only. ``settle_outcome_async`` moves no cash for
an outcome nobody holds, so that is safe by construction.

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

# Seconds between lookups, per venue. Polymarket's gateway throttles at about
# five requests with Retry-After 10 (see adapters/settlement.py), so pacing it
# at a second wastes far less time than tripping it does.
DEFAULT_SPACING_S: dict[str, float] = {"kalshi": 0.2, "polymarket_us": 1.0}


@dataclass
class VenueSettleResult:
    """One pass, for the admin endpoint, /health and the log."""

    checked: int = 0
    settled: list[tuple[str, str, Decimal]] = field(default_factory=list)
    unresolved: list[tuple[str, str]] = field(default_factory=list)
    skipped_not_started: int = 0
    # Already paid out once; closed without paying again.
    ghosts_closed: list[tuple[str, str]] = field(default_factory=list)
    # Paid out and closed, but the settlement record was dropped. Record
    # written, no cash moved.
    recorded: list[tuple[str, str, Decimal]] = field(default_factory=list)
    finished_at: datetime | None = None


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
        unrecorded_outcomes: Callable[[], Awaitable[list[tuple[str, str]]]] | None = None,
        interval_s: float = 900.0,
        request_spacing_s: dict[str, float] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._brokers = brokers
        self._account_ids = list(account_ids)
        self._resolvers = resolvers
        self._event_groups = event_groups
        self._mark_group_settled = mark_group_settled
        self._already_settled = already_settled
        self._unrecorded_outcomes = unrecorded_outcomes
        self._interval_s = interval_s
        self._spacing_s = DEFAULT_SPACING_S if request_spacing_s is None else request_spacing_s
        self._now = now
        self._task: asyncio.Task[None] | None = None
        self._manual: asyncio.Task[VenueSettleResult] | None = None
        # One pass at a time: the timer and the admin button share this.
        self._lock = asyncio.Lock()
        # Cumulative, for /health.
        self.settled_total = 0
        self.ghosts_closed_total = 0
        self.recorded_total = 0
        self.last_result: VenueSettleResult | None = None

    @property
    def last_unresolved(self) -> int:
        return 0 if self.last_result is None else len(self.last_result.unresolved)

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        for task in (self._task, self._manual):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._task = None
        self._manual = None

    def trigger(self) -> bool:
        """Start a pass in the background. False if one is already running.

        A pass paces Polymarket at a second a request and waits out its 429s,
        so it takes minutes -- too long to hold an HTTP request open behind a
        proxy. The result lands in `last_result` and /health.
        """
        if self.running or (self._manual is not None and not self._manual.done()):
            return False
        self._manual = asyncio.create_task(self.settle_once())
        return True

    def reset_counters(self) -> None:
        self.settled_total = 0
        self.ghosts_closed_total = 0
        self.recorded_total = 0
        self.last_result = None

    def _not_started(self) -> set[tuple[str, str]]:
        """(venue, outcome) for legs of games still to come."""
        now = self._now()
        out: set[tuple[str, str]] = set()
        for group in self._event_groups.values():
            start = group.start_time
            if start is None:
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
            if start > now:
                out.update((leg.venue_id, leg.outcome_id) for leg in group.legs)
        return out

    def _close_group(self, venue_id: str, outcome_id: str) -> None:
        for group in self._event_groups.values():
            for leg in group.legs:
                if leg.venue_id == venue_id and leg.outcome_id == outcome_id:
                    self._mark_group_settled(group.id)
                    return

    async def _held(self, broker: PaperExecutionAdapter) -> set[str]:
        held: set[str] = set()
        for account_id in self._account_ids:
            try:
                positions = await broker.get_positions(account_id)
            except KeyError:
                continue  # never funded on this venue
            held.update(o for o, q in positions.items() if q != 0)
        return held

    async def _resolve(self, venue_id: str, outcome_id: str) -> Decimal | None:
        value = await self._resolvers[venue_id](outcome_id)
        spacing = self._spacing_s.get(venue_id, 0.0)
        if spacing:
            await asyncio.sleep(spacing)
        return value

    async def settle_once(self) -> VenueSettleResult:
        async with self._lock:
            return await self._settle_once()

    async def _settle_once(self) -> VenueSettleResult:
        result = VenueSettleResult()
        pending = self._not_started()
        held_by_venue: dict[str, set[str]] = {}
        for venue_id, broker in self._brokers.items():
            if venue_id not in self._resolvers:
                continue
            held = await self._held(broker)
            held_by_venue[venue_id] = held
            for outcome_id in sorted(held):
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
                    self._close_group(venue_id, outcome_id)
                    continue
                result.checked += 1
                value = await self._resolve(venue_id, outcome_id)
                if value is None:
                    result.unresolved.append((venue_id, outcome_id))
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
                self._close_group(venue_id, outcome_id)

        if self._unrecorded_outcomes is not None:
            for venue_id, outcome_id in await self._unrecorded_outcomes():
                broker = self._brokers.get(venue_id)
                if broker is None or venue_id not in self._resolvers:
                    continue
                if outcome_id in held_by_venue.get(venue_id, set()):
                    continue  # the first phase owns it
                if (venue_id, outcome_id) in pending:
                    continue
                result.checked += 1
                value = await self._resolve(venue_id, outcome_id)
                if value is None:
                    result.unresolved.append((venue_id, outcome_id))
                    continue
                # A fill may have landed while we waited on the venue. If so
                # the first phase will settle it next pass, paying properly.
                if outcome_id in await self._held(broker):
                    continue
                log.info(
                    "venue-settle %s %s recorded at %s (closed, record was missing)",
                    venue_id,
                    outcome_id,
                    value,
                )
                await broker.settle_outcome_async(
                    outcome_id, value, source=SETTLEMENT_SOURCE
                )
                result.recorded.append((venue_id, outcome_id, value))
                self._close_group(venue_id, outcome_id)

        result.finished_at = self._now()
        self.settled_total += len(result.settled)
        self.ghosts_closed_total += len(result.ghosts_closed)
        self.recorded_total += len(result.recorded)
        self.last_result = result
        if result.checked or result.ghosts_closed:
            log.info(
                "venue-settle pass: checked=%d settled=%d recorded=%d unresolved=%d "
                "ghosts_closed=%d not_started=%d",
                result.checked,
                len(result.settled),
                len(result.recorded),
                len(result.unresolved),
                len(result.ghosts_closed),
                result.skipped_not_started,
            )
            for venue_id, outcome_id in result.unresolved:
                log.info("venue-settle unresolved %s %s", venue_id, outcome_id)
        return result

    async def _run(self) -> None:
        while True:
            try:
                await self.settle_once()
            except Exception:
                log.exception("venue-settle pass failed")
            await asyncio.sleep(self._interval_s)
