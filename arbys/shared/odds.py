"""Odds conversion helpers for sportsbook integration.

The arb engine only ever sees probabilities in [0, 1], so sportsbook adapters
must convert their native odds format before publishing a quote.
"""

from __future__ import annotations

from decimal import Decimal


def american_to_decimal(american: int) -> Decimal:
    if american == 0:
        raise ValueError("American odds cannot be zero")
    if american > 0:
        return Decimal(american) / Decimal(100) + Decimal(1)
    return Decimal(100) / Decimal(-american) + Decimal(1)


def decimal_to_implied_prob(decimal_odds: Decimal) -> Decimal:
    if decimal_odds <= 1:
        raise ValueError(f"Decimal odds must be > 1, got {decimal_odds}")
    return Decimal(1) / decimal_odds


def american_to_implied_prob(american: int) -> Decimal:
    return decimal_to_implied_prob(american_to_decimal(american))


def devig_two_way(p_a: Decimal, p_b: Decimal) -> tuple[Decimal, Decimal]:
    """Normalize a two-outcome market's implied probs to sum to 1.

    Uses proportional (multiplicative) de-vig. Returned pair is the fair
    probability for A and B respectively.
    """
    total = p_a + p_b
    if total <= 0:
        raise ValueError("Cannot de-vig non-positive total")
    return p_a / total, p_b / total
