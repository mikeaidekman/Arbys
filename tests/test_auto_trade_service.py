"""AutoTradeService decides; it does not resolve, size, or price.

Everything the service needs is injected, so these tests need no AppState, no
database and no venue. That is the same boundary that keeps `arbys/ingest/`
from importing `arbys/backend/`.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

import pytest

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
        self.too_late = False
        self.cross_venue_only = True
        self.submitted: list[ArbOpportunity] = []
        # (opportunity, record_nonfill) for every submission, so a test can
        # assert on whether the audit row was asked for as well as on whether
        # the attempt happened at all - the two are deliberately separable.
        self.recorded: list[bool] = []
        self.now = 1000.0
        self.queue: asyncio.Queue[ArbOpportunity] = asyncio.Queue(maxsize=100)
        self.unsubscribed: list[asyncio.Queue[ArbOpportunity]] = []

    async def submit(self, opp: ArbOpportunity, *, record_nonfill: bool = True) -> str:
        self.submitted.append(opp)
        self.recorded.append(record_nonfill)
        return self.status

    def service(
        self, *, cooldown_s: float = 60.0, nonfill_log_s: float = 0.0
    ) -> AutoTradeService:
        """`nonfill_log_s=0` by default so the pre-existing tests, which are
        about the *fill* cooldown, keep seeing every attempt recorded."""
        return AutoTradeService(
            subscribe=lambda: self.queue,
            unsubscribe=self.unsubscribed.append,
            submit=self.submit,
            would_breach_cap=lambda _opp: self.breach,
            would_start_too_late=lambda _opp: self.too_late,
            enabled=lambda: self.enabled,
            cooldown_s=cooldown_s,
            cross_venue_only=lambda: self.cross_venue_only,
            nonfill_log_s=nonfill_log_s,
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


async def test_the_far_out_precheck_skips_silently_without_submitting():
    """An edge on a game a fortnight away persists for days and republishes on
    every depth tick; recording each refusal would fill the ledger with rows
    saying only "still too early". Same treatment as the cap, same reason."""
    h = _Harness()
    h.too_late = True
    assert await h.service().handle(_opp()) is None
    assert h.submitted == []


async def test_the_far_out_precheck_defaults_to_never():
    """A caller that does not wire the callable gets the old behaviour."""
    h = _Harness()
    service = AutoTradeService(
        subscribe=lambda: h.queue,
        unsubscribe=h.unsubscribed.append,
        submit=h.submit,
        would_breach_cap=lambda _opp: False,
        enabled=lambda: True,
        cooldown_s=0.0,
        nonfill_log_s=0.0,
        clock=lambda: h.now,
    )
    assert await service.handle(_opp()) == "filled"
    assert len(h.submitted) == 1


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

    async def flaky(opp: ArbOpportunity, *, record_nonfill: bool = True) -> str:
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

    async def slow_submit(opp: ArbOpportunity, *, record_nonfill: bool = True) -> str:
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


async def test_stop_propagates_a_cancellation_aimed_at_itself():
    """stop() must not absorb a cancellation aimed at stop() itself.

    `asyncio.shield` means the only CancelledError this can catch is *not*
    the shielded consumer's own cancellation - it can only be an external
    cancellation of the `stop()` call itself (e.g. uvicorn's forced-shutdown
    deadline cancelling the lifespan task while stop() is mid-wait).
    Swallowing that would let a caller like AppState.shutdown() wrongly
    believe shutdown completed cleanly and carry on to the next service.
    """
    h = _Harness()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_submit(opp: ArbOpportunity, *, record_nonfill: bool = True) -> str:
        started.set()
        await release.wait()
        h.submitted.append(opp)
        return "filled"

    h.submit = slow_submit  # type: ignore[method-assign]
    svc = h.service(cooldown_s=0.0)
    await svc.start()
    inner_task = svc._task
    h.queue.put_nowait(_opp())
    await asyncio.wait_for(started.wait(), timeout=2.0)

    stop_task = asyncio.create_task(svc.stop())
    await asyncio.sleep(0.02)
    stop_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await stop_task

    # Cleanup still ran on the way out, despite the cancellation propagating.
    assert svc._task is None
    assert h.unsubscribed == [h.queue]

    # The shielded submit was never touched by any of the above - let it
    # finish so the test doesn't leak a running task.
    release.set()
    assert inner_task is not None
    await asyncio.wait_for(inner_task, timeout=2.0)
    assert len(h.submitted) == 1, "the shielded submit must not have been cancelled"


async def test_stop_logs_when_the_consumer_died_of_a_real_bug(caplog):
    """A consumer that crashed from a genuine bug must be visible, not
    silently swallowed the way a routine cancellation is."""

    class _ExplodingQueue(asyncio.Queue):
        async def get(self):
            raise RuntimeError("queue explode")

    queue: asyncio.Queue[ArbOpportunity] = _ExplodingQueue()
    unsubscribed: list[asyncio.Queue[ArbOpportunity]] = []

    async def submit(opp: ArbOpportunity, *, record_nonfill: bool = True) -> str:
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

    async def submit(opp: ArbOpportunity, *, record_nonfill: bool = True) -> str:
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


def _intra_opp(group_id: str = "eg-1:kalshi") -> ArbOpportunity:
    """A complementary edge: both legs on one venue.

    This is what `engine_runtime` publishes under a synthetic `<group>:<venue>`
    id, but the service reads the legs rather than the id, so the id here is
    incidental.
    """
    return ArbOpportunity(
        event_group_id=group_id,
        legs=(
            ArbLeg(
                outcome_id="k-a:YES",
                venue_id="kalshi",
                is_buy=True,
                price=Decimal("0.47"),
                qty=Decimal("10"),
                fee=Decimal("0"),
            ),
            ArbLeg(
                outcome_id="k-b:YES",
                venue_id="kalshi",
                is_buy=True,
                price=Decimal("0.48"),
                qty=Decimal("10"),
                fee=Decimal("0"),
            ),
        ),
        total_stake=Decimal("9.5"),
        guaranteed_profit=Decimal("0.5"),
        guaranteed_profit_bps=Decimal("526"),
    )


async def test_an_intra_venue_edge_is_not_traded():
    """One venue's own book crossed against itself is not an arbitrage we can
    win: a co-located taker clears it in milliseconds, and the large ones are
    one-sided stale quotes. 5 fills of 244 attempts worth $0.41 over a day."""
    h = _Harness()
    assert await h.service().handle(_intra_opp()) is None
    assert h.submitted == []


async def test_intra_venue_is_judged_by_leg_venues_not_by_the_group_id():
    """The `<group>:<venue>` id is a naming convention; what makes a ticket
    unwinnable is that both legs rest on the same book. A cross-venue edge
    must still trade even if something publishes it under a suffixed id."""
    h = _Harness()
    cross_under_suffixed_id = _opp(group_id="eg-1:kalshi")
    assert await h.service().handle(cross_under_suffixed_id) == "filled"
    assert len(h.submitted) == 1


async def test_intra_venue_can_be_switched_back_on():
    h = _Harness()
    h.cross_venue_only = False
    assert await h.service().handle(_intra_opp()) == "filled"
    assert len(h.submitted) == 1


async def test_a_repeat_miss_is_reattempted_but_not_logged_twice():
    """The attempt must continue at full rate — a vanished edge is no reason
    to stop watching — while the duplicate audit row is suppressed. Without
    this, one dying edge wrote a row per depth tick: 1,149 missed tickets
    describing 116 groups, 74% of the repeats inside the same second."""
    h = _Harness(status="missed")
    svc = h.service(nonfill_log_s=60.0)

    assert await svc.handle(_opp()) == "missed"
    assert h.recorded == [True]

    assert await svc.handle(_opp()) == "missed"
    assert len(h.submitted) == 2, "the edge must still be re-attempted"
    assert h.recorded == [True, False], "the second row must be suppressed"


async def test_the_nonfill_log_window_expires():
    h = _Harness(status="missed")
    svc = h.service(nonfill_log_s=60.0)
    await svc.handle(_opp())
    h.now += 59
    await svc.handle(_opp())
    h.now += 2
    await svc.handle(_opp())
    assert h.recorded == [True, False, True]


async def test_the_nonfill_log_window_is_per_group():
    h = _Harness(status="missed")
    svc = h.service(nonfill_log_s=60.0)
    await svc.handle(_opp(group_id="eg-1"))
    await svc.handle(_opp(group_id="eg-2"))
    assert h.recorded == [True, True]


async def test_a_rejection_shares_the_nonfill_window_with_a_miss():
    """Rejections duplicate for the same reason misses do — 365 tickets across
    61 groups — so one window governs both rather than two mechanisms."""
    h = _Harness(status="rejected")
    svc = h.service(nonfill_log_s=60.0)
    await svc.handle(_opp())
    await svc.handle(_opp())
    assert h.recorded == [True, False]


async def test_a_fill_reopens_the_nonfill_window():
    """A group that just filled has done something new, so the next miss on it
    is news rather than more of the burst the window exists to collapse.

    The flag passed on the *filling* call is not asserted: a fill's row is
    written before the router runs whatever this says, so the value is
    meaningless there. What matters is the miss that follows it.
    """
    h = _Harness(status="missed")
    svc = h.service(cooldown_s=0.0, nonfill_log_s=60.0)
    await svc.handle(_opp())
    await svc.handle(_opp())
    assert h.recorded == [True, False], "the burst is collapsed"

    h.status = "filled"
    await svc.handle(_opp())
    h.status = "missed"
    await svc.handle(_opp())
    assert h.recorded[-1] is True, "the miss after a fill is news again"


async def test_a_zero_nonfill_window_records_every_attempt():
    h = _Harness(status="missed")
    svc = h.service(nonfill_log_s=0.0)
    await svc.handle(_opp())
    await svc.handle(_opp())
    assert h.recorded == [True, True]


async def test_clear_cooldowns_also_reopens_the_nonfill_window():
    """A reset wipes the ticket log, so suppressing the next miss as a
    duplicate of a row that no longer exists would lose it for nothing."""
    h = _Harness(status="missed")
    svc = h.service(nonfill_log_s=60.0)
    await svc.handle(_opp())
    svc.clear_cooldowns()
    await svc.handle(_opp())
    assert h.recorded == [True, True]
