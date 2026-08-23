"""Adapter interfaces — the seams between the engine and the outside world.

Every venue integration implements two ABCs:

* `MarketDataAdapter` — read-side. Discover markets and stream live quotes.
* `ExecutionAdapter` — write-side. In v1 the *only* implementation is the paper
  broker; live venue implementations are deferred but must slot into the same
  interface without engine changes.

An `ExecutionRouter` fans an `ExecutionIntent` out to the correct venue-specific
`ExecutionAdapter`s.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from ..shared.types import Outcome, Quote


class OrderStatus(StrEnum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Order:
    id: str
    venue_id: str
    outcome_id: str
    is_buy: bool
    qty: Decimal
    limit_price: Decimal
    status: OrderStatus
    # Groups the legs of one arb ticket. None for orders placed outside a
    # ticket, and for every row written before migration 0006.
    ticket_id: str | None = None


@dataclass(frozen=True)
class Fill:
    order_id: str
    qty: Decimal
    price: Decimal
    fee: Decimal


@dataclass(frozen=True)
class ExecutionIntent:
    """A multi-leg trade the router should submit atomically ("arb ticket")."""

    event_group_id: str
    account_id: str
    legs: tuple[IntentLeg, ...]
    ticket_id: str | None = None


@dataclass(frozen=True)
class IntentLeg:
    venue_id: str
    outcome_id: str
    is_buy: bool
    qty: Decimal
    limit_price: Decimal


class MarketDataAdapter(ABC):
    venue_id: str

    @abstractmethod
    async def list_markets(self) -> list[Outcome]: ...

    @abstractmethod
    async def stream_quotes(self) -> AsyncIterator[Quote]: ...


class ExecutionAdapter(ABC):
    venue_id: str

    @abstractmethod
    async def place_order(
        self,
        *,
        account_id: str,
        outcome_id: str,
        is_buy: bool,
        qty: Decimal,
        limit_price: Decimal,
        ticket_id: str | None = None,
    ) -> Order: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> Order: ...

    @abstractmethod
    async def get_balances(self, account_id: str) -> dict[str, Decimal]: ...

    @abstractmethod
    async def get_positions(self, account_id: str) -> dict[str, Decimal]: ...

    @abstractmethod
    async def get_fills(self, order_id: str) -> list[Fill]: ...
