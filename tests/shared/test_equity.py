"""Equity is computed in exactly one place.

The summary endpoint and the PnL snapshotter both call this. If they computed
it differently the account strip and the curve below it would disagree on the
same page.
"""

from __future__ import annotations

from decimal import Decimal

from arbys.shared.equity import account_equity
from arbys.shared.fees import KalshiFeeModel
from arbys.shared.paper_broker import PaperExecutionAdapter
from arbys.shared.quotebook import QuoteBook
from arbys.shared.types import Quote


def _broker(book: QuoteBook) -> PaperExecutionAdapter:
    return PaperExecutionAdapter(
        venue_id="kalshi", quotebook=book, fee_model=KalshiFeeModel()
    )


def test_marks_positions_at_the_mid():
    book = QuoteBook()
    book.upsert(
        Quote(
            outcome_id="k-yes",
            bid=Decimal("0.60"),
            ask=Decimal("0.70"),
        )
    )
    broker = _broker(book)
    broker.deposit("acct", Decimal("100"))
    broker.hydrate_position(
        "acct", "k-yes", qty=Decimal("10"), avg_price=Decimal("0.50"),
        realized_pnl=Decimal("0"),
    )
    eq = account_equity({"kalshi": broker}, book, "acct")
    assert eq.cash == Decimal("100")
    assert eq.position_value == Decimal("6.5")
    assert eq.equity == Decimal("106.5")
    assert eq.unrealized == Decimal("1.5")


def test_falls_back_to_avg_price_without_a_quote():
    """Flat MTM, not zero — a missing quote is unknown, not worthless."""
    book = QuoteBook()
    broker = _broker(book)
    broker.deposit("acct", Decimal("50"))
    broker.hydrate_position(
        "acct", "no-quote", qty=Decimal("4"), avg_price=Decimal("0.25"),
        realized_pnl=Decimal("0"),
    )
    eq = account_equity({"kalshi": broker}, book, "acct")
    assert eq.position_value == Decimal("1.00")
    assert eq.unrealized == Decimal("0")


def test_realized_is_summed_across_venues():
    book = QuoteBook()
    a = PaperExecutionAdapter(
        venue_id="kalshi", quotebook=book, fee_model=KalshiFeeModel()
    )
    b = PaperExecutionAdapter(
        venue_id="polymarket_us", quotebook=book, fee_model=KalshiFeeModel()
    )
    a.hydrate_position(
        "acct", "x", qty=Decimal("0"), avg_price=Decimal("0"),
        realized_pnl=Decimal("3"),
    )
    b.hydrate_position(
        "acct", "y", qty=Decimal("0"), avg_price=Decimal("0"),
        realized_pnl=Decimal("4"),
    )
    eq = account_equity({"kalshi": a, "polymarket_us": b}, book, "acct")
    assert eq.realized == Decimal("7")
