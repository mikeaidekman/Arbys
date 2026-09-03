"""Automatic execution of published arbitrage opportunities.

Consumes `AppState`'s opportunity broadcast and submits a paper ticket for
every edge it receives. Event-driven rather than polled: tradeable edges are
expected to exist for short moments, so reacting on the tick that created one
is the difference between filling and missing.

**Every published opportunity is already net-positive of fees** — both
detectors gate on `net_edge_per_contract(...) <= 0` before publishing — so
there is no edge test here. "Any opportunity received" is the whole trigger.

Two filters narrow what reaches a ticket, and both are measurements
rather than profitability judgements:

* **Cross-venue only.** A complementary (same-venue) edge is one venue's own
  book crossed against itself, which a co-located taker clears in
  milliseconds. Over 2026-08-27 those were 5 fills of 244 attempts worth
  $0.41, while accounting for 537 of 1,149 missed tickets and half the
  forgone edge -- the large ones (a 1002bps "arb" on one venue's own book)
  being one-sided stale quotes, not arbitrage. This is a trigger narrowing
  and so brushes the spec's non-goal; unlike an edge floor it has a
  mechanism behind it, and it is switchable via ARBYS_CROSS_VENUE_ONLY.
  That flag is system-wide: with it on, `EngineRuntime` never publishes a
  same-venue edge in the first place, so the check here is a last line of
  defence against a future publisher rather than the only one.
* **One non-fill row per group per window.** A miss deliberately starts no
  cooldown -- a vanished edge is no reason to stop watching -- but
  `_opp_fingerprint` includes depth-derived `qty`, so a live book republishes
  on nearly every tick and each republish missed again. That wrote 1,149
  missed tickets describing 116 distinct groups, 74% of the repeats landing
  in the *same second*, which put per-ticket fill rate at 16% against a
  per-group 94%. Attempts continue at full rate; only the duplicate audit row
  is suppressed. Nothing counts the suppressed attempts, and deliberately so:
  every published opportunity is already persisted to `arb_opportunity`, so
  attempt volume stays recoverable from the tape.

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
from typing import Protocol

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


class SubmitTicket(Protocol):
    """How the service submits, with control over the audit row.

    `record_nonfill=False` asks the ticket service to run the attempt exactly
    as normal but skip writing the row *if* the outcome is a miss or a
    pre-execution rejection. A fill is always recorded: its row has to exist
    before the router runs, because `paper_order.ticket_id` is an FK to it.
    """

    def __call__(
        self, opp: ArbOpportunity, *, record_nonfill: bool
    ) -> Awaitable[str]: ...


class AutoTradeService:
    def __init__(
        self,
        *,
        subscribe: Callable[[], asyncio.Queue[ArbOpportunity]],
        unsubscribe: Callable[[asyncio.Queue[ArbOpportunity]], None],
        submit: SubmitTicket,
        would_breach_cap: Callable[[ArbOpportunity], bool],
        would_start_too_late: Callable[[ArbOpportunity], bool] = lambda _opp: False,
        enabled: Callable[[], bool],
        cooldown_s: float,
        cross_venue_only: Callable[[], bool] = lambda: True,
        nonfill_log_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._subscribe = subscribe
        self._unsubscribe = unsubscribe
        self._submit = submit
        self._would_breach_cap = would_breach_cap
        self._would_start_too_late = would_start_too_late
        self._enabled = enabled
        self._cooldown_s = cooldown_s
        self._cross_venue_only = cross_venue_only
        self._nonfill_log_s = nonfill_log_s
        self._clock = clock
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue[ArbOpportunity] | None = None
        self._stop_event: asyncio.Event | None = None
        # group id -> monotonic deadline before which that group is ignored.
        self._cooldown_until: dict[str, float] = {}
        # group id -> monotonic deadline before which a *non-fill* outcome for
        # that group is attempted but not written to the ticket log.
        self._nonfill_logged_until: dict[str, float] = {}

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
            # `asyncio.shield` means this cannot be the shielded task's own
            # cancellation - it can only be this `wait_for` await itself
            # being cancelled from outside (e.g. uvicorn's forced-shutdown
            # deadline cancelling the lifespan task). That cancellation must
            # keep propagating so the caller (AppState.shutdown()) knows
            # shutdown was interrupted, rather than silently carrying on to
            # stop auto_settle_service and pnl_service as though this step
            # had completed normally.
            raise
        except Exception:
            log.exception("auto-trade consumer task ended with an unhandled exception")
        finally:
            self._task = None
            if self._queue is not None:
                self._unsubscribe(self._queue)
                self._queue = None

    def clear_cooldowns(self) -> None:
        """Forget every group's cooldown; used after a portfolio reset.

        Clears the non-fill log window too: a reset wipes the ticket log, so
        suppressing the next miss as a "duplicate" of a row that no longer
        exists would lose it for nothing.
        """
        self._cooldown_until.clear()
        self._nonfill_logged_until.clear()

    async def handle(self, opp: ArbOpportunity) -> str | None:
        """Submit one opportunity, or return None having deliberately skipped it.

        The enabled check is re-read here rather than captured at construction,
        so the flag governs behaviour and not merely whether a task exists.
        """
        if not self._enabled():
            return None

        # Intra-venue: every leg on one venue. Read from the legs rather than
        # from the `<group>:<venue>` id `engine_runtime` publishes these under,
        # so the rule follows what the ticket would actually do and not a
        # naming convention that could change.
        if self._cross_venue_only() and len({leg.venue_id for leg in opp.legs}) < 2:
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

        # Same treatment as the cap, for the same reason: an edge on a game a
        # fortnight away can persist for days and republishes on every depth
        # tick, so a recorded rejection per tick would say only "still too
        # early" all night. `submit_arb_ticket` remains authoritative.
        if self._would_start_too_late(opp):
            return None

        record = self._should_record_nonfill(group_id)
        status = await self._submit(opp, record_nonfill=record)
        if status == "filled":
            # A fill ends the suppression window: the group has done something
            # new, so the *next* miss on it is news again rather than more of
            # the burst this window exists to collapse.
            self._nonfill_logged_until.pop(group_id, None)
            if self._cooldown_s > 0:
                self._cooldown_until[group_id] = self._clock() + self._cooldown_s
        elif record:
            self._nonfill_logged_until[group_id] = self._clock() + self._nonfill_log_s
        return status

    def _should_record_nonfill(self, group_id: str) -> bool:
        """Whether a miss or rejection on this group is worth an audit row.

        The window suppresses only the *row*; the attempt above runs either
        way, so an edge that comes back is still filled. `nonfill_log_s <= 0`
        disables suppression entirely, matching how `cooldown_s` reads 0.
        """
        if self._nonfill_log_s <= 0:
            return True
        until = self._nonfill_logged_until.get(group_id)
        if until is None:
            return True
        if self._clock() < until:
            return False
        # Expired: drop it so this dict stays the size of the currently
        # suppressed set rather than of every group ever missed.
        self._nonfill_logged_until.pop(group_id, None)
        return True

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
