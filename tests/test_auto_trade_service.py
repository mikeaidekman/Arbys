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
