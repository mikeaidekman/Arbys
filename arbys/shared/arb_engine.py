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

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from .fees import FeeModel, FeeModelRegistry
from .qty import tradeable_qty
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


# Granularity a derived ticket size is floored to when the caller supplies no
# per-venue tick. Budget-bound sizing is a division, so without a tick it
# yields things like 214.615302071037664985513467 contracts — an order size no
# venue would accept, and one the `qty` column (12 decimal places) cannot
# round-trip. Measured 2026-08-22, both venues report resting size to two
# decimals: Kalshi 1156.25 and 412.00, Polymarket US 2616.69 and 64.71. So 0.01
# is the observed granularity — deliberately *not* whole contracts, because the
# venues themselves quote fractional size.
DEFAULT_QTY_TICK = Decimal("0.01")


def leg_unit_cost(
    ask: Decimal,
    fee_model: FeeModel,
    *,
    is_buy: bool = True,
) -> Decimal:
    """Cost per 1 contract, including per-unit fees.

    Uses qty=1 to get an average fee-per-unit at this price. Because our fee
    models are linear in qty, this is equivalent to (fees(N) / N) for any N > 0.
    """
    fee = fee_model.fee(price=ask, qty=Decimal("1"), is_buy=is_buy)
    return ask + fee


def net_edge_per_contract(unit_costs: Iterable[Decimal]) -> Decimal:
    """Guaranteed profit per contract after fees. Positive means an arb.

    Size-independent: this is the whole arb test. Exactly one leg of a
    complete ticket settles at 1, so the edge is 1 minus what the ticket costs.
    """
    return Decimal("1") - sum(unit_costs, Decimal("0"))


def detect_cross_venue_two_leg(
    event_group: EventGroup,
    quotes: dict[str, Quote],
    fees: FeeModelRegistry,
    *,
    max_ticket_stake: Decimal | None = None,
    tick_by_venue: dict[str, Decimal] | None = None,
) -> ArbOpportunity | None:
    """Detect the most profitable YES-leg + NO-leg cross-venue arb for a group.

    The arb test is per-contract and size-independent: if the all-in cost of
    one contract on each side is under 1, the pair is an arb. Sizing is then a
    separate step bounded by book depth and `max_ticket_stake`, and floored to
    `tick_by_venue`'s granularity for the legs' venues, or `DEFAULT_QTY_TICK`
    where that says nothing.
    """
    tick_by_venue = tick_by_venue or {}
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
        y_unit = leg_unit_cost(yq.ask, y_fee_model, is_buy=True)

        for n in no_legs:
            nq = quotes.get(n.outcome_id)
            if nq is None:
                continue
            n_fee_model = fees.get(n.venue_id)
            if n_fee_model is None:
                continue
            n_unit = leg_unit_cost(nq.ask, n_fee_model, is_buy=True)

            unit_cost = y_unit + n_unit
            # Per contract, and nothing to do with sizing.
            if net_edge_per_contract([y_unit, n_unit]) <= 0:
                continue

            # Per-venue override wins; otherwise floor to the observed
            # granularity so the ticket is an order size a venue would take.
            tick = max(
                tick_by_venue.get(y.venue_id, DEFAULT_QTY_TICK),
                tick_by_venue.get(n.venue_id, DEFAULT_QTY_TICK),
            )
            qty = tradeable_qty(
                unit_cost=unit_cost,
                depths=[yq.ask_size, nq.ask_size],
                max_stake=max_ticket_stake,
                tick=tick,
            )
            if qty <= 0:
                continue

            y_fee = y_fee_model.fee(price=yq.ask, qty=qty, is_buy=True)
            n_fee = n_fee_model.fee(price=nq.ask, qty=qty, is_buy=True)
            total_stake = yq.ask * qty + nq.ask * qty + y_fee + n_fee
            # Exactly one side settles at 1 per contract, so payoff is qty.
            profit = qty - total_stake
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
    *,
    max_ticket_stake: Decimal | None = None,
    tick_by_venue: dict[str, Decimal] | None = None,
) -> ArbOpportunity | None:
    """Single-venue multi-outcome arb: buy every outcome so exactly one pays 1.

    Every leg must be a mutually-exclusive outcome of the same event. Sizing
    is bounded by the thinnest leg's depth and by `max_ticket_stake`, same as
    the cross-venue detector.
    """
    tick_by_venue = tick_by_venue or {}
    if len(legs) < 2:
        return None

    unit_costs: list[Decimal] = []
    depths: list[Decimal | None] = []
    resolved: list[tuple[EventGroupLeg, Quote, FeeModel]] = []
    for leg in legs:
        q = quotes.get(leg.outcome_id)
        if q is None:
            return None
        fee_model = fees.get(leg.venue_id)
        if fee_model is None:
            return None
        unit_costs.append(leg_unit_cost(q.ask, fee_model, is_buy=True))
        depths.append(q.ask_size)
        resolved.append((leg, q, fee_model))

    if net_edge_per_contract(unit_costs) <= 0:
        return None

    # Per-venue override wins; otherwise floor to the observed granularity,
    # same fallback `detect_cross_venue_two_leg` uses.
    tick = max(
        (tick_by_venue.get(leg.venue_id, DEFAULT_QTY_TICK) for leg, _, _ in resolved),
        default=DEFAULT_QTY_TICK,
    )
    qty = tradeable_qty(
        unit_cost=sum(unit_costs, Decimal("0")),
        depths=depths,
        max_stake=max_ticket_stake,
        tick=tick,
    )
    if qty <= 0:
        return None

    total_stake = Decimal("0")
    arb_legs: list[ArbLeg] = []
    for leg, q, fee_model in resolved:
        fee = fee_model.fee(price=q.ask, qty=qty, is_buy=True)
        total_stake += q.ask * qty + fee
        arb_legs.append(
            ArbLeg(
                outcome_id=leg.outcome_id,
                venue_id=leg.venue_id,
                is_buy=True,
                price=q.ask,
                qty=qty,
                fee=fee,
            )
        )

    profit = qty - total_stake
    if profit <= 0:
        return None

    return ArbOpportunity(
        event_group_id=event_group_id,
        legs=tuple(arb_legs),
        total_stake=total_stake,
        guaranteed_profit=profit,
        guaranteed_profit_bps=(profit / total_stake) * Decimal(10_000),
    )
