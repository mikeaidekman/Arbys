"""Optimal stake sizing for a detected arbitrage.

For a two-leg cross-venue arb, the simple "buy N of each side" sizing already
locks in profit — payoff is N regardless of which side wins. This module adds:

  * `size_to_bankroll` — scale up an opportunity to consume as much of a
    per-venue bankroll as possible while respecting each venue's tick size and
    max-position caps.
  * `size_to_max_stake` — cap total capital deployed.

Everything here is pure and deterministic.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from .arb_engine import ArbLeg, ArbOpportunity
from .qty import LEGACY_UNBOUNDED_QTY, _round_down_tick, tradeable_qty  # noqa: F401


def size_to_bankroll(
    opp: ArbOpportunity,
    bankroll_by_venue: dict[str, Decimal],
    tick_by_venue: dict[str, Decimal] | None = None,
) -> ArbOpportunity | None:
    """Scale opp so no leg's cost (incl. fee) exceeds that venue's bankroll.

    The scaling factor is the min over legs of (bankroll_venue / leg_cost_venue),
    then rounded down to each venue's tick size. Returns None if any bankroll
    is insufficient to trade one tick.
    """
    tick_by_venue = tick_by_venue or {}
    if not opp.legs:
        return None

    # Per-venue total leg cost from the current opp (multiple legs on one venue
    # sum together).
    per_venue_cost: dict[str, Decimal] = {}
    for leg in opp.legs:
        cost = leg.price * leg.qty + leg.fee
        per_venue_cost[leg.venue_id] = per_venue_cost.get(leg.venue_id, Decimal("0")) + cost

    scale = None
    for venue_id, cost in per_venue_cost.items():
        bank = bankroll_by_venue.get(venue_id, Decimal("0"))
        if bank <= 0 or cost <= 0:
            return None
        s = bank / cost
        scale = s if scale is None else min(scale, s)
    if scale is None or scale <= 0:
        return None

    new_legs: list[ArbLeg] = []
    for leg in opp.legs:
        tick = tick_by_venue.get(leg.venue_id, Decimal("0"))
        new_qty = _round_down_tick(leg.qty * scale, tick) if tick > 0 else leg.qty * scale
        if new_qty <= 0:
            return None
        # Fee scales with qty for our linear fee models; rescale proportionally.
        new_fee = leg.fee * (new_qty / leg.qty) if leg.qty > 0 else Decimal("0")
        new_legs.append(replace(leg, qty=new_qty, fee=new_fee))

    total_stake = sum((leg.price * leg.qty + leg.fee for leg in new_legs), Decimal("0"))
    # Guaranteed payoff = qty on the side that wins. In a two-leg cross-venue
    # arb both sides have equal qty; use the minimum to be safe.
    guaranteed_payoff = min(leg.qty for leg in new_legs)
    profit = guaranteed_payoff - total_stake
    if profit <= 0:
        return None
    return ArbOpportunity(
        event_group_id=opp.event_group_id,
        legs=tuple(new_legs),
        total_stake=total_stake,
        guaranteed_profit=profit,
        guaranteed_profit_bps=(profit / total_stake) * Decimal(10_000),
    )


def size_to_max_stake(
    opp: ArbOpportunity,
    max_stake: Decimal,
    tick_by_venue: dict[str, Decimal] | None = None,
) -> ArbOpportunity | None:
    """Cap total capital deployed at `max_stake`."""
    if opp.total_stake <= 0 or max_stake <= 0:
        return None
    if opp.total_stake <= max_stake:
        return opp
    scale = max_stake / opp.total_stake
    per_venue_cost: dict[str, Decimal] = {}
    for leg in opp.legs:
        cost = leg.price * leg.qty + leg.fee
        per_venue_cost[leg.venue_id] = per_venue_cost.get(leg.venue_id, Decimal("0")) + cost
    bankroll_by_venue = {v: c * scale for v, c in per_venue_cost.items()}
    return size_to_bankroll(opp, bankroll_by_venue, tick_by_venue)
