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

An optional `PaperPersistenceSink` receives every mutation for durable storage.
The sink is called synchronously as part of the mutation so the DB and the
in-memory state cannot diverge.
"""

from __future__ import annotations

import contextlib
import uuid
from collections import defaultdict
from collections.abc import Awaitable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from ..adapters.base import ExecutionAdapter, Fill, Order, OrderStatus
from .fees import FeeModel
from .quotebook import QuoteBook


def _uid() -> str:
    return uuid.uuid4().hex


BPS = Decimal(10_000)


class PaperPersistenceSink(Protocol):
    """Called by PaperExecutionAdapter on every state-changing event.

    Implementations should not raise — swallow persistence errors and log,
    because paper trading must not break if DB is temporarily unavailable.
    """

    async def on_order(self, order: Order, *, rejection_reason: str | None = None) -> None: ...
    async def on_fill(self, order: Order, fill: Fill) -> None: ...
    async def on_balance(self, account_id: str, venue_id: str, amount: Decimal) -> None: ...
    async def on_position(
        self,
        account_id: str,
        outcome_id: str,
        qty: Decimal,
        avg_price: Decimal,
        realized_pnl: Decimal,
        *,
        venue_id: str,
    ) -> None: ...


@dataclass
class _AccountState:
    balances: dict[str, Decimal] = field(default_factory=dict)  # venue_id -> cash
    positions: dict[str, Decimal] = field(default_factory=lambda: defaultdict(lambda: Decimal("0")))
    avg_price: dict[str, Decimal] = field(default_factory=lambda: defaultdict(lambda: Decimal("0")))
    realized_pnl: Decimal = Decimal("0")
    realized_by_outcome: dict[str, Decimal] = field(
        default_factory=lambda: defaultdict(lambda: Decimal("0"))
    )


class PaperExecutionAdapter(ExecutionAdapter):
    """Per-venue paper broker."""

    def __init__(
        self,
        *,
        venue_id: str,
        quotebook: QuoteBook,
        fee_model: FeeModel,
        slippage_bps: Decimal = Decimal("0"),
        sink: PaperPersistenceSink | None = None,
    ) -> None:
        self.venue_id = venue_id
        self._book = quotebook
        self._fees = fee_model
        self._slippage_bps = slippage_bps
        self._sink = sink

        self._orders: dict[str, Order] = {}
        self._fills: dict[str, list[Fill]] = defaultdict(list)
        self._accounts: dict[str, _AccountState] = defaultdict(_AccountState)

    def set_sink(self, sink: PaperPersistenceSink | None) -> None:
        self._sink = sink

    # ------------------------------------------------------------------
    # Admin helpers (not part of the ExecutionAdapter interface)
    # ------------------------------------------------------------------

    def deposit(self, account_id: str, amount: Decimal) -> None:
        st = self._accounts[account_id]
        st.balances[self.venue_id] = st.balances.get(self.venue_id, Decimal("0")) + amount

    def hydrate_balance(self, account_id: str, amount: Decimal) -> None:
        """Overwrite in-memory balance without triggering the persistence sink.

        Use during startup rehydration from DB.
        """
        self._accounts[account_id].balances[self.venue_id] = amount

    def reset_account(self, account_id: str) -> None:
        """Wipe in-memory state (balances, positions, realized PnL) for account.

        Also drops any orders/fills that referenced this account. Does not touch
        the DB — callers must delete persisted rows separately.
        """
        self._accounts.pop(account_id, None)
        # _orders and _fills are keyed by order_id; we can't cheaply filter by
        # account here, so we drop everything. Safe for the current single-
        # account paper setup.
        self._orders.clear()
        self._fills.clear()

    def hydrate_position(
        self,
        account_id: str,
        outcome_id: str,
        *,
        qty: Decimal,
        avg_price: Decimal,
        realized_pnl: Decimal,
    ) -> None:
        st = self._accounts[account_id]
        st.positions[outcome_id] = qty
        st.avg_price[outcome_id] = avg_price
        st.realized_by_outcome[outcome_id] = realized_pnl
        st.realized_pnl += realized_pnl

    def account_snapshot(self, account_id: str) -> tuple[Decimal, dict[str, tuple[Decimal, Decimal, Decimal]]]:
        st = self._accounts[account_id]
        cash = st.balances.get(self.venue_id, Decimal("0"))
        positions = {
            oid: (qty, st.avg_price[oid], st.realized_by_outcome[oid])
            for oid, qty in st.positions.items()
            if qty != 0
        }
        return cash, positions

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
        quote = self._book.get(outcome_id)
        if quote is None:
            return "no_quote"
        # A one-sided book keeps its live side and synthesises the missing one
        # at size 0, so the live side stays tradeable. Filling against the
        # synthesised side would be a trade into an empty book.
        #
        # Only an explicit 0 blocks. None means the venue did not report depth
        # — most quotes, including every hand-pushed one — and must still fill,
        # or POST /quotes stops working.
        resting = quote.ask_size if is_buy else quote.bid_size
        if resting is not None and resting <= 0:
            return "no_liquidity"
        raw_px = quote.ask if is_buy else quote.bid
        px = self._apply_slippage(raw_px, is_buy)
        if is_buy and px > limit_price:
            return "limit_exceeded"
        if not is_buy and px < limit_price:
            return "limit_exceeded"
        fee = self._fees.fee(price=px, qty=qty, is_buy=is_buy)
        cost = px * qty + fee if is_buy else -(px * qty) + fee
        return px, cost

    async def _emit(self, coro: Awaitable[None] | None) -> None:
        if coro is None:
            return
        # Persistence must never break the broker; swallow all sink errors.
        with contextlib.suppress(Exception):
            await coro

    # ------------------------------------------------------------------
    # ExecutionAdapter implementation
    # ------------------------------------------------------------------

    def apply_fill(
        self,
        *,
        account_id: str,
        outcome_id: str,
        is_buy: bool,
        qty: Decimal,
        limit_price: Decimal,
    ) -> tuple[Order, Fill | None, str | None]:
        """Decide and apply one order **without awaiting**.

        Returns ``(order, fill, rejection_reason)``. Every balance and position
        mutation happens here, synchronously, so a caller filling several legs
        back to back cannot be interleaved by a quote update — coroutines only
        yield at ``await``. Notifications are deferred to
        :meth:`emit_order_events`, which the caller must invoke once the whole
        ticket is committed.
        """
        order_id = _uid()

        def _rejected(reason: str) -> tuple[Order, None, str]:
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
            return order, None, reason

        preview = self._preview_fill(
            outcome_id=outcome_id, is_buy=is_buy, qty=qty, limit_price=limit_price
        )
        if isinstance(preview, str):
            return _rejected(preview)

        px, cost = preview
        st = self._accounts[account_id]
        cash = st.balances.get(self.venue_id, Decimal("0"))
        if is_buy and cost > cash:
            return _rejected("insufficient_funds")

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
        fill = Fill(order_id=order_id, qty=qty, price=px, fee=fee)
        self._orders[order_id] = order
        self._fills[order_id].append(fill)
        return order, fill, None

    def snapshot_account(self, account_id: str) -> dict[str, object]:
        """Copy the mutable account state, for rollback of a failed ticket."""
        st = self._accounts[account_id]
        return {
            "balances": dict(st.balances),
            "positions": dict(st.positions),
            "avg_price": dict(st.avg_price),
            "realized_pnl": st.realized_pnl,
            "realized_by_outcome": dict(st.realized_by_outcome),
        }

    def restore_account(self, account_id: str, snap: dict[str, object]) -> None:
        """Put back a :meth:`snapshot_account` copy, leaving defaultdicts intact."""
        st = self._accounts[account_id]
        for name in ("balances", "positions", "avg_price", "realized_by_outcome"):
            target = getattr(st, name)
            target.clear()
            target.update(snap[name])  # type: ignore[arg-type]
        st.realized_pnl = snap["realized_pnl"]  # type: ignore[assignment]

    def forget_order(self, order_id: str) -> None:
        """Drop a locally recorded order/fill that is being rolled back."""
        self._orders.pop(order_id, None)
        self._fills.pop(order_id, None)

    async def emit_order_events(
        self,
        account_id: str,
        order: Order,
        fill: Fill | None,
        rejection_reason: str | None,
    ) -> None:
        """Flush sink notifications for an order already applied by apply_fill."""
        if self._sink is None:
            return
        if fill is None:
            await self._emit(
                self._sink.on_order(order, rejection_reason=rejection_reason)
            )
            return
        st = self._accounts[account_id]
        await self._emit(self._sink.on_order(order))
        await self._emit(self._sink.on_fill(order, fill))
        await self._emit(
            self._sink.on_balance(account_id, self.venue_id, st.balances[self.venue_id])
        )
        await self._emit(
            self._sink.on_position(
                account_id,
                order.outcome_id,
                st.positions[order.outcome_id],
                st.avg_price[order.outcome_id],
                st.realized_by_outcome[order.outcome_id],
                venue_id=self.venue_id,
            )
        )

    async def place_order(
        self,
        *,
        account_id: str,
        outcome_id: str,
        is_buy: bool,
        qty: Decimal,
        limit_price: Decimal,
    ) -> Order:
        order, fill, reason = self.apply_fill(
            account_id=account_id,
            outcome_id=outcome_id,
            is_buy=is_buy,
            qty=qty,
            limit_price=limit_price,
        )
        await self.emit_order_events(account_id, order, fill, reason)
        return order

    async def cancel_order(self, order_id: str) -> Order:
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
        st.realized_by_outcome[outcome_id] += realized
        st.positions[outcome_id] = cur_qty - qty

    # ------------------------------------------------------------------
    # Resolution / settlement
    # ------------------------------------------------------------------

    async def settle_outcome_async(self, outcome_id: str, resolved_value: Decimal) -> None:
        for account_id, st in self._accounts.items():
            qty = st.positions.get(outcome_id, Decimal("0"))
            if qty == 0:
                continue
            avg = st.avg_price[outcome_id]
            realized = (resolved_value - avg) * qty
            st.realized_pnl += realized
            st.realized_by_outcome[outcome_id] += realized
            st.balances[self.venue_id] = (
                st.balances.get(self.venue_id, Decimal("0")) + resolved_value * qty
            )
            st.positions[outcome_id] = Decimal("0")
            st.avg_price[outcome_id] = Decimal("0")
            if self._sink is not None:
                await self._emit(
                    self._sink.on_balance(account_id, self.venue_id, st.balances[self.venue_id])
                )
                await self._emit(
                    self._sink.on_position(
                        account_id,
                        outcome_id,
                        Decimal("0"),
                        Decimal("0"),
                        st.realized_by_outcome[outcome_id],
                        venue_id=self.venue_id,
                    )
                )

    def settle_outcome(self, outcome_id: str, resolved_value: Decimal) -> None:
        """Synchronous settlement (no persistence). Kept for existing callers."""
        for st in self._accounts.values():
            qty = st.positions.get(outcome_id, Decimal("0"))
            if qty == 0:
                continue
            avg = st.avg_price[outcome_id]
            realized = (resolved_value - avg) * qty
            st.realized_pnl += realized
            st.realized_by_outcome[outcome_id] += realized
            st.balances[self.venue_id] = (
                st.balances.get(self.venue_id, Decimal("0")) + resolved_value * qty
            )
            st.positions[outcome_id] = Decimal("0")
            st.avg_price[outcome_id] = Decimal("0")

    def realized_pnl(self, account_id: str) -> Decimal:
        return self._accounts[account_id].realized_pnl

