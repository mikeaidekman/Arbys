"""Pure arbitrage detection functions.

All functions in this module are deterministic and take fully-materialized
inputs (quotes + fee models). No I/O, no clocks, no globals. This makes them
exhaustively testable and safe to reuse in the live engine, paper broker, and
backtester.

Two detectors are provided:

* `detect_cross_venue_two_leg` — the classic prediction-market arb:
    Event group has (at least) two legs, one YES-side and one NO-side. If the
    sum of "buy YES ask" + "buy NO ask" (net of fees, translated to a unit
    payoff) is < 1, there is a guaranteed profit.

* `detect_complementary_set` — single-venue multi-outcome markets where
    buying one share of every outcome guarantees a $1 payoff. If sum(asks +
    fees) < 1, the set is arbitrageable.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .fees import FeeModel, FeeModelRegistry
from .types import EventGroup, EventGroupLeg, Quote


@dataclass(frozen=True)
class ArbLeg:
    """One trade in an arb ticket."""

    outcome_id: str
    venue_id: str
    is_buy: bool
    price: Decimal
    qty: Decimal
    fee: Decimal


@dataclass(frozen=True)
class ArbOpportunity:
    """A detected, executable arbitrage.

    `guaranteed_profit` is in the same currency as the leg prices — for
    prediction markets that's USD/USDC per $1 of guaranteed payoff.
    `guaranteed_profit_bps` is profit / total_stake * 10_000.
    """

    event_group_id: str
    legs: tuple[ArbLeg, ...]
    total_stake: Decimal
    guaranteed_profit: Decimal
    guaranteed_profit_bps: Decimal


def _leg_unit_cost(
    ask: Decimal,
    fee_model: FeeModel,
    is_buy: bool,
) -> Decimal:
    """Cost per 1 unit of contract, including per-unit fees.

    Uses qty=1 to get an average fee-per-unit at this price. Because our fee
    models are linear in qty, this is equivalent to (fees(N) / N) for any N > 0.
    """
    qty = Decimal("1")
    fee = fee_model.fee(price=ask, qty=qty, is_buy=is_buy)
    return ask + fee


def detect_cross_venue_two_leg(
    event_group: EventGroup,
    quotes: dict[str, Quote],
    fees: FeeModelRegistry,
    target_payoff: Decimal = Decimal("1"),
) -> ArbOpportunity | None:
    """Detect the cheapest YES-leg + NO-leg cross-venue arb for an event group.

    Strategy: for every (yes_leg, no_leg) pair drawn from the group's legs,
    compute the sum of unit costs (ask + per-unit fee). If < target_payoff,
    the pair is an arb. Return the pair with the largest guaranteed profit.

    Sizing is per-unit for now: buy `target_payoff` shares of each side so the
    payoff is exactly `target_payoff` regardless of which side wins.
    """
    yes_legs = [leg for leg in event_group.legs if leg.is_yes_side]
    no_legs = [leg for leg in event_group.legs if not leg.is_yes_side]
    if not yes_legs or not no_legs:
        return None

    best: ArbOpportunity | None = None
    for y in yes_legs:
        yq = quotes.get(y.outcome_id)
        if yq is None:
            continue
        y_fee_model = fees.get(y.venue_id)
        if y_fee_model is None:
            continue
        y_unit = _leg_unit_cost(yq.ask, y_fee_model, is_buy=True)

        for n in no_legs:
            nq = quotes.get(n.outcome_id)
            if nq is None:
                continue
            n_fee_model = fees.get(n.venue_id)
            if n_fee_model is None:
                continue
            n_unit = _leg_unit_cost(nq.ask, n_fee_model, is_buy=True)

            total_unit_cost = y_unit + n_unit
            if total_unit_cost >= target_payoff:
                continue

            qty = target_payoff  # shares of each side; payoff = qty * 1
            y_cost = yq.ask * qty
            n_cost = nq.ask * qty
            y_fee = y_fee_model.fee(price=yq.ask, qty=qty, is_buy=True)
            n_fee = n_fee_model.fee(price=nq.ask, qty=qty, is_buy=True)
            total_stake = y_cost + n_cost + y_fee + n_fee
            profit = target_payoff - total_stake
            if profit <= 0:
                continue

            opp = ArbOpportunity(
                event_group_id=event_group.id,
                legs=(
                    ArbLeg(
                        outcome_id=y.outcome_id,
                        venue_id=y.venue_id,
                        is_buy=True,
                        price=yq.ask,
                        qty=qty,
                        fee=y_fee,
                    ),
                    ArbLeg(
                        outcome_id=n.outcome_id,
                        venue_id=n.venue_id,
                        is_buy=True,
                        price=nq.ask,
                        qty=qty,
                        fee=n_fee,
                    ),
                ),
                total_stake=total_stake,
                guaranteed_profit=profit,
                guaranteed_profit_bps=(profit / total_stake) * Decimal(10_000),
            )
            if best is None or opp.guaranteed_profit > best.guaranteed_profit:
                best = opp

    return best


def detect_complementary_set(
    event_group_id: str,
    legs: list[EventGroupLeg],
    quotes: dict[str, Quote],
    fees: FeeModelRegistry,
    target_payoff: Decimal = Decimal("1"),
) -> ArbOpportunity | None:
    """Single-venue multi-outcome arb: buy every outcome so exactly one pays 1.

    Every leg must resolve to a mutually-exclusive outcome of the same event.
    Buying `target_payoff` of every outcome guarantees a payoff of
    `target_payoff` (because exactly one leg wins).
    """
    if len(legs) < 2:
        return None

    total_stake = Decimal("0")
    arb_legs: list[ArbLeg] = []
    for leg in legs:
        q = quotes.get(leg.outcome_id)
        if q is None:
            return None
        fee_model = fees.get(leg.venue_id)
        if fee_model is None:
            return None
        cost = q.ask * target_payoff
        fee = fee_model.fee(price=q.ask, qty=target_payoff, is_buy=True)
        total_stake += cost + fee
        arb_legs.append(
            ArbLeg(
                outcome_id=leg.outcome_id,
                venue_id=leg.venue_id,
                is_buy=True,
                price=q.ask,
                qty=target_payoff,
                fee=fee,
            )
        )

    profit = target_payoff - total_stake
    if profit <= 0:
        return None

    return ArbOpportunity(
        event_group_id=event_group_id,
        legs=tuple(arb_legs),
        total_stake=total_stake,
        guaranteed_profit=profit,
        guaranteed_profit_bps=(profit / total_stake) * Decimal(10_000),
    )
