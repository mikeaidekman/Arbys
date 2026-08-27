"""AutoTradeService decides; it does not resolve, size, or price.

Everything the service needs is injected, so these tests need no AppState, no
database and no venue. That is the same boundary that keeps `arbys/ingest/`
from importing `arbys/backend/`.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from arbys.ingest.auto_trade_service import BACKPRESSURE_WARN_QSIZE, AutoTradeService
from arbys.shared.arb_engine import ArbLeg, ArbOpportunity


def _opp(group_id: str = "eg-1", qty: str = "10") -> ArbOpportunity:
    return ArbOpportunity(
        event_group_id=group_id,
        legs=(
            ArbLeg(
                outcome_id="p-yes",
                venue_id="polymarket_us",
                is_buy=True,
                price=Decimal("0.40"),
                qty=Decimal(qty),
                fee=Decimal("0"),
            ),
            ArbLeg(
                outcome_id="k-no",
                venue_id="kalshi",
                is_buy=True,
                price=Decimal("0.50"),
                qty=Decimal(qty),
                fee=Decimal("0"),
            ),
        ),
        total_stake=Decimal("9"),
        guaranteed_profit=Decimal("1"),
        guaranteed_profit_bps=Decimal("1111"),
    )


class _Harness:
    """Records what the service asked for and lets a test dictate the answers.

    Reassign `submit` or flip `enabled`/`breach` *before* calling `service()`
    where the value is read at construction; `enabled` and `breach` are read
    through lambdas, so those two can also be changed afterwards.
    """

    def __init__(self, *, status: str = "filled", enabled: bool = True) -> None:
        self.status = status
        self.enabled = enabled
        self.breach = False
        self.submitted: list[ArbOpportunity] = []
        self.now = 1000.0
        self.queue: asyncio.Queue[ArbOpportunity] = asyncio.Queue(maxsize=100)
        self.unsubscribed: list[asyncio.Queue[ArbOpportunity]] = []

    async def submit(self, opp: ArbOpportunity) -> str:
        self.submitted.append(opp)
        return self.status

    def service(self, *, cooldown_s: float = 60.0) -> AutoTradeService:
        return AutoTradeService(
            subscribe=lambda: self.queue,
            unsubscribe=self.unsubscribed.append,
            submit=lambda opp: self.submit(opp),
            would_breach_cap=lambda _opp: self.breach,
            enabled=lambda: self.enabled,
            cooldown_s=cooldown_s,
            clock=lambda: self.now,
        )


async def test_fires_on_an_opportunity_and_reports_the_status():
    h = _Harness()
    assert await h.service().handle(_opp()) == "filled"
    assert len(h.submitted) == 1


async def test_does_nothing_when_disabled():
    h = _Harness(enabled=False)
    assert await h.service().handle(_opp()) is None
    assert h.submitted == []


async def test_enabled_is_read_per_opportunity_not_captured_at_construction():
    h = _Harness(enabled=False)
    svc = h.service()
    assert await svc.handle(_opp()) is None
    h.enabled = True
    assert await svc.handle(_opp()) == "filled"


async def test_a_fill_cools_the_group_down():
    h = _Harness()
    svc = h.service(cooldown_s=60.0)
    assert await svc.handle(_opp()) == "filled"
    h.now += 30.0
    assert await svc.handle(_opp()) is None
    assert len(h.submitted) == 1


async def test_the_cooldown_expires():
    h = _Harness()
    svc = h.service(cooldown_s=60.0)
    await svc.handle(_opp())
    h.now += 61.0
    assert await svc.handle(_opp()) == "filled"
    assert len(h.submitted) == 2


async def test_the_cooldown_is_per_group():
    h = _Harness()
    svc = h.service()
    await svc.handle(_opp("eg-1"))
    assert await svc.handle(_opp("eg-2")) == "filled"
    assert len(h.submitted) == 2


async def test_a_miss_does_not_start_a_cooldown():
    """A miss means the edge was gone, which is no reason to stop watching."""
    h = _Harness(status="missed")
    svc = h.service()
    assert await svc.handle(_opp()) == "missed"
    assert await svc.handle(_opp()) == "missed"
    assert len(h.submitted) == 2


async def test_a_rejection_does_not_start_a_cooldown():
    h = _Harness(status="rejected")
    svc = h.service()
    assert await svc.handle(_opp()) == "rejected"
    assert await svc.handle(_opp()) == "rejected"
    assert len(h.submitted) == 2


async def test_the_cap_precheck_skips_silently_without_submitting():
    """Without this, a capped-out group writes a rejected ticket on every
    publish for the rest of the night."""
    h = _Harness()
    h.breach = True
    assert await h.service().handle(_opp()) is None
    assert h.submitted == []


async def test_clear_cooldowns_forgets_everything():
    h = _Harness()
    svc = h.service()
    await svc.handle(_opp())
    svc.clear_cooldowns()
    assert await svc.handle(_opp()) == "filled"
    assert len(h.submitted) == 2


async def test_a_zero_cooldown_never_suppresses():
    h = _Harness()
    svc = h.service(cooldown_s=0.0)
    await svc.handle(_opp())
    assert await svc.handle(_opp()) == "filled"


async def test_the_run_loop_drains_the_queue_and_survives_a_failure():
    h = _Harness()
    calls = {"n": 0}

    async def flaky(opp: ArbOpportunity) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("submit blew up")
        h.submitted.append(opp)
        return "filled"

    h.submit = flaky  # type: ignore[method-assign]
    svc = h.service(cooldown_s=0.0)
    await svc.start()
    h.queue.put_nowait(_opp())
    h.queue.put_nowait(_opp())
    for _ in range(400):
        if len(h.submitted) == 1:
            break
        await asyncio.sleep(0.005)
    await svc.stop()
    assert calls["n"] == 2, "a raising submit must not kill the consumer"
    assert len(h.submitted) == 1


async def test_stop_unsubscribes_the_queue():
    h = _Harness()
    svc = h.service()
    await svc.start()
    await svc.stop()
    assert h.unsubscribed == [h.queue]


async def test_a_deep_queue_warns_about_silent_drops(caplog):
    """The subscriber queue drops with put_nowait under suppress(QueueFull), so
    backpressure is invisible by construction unless something says so."""
    h = _Harness()
    svc = h.service(cooldown_s=0.0)
    for _ in range(BACKPRESSURE_WARN_QSIZE + 2):
        h.queue.put_nowait(_opp())
    with caplog.at_level(logging.WARNING, logger="arbys.ingest.auto_trade_service"):
        await svc.start()
        for _ in range(400):
            if h.queue.qsize() == 0:
                break
            await asyncio.sleep(0.005)
        await svc.stop()
    assert any("auto-trade backpressure" in r.message for r in caplog.records)


async def test_stop_lets_an_in_flight_submit_finish():
    """stop() must not cancel a submission already under way. Cancelling it
    mid-await can land inside `submit_arb_ticket` after a `pending`
    paper_ticket row is already written and fills already applied to the
    in-memory broker, abandoning that ticket with nothing recording it."""
    h = _Harness()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_submit(opp: ArbOpportunity) -> str:
        started.set()
        await release.wait()
        h.submitted.append(opp)
        return "filled"

    h.submit = slow_submit  # type: ignore[method-assign]
    svc = h.service(cooldown_s=0.0)
    await svc.start()
    h.queue.put_nowait(_opp())
    await asyncio.wait_for(started.wait(), timeout=2.0)

    stop_task = asyncio.create_task(svc.stop())
    await asyncio.sleep(0.02)
    assert not stop_task.done(), "stop() must wait for the in-flight submit, not cancel it"

    release.set()
    await asyncio.wait_for(stop_task, timeout=2.0)

    assert len(h.submitted) == 1, "the in-flight submit must complete, not be abandoned"


async def test_stop_logs_when_the_consumer_died_of_a_real_bug(caplog):
    """A consumer that crashed from a genuine bug must be visible, not
    silently swallowed the way a routine cancellation is."""

    class _ExplodingQueue(asyncio.Queue):
        async def get(self):
            raise RuntimeError("queue explode")

    queue: asyncio.Queue[ArbOpportunity] = _ExplodingQueue()
    unsubscribed: list[asyncio.Queue[ArbOpportunity]] = []

    async def submit(opp: ArbOpportunity) -> str:
        return "filled"

    svc = AutoTradeService(
        subscribe=lambda: queue,
        unsubscribe=unsubscribed.append,
        submit=submit,
        would_breach_cap=lambda _opp: False,
        enabled=lambda: True,
        cooldown_s=0.0,
    )
    await svc.start()
    for _ in range(400):
        if svc._task is not None and svc._task.done():
            break
        await asyncio.sleep(0.005)
    assert svc._task is not None and svc._task.done(), "the consumer never crashed as expected"

    with caplog.at_level(logging.ERROR, logger="arbys.ingest.auto_trade_service"):
        await svc.stop()

    assert any(
        r.levelno >= logging.ERROR and "unhandled exception" in r.message
        for r in caplog.records
    ), "a crashed consumer must be logged, not silently swallowed"
    assert unsubscribed == [queue], "stop() must still clean up after a crashed consumer"


async def test_start_restarts_after_the_consumer_previously_died():
    """A crashed consumer must not permanently block start() from ever
    running the service again."""

    class _ExplodeOnceQueue(asyncio.Queue):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._boom = True

        async def get(self):
            if self._boom:
                self._boom = False
                raise RuntimeError("boom")
            return await super().get()

    queue: _ExplodeOnceQueue = _ExplodeOnceQueue()
    unsubscribed: list[asyncio.Queue[ArbOpportunity]] = []
    submitted: list[ArbOpportunity] = []

    async def submit(opp: ArbOpportunity) -> str:
        submitted.append(opp)
        return "filled"

    svc = AutoTradeService(
        subscribe=lambda: queue,
        unsubscribe=unsubscribed.append,
        submit=submit,
        would_breach_cap=lambda _opp: False,
        enabled=lambda: True,
        cooldown_s=0.0,
    )
    await svc.start()
    for _ in range(400):
        if svc._task is not None and svc._task.done():
            break
        await asyncio.sleep(0.005)
    assert svc._task is not None and svc._task.done(), "the consumer never crashed as expected"

    await svc.start()  # must notice the old task is dead and actually restart
    queue.put_nowait(_opp())
    for _ in range(400):
        if submitted:
            break
        await asyncio.sleep(0.005)
    await svc.stop()

    assert len(submitted) == 1, "start() must actually restart the consumer after a crash"
    assert queue in unsubscribed, "the dead run's subscription must not leak on restart"
