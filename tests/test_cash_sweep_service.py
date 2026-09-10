"""CashSweepService levels the per-venue books and records why.

The service owns the interval, the floor and the audit write; the arithmetic
is `shared/cash.py`'s and is tested there. What these pin is the part that
would be silent if wrong: that a transfer is persisted as a transfer, that
both balances land in the same transaction, and that nothing moves when the
flag is off.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import select

from arbys.db import models as m
from arbys.db.session import session_scope
from arbys.ingest.cash_sweep_service import CashSweepService
from arbys.shared.fees import KalshiFeeModel, PolymarketUsFeeModel
from arbys.shared.paper_broker import PaperExecutionAdapter
from arbys.shared.quotebook import QuoteBook

D = Decimal


def _brokers(kalshi: str, polymarket_us: str) -> dict[str, PaperExecutionAdapter]:
    book = QuoteBook()
    fees = {"kalshi": KalshiFeeModel(), "polymarket_us": PolymarketUsFeeModel()}
    out: dict[str, PaperExecutionAdapter] = {}
    for venue, amount in (("kalshi", kalshi), ("polymarket_us", polymarket_us)):
        b = PaperExecutionAdapter(venue_id=venue, quotebook=book, fee_model=fees[venue])
        b.deposit("default", D(amount))
        out[venue] = b
    return out


def _service(brokers, **kw) -> CashSweepService:
    kw.setdefault("min_transfer", D("25"))
    return CashSweepService(brokers=brokers, account_ids=["default"], **kw)


async def _schema(seed_reference_rows) -> None:
    from arbys.db.session import get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(m.Base.metadata.create_all)
    await seed_reference_rows()


async def test_a_drained_venue_is_refunded_from_the_flush_one(seed_reference_rows):
    await _schema(seed_reference_rows)
    brokers = _brokers(kalshi="3800", polymarket_us="200")

    moved = await _service(brokers).sweep_once()

    assert len(moved) == 1
    assert moved[0].from_venue == "kalshi"
    assert moved[0].to_venue == "polymarket_us"
    assert moved[0].amount == D("1800.00")
    assert brokers["kalshi"].cash("default") == D("2000.00")
    assert brokers["polymarket_us"].cash("default") == D("2000.00")


async def test_the_transfer_and_both_balances_are_persisted(seed_reference_rows):
    await _schema(seed_reference_rows)
    brokers = _brokers(kalshi="3800", polymarket_us="200")

    await _service(brokers).sweep_once()

    async with session_scope() as s:
        transfers = (await s.execute(select(m.PaperTransfer))).scalars().all()
        balances = {
            row.venue_id: row.amount
            for row in (await s.execute(select(m.PaperBalance))).scalars().all()
        }
    assert len(transfers) == 1
    t = transfers[0]
    assert (t.from_venue_id, t.to_venue_id, t.source) == (
        "kalshi",
        "polymarket_us",
        "sweep",
    )
    assert D(str(t.amount)) == D("1800.00")
    # Both sides, or the stored split contradicts the transfer explaining it.
    assert D(str(balances["kalshi"])) == D("2000.00")
    assert D(str(balances["polymarket_us"])) == D("2000.00")


async def test_a_balanced_account_writes_nothing(seed_reference_rows):
    await _schema(seed_reference_rows)
    brokers = _brokers(kalshi="2000", polymarket_us="2000")

    assert await _service(brokers).sweep_once() == ()

    async with session_scope() as s:
        assert (await s.execute(select(m.PaperTransfer))).scalars().all() == []


async def test_the_flag_off_moves_nothing(seed_reference_rows):
    await _schema(seed_reference_rows)
    brokers = _brokers(kalshi="4000", polymarket_us="0")

    assert await _service(brokers, enabled=lambda: False).sweep_once() == ()
    assert brokers["kalshi"].cash("default") == D("4000")


async def test_both_venues_dry_is_left_alone(seed_reference_rows):
    """Real capital exhaustion. Levelling $5.52 between two books helps nobody."""
    await _schema(seed_reference_rows)
    brokers = _brokers(kalshi="3.75", polymarket_us="1.77")

    assert await _service(brokers).sweep_once() == ()


async def test_counters_summarise_what_moved(seed_reference_rows):
    await _schema(seed_reference_rows)
    brokers = _brokers(kalshi="4000", polymarket_us="0")
    svc = _service(brokers)

    await svc.sweep_once()
    # Level now, so a second pass is a no-op and must not double-count.
    await svc.sweep_once()

    assert svc.transfers == 1
    assert svc.moved == D("2000.00")


async def test_the_loop_sweeps_and_keeps_running(seed_reference_rows):
    await _schema(seed_reference_rows)
    brokers = _brokers(kalshi="4000", polymarket_us="0")
    svc = _service(brokers, interval_s=5.0)

    await svc.start()
    try:
        for _ in range(200):
            if svc.transfers:
                break
            await asyncio.sleep(0.01)
    finally:
        await svc.stop()

    assert svc.transfers == 1
    assert brokers["polymarket_us"].cash("default") == D("2000.00")


async def test_stop_is_idempotent(seed_reference_rows):
    await _schema(seed_reference_rows)
    svc = _service(_brokers(kalshi="2000", polymarket_us="2000"))
    await svc.start()
    await svc.stop()
    await svc.stop()


async def test_a_failing_iteration_does_not_kill_the_loop(seed_reference_rows, caplog):
    """The sweep is not load-bearing for a trade, so it must never die quietly."""
    await _schema(seed_reference_rows)
    brokers = _brokers(kalshi="4000", polymarket_us="0")
    svc = _service(brokers, interval_s=0.01)
    calls = {"n": 0}
    real = svc.sweep_once

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return await real()

    svc.sweep_once = flaky  # type: ignore[method-assign]
    await svc.start()
    try:
        for _ in range(300):
            if svc.transfers:
                break
            await asyncio.sleep(0.01)
    finally:
        await svc.stop()

    assert calls["n"] >= 2
    assert svc.transfers == 1
