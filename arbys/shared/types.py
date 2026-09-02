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
    # How stale the venue itself said this book was when the quote reached us,
    # in seconds. None when the venue publishes no such timestamp, which is
    # not the same as zero - it means unknown, and the book falls back to
    # arrival time.
    #
    # This exists because arrival time is a lie on a replayed snapshot.
    # Polymarket US serves a cached book on subscribe whose own `transactTime`
    # can be hours behind: measured 2026-08-25, 199 of 571 markets came back
    # over an hour stale and 58% of those disagreed with the live book by 2c
    # or more, one by 97c. Stamped on arrival they read as 0.2s old, so no age
    # check could withhold them, and a stale leg invented arbitrage against a
    # live one on the other venue.
    source_age_s: float | None = None

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
    # Whether a venue says the real-world event is under way right now.
    # ``None`` means nobody said, in which case callers fall back to guessing
    # from ``start_time``. Deliberately **not persisted**: it flips as games
    # start and finish, so a value rehydrated from the database would be a
    # confident lie, whereas ``None`` correctly means "ask again".
    in_play: bool | None = None
    # Whether a venue says the real-world event has *finished*. Distinct from
    # ``in_play is False``, which conflates "not started yet" with "over" --
    # and settlement needs to tell those apart, since only one of them has a
    # result. ``None`` means nobody said; Kalshi never does.
    #
    # Not persisted, for the same reason as ``in_play``: it flips as games
    # finish, so a rehydrated value would be a confident lie where ``None``
    # correctly means "ask again".
    ended: bool | None = None
    # "discovery" for auto-registered groups, "manual" for hand-registered
    # ones. Discovery retires its own groups when they stop matching; manual
    # groups are never touched.
    source: str = "manual"
