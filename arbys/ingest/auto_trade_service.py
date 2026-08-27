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

# How long stop() gives an in-flight handle() to finish on its own before
# falling back to cancel(). An in-flight call is a submission already past
# the position-cap pre-check: cancelling it mid-await can land inside
# `submit_arb_ticket` after it has written a `pending` paper_ticket row and
# applied fills to the in-memory broker, leaving that row stuck at `pending`
# forever with nothing on `/health` pointing at it — the same end state
# CLAUDE.md records for the 2026-08-25 dropped-write incident, reached by a
# path the retry-and-count machinery in db/session.py cannot see, because
# `CancelledError` is a `BaseException` that its `except Exception` does not
# catch. Letting the current iteration finish avoids that; the timeout exists
# so a genuinely wedged submit cannot block shutdown forever.
STOP_TIMEOUT_S = 5.0


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
        self._stop_event: asyncio.Event | None = None
        # group id -> monotonic deadline before which that group is ignored.
        self._cooldown_until: dict[str, float] = {}

    async def start(self) -> None:
        if self._task is not None:
            if not self._task.done():
                return
            # The previous run ended without stop() ever being called to
            # notice — most likely a crash (see stop()'s docstring for the
            # normal path). Refusing to restart here would strand the
            # service permanently; unsubscribe the dead run's queue first so
            # restarting doesn't also leak a subscription.
            if self._queue is not None:
                self._unsubscribe(self._queue)
                self._queue = None
        self._stop_event = asyncio.Event()
        self._queue = self._subscribe()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Ask the consumer to finish its current iteration, then wait for it.

        An in-flight `handle()` call is a submission already past the
        position-cap pre-check, so it is signalled to stop rather than
        cancelled outright: cancelling it mid-await can land inside
        `submit_arb_ticket` after a `pending` paper_ticket row is written and
        fills are applied to the in-memory broker, abandoning that ticket
        with nothing anywhere recording it happened (see `STOP_TIMEOUT_S`).
        `asyncio.shield` keeps the wait's own timeout from reaching into the
        task; only the explicit `task.cancel()` below does that, and only
        once the grace period has passed.

        A task that ended with a real exception is logged here rather than
        swallowed — a consumer that died from a bug must be visible, and
        `start()` needs the task to actually be done (not merely thought to
        be running) to know it may restart.
        """
        if self._task is None:
            return
        task = self._task
        if self._stop_event is not None:
            self._stop_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=STOP_TIMEOUT_S)
        except TimeoutError:
            log.error(
                "auto-trade consumer did not stop within %.1fs; cancelling "
                "what may be an in-flight submit",
                STOP_TIMEOUT_S,
            )
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("auto-trade consumer task ended with an unhandled exception")
        finally:
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
        stop_event = self._stop_event
        assert queue is not None  # set by start() before the task is created
        assert stop_event is not None  # set by start() before the task is created
        # A separate task, not just a coroutine to await inline: it has to
        # keep progressing (so stop() setting the event resolves it promptly)
        # regardless of which line below is currently suspended.
        stop_wait = asyncio.create_task(stop_event.wait())
        try:
            while True:
                # Checked before blocking on the queue, not just raced against
                # it: once stop() has been asked for, an item already resting
                # in the queue is not "in-flight" and must not be picked up.
                if stop_event.is_set():
                    return
                get_next = asyncio.create_task(queue.get())
                done, _pending = await asyncio.wait(
                    {get_next, stop_wait}, return_when=asyncio.FIRST_COMPLETED
                )
                if get_next not in done:
                    get_next.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await get_next
                    return
                opp = get_next.result()
                depth = queue.qsize()
                if depth > BACKPRESSURE_WARN_QSIZE:
                    log.warning(
                        "auto-trade backpressure: %d opportunities queued "
                        "(max 100, excess is dropped silently)",
                        depth,
                    )
                # Serial on purpose, and deliberately not interrupted by
                # stop() partway through (see stop()'s docstring). Concurrent
                # tickets would also race each other on both the cash balance
                # and the position cap, and a lost race there is a real
                # oversized position rather than a missed trade.
                try:
                    await self.handle(opp)
                except Exception:
                    log.exception("auto-trade failed for group=%s", opp.event_group_id)
        finally:
            stop_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_wait
