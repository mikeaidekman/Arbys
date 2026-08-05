"""Paper execution: real quotes, simulated fills.

`PaperExecutionAdapter` implements `ExecutionAdapter` per venue. It:

* Looks up the current top-of-book from a shared `QuoteBook`.
* Applies configurable *slippage* (in basis points, adverse to the taker) and
  *latency* (currently modeled as "re-read the book after N ms" — for the pure
  version we just apply slippage once).
* Applies the venue's `FeeModel`.
* Enforces limit prices (rejects if the post-slippage price is worse than the
  submitted limit).
* Debits/credits an in-memory balance ledger and tracks positions.

`ExecutionRouter` fans an `ExecutionIntent` out to per-venue adapters and
enforces atomicity: if any leg would fail, none are placed.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from ..adapters.base import ExecutionAdapter, Fill, Order, OrderStatus
from .fees import FeeModel
from .quotebook import QuoteBook


def _uid() -> str:
    return uuid.uuid4().hex


BPS = Decimal(10_000)


@dataclass
class _AccountState:
    balances: dict[str, Decimal] = field(default_factory=dict)  # venue_id -> cash
    positions: dict[str, Decimal] = field(default_factory=lambda: defaultdict(lambda: Decimal("0")))
    avg_price: dict[str, Decimal] = field(default_factory=lambda: defaultdict(lambda: Decimal("0")))
    realized_pnl: Decimal = Decimal("0")


class PaperExecutionAdapter(ExecutionAdapter):
    """Per-venue paper broker.

    All accounts and orders live in-process. A follow-up will add a DB-backed
    persistence layer that wraps this class so restarts don't lose state.
    """

    def __init__(
        self,
        *,
        venue_id: str,
        quotebook: QuoteBook,
        fee_model: FeeModel,
        slippage_bps: Decimal = Decimal("0"),
    ) -> None:
        self.venue_id = venue_id
        self._book = quotebook
        self._fees = fee_model
        self._slippage_bps = slippage_bps

        self._orders: dict[str, Order] = {}
        self._fills: dict[str, list[Fill]] = defaultdict(list)
        self._accounts: dict[str, _AccountState] = defaultdict(_AccountState)

    # ------------------------------------------------------------------
    # Admin helpers (not part of the ExecutionAdapter interface)
    # ------------------------------------------------------------------

    def deposit(self, account_id: str, amount: Decimal) -> None:
        st = self._accounts[account_id]
        st.balances[self.venue_id] = st.balances.get(self.venue_id, Decimal("0")) + amount

    def _apply_slippage(self, price: Decimal, is_buy: bool) -> Decimal:
        adj = price * self._slippage_bps / BPS
        p = price + adj if is_buy else price - adj
        if p < 0:
            return Decimal("0")
        if p > 1:
            return Decimal("1")
        return p

    def _preview_fill(
        self, *, outcome_id: str, is_buy: bool, qty: Decimal, limit_price: Decimal
    ) -> tuple[Decimal, Decimal] | str:
        """Return (fill_price, total_cost) or a rejection reason string."""
        quote = self._book.get(outcome_id)
        if quote is None:
            return "no_quote"
        raw_px = quote.ask if is_buy else quote.bid
        px = self._apply_slippage(raw_px, is_buy)
        if is_buy and px > limit_price:
            return "limit_exceeded"
        if not is_buy and px < limit_price:
            return "limit_exceeded"
        fee = self._fees.fee(price=px, qty=qty, is_buy=is_buy)
        cost = px * qty + fee if is_buy else -(px * qty) + fee
        return px, cost

    # ------------------------------------------------------------------
    # ExecutionAdapter implementation
    # ------------------------------------------------------------------

    async def place_order(
        self,
        *,
        account_id: str,
        outcome_id: str,
        is_buy: bool,
        qty: Decimal,
        limit_price: Decimal,
    ) -> Order:
        order_id = _uid()
        preview = self._preview_fill(
            outcome_id=outcome_id, is_buy=is_buy, qty=qty, limit_price=limit_price
        )
        if isinstance(preview, str):
            order = Order(
                id=order_id,
                venue_id=self.venue_id,
                outcome_id=outcome_id,
                is_buy=is_buy,
                qty=qty,
                limit_price=limit_price,
                status=OrderStatus.REJECTED,
            )
            self._orders[order_id] = order
            return order

        px, cost = preview
        st = self._accounts[account_id]
        cash = st.balances.get(self.venue_id, Decimal("0"))
        if is_buy and cost > cash:
            order = Order(
                id=order_id,
                venue_id=self.venue_id,
                outcome_id=outcome_id,
                is_buy=is_buy,
                qty=qty,
                limit_price=limit_price,
                status=OrderStatus.REJECTED,
            )
            self._orders[order_id] = order
            return order

        # Apply fill.
        fee = self._fees.fee(price=px, qty=qty, is_buy=is_buy)
        if is_buy:
            st.balances[self.venue_id] = cash - (px * qty + fee)
            self._acquire(st, outcome_id, qty, px)
        else:
            st.balances[self.venue_id] = cash + (px * qty - fee)
            self._release(st, outcome_id, qty, px)

        order = Order(
            id=order_id,
            venue_id=self.venue_id,
            outcome_id=outcome_id,
            is_buy=is_buy,
            qty=qty,
            limit_price=limit_price,
            status=OrderStatus.FILLED,
        )
        self._orders[order_id] = order
        self._fills[order_id].append(Fill(order_id=order_id, qty=qty, price=px, fee=fee))
        return order

    async def cancel_order(self, order_id: str) -> Order:
        # Paper orders fill (or reject) synchronously so there's nothing to cancel.
        return self._orders[order_id]

    async def get_balances(self, account_id: str) -> dict[str, Decimal]:
        st = self._accounts[account_id]
        return dict(st.balances)

    async def get_positions(self, account_id: str) -> dict[str, Decimal]:
        st = self._accounts[account_id]
        return dict(st.positions)

    async def get_fills(self, order_id: str) -> list[Fill]:
        return list(self._fills.get(order_id, []))

    # ------------------------------------------------------------------
    # Position bookkeeping
    # ------------------------------------------------------------------

    def _acquire(
        self, st: _AccountState, outcome_id: str, qty: Decimal, price: Decimal
    ) -> None:
        cur_qty = st.positions[outcome_id]
        cur_avg = st.avg_price[outcome_id]
        new_qty = cur_qty + qty
        st.avg_price[outcome_id] = (
            (cur_qty * cur_avg + qty * price) / new_qty if new_qty > 0 else Decimal("0")
        )
        st.positions[outcome_id] = new_qty

    def _release(
        self, st: _AccountState, outcome_id: str, qty: Decimal, price: Decimal
    ) -> None:
        cur_qty = st.positions[outcome_id]
        cur_avg = st.avg_price[outcome_id]
        realized = (price - cur_avg) * min(qty, cur_qty)
        st.realized_pnl += realized
        st.positions[outcome_id] = cur_qty - qty

    # ------------------------------------------------------------------
    # Resolution / settlement (for `paper-resolution` scope)
    # ------------------------------------------------------------------

    def settle_outcome(self, outcome_id: str, resolved_value: Decimal) -> None:
        """Settle every account's position in `outcome_id` at `resolved_value` (0 or 1)."""
        for st in self._accounts.values():
            qty = st.positions.get(outcome_id, Decimal("0"))
            if qty == 0:
                continue
            avg = st.avg_price[outcome_id]
            st.realized_pnl += (resolved_value - avg) * qty
            st.balances[self.venue_id] = (
                st.balances.get(self.venue_id, Decimal("0")) + resolved_value * qty
            )
            st.positions[outcome_id] = Decimal("0")
            st.avg_price[outcome_id] = Decimal("0")

    def realized_pnl(self, account_id: str) -> Decimal:
        return self._accounts[account_id].realized_pnl
