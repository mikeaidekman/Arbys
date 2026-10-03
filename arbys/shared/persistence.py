"""DB-backed implementation of `PaperPersistenceSink`.

Mirrors every paper broker mutation into the paper_* tables. Every write here
goes through `run_write`, which retries transient contention and never raises
-- so DB flakiness never breaks the in-memory truth of the simulator, and a
write it finally gives up on is retried, logged, and counted rather than
swallowed silently. See `arbys.db.session.run_write` and
`dropped_write_stats` for the retry/alerting this module relies on; a caller
here has no `try/except` of its own to add because `run_write` already
guarantees it cannot raise.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

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
        open_fees: Decimal = Decimal("0"),
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
                open_fees=open_fees,
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

    async def on_settled(
        self,
        outcome_id: str,
        resolved_value: Decimal,
        *,
        venue_id: str,
        source: str,
        closed: tuple[tuple[str, Decimal, Decimal, Decimal], ...],
    ) -> None:
        """A settlement's balance, position and record in one transaction.

        `closed` is (account, cash after, realized on the outcome, open fees)
        per account that held the outcome. All of it lands or none of it does:
        a paid-out balance beside a still-open position is what a restart
        turned into a position held forever.
        """

        async def work(s: AsyncSession) -> None:
            for account_id, balance, realized, open_fees in closed:
                await repo.upsert_paper_balance(
                    s, account_id=account_id, venue_id=venue_id, amount=balance
                )
                await repo.upsert_paper_position(
                    s,
                    account_id=account_id,
                    venue_id=venue_id,
                    outcome_id=outcome_id,
                    qty=Decimal("0"),
                    avg_price=Decimal("0"),
                    realized_pnl=realized,
                    open_fees=open_fees,
                )
            await repo.insert_paper_settlement(
                s,
                outcome_id=outcome_id,
                venue_id=venue_id,
                resolved_value=resolved_value,
                source=source,
            )

        await run_write("sink.on_settled", work)


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
        open_fees: Decimal = Decimal("0"),
    ) -> None:
        await self._inner.on_position(
            account_id, outcome_id, qty, avg_price, realized_pnl,
            venue_id=venue_id, open_fees=open_fees,
        )

    async def on_settlement(
        self, outcome_id: str, resolved_value: Decimal, *, venue_id: str, source: str
    ) -> None:
        await self._inner.on_settlement(
            outcome_id, resolved_value, venue_id=venue_id, source=source
        )

    async def on_settled(
        self,
        outcome_id: str,
        resolved_value: Decimal,
        *,
        venue_id: str,
        source: str,
        closed: tuple[tuple[str, Decimal, Decimal, Decimal], ...],
    ) -> None:
        await self._inner.on_settled(
            outcome_id, resolved_value, venue_id=venue_id, source=source, closed=closed
        )
