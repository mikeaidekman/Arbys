"""The loop-lag probe, which exists to tell our fault from the venue's.

Both websocket adapters run `ping_timeout=20`, so a loop held for 20s drops
every venue connection at once -- which is exactly what the hosted instance did,
and is indistinguishable from a venue outage in the logs. These pin that the
measurement actually responds to a blocked loop rather than quietly reporting
zeros forever.
"""

from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from arbys.backend.app import create_app
from arbys.backend.loop_health import LoopLagMonitor


async def test_an_idle_loop_reports_near_zero_lag():
    mon = LoopLagMonitor(interval_s=0.01, window=30)
    mon.start()
    await asyncio.sleep(0.3)
    await mon.stop()
    stats = mon.stats()
    assert stats["samples"] > 5
    assert stats["p50_ms"] < 50, stats


async def test_a_blocked_loop_is_visible():
    """The whole point: synchronous work must show up as lag.

    `time.sleep` in a coroutine blocks the loop exactly the way a heavy
    synchronous pass does -- which is the failure being hunted.
    """
    mon = LoopLagMonitor(interval_s=0.01, window=60)
    mon.start()
    await asyncio.sleep(0.05)
    time.sleep(0.4)  # blocking the loop on purpose: it is the subject
    await asyncio.sleep(0.05)
    await mon.stop()
    assert mon.stats()["max_ms"] > 200, mon.stats()


async def test_stop_is_idempotent_and_safe_before_start():
    mon = LoopLagMonitor()
    await mon.stop()
    mon.start()
    await mon.stop()
    await mon.stop()


def test_health_reports_loop_lag():
    with TestClient(create_app()) as client:
        body = client.get("/health").json()
    assert "loop_lag" in body
    for key in ("samples", "p50_ms", "p95_ms", "max_ms"):
        assert key in body["loop_lag"]


def test_an_empty_window_reports_zeros_not_nulls():
    """A caller should never have to None-check this to draw a graph."""
    assert LoopLagMonitor().stats() == {
        "samples": 0,
        "p50_ms": 0.0,
        "p95_ms": 0.0,
        "max_ms": 0.0,
    }
