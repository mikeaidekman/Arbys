from decimal import Decimal

import pytest

from arbys.adapters.base import ExecutionIntent, IntentLeg, OrderStatus
from arbys.shared.execution_router import ExecutionRouter, InsufficientLegsError
from arbys.shared.fees import KalshiFeeModel, ZeroFeeModel
from arbys.shared.paper_broker import PaperExecutionAdapter
from arbys.shared.quotebook import QuoteBook
from arbys.shared.types import Quote


def _make_broker(venue_id, quotebook, fee_model=None, slippage_bps=Decimal("0")):
    return PaperExecutionAdapter(
        venue_id=venue_id,
        quotebook=quotebook,
        fee_model=fee_model or ZeroFeeModel(venue_id),
        slippage_bps=slippage_bps,
    )


@pytest.mark.asyncio
async def test_place_order_fills_and_deducts_balance():
    book = QuoteBook()
    book.upsert(Quote(outcome_id="A", bid=Decimal("0.40"), ask=Decimal("0.45")))
    broker = _make_broker("poly", book)
    broker.deposit("acct", Decimal("100"))
    order = await broker.place_order(
        account_id="acct",
        outcome_id="A",
        is_buy=True,
        qty=Decimal("10"),
        limit_price=Decimal("0.50"),
    )
    assert order.status is OrderStatus.FILLED
    balances = await broker.get_balances("acct")
    assert balances["poly"] == Decimal("100") - Decimal("0.45") * Decimal("10")
    positions = await broker.get_positions("acct")
    assert positions["A"] == Decimal("10")


@pytest.mark.asyncio
async def test_place_order_rejected_no_quote():
    broker = _make_broker("poly", QuoteBook())
    broker.deposit("acct", Decimal("100"))
    order = await broker.place_order(
        account_id="acct",
        outcome_id="X",
        is_buy=True,
        qty=Decimal("1"),
        limit_price=Decimal("1"),
    )
    assert order.status is OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_place_order_rejected_limit_exceeded():
    book = QuoteBook()
    book.upsert(Quote(outcome_id="A", bid=Decimal("0.40"), ask=Decimal("0.60")))
    broker = _make_broker("poly", book)
    broker.deposit("acct", Decimal("100"))
    order = await broker.place_order(
        account_id="acct",
        outcome_id="A",
        is_buy=True,
        qty=Decimal("1"),
        limit_price=Decimal("0.50"),
    )
    assert order.status is OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_place_order_rejected_insufficient_funds():
    book = QuoteBook()
    book.upsert(Quote(outcome_id="A", bid=Decimal("0.40"), ask=Decimal("0.45")))
    broker = _make_broker("poly", book)
    broker.deposit("acct", Decimal("1"))
    order = await broker.place_order(
        account_id="acct",
        outcome_id="A",
        is_buy=True,
        qty=Decimal("100"),
        limit_price=Decimal("1"),
    )
    assert order.status is OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_slippage_moves_price_adversely():
    book = QuoteBook()
    book.upsert(Quote(outcome_id="A", bid=Decimal("0.40"), ask=Decimal("0.50")))
    broker = _make_broker("poly", book, slippage_bps=Decimal("1000"))  # 10% slippage
    broker.deposit("acct", Decimal("100"))
    order = await broker.place_order(
        account_id="acct",
        outcome_id="A",
        is_buy=True,
        qty=Decimal("1"),
        limit_price=Decimal("1"),
    )
    fills = await broker.get_fills(order.id)
    # ask 0.50 + 10% = 0.55
    assert fills[0].price == Decimal("0.55")


@pytest.mark.asyncio
async def test_settlement_realizes_pnl():
    book = QuoteBook()
    book.upsert(Quote(outcome_id="A", bid=Decimal("0.40"), ask=Decimal("0.45")))
    broker = _make_broker("poly", book)
    broker.deposit("acct", Decimal("100"))
    await broker.place_order(
        account_id="acct",
        outcome_id="A",
        is_buy=True,
        qty=Decimal("10"),
        limit_price=Decimal("1"),
    )
    broker.settle_outcome("A", Decimal("1"))  # Bought at 0.45, resolves 1 -> 5.50 profit.
    assert broker.realized_pnl("acct") == Decimal("0.55") * Decimal("10")


@pytest.mark.asyncio
async def test_router_atomic_all_or_none_on_reject():
    book = QuoteBook()
    book.upsert(Quote(outcome_id="Y", bid=Decimal("0.40"), ask=Decimal("0.45")))
    # No quote for "N" -> preview fails -> whole ticket rejected.
    poly = _make_broker("poly", book)
    kals = _make_broker("kals", book, fee_model=KalshiFeeModel())
    poly.deposit("acct", Decimal("100"))
    kals.deposit("acct", Decimal("100"))
    router = ExecutionRouter({"poly": poly, "kals": kals})

    intent = ExecutionIntent(
        event_group_id="eg",
        account_id="acct",
        legs=(
            IntentLeg(venue_id="poly", outcome_id="Y", is_buy=True, qty=Decimal("1"), limit_price=Decimal("1")),
            IntentLeg(venue_id="kals", outcome_id="N", is_buy=True, qty=Decimal("1"), limit_price=Decimal("1")),
        ),
    )
    with pytest.raises(InsufficientLegsError):
        await router.submit(intent)

    # Neither adapter should have moved balances.
    assert (await poly.get_balances("acct"))["poly"] == Decimal("100")
    assert (await kals.get_balances("acct"))["kals"] == Decimal("100")


@pytest.mark.asyncio
async def test_router_commits_all_legs_when_previews_pass():
    book = QuoteBook()
    book.upsert(Quote(outcome_id="Y", bid=Decimal("0.40"), ask=Decimal("0.45")))
    book.upsert(Quote(outcome_id="N", bid=Decimal("0.45"), ask=Decimal("0.50")))
    poly = _make_broker("poly", book)
    kals = _make_broker("kals", book)
    poly.deposit("acct", Decimal("100"))
    kals.deposit("acct", Decimal("100"))
    router = ExecutionRouter({"poly": poly, "kals": kals})

    intent = ExecutionIntent(
        event_group_id="eg",
        account_id="acct",
        legs=(
            IntentLeg(venue_id="poly", outcome_id="Y", is_buy=True, qty=Decimal("10"), limit_price=Decimal("1")),
            IntentLeg(venue_id="kals", outcome_id="N", is_buy=True, qty=Decimal("10"), limit_price=Decimal("1")),
        ),
    )
    orders = await router.submit(intent)
    assert all(o.status is OrderStatus.FILLED for o in orders)
    assert (await poly.get_positions("acct"))["Y"] == Decimal("10")
    assert (await kals.get_positions("acct"))["N"] == Decimal("10")
