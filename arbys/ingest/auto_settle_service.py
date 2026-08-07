"""Heuristic auto-settlement for paper positions.

Watches the quote book for legs whose ask has been pinned at or above
``ASK_THRESHOLD`` for ``CONSECUTIVE_HITS`` consecutive polls. When that
condition is met, treats the leg's side (``is_yes_side``) as the winning
side of the event group's canonical proposition and settles every leg in
that group via ``PaperExecutionAdapter.settle_outcome_async``: winning-side
legs to 1, opposing-side legs to 0.

This is deliberately a heuristic — venues expose no realtime resolution
feed we currently consume. Once a group is settled it is not re-checked.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from decimal import Decimal

from ..shared.paper_broker import PaperExecutionAdapter
from ..shared.quotebook import QuoteBook
from ..shared.types import EventGroup

log = logging.getLogger(__name__)

ASK_THRESHOLD = Decimal("0.99")
CONSECUTIVE_HITS = 3


class AutoSettleService:
    def __init__(
        self,
        *,
        event_groups: dict[str, EventGroup],
        brokers: dict[str, PaperExecutionAdapter],
        quotebook: QuoteBook,
        interval_s: float = 10.0,
    ) -> None:
        self._event_groups = event_groups
        self._brokers = brokers
        self._book = quotebook
        self._interval_s = interval_s
        self._task: asyncio.Task | None = None
        self._settled: set[str] = set()
        self._hits: dict[str, tuple[bool, int]] = {}

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
        """Forget which groups have been auto-settled; used after a portfolio reset."""
        self._settled.clear()
        self._hits.clear()

    async def tick(self) -> None:
        for group_id, group in list(self._event_groups.items()):
            if group_id in self._settled:
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
                await self._settle_group(group, winner)
                self._settled.add(group_id)
                self._hits.pop(group_id, None)

    def _winning_side(self, group: EventGroup) -> bool | None:
        yes_hit = False
        no_hit = False
        for leg in group.legs:
            q = self._book.get(leg.outcome_id)
            if q is None or q.ask is None:
                continue
            if q.ask >= ASK_THRESHOLD:
                if leg.is_yes_side:
                    yes_hit = True
                else:
                    no_hit = True
        if yes_hit and not no_hit:
            return True
        if no_hit and not yes_hit:
            return False
        return None

    async def _settle_group(self, group: EventGroup, winner: bool) -> None:
        log.info(
            "auto-settle group=%s winner=%s (legs=%d)",
            group.id,
            "YES" if winner else "NO",
            len(group.legs),
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
