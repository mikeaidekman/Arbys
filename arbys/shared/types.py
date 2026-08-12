"""Shared domain types used across adapters, engine, and API.

These are pure data classes with no I/O and no framework dependencies so they
are safe to import from any layer, including inside pure unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class VenueKind(StrEnum):
    EXCHANGE = "exchange"
    SPORTSBOOK = "sportsbook"


class Side(StrEnum):
    YES = "YES"
    NO = "NO"


@dataclass(frozen=True)
class Venue:
    id: str
    name: str
    kind: VenueKind


@dataclass(frozen=True)
class Outcome:
    """A single tradeable outcome on a specific venue's market.

    `probability` is the venue-native probability of this outcome resolving true.
    For sportsbook moneylines, callers must convert the raw American/decimal odds
    to a probability *before* constructing an Outcome, because the arb engine
    only sees probabilities.
    """

    id: str
    venue_id: str
    market_id: str
    label: str
    side: Side | None = None


@dataclass(frozen=True)
class Quote:
    """Top-of-book quote for one Outcome.

    `bid` = best price at which you can *sell* one share of this outcome.
    `ask` = best price at which you can *buy* one share of this outcome.
    Prices are probabilities in [0, 1].
    """

    outcome_id: str
    bid: Decimal
    ask: Decimal
    # Resting size at the quoted price, with three distinct states:
    #   None -> unknown. The venue did not report depth, or the endpoint
    #           cannot (Polymarket US /bbo reports level counts, not sizes).
    #   0    -> known empty. Nothing is resting on that side; this is what a
    #           synthesised side of a one-sided book carries.
    #   > 0  -> a real quantity.
    # The paper broker refuses to fill against a *known empty* side but will
    # fill against an unknown one, so conflating the two either blocks real
    # trades or invents fills into an empty book.
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None

    def __post_init__(self) -> None:
        if not (Decimal("0") <= self.bid <= Decimal("1")):
            raise ValueError(f"bid out of range: {self.bid}")
        if not (Decimal("0") <= self.ask <= Decimal("1")):
            raise ValueError(f"ask out of range: {self.ask}")
        if self.ask < self.bid:
            raise ValueError(f"ask {self.ask} < bid {self.bid}")


@dataclass(frozen=True)
class EventGroupLeg:
    """One outcome-on-a-venue that belongs to an event group.

    `is_yes_side` tells the engine whether taking this leg *long* (buying at ask)
    is a bet that the event group's canonical proposition resolves TRUE.
    This is how we tie together (e.g.) Polymarket YES + Kalshi NO for the
    opposite side of the same real-world question.
    """

    outcome_id: str
    venue_id: str
    is_yes_side: bool


@dataclass(frozen=True)
class EventGroup:
    id: str
    title: str
    resolution_source_by_venue: dict[str, str] = field(default_factory=dict)
    legs: tuple[EventGroupLeg, ...] = ()
    # Scheduled start of the underlying real-world event, UTC. None when the
    # group was registered by hand or the venue reported no time.
    start_time: datetime | None = None
    # "discovery" for auto-registered groups, "manual" for hand-registered
    # ones. Discovery retires its own groups when they stop matching; manual
    # groups are never touched.
    source: str = "manual"
