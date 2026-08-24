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


@pytest.mark.asyncio
async def test_ticket_commit_is_not_interleaved_by_quote_updates():
    """A ticket must not be split by a mid-flight price move.

    The commit loop used to await between legs, so a quote update could land
    after leg one filled and push leg two past its limit -- leaving a naked
    position while the caller only saw an error. The sink is the hostile
    party here: it moves the market the instant it is notified, which is the
    worst realistic interleaving.
    """
    book = QuoteBook()
    book.upsert(Quote(outcome_id="Y", bid=Decimal("0.40"), ask=Decimal("0.40")))
    book.upsert(Quote(outcome_id="N", bid=Decimal("0.50"), ask=Decimal("0.50")))

    poly = _make_broker("poly", book)
    kals = _make_broker("kals", book)
    poly.deposit("acct", Decimal("1000"))
    kals.deposit("acct", Decimal("1000"))

    class MarketMovingSink:
        """Jumps the second leg's ask out of range as soon as it is told anything."""

        async def on_order(self, order, *, rejection_reason=None):
            book.upsert(Quote(outcome_id="N", bid=Decimal("0.90"), ask=Decimal("0.90")))

        async def on_fill(self, order, fill):
            book.upsert(Quote(outcome_id="N", bid=Decimal("0.90"), ask=Decimal("0.90")))

        async def on_balance(self, account_id, venue_id, amount):
            return None

        async def on_position(self, account_id, outcome_id, qty, avg_price,
                              realized_pnl, *, venue_id):
            return None

    poly.set_sink(MarketMovingSink())
    kals.set_sink(MarketMovingSink())

    router = ExecutionRouter({"poly": poly, "kals": kals})
    intent = ExecutionIntent(
        event_group_id="eg",
        account_id="acct",
        legs=(
            IntentLeg(venue_id="poly", outcome_id="Y", is_buy=True,
                      qty=Decimal("100"), limit_price=Decimal("0.40")),
            IntentLeg(venue_id="kals", outcome_id="N", is_buy=True,
                      qty=Decimal("100"), limit_price=Decimal("0.50")),
        ),
    )

    orders = await router.submit(intent)
    assert len(orders) == 2
    assert all(o.status is OrderStatus.FILLED for o in orders)

    # Both legs, or neither -- never one.
    assert (await poly.get_positions("acct"))["Y"] == Decimal("100")
    assert (await kals.get_positions("acct"))["N"] == Decimal("100")


@pytest.mark.asyncio
async def test_failed_ticket_leaves_no_position_and_no_orders():
    """If a leg cannot fill, the whole ticket unwinds -- no naked leg."""
    book = QuoteBook()
    book.upsert(Quote(outcome_id="Y", bid=Decimal("0.40"), ask=Decimal("0.40")))
    book.upsert(Quote(outcome_id="N", bid=Decimal("0.50"), ask=Decimal("0.50")))

    poly = _make_broker("poly", book)
    kals = _make_broker("kals", book)
    poly.deposit("acct", Decimal("1000"))
    kals.deposit("acct", Decimal("1000"))

    poly_before = await poly.get_balances("acct")
    kals_before = await kals.get_balances("acct")

    router = ExecutionRouter({"poly": poly, "kals": kals})
    # Second leg is unfillable: no quote for that outcome at all.
    intent = ExecutionIntent(
        event_group_id="eg",
        account_id="acct",
        legs=(
            IntentLeg(venue_id="poly", outcome_id="Y", is_buy=True,
                      qty=Decimal("100"), limit_price=Decimal("0.40")),
            IntentLeg(venue_id="kals", outcome_id="MISSING", is_buy=True,
                      qty=Decimal("100"), limit_price=Decimal("0.50")),
        ),
    )
    with pytest.raises(InsufficientLegsError):
        await router.submit(intent)

    assert (await poly.get_positions("acct")).get("Y", Decimal("0")) == Decimal("0")
    assert await poly.get_balances("acct") == poly_before
    assert await kals.get_balances("acct") == kals_before


def test_sell_into_a_known_empty_bid_is_rejected():
    """A one-sided book synthesises the missing side at size 0. Without this
    guard the broker would report selling into a book with no buyers."""
    book = QuoteBook()
    book.upsert(
        Quote(
            outcome_id="X",
            bid=Decimal("0.0050"),
            ask=Decimal("0.0050"),
            bid_size=Decimal("0"),        # known empty
            ask_size=Decimal("419882"),
        )
    )
    broker = _make_broker("poly", book)
    broker.deposit("acct", Decimal("100"))
    _order, fill, reason = broker.apply_fill(
        account_id="acct", outcome_id="X", is_buy=False,
        qty=Decimal("1"), limit_price=Decimal("0"),
    )
    assert fill is None
    assert reason == "no_liquidity"


