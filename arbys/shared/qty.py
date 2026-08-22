"""Contract-count arithmetic: how much of an arb is actually tradeable.

A leaf module on purpose. `sizing.py` imports ArbOpportunity from
`arb_engine`, and `arb_engine` needs `tradeable_qty`, so this cannot live in
`sizing.py` without a cycle.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_DOWN, Decimal


def _round_down_tick(value: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return value
    return (value / tick).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick


# Retained only for the case where the stake budget is disabled *and* no leg
# reports depth. Without it there would be no ceiling at all. This is the old
# DEFAULT_TARGET_PAYOFF, so disabling the budget reproduces prior behaviour.
LEGACY_UNBOUNDED_QTY = Decimal("100")


def tradeable_qty(
    *,
    unit_cost: Decimal,
    depths: Sequence[Decimal | None],
    max_stake: Decimal | None,
    tick: Decimal = Decimal("0"),
) -> Decimal:
    """Contracts actually tradeable at the quoted prices.

    `unit_cost` is the all-in cost of one contract across every leg (asks plus
    per-unit fees). `depths` is each leg's resting size at its quoted price,
    under the project's three-state rule. `max_stake` caps total capital
    deployed; None disables that cap.

    The order of the depth checks is the whole point:

      * an explicit 0 on any leg means *known empty* -> nothing is tradeable,
      * None means *unknown* -> that leg imposes no ceiling.

    Treating None as 0 would silence every opportunity built from POST /quotes,
    which omits sizes entirely. Treating 0 as unknown would size tickets
    against an empty book.
    """
    if any(d is not None and d <= 0 for d in depths):
        return Decimal("0")

    known = [d for d in depths if d is not None]
    qty: Decimal | None = min(known) if known else None

    if max_stake is not None and max_stake > 0 and unit_cost > 0:
        budget_qty = max_stake / unit_cost
        qty = budget_qty if qty is None else min(qty, budget_qty)

    if qty is None:
        qty = LEGACY_UNBOUNDED_QTY

    if tick > 0:
        qty = _round_down_tick(qty, tick)
    return qty if qty > 0 else Decimal("0")
