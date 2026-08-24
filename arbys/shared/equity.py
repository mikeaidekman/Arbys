"""Mark-to-market for a paper account.

Pure: takes the brokers and the quote book as arguments and performs no I/O,
so it is legal in `shared/`. Both `PnlSnapshotService` and the account summary
endpoint call this — the strip and the equity curve must not disagree.

A position with no live quote marks at its own average price (flat MTM) rather
than zero: a missing quote means unknown, not worthless.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .paper_broker import PaperExecutionAdapter
from .quotebook import QuoteBook
from .types import Quote


@dataclass(frozen=True)
class AccountEquity:
    cash: Decimal
    position_value: Decimal
    equity: Decimal
    unrealized: Decimal
    realized: Decimal


def _mid(q: Quote) -> Decimal:
    return (q.bid + q.ask) / Decimal(2)


def account_equity(
    brokers: dict[str, PaperExecutionAdapter],
    quotebook: QuoteBook,
    account_id: str,
) -> AccountEquity:
    cash = Decimal("0")
    position_value = Decimal("0")
    unrealized = Decimal("0")
    realized = Decimal("0")
    for broker in brokers.values():
        broker_cash, positions = broker.account_snapshot(account_id)
        cash += broker_cash
        realized += broker.realized_pnl(account_id)
        for outcome_id, (qty, avg_price, _realized) in positions.items():
            quote = quotebook.get(outcome_id)
            mark = _mid(quote) if quote is not None else avg_price
            position_value += mark * qty
            unrealized += (mark - avg_price) * qty
    return AccountEquity(
        cash=cash,
        position_value=position_value,
        equity=cash + position_value,
        unrealized=unrealized,
        realized=realized,
    )