def test_buy_against_a_live_ask_still_fills_on_a_one_sided_book():
    """The point of keeping one-sided books at all: the ask is real."""
    book = QuoteBook()
    book.upsert(
        Quote(
            outcome_id="X",
            bid=Decimal("0.0050"),
            ask=Decimal("0.0050"),
            bid_size=Decimal("0"),
            ask_size=Decimal("419882"),
        )
    )
    broker = _make_broker("poly", book)
    broker.deposit("acct", Decimal("100"))
    _order, fill, reason = broker.apply_fill(
        account_id="acct", outcome_id="X", is_buy=True,
        qty=Decimal("1"), limit_price=Decimal("1"),
    )
    assert reason is None
    assert fill is not None


def test_unknown_size_still_fills():
    """None means the venue did not report depth - most quotes, including
    every hand-pushed one. Blocking those would break POST /quotes."""
    book = QuoteBook()
    book.upsert(Quote(outcome_id="X", bid=Decimal("0.40"), ask=Decimal("0.45")))
    broker = _make_broker("poly", book)
    broker.deposit("acct", Decimal("100"))
    _order, fill, reason = broker.apply_fill(
        account_id="acct", outcome_id="X", is_buy=True,
        qty=Decimal("1"), limit_price=Decimal("1"),
    )
    assert reason is None
    assert fill is not None


def test_order_larger_than_resting_size_is_rejected():
    """The book moved between detection and execution: reject rather than
    partial-fill, since a partial leg of a two-leg arb leaves it unhedged."""
    book = QuoteBook()
    book.upsert(Quote(outcome_id="o1", bid=Decimal("0.40"), ask=Decimal("0.45"),
                       ask_size=Decimal("3")))
    broker = _make_broker("poly", book)
    broker.deposit("acct", Decimal("100"))
    _order, fill, reason = broker.apply_fill(
        account_id="acct", outcome_id="o1", is_buy=True,
        qty=Decimal("100"), limit_price=Decimal("0.50"),
    )
    assert fill is None
    assert reason == "insufficient_liquidity"


def test_order_within_resting_size_fills():
    book = QuoteBook()
    book.upsert(Quote(outcome_id="o1", bid=Decimal("0.40"), ask=Decimal("0.45"),
                       ask_size=Decimal("3")))
    broker = _make_broker("poly", book)
    broker.deposit("acct", Decimal("100"))
    _order, fill, reason = broker.apply_fill(
        account_id="acct", outcome_id="o1", is_buy=True,
        qty=Decimal("3"), limit_price=Decimal("0.50"),
    )
    assert reason is None
    assert fill is not None and fill.qty == Decimal("3")


def test_unknown_size_still_fills_any_qty():
    """None = unknown. POST /quotes omits sizes, and those must keep working."""
    book = QuoteBook()
    book.upsert(Quote(outcome_id="o1", bid=Decimal("0.40"), ask=Decimal("0.45")))
    broker = _make_broker("poly", book)
    broker.deposit("acct", Decimal("100"))
    _order, fill, reason = broker.apply_fill(
        account_id="acct", outcome_id="o1", is_buy=True,
        qty=Decimal("100"), limit_price=Decimal("0.50"),
    )
    assert reason is None
    assert fill is not None


def test_known_empty_still_reports_no_liquidity():
    """0 and "too small" are different failures and must stay distinguishable."""
    book = QuoteBook()
    book.upsert(Quote(outcome_id="o1", bid=Decimal("0.40"), ask=Decimal("0.45"),
                       ask_size=Decimal("0")))
    broker = _make_broker("poly", book)
    broker.deposit("acct", Decimal("100"))
    _order, _fill, reason = broker.apply_fill(
        account_id="acct", outcome_id="o1", is_buy=True,
        qty=Decimal("1"), limit_price=Decimal("0.50"),
    )
    assert reason == "no_liquidity"


def test_apply_fill_stamps_ticket_id_on_the_order():
    """The sink reads ticket_id off the Order, so it must survive apply_fill."""
    book = QuoteBook()
    book.upsert(
        Quote(
            outcome_id="k-yes",
            bid=Decimal("0.39"),
            ask=Decimal("0.40"),
        )
    )
    broker = PaperExecutionAdapter(
        venue_id="kalshi", quotebook=book, fee_model=KalshiFeeModel()
    )
    broker.deposit("acct", Decimal("100"))
    order, fill, reason = broker.apply_fill(
        account_id="acct",
        outcome_id="k-yes",
        is_buy=True,
        qty=Decimal("10"),
        limit_price=Decimal("0.40"),
        ticket_id="tkt-42",
    )
    assert reason is None
    assert fill is not None
    assert order.ticket_id == "tkt-42"


def test_rejected_order_also_carries_the_ticket_id():
    """A rejected leg must be attributable to its ticket, or the audit log
    cannot show why a ticket failed."""
    book = QuoteBook()
    broker = PaperExecutionAdapter(
        venue_id="kalshi", quotebook=book, fee_model=KalshiFeeModel()
    )
    order, fill, reason = broker.apply_fill(
        account_id="acct",
        outcome_id="missing",
        is_buy=True,
        qty=Decimal("10"),
        limit_price=Decimal("0.40"),
        ticket_id="tkt-43",
    )
    assert reason == "no_quote"
    assert fill is None
    assert order.ticket_id == "tkt-43"


