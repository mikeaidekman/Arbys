"""Pydantic response/request schemas for the API."""

from __future__ import annotations

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


class MonitoredGroupOut(BaseModel):
    id: str
    title: str
    legs: list[MonitoredLegOut]
    best_yes_ask: Decimal | None
    best_yes_venue: str | None
    best_no_ask: Decimal | None
    best_no_venue: str | None
    arb_edge: Decimal | None  # 1 - (best_yes_ask + best_no_ask); positive = arb
    has_arb: bool
    fully_quoted: bool  # all legs have quotes


class QuoteIn(BaseModel):
    outcome_id: str
    bid: Decimal
    ask: Decimal


class ExecuteArbIn(BaseModel):
    opportunity_index: int = 0
    account_id: str | None = None


class PaperAccountSummary(BaseModel):
    account_id: str
    balances: dict[str, Decimal]
    positions: dict[str, Decimal]
    realized_pnl: dict[str, Decimal]
