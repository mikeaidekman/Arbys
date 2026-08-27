"""Automatic execution of published arbitrage opportunities.

Consumes `AppState`'s opportunity broadcast and submits a paper ticket for
every edge it receives. Event-driven rather than polled: tradeable edges are
expected to exist for short moments, so reacting on the tick that created one
is the difference between filling and missing.

**Every published opportunity is already net-positive of fees** — both
detectors gate on `net_edge_per_contract(...) <= 0` before publishing — so
there is no edge test here. "Any opportunity received" is the whole trigger.

Nothing in this module may import `arbys.backend`: `backend.state` imports this
package, and `backend.ticket_service` imports `backend.state`, so an import in
the other direction is a cycle. Submission and the position-cap pre-check
therefore arrive as injected callables, which also means every branch below is
testable without a database or an `AppState`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from ..shared.arb_engine import ArbOpportunity

log = logging.getLogger(__name__)

# `subscribe_opportunities` hands out a queue of maxsize=100 and publishers use
# `put_nowait` under `suppress(QueueFull)`, so a slow consumer loses
# opportunities with no error anywhere. Raising the maxsize or adding a
# per-subscriber drop counter is out of scope; a warning is enough to tell us
# whether either is ever needed.
BACKPRESSURE_WARN_QSIZE = 50


class AutoTradeService:
    def __init__(
        self,
        *,
        subscribe: Callable[[], asyncio.Queue[ArbOpportunity]],
        unsubscribe: Callable[[asyncio.Queue[ArbOpportunity]], None],
        submit: Callable[[ArbOpportunity], Awaitable[str]],
        would_breach_cap: Callable[[ArbOpportunity], bool],
        enabled: Callable[[], bool],
        cooldown_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._subscribe = subscribe
        self._unsubscribe = unsubscribe
        self._submit = submit
        self._would_breach_cap = would_breach_cap
        self._enabled = enabled
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue[ArbOpportunity] | None = None
        # group id -> monotonic deadline before which that group is ignored.
        self._cooldown_until: dict[str, float] = {}

    async def start(self) -> None:
        if self._task is not None:
            return
        self._queue = self._subscribe()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None
        if self._queue is not None:
            self._unsubscribe(self._queue)
            self._queue = None

    def clear_cooldowns(self) -> None:
        """Forget every group's cooldown; used after a portfolio reset."""
        self._cooldown_until.clear()

    async def handle(self, opp: ArbOpportunity) -> str | None:
        """Submit one opportunity, or return None having deliberately skipped it.

        The enabled check is re-read here rather than captured at construction,
        so the flag governs behaviour and not merely whether a task exists.
        """
        if not self._enabled():
            return None

        group_id = opp.event_group_id
        until = self._cooldown_until.get(group_id)
        if until is not None:
            if self._clock() < until:
                return None
            # Expired: drop the entry so this dict stays the size of the
            # currently-cooling set rather than of every group ever filled.
            self._cooldown_until.pop(group_id, None)

        # Pre-check, not enforcement: `submit_arb_ticket` remains authoritative.
        # The point is to skip *silently* — opportunities republish on
        # fingerprint change, so a capped-out group would otherwise write a
        # rejected ticket on every tick for the rest of the night, filling the
        # audit log with rows that say only "still capped".
        if self._would_breach_cap(opp):
            return None

        status = await self._submit(opp)
        if status == "filled" and self._cooldown_s > 0:
            self._cooldown_until[group_id] = self._clock() + self._cooldown_s
        return status

    async def _run(self) -> None:
        queue = self._queue
        assert queue is not None  # set by start() before the task is created
        while True:
            opp = await queue.get()
            depth = queue.qsize()
            if depth > BACKPRESSURE_WARN_QSIZE:
                log.warning(
                    "auto-trade backpressure: %d opportunities queued "
                    "(max 100, excess is dropped silently)",
                    depth,
                )
            # Serial on purpose. Concurrent tickets would race each other on
            # both the cash balance and the position cap, and a lost race there
            # is a real oversized position rather than a missed trade.
            try:
                await self.handle(opp)
            except Exception:
                log.exception("auto-trade failed for group=%s", opp.event_group_id)
