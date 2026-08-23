"""Pydantic response/request schemas for the API."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class ArbLegOut(BaseModel):
    outcome_id: str
    venue_id: str
    is_buy: bool
    price: Decimal
    qty: Decimal
    fee: Decimal


class ArbOpportunityOut(BaseModel):
    event_group_id: str
    total_stake: Decimal
    guaranteed_profit: Decimal
    guaranteed_profit_bps: Decimal
    legs: list[ArbLegOut]


class EventGroupLegIn(BaseModel):
    outcome_id: str
    venue_id: str
    is_yes_side: bool


class EventGroupIn(BaseModel):
    id: str
    title: str
    legs: list[EventGroupLegIn]


class EventGroupOut(EventGroupIn):
    pass


class MonitoredLegOut(BaseModel):
    outcome_id: str
    venue_id: str
    is_yes_side: bool
    bid: Decimal | None
    ask: Decimal | None
    # Size resting at the quoted price. 0 means the venue reported no depth,
    # which is not the same as "none available" — treat it as unknown.
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None
    # Seconds since this leg last updated, and whether that exceeds the book's
    # staleness threshold. A stale leg reports bid/ask None — it is not
    # tradeable — but keeps its age so the UI can say why.
    quote_age_s: float | None = None
    is_stale: bool = False


class MonitoredGroupOut(BaseModel):
    id: str
    title: str
    # Scheduled start of the real-world event, UTC. None when unknown.
    start_time: datetime | None
    legs: list[MonitoredLegOut]
    best_yes_ask: Decimal | None
    best_yes_venue: str | None
    best_no_ask: Decimal | None
    best_no_venue: str | None
    arb_edge: Decimal | None  # 1 - (best_yes_ask + best_no_ask); positive = arb
    has_arb: bool
    fully_quoted: bool  # all legs have quotes
    # Net-of-fee figures for the best tradeable YES+NO pair, ranked by highest
    # net_edge * qty -- the same objective detect_cross_venue_two_leg uses to
    # pick its winning pair. NOT cheapest unit cost, and NOT derived from
    # best_yes_ask/best_no_ask above: those two are independently cheapest per
    # side and can both come from the same venue, corresponding to no single
    # tradeable pair. All six fields are None when no (yes, no) combination is
    # fully quoted with a known fee model. net_edge may be negative -- that is
    # normal near a coin flip and the row still needs to state its position.
    net_edge: Decimal | None  # profit per contract, after both legs' fees
    max_tradeable_qty: Decimal | None  # thinnest leg's depth, after stake cap
    net_max_profit: Decimal | None  # net_edge * max_tradeable_qty
    capital_required: Decimal | None  # total stake for max_tradeable_qty
    # Which pair the four fields above describe. The frontend has no fee model
    # of its own and cannot re-derive the ranking, so the backend names the
    # pair explicitly -- match on leg outcome_id against a published opportunity.
    best_pair_yes_outcome_id: str | None
    best_pair_no_outcome_id: str | None


class QuoteIn(BaseModel):
    # None = unknown, which is what a hand-pushed quote almost always means.
    # 0 would assert "nothing is resting there" and block fills.
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None
    outcome_id: str
    bid: Decimal
    ask: Decimal


class ExecuteArbIn(BaseModel):
    """Which opportunity to fill.

    Prefer ``event_group_id`` (+ optionally ``outcome_ids``): the server
    resolves it against the *current* opportunity list at execution time.
    ``opportunity_index`` is a position in a list the client fetched earlier
    and the engine rewrites continuously, so it can select a different arb
    than the caller saw. It is retained for compatibility only.
    """

    opportunity_index: int = 0
    account_id: str | None = None
    event_group_id: str | None = None
    # Buy-leg outcome ids identifying the exact combination to fill. When
    # omitted, any live opportunity for the event group is eligible.
    outcome_ids: list[str] | None = None


class PaperAccountSummary(BaseModel):
    account_id: str
    balances: dict[str, Decimal]
    positions: dict[str, Decimal]
    realized_pnl: dict[str, Decimal]
