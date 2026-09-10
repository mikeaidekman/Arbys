"""Per-venue fee models.

A fee model is a pure function: given a trade (outcome, side, quantity, price),
return the fee charged in the *same currency* as `quantity * price`.

Keeping this as a small protocol lets the arb engine, the paper broker, and the
backtester share exactly one source of truth for fee assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


class FeeModel(Protocol):
    venue_id: str

    def fee(self, *, price: Decimal, qty: Decimal, is_buy: bool) -> Decimal:
        """Return the fee (in quote currency units) for a fill of `qty` at `price`."""
        ...


@dataclass(frozen=True)
class ZeroFeeModel:
    """Useful for tests and for venues where fees are already priced into odds."""

    venue_id: str

    def fee(self, *, price: Decimal, qty: Decimal, is_buy: bool) -> Decimal:
        return Decimal("0")


@dataclass(frozen=True)
class KalshiFeeModel:
    """Approximation of Kalshi's fee schedule.

    Kalshi charges a per-contract fee of roughly 7% * price * (1 - price) * qty
    (rounded up to the cent per contract in practice; we return the exact value
    and let the paper broker handle rounding to venue tick).
    """

    venue_id: str = "kalshi"
    rate: Decimal = Decimal("0.07")

    def fee(self, *, price: Decimal, qty: Decimal, is_buy: bool) -> Decimal:
        if qty <= 0:
            return Decimal("0")
        return self.rate * price * (Decimal("1") - price) * qty


@dataclass(frozen=True)
class PolymarketUsFeeModel:
    """Polymarket US taker fee.

    Official schedule: ``fee = 0.06 * C * p * (1 - p)``, the same shape as
    Kalshi's with a lower coefficient. Peaks at a coin flip ($1.50 per 100
    contracts at p=0.50) and vanishes at the extremes.

    This replaces a model that returned zero, which overstated every net edge
    on a Polymarket leg by up to 1.25c/contract — larger than most edges the
    scanner detects.

    Two deliberate omissions:

    * The maker rebate (-0.0125) is not modelled. The paper broker fills
      against the ask as a taker, so a maker rebate would never apply.
    * Polymarket US rounds to the cent per contract using banker's rounding
      and we do not round at all, so modelled fees come out slightly low and
      marginal edges look slightly better than they are. This matches the
      existing understatement on the Kalshi side.
    """

    venue_id: str = "polymarket_us"
    rate: Decimal = Decimal("0.06")

    def fee(self, *, price: Decimal, qty: Decimal, is_buy: bool) -> Decimal:
        if qty <= 0:
            return Decimal("0")
        return self.rate * price * (Decimal("1") - price) * qty


@dataclass(frozen=True)
class NovigFeeModel:
    """Novig (Ludlow Exchange) taker fee.

    ``fee = 0.03 * C * p * (1 - p)`` — the same shape as Kalshi's and
    Polymarket US's, at half the coefficient. Peaks at a coin flip ($0.75 per
    100 contracts at p=0.50), which is the "capped at $0.0075 per contract"
    figure the venue advertises, and vanishes at the extremes.

    Why the coefficient matters more than it looks: fee drag is roughly
    constant while divergence varies, and at even money the existing
    Kalshi + Polymarket US pair drags 3.25c/contract against a maximum
    observed divergence of 2.75c — which is why 12 groups were gross-positive
    on 2026-08-22 and 0 were net-positive. Substituting a Novig leg drags
    2.25c, under that ceiling, so a 50/50 market becomes net-positive-capable
    at all. Fees are the only term in that comparison we can move.

    Provenance: the venue's published taker schedule as read on 2026-09-09.
    **Not yet confirmed against the Ludlow Exchange DCM rulebook**, and the
    figure may describe the pre-designation app rather than the exchange.
    Confirm before this model gates a live ticket.

    Two deliberate omissions, matching how the other venues are treated:

    * The maker credit is not modelled. Novig pays makers rather than charging
      them, but the paper broker fills against the ask as a taker, so it would
      never apply — the same reasoning that omits Polymarket US's -0.0125
      maker rebate.
    * No rounding, so modelled fees come out slightly low, on the same side as
      the existing Kalshi and Polymarket US understatement.

    Deliberately **not** registered in ``AppState.fees``: there is no Novig
    adapter yet, and a fee model for a venue that carries no legs would seed a
    paper broker and a starting balance for a venue that can never trade —
    exactly the DraftKings defect migration 0010 had to undo.
    """

    venue_id: str = "novig"
    rate: Decimal = Decimal("0.03")

    def fee(self, *, price: Decimal, qty: Decimal, is_buy: bool) -> Decimal:
        if qty <= 0:
            return Decimal("0")
        return self.rate * price * (Decimal("1") - price) * qty


@dataclass(frozen=True)
class SportsbookFeeModel:
    """Sportsbook 'fee' is the vig already embedded in the odds. If callers pass
    the *raw* implied probability from the offered odds (i.e. without de-vigging)
    then the fee model returns 0 — the vig is already priced in.
    """

    venue_id: str
    def fee(self, *, price: Decimal, qty: Decimal, is_buy: bool) -> Decimal:
        return Decimal("0")


FeeModelRegistry = dict[str, FeeModel]
