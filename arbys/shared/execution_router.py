"""Execution router — routes an ExecutionIntent to per-venue ExecutionAdapters.

Enforces atomicity for arb tickets: previews each leg with the target adapter,
and only commits every leg if all previews succeed.
"""

from __future__ import annotations

from decimal import Decimal

from ..adapters.base import (
    ExecutionAdapter,
    ExecutionIntent,
    Fill,
    Order,
    OrderStatus,
)
from .paper_broker import PaperExecutionAdapter


class InsufficientLegsError(RuntimeError):
    pass


class ExecutionRouter:
    def __init__(self, adapters: dict[str, ExecutionAdapter]) -> None:
        self._adapters = adapters

    async def submit(self, intent: ExecutionIntent) -> list[Order]:
        # Preview phase (paper broker only): only the paper adapter exposes
        # `_preview_fill`. If any leg would be rejected, refuse the whole ticket.
        rejections: list[str] = []
        for leg in intent.legs:
            adapter = self._adapters.get(leg.venue_id)
            if adapter is None:
                rejections.append(f"{leg.venue_id}:no_adapter")
                continue
            if isinstance(adapter, PaperExecutionAdapter):
                preview = adapter._preview_fill(
                    outcome_id=leg.outcome_id,
                    is_buy=leg.is_buy,
                    qty=leg.qty,
                    limit_price=leg.limit_price,
                )
                if isinstance(preview, str):
                    rejections.append(f"{leg.venue_id}:{preview}")
                    continue
                # Also require sufficient cash if buying.
                if leg.is_buy:
                    _px, cost = preview
                    balances = await adapter.get_balances(intent.account_id)
                    if cost > balances.get(leg.venue_id, Decimal("0")):
                        rejections.append(f"{leg.venue_id}:insufficient_funds")
        if rejections:
            raise InsufficientLegsError(", ".join(rejections))

        # Commit phase.
        adapters = [self._adapters[leg.venue_id] for leg in intent.legs]
        if all(isinstance(a, PaperExecutionAdapter) for a in adapters):
            return await self._commit_atomically(intent, adapters)
        return await self._commit_sequentially(intent)

    async def _commit_atomically(
        self, intent: ExecutionIntent, adapters: list[ExecutionAdapter]
    ) -> list[Order]:
        """Fill every leg with no ``await`` in between, then notify.

        Awaiting between legs let a quote update land mid-ticket, so a later
        leg could be rejected after an earlier one had already filled —
        leaving a naked position on one venue while the caller saw only an
        error. Because ``apply_fill`` is synchronous, nothing can interleave
        here. The snapshot/rollback is a backstop for a rejection arising from
        something other than a price move.
        """
        account_id = intent.account_id
        papers: list[PaperExecutionAdapter] = [
            a for a in adapters if isinstance(a, PaperExecutionAdapter)
        ]
        # One snapshot per distinct adapter; several legs may share a venue.
        snapshots: list[tuple[PaperExecutionAdapter, dict]] = []
        seen: set[int] = set()
        for a in papers:
            if id(a) not in seen:
                seen.add(id(a))
                snapshots.append((a, a.snapshot_account(account_id)))

        applied: list[tuple[PaperExecutionAdapter, Order, Fill | None]] = []
        failure: str | None = None
        for leg, adapter in zip(intent.legs, adapters, strict=True):
            assert isinstance(adapter, PaperExecutionAdapter)
            order, fill, reason = adapter.apply_fill(
                account_id=account_id,
                outcome_id=leg.outcome_id,
                is_buy=leg.is_buy,
                qty=leg.qty,
                limit_price=leg.limit_price,
                ticket_id=intent.ticket_id,
            )
            if order.status != OrderStatus.FILLED:
                adapter.forget_order(order.id)
                failure = f"post-preview rejection on {leg.venue_id}: {reason or order.status}"
                break
            applied.append((adapter, order, fill))

        if failure is not None:
            # Unwind: restore balances/positions and drop the recorded orders,
            # so a partial ticket leaves no trace and no naked leg.
            for adapter, snap in snapshots:
                adapter.restore_account(account_id, snap)
            for adapter, order, _fill in applied:
                adapter.forget_order(order.id)
            raise InsufficientLegsError(failure)

        # Whole ticket is committed; notifications can safely await now.
        for adapter, order, fill in applied:
            await adapter.emit_order_events(account_id, order, fill, None)
        return [order for _adapter, order, _fill in applied]

    async def _commit_sequentially(self, intent: ExecutionIntent) -> list[Order]:
        """Fallback for non-paper adapters, which cannot be rolled back.

        A real venue fill is not reversible, so this path can still leave a
        partial ticket — the error names the leg that failed.
        """
        orders: list[Order] = []
        for leg in intent.legs:
            adapter = self._adapters[leg.venue_id]
            order = await adapter.place_order(
                account_id=intent.account_id,
                outcome_id=leg.outcome_id,
                is_buy=leg.is_buy,
                qty=leg.qty,
                limit_price=leg.limit_price,
                ticket_id=intent.ticket_id,
            )
            orders.append(order)
            if order.status != OrderStatus.FILLED:
                raise InsufficientLegsError(
                    f"post-preview rejection on {leg.venue_id}: {order.status} "
                    f"({len(orders) - 1} leg(s) already filled and NOT reversed)"
                )
        return orders
