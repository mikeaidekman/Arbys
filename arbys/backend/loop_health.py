"""How far behind the event loop is running.

A remote bot's feed can only fail in ways someone can see from outside the box,
and this one failed in a way `/health` could not describe: both venue websockets
dropped every ~60s with `keepalive ping timeout`, and Polymarket then could not
finish an opening handshake at all. Both are what a *starved event loop* looks
like from the network -- a ping that is never written, a handshake whose await
is never scheduled -- and neither is distinguishable from a venue problem
without knowing whether our own loop is keeping up.

So measure it directly: sleep for a known interval and record how much longer
than that it actually took. On an idle loop the delta is under a millisecond.
Anything in the hundreds means callbacks are queueing behind CPU work; anything
approaching `ping_timeout` (20s, set in both WS adapters) means a disconnect is
imminent and is our fault rather than the venue's.

Deliberately not a fix for anything. It is the instrument that says whether a
fix -- a bigger VM, cheaper per-tick work -- did anything.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque

# One second between probes, sixty samples retained: a one-minute window, which
# is the timescale the disconnects arrive on.
PROBE_INTERVAL_S = 1.0
WINDOW = 60


class LoopLagMonitor:
    """Samples scheduling delay on a fixed interval."""

    def __init__(self, interval_s: float = PROBE_INTERVAL_S, window: int = WINDOW):
        self._interval = interval_s
        self._samples: deque[float] = deque(maxlen=window)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            before = loop.time()
            await asyncio.sleep(self._interval)
            # Everything past the interval is time the loop owed us and could
            # not pay: another callback held it.
            self._samples.append(max(0.0, (loop.time() - before) - self._interval))

    def stats(self) -> dict[str, float | int]:
        """Milliseconds. Empty window reports zeros rather than nulls, so a
        caller can always subtract without a None check."""
        if not self._samples:
            return {"samples": 0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
        ordered = sorted(self._samples)
        n = len(ordered)

        def pct(p: float) -> float:
            return round(ordered[min(n - 1, int(p * n))] * 1000, 1)

        return {
            "samples": n,
            "p50_ms": pct(0.50),
            "p95_ms": pct(0.95),
            "max_ms": round(ordered[-1] * 1000, 1),
        }


_monitor = LoopLagMonitor()


def monitor() -> LoopLagMonitor:
    return _monitor
