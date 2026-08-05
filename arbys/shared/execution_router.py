"""Execution router — routes an ExecutionIntent to per-venue ExecutionAdapters.

Enforces atomicity for arb tickets: previews each leg with the target adapter,
and only commits every leg if all previews succeed.
"""

from __future__ import annotations

from decimal import Decimal

from ..adapters.base import ExecutionAdapter, ExecutionIntent, Order, OrderStatus
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
        orders: list[Order] = []
        for leg in intent.legs:
            adapter = self._adapters[leg.venue_id]
            order = await adapter.place_order(
                account_id=intent.account_id,
                outcome_id=leg.outcome_id,
                is_buy=leg.is_buy,
                qty=leg.qty,
                limit_price=leg.limit_price,
            )
            orders.append(order)
            if order.status != OrderStatus.FILLED:
                # Should not happen because preview succeeded; surface anyway.
                raise InsufficientLegsError(
                    f"post-preview rejection on {leg.venue_id}: {order.status}"
                )
        return orders