@pytest.mark.asyncio
async def test_router_rejection_names_the_failing_leg():
    """The audit log needs the leg, not just a joined message string."""
    book = QuoteBook()
    book.upsert(
        Quote(
            outcome_id="k-yes",
            bid=Decimal("0.39"),
            ask=Decimal("0.40"),
            ask_size=Decimal("5"),
        )
    )
    broker = PaperExecutionAdapter(
        venue_id="kalshi", quotebook=book, fee_model=KalshiFeeModel()
    )
    broker.deposit("acct", Decimal("1000"))
    router = ExecutionRouter({"kalshi": broker})
    intent = ExecutionIntent(
        event_group_id="eg-1",
        account_id="acct",
        legs=(
            IntentLeg(
                venue_id="kalshi",
                outcome_id="k-yes",
                is_buy=True,
                qty=Decimal("100"),
                limit_price=Decimal("0.40"),
            ),
        ),
        ticket_id="tkt-1",
    )
    with pytest.raises(InsufficientLegsError) as excinfo:
        await router.submit(intent)
    rejections = excinfo.value.rejections
    assert len(rejections) == 1
    assert rejections[0].outcome_id == "k-yes"
    assert rejections[0].reason == "insufficient_liquidity"
    assert "kalshi:insufficient_liquidity" in str(excinfo.value)


@pytest.mark.asyncio
async def test_settlement_notifies_the_sink_exactly_once_with_multiple_holders():
    """Settlement emits exactly once per outcome, even with multiple accounts
    holding the position. This catches emit-inside-loop regressions that would
    fire once per holding account."""
    recorded: list[tuple[str, Decimal, str, str]] = []

    class _Sink:
        async def on_order(self, order, *, rejection_reason=None): ...
        async def on_fill(self, order, fill): ...
        async def on_balance(self, account_id, venue_id, amount): ...
        async def on_position(
            self, account_id, outcome_id, qty, avg_price, realized_pnl, *, venue_id
        ): ...
        async def on_settlement(
            self, outcome_id, resolved_value, *, venue_id, source
        ):
            recorded.append((outcome_id, resolved_value, venue_id, source))

    book = QuoteBook()
    book.upsert(
        Quote(
            outcome_id="k-yes",
            bid=Decimal("0.39"),
            ask=Decimal("0.40"),
        )
    )
    broker = PaperExecutionAdapter(
        venue_id="kalshi", quotebook=book, fee_model=KalshiFeeModel(), sink=_Sink()
    )
    # First account places and holds a position
    broker.deposit("acct", Decimal("100"))
    await broker.place_order(
        account_id="acct",
        outcome_id="k-yes",
        is_buy=True,
        qty=Decimal("10"),
        limit_price=Decimal("0.40"),
    )
    # Second account also holds the same position (without placing order,
    # to avoid polluting the recorded list)
    broker.deposit("acct2", Decimal("100"))
    broker.hydrate_position(
        "acct2", "k-yes", qty=Decimal("5"), avg_price=Decimal("0.40"), realized_pnl=Decimal("0")
    )
    # Settlement should emit exactly once, not once per holding account
    await broker.settle_outcome_async("k-yes", Decimal("1"))
    assert len(recorded) == 1, f"Expected 1 settlement record, got {len(recorded)}"
    assert recorded[0] == ("k-yes", Decimal("1"), "kalshi", "heuristic")


@pytest.mark.asyncio
async def test_settlement_notifies_the_sink_when_no_account_holds():
    """Settlement emits exactly once even when no account holds the position.
    This catches emit-inside-loop regressions that would fire zero times when
    the loop has nothing to settle."""
    recorded: list[tuple[str, Decimal, str, str]] = []

    class _Sink:
        async def on_order(self, order, *, rejection_reason=None): ...
        async def on_fill(self, order, fill): ...
        async def on_balance(self, account_id, venue_id, amount): ...
        async def on_position(
            self, account_id, outcome_id, qty, avg_price, realized_pnl, *, venue_id
        ): ...
        async def on_settlement(
            self, outcome_id, resolved_value, *, venue_id, source
        ):
            recorded.append((outcome_id, resolved_value, venue_id, source))

    book = QuoteBook()
    book.upsert(
        Quote(
            outcome_id="k-yes",
            bid=Decimal("0.39"),
            ask=Decimal("0.40"),
        )
    )
    broker = PaperExecutionAdapter(
        venue_id="kalshi", quotebook=book, fee_model=KalshiFeeModel(), sink=_Sink()
    )
    broker.deposit("acct", Decimal("100"))
    # Settle an outcome that no account holds
    await broker.settle_outcome_async("k-yes", Decimal("1"))
    assert len(recorded) == 1, f"Expected 1 settlement record, got {len(recorded)}"
    assert recorded[0] == ("k-yes", Decimal("1"), "kalshi", "heuristic")
