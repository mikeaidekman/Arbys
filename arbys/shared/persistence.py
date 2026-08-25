"""DB-backed implementation of `PaperPersistenceSink`.

Mirrors every paper broker mutation into the paper_* tables. Failures are
swallowed by the broker's `_emit` wrapper so DB flakiness never breaks the
in-memory truth of the simulator; a real deployment would add retry + alerting.
"""

from __future__ import annotations

from decimal import Decimal

from ..adapters.base import Fill, Order, OrderStatus
from ..db import repositories as repo
from ..db.session import run_write


class DbPaperPersistenceSink:
    async def on_order(self, order: Order, *, rejection_reason: str | None = None) -> None:
        status = order.status.value if isinstance(order.status, OrderStatus) else str(order.status)
        await run_write(
            "sink.on_order",
            lambda s: repo.insert_paper_order(
                s,
                order_id=order.id,
                account_id="",  # populated via wrapper below; sinks are per-account in v2
                venue_id=order.venue_id,
                outcome_id=order.outcome_id,
                is_buy=order.is_buy,
                qty=order.qty,
                limit_price=order.limit_price,
                status=status,
                rejection_reason=rejection_reason,
                ticket_id=order.ticket_id,
            ),
        )

    async def on_fill(self, order: Order, fill: Fill) -> None:
        await run_write(
            "sink.on_fill",
            lambda s: repo.insert_paper_fill(
                s, order_id=order.id, qty=fill.qty, price=fill.price, fee=fill.fee
            ),
        )

    async def on_balance(self, account_id: str, venue_id: str, amount: Decimal) -> None:
        await run_write(
            "sink.on_balance",
            lambda s: repo.upsert_paper_balance(
                s, account_id=account_id, venue_id=venue_id, amount=amount
            ),
        )

    async def on_position(
        self,
        account_id: str,
        outcome_id: str,
        qty: Decimal,
        avg_price: Decimal,
        realized_pnl: Decimal,
        *,
        venue_id: str,
    ) -> None:
        await run_write(
            "sink.on_position",
            lambda s: repo.upsert_paper_position(
                s,
                account_id=account_id,
                venue_id=venue_id,
                outcome_id=outcome_id,
                qty=qty,
                avg_price=avg_price,
                realized_pnl=realized_pnl,
            ),
        )

    async def on_settlement(
        self, outcome_id: str, resolved_value: Decimal, *, venue_id: str, source: str
    ) -> None:
        await run_write(
            "sink.on_settlement",
            lambda s: repo.insert_paper_settlement(
                s,
                outcome_id=outcome_id,
                venue_id=venue_id,
                resolved_value=resolved_value,
                source=source,
            ),
        )


class AccountScopedSink:
    """Wraps a `DbPaperPersistenceSink` and pins the account id on `on_order`."""

    def __init__(self, inner: DbPaperPersistenceSink, account_id: str) -> None:
        self._inner = inner
        self._account_id = account_id

    async def on_order(self, order: Order, *, rejection_reason: str | None = None) -> None:
        # Route through the inner sink but inject account_id.
        status = order.status.value if isinstance(order.status, OrderStatus) else str(order.status)
        await run_write(
            "sink.on_order.scoped",
            lambda s: repo.insert_paper_order(
                s,
                order_id=order.id,
                account_id=self._account_id,
                venue_id=order.venue_id,
                outcome_id=order.outcome_id,
                is_buy=order.is_buy,
                qty=order.qty,
                limit_price=order.limit_price,
                status=status,
                rejection_reason=rejection_reason,
                ticket_id=order.ticket_id,
            ),
        )

    async def on_fill(self, order: Order, fill: Fill) -> None:
        await self._inner.on_fill(order, fill)

    async def on_balance(self, account_id: str, venue_id: str, amount: Decimal) -> None:
        await self._inner.on_balance(account_id, venue_id, amount)

    async def on_position(
        self,
        account_id: str,
        outcome_id: str,
        qty: Decimal,
        avg_price: Decimal,
        realized_pnl: Decimal,
        *,
        venue_id: str,
    ) -> None:
        await self._inner.on_position(
            account_id, outcome_id, qty, avg_price, realized_pnl, venue_id=venue_id
        )

    async def on_settlement(
        self, outcome_id: str, resolved_value: Decimal, *, venue_id: str, source: str
    ) -> None:
        await self._inner.on_settlement(
            outcome_id, resolved_value, venue_id=venue_id, source=source
        )
