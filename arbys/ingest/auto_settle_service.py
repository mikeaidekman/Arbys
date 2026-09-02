"""Auto-settlement for paper positions.

Three routes to a result, in descending order of trust:

1. **The venue says the event ended.** Polymarket US publishes ``ended`` per
   event; discovery parses it onto ``VenueGame`` and the matcher resolves it
   onto ``EventGroup.ended``. This is a statement of fact, not an inference,
   so it settles on the first sighting with no confirmation delay.
2. **The group was retired.** Discovery removes a group when a complete pass
   stops finding it, which for a finished fixture is the last moment anything
   knows the group existed. Settled from the final book on the way out.
3. **The price heuristic.** A leg's ask pinned at or above ``ASK_THRESHOLD``
   for ``CONSECUTIVE_HITS`` consecutive polls. Retained for venues that
   publish no lifecycle at all -- Kalshi publishes none -- but it is the
   weakest signal here and is now fenced on both sides.

**Why routes 1 and 2 exist.** Settlement used to be route 3 alone, and
inferring resolution from live prices fails at both ends:

- *Too late, and permanently.* When a game finishes the venue delists the
  market, quotes stop, and ``QuoteBook.get`` withholds them past
  ``ARBYS_QUOTE_MAX_AGE_S``. ``_winning_side`` then reads ``None`` forever and
  the group can never settle -- while discovery retires it, so nothing looks
  at it again. Measured against the hosted account on 2026-09-02: **39 of 204
  open positions were on games that had already been played, holding ~$2,130
  of a $2,883 book**, every one of them quoting ``mark: null``. Cash on both
  tradeable venues had fallen to $1,177 of a $4,000 start.
- *Too early.* A heavy pre-game favourite sits at 0.99 for days. On the local
  ledger **908 of 2,013 settlements fired before the game date**, up to 13 days
  early. Nothing then stopped the still-live market being traded again, and
  those positions could never settle a second time.

Both are the same flaw -- resolution inferred from prices -- so both are fixed
by preferring a lifecycle signal and, where none exists, refusing to guess
before kickoff.

**Where a decisive book is required, it is required honestly.** Routes 1 and 2
read the *last known* quote, stale included, because a dark market is exactly
the case they exist for. If that final book does not name a winner the group
is left open and logged, never settled on a guess: an unsettled position is
visible and recoverable, a wrongly settled one silently corrupts the ledger.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from ..shared.paper_broker import PaperExecutionAdapter
from ..shared.quotebook import QuoteBook
from ..shared.types import EventGroup, Quote

log = logging.getLogger(__name__)

# Live-market confidence. Deliberately extreme: while a market is still
# trading, anything short of near-certainty is a price, not a result.
ASK_THRESHOLD = Decimal("0.99")
CONSECUTIVE_HITS = 3

# Confidence required of a book we have independent reason to believe is
# final -- the venue said the event ended, or the group was retired. Lower
# than ASK_THRESHOLD because the last frame before a delisting is not always
# the settled price, and holding out for 0.99 there is what left positions
# frozen indefinitely. Still high enough that a mid-game book cannot clear it.
RESOLVED_ASK_THRESHOLD = Decimal("0.90")


class AutoSettleService:
    def __init__(
        self,
        *,
        event_groups: dict[str, EventGroup],
        brokers: dict[str, PaperExecutionAdapter],
        quotebook: QuoteBook,
        interval_s: float = 10.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._event_groups = event_groups
        self._brokers = brokers
        self._book = quotebook
        self._interval_s = interval_s
        self._now = now or (lambda: datetime.now(UTC))
        self._task: asyncio.Task | None = None
        self._settled: set[str] = set()
        self._hits: dict[str, tuple[bool, int]] = {}
        # Groups seen on a previous tick, so a disappearance can be noticed.
        # Retirement is the last moment anything knows the group existed.
        self._seen: dict[str, EventGroup] = {}
        # Ended or retired, but the final book named no winner. Tracked so the
        # warning is logged once rather than every tick, and so the count is
        # answerable.
        self._unresolved: dict[str, EventGroup] = {}

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

    def clear_settled(self) -> None:
        """Forget which groups have been auto-settled; used after a reset."""
        self._settled.clear()
        self._hits.clear()
        self._seen.clear()
        self._unresolved.clear()

    def is_settled(self, event_group_id: str) -> bool:
        """Whether this group has been settled and must not be traded again.

        Accepts a synthetic ``<group>:<venue>`` opportunity id as well as a
        plain group id -- ``engine_runtime`` publishes the former for a
        venue's intra-venue edge, and a settled game is settled whichever id
        names it.
        """
        return event_group_id.split(":", 1)[0] in self._settled

    def unresolved_groups(self) -> tuple[str, ...]:
        """Groups that finished without a decisive book, so are still open."""
        return tuple(sorted(self._unresolved))

    async def tick(self) -> None:
        current = dict(self._event_groups)

        # Retired groups first. They are already gone from the registry, so
        # this tick is the last chance to resolve them; leaving them is what
        # stranded capital indefinitely.
        for group_id, group in list(self._seen.items()):
            if group_id in current:
                continue
            del self._seen[group_id]
            self._hits.pop(group_id, None)
            if group_id not in self._settled:
                await self._settle_final(group, reason="retired")

        for group_id, group in current.items():
            self._seen[group_id] = group
            if group_id in self._settled:
                continue

            # The venue's own lifecycle beats anything inferred from a price.
            if group.ended:
                await self._settle_final(group, reason="venue reported ended")
                continue

            # A game that has not kicked off has no result to read, whatever
            # its price is doing. `None` start time means unknown rather than
            # "not started", so it does not block -- a hand-registered group
            # reports none and would otherwise never settle at all.
            if not self._has_started(group):
                self._hits.pop(group_id, None)
                continue

            winner = self._winning_side(group)
            if winner is None:
                self._hits.pop(group_id, None)
                continue
            prev_side, prev_count = self._hits.get(group_id, (winner, 0))
            if prev_side != winner:
                prev_count = 0
            new_count = prev_count + 1
            self._hits[group_id] = (winner, new_count)
            if new_count >= CONSECUTIVE_HITS:
                await self._settle_group(group, winner, reason="price pinned")
                self._settled.add(group_id)
                self._hits.pop(group_id, None)

    async def _settle_final(self, group: EventGroup, *, reason: str) -> None:
        """Settle from the last known book, stale included.

        The whole point is that the market has gone dark, so `QuoteBook.get`
        would withhold exactly the quotes needed. When no winner can be read
        the group is left open rather than guessed at.
        """
        if not self._has_started(group):
            # Retired or delisted before kickoff. A fixture that moved, or a
            # market the venue pulled -- either way nobody won it, and
            # settling would invent a result for a game that never happened.
            log.info("auto-settle skipped group=%s (%s, not started)", group.id, reason)
            return
        winner = self._winning_side(
            group, threshold=RESOLVED_ASK_THRESHOLD, allow_stale=True
        )
        if winner is None:
            if group.id not in self._unresolved:
                self._unresolved[group.id] = group
                log.warning(
                    "auto-settle cannot resolve group=%s (%s): final book names "
                    "no winner, position left open",
                    group.id,
                    reason,
                )
            return
        await self._settle_group(group, winner, reason=reason)
        self._settled.add(group.id)
        self._unresolved.pop(group.id, None)

    def _has_started(self, group: EventGroup) -> bool:
        if group.start_time is None:
            return True
        start = group.start_time
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        return self._now() >= start

    def _quote(self, outcome_id: str, *, allow_stale: bool) -> Quote | None:
        if not allow_stale:
            return self._book.get(outcome_id)
        entry = self._book.get_with_age(outcome_id)
        return None if entry is None else entry[0]

    def _winning_side(
        self,
        group: EventGroup,
        *,
        threshold: Decimal = ASK_THRESHOLD,
        allow_stale: bool = False,
    ) -> bool | None:
        yes_hit = False
        no_hit = False
        for leg in group.legs:
            q = self._quote(leg.outcome_id, allow_stale=allow_stale)
            if q is None or q.ask is None:
                continue
            if q.ask >= threshold:
                if leg.is_yes_side:
                    yes_hit = True
                else:
                    no_hit = True
        if yes_hit and not no_hit:
            return True
        if no_hit and not yes_hit:
            return False
        return None

    async def _settle_group(
        self, group: EventGroup, winner: bool, *, reason: str
    ) -> None:
        log.info(
            "auto-settle group=%s winner=%s legs=%d (%s)",
            group.id,
            "YES" if winner else "NO",
            len(group.legs),
            reason,
        )
        for leg in group.legs:
            broker = self._brokers.get(leg.venue_id)
            if broker is None:
                continue
            resolved = Decimal("1") if leg.is_yes_side == winner else Decimal("0")
            try:
                await broker.settle_outcome_async(leg.outcome_id, resolved)
            except Exception:
                log.exception(
                    "auto-settle failed for outcome=%s venue=%s",
                    leg.outcome_id,
                    leg.venue_id,
                )

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("auto-settle tick failed")
            await asyncio.sleep(self._interval_s)
