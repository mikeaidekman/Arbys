from decimal import Decimal

from arbys.shared.arb_engine import leg_unit_cost, net_edge_per_contract
from arbys.shared.fees import (
    KalshiFeeModel,
    NovigFeeModel,
    PolymarketUsFeeModel,
    SportsbookFeeModel,
    ZeroFeeModel,
)


def test_zero_fee():
    m = ZeroFeeModel(venue_id="x")
    assert m.fee(price=Decimal("0.5"), qty=Decimal("100"), is_buy=True) == 0


def test_kalshi_fee_scales_with_qty_and_pq():
    m = KalshiFeeModel()
    f = m.fee(price=Decimal("0.5"), qty=Decimal("100"), is_buy=True)
    # 0.07 * 0.5 * 0.5 * 100 = 1.75
    assert f == Decimal("1.75")


def test_kalshi_fee_zero_at_edges():
    m = KalshiFeeModel()
    assert m.fee(price=Decimal("0"), qty=Decimal("100"), is_buy=True) == 0
    assert m.fee(price=Decimal("1"), qty=Decimal("100"), is_buy=True) == 0


def test_polymarket_us_fee_peaks_at_a_coin_flip():
    """Official schedule: fee = 0.06 * C * p * (1-p).

    Max at p=0.50 -> 0.06 * 0.25 = 0.015/contract = $1.50 per 100.
    """
    m = PolymarketUsFeeModel()
    assert m.fee(price=Decimal("0.50"), qty=Decimal("100"), is_buy=True) == Decimal("1.5000")


def test_polymarket_us_fee_vanishes_at_the_extremes():
    m = PolymarketUsFeeModel()
    assert m.fee(price=Decimal("0"), qty=Decimal("100"), is_buy=True) == 0
    assert m.fee(price=Decimal("1"), qty=Decimal("100"), is_buy=True) == 0


def test_polymarket_us_fee_is_cheaper_than_kalshi_at_the_same_price():
    """0.06 vs Kalshi's 0.07 — same shape, lower coefficient."""
    price, qty = Decimal("0.45"), Decimal("100")
    poly = PolymarketUsFeeModel().fee(price=price, qty=qty, is_buy=True)
    kalshi = KalshiFeeModel().fee(price=price, qty=qty, is_buy=True)
    assert poly < kalshi


def test_polymarket_us_fee_is_zero_for_nonpositive_qty():
    m = PolymarketUsFeeModel()
    assert m.fee(price=Decimal("0.5"), qty=Decimal("0"), is_buy=True) == 0
    assert m.fee(price=Decimal("0.5"), qty=Decimal("-5"), is_buy=True) == 0


def test_polymarket_us_venue_id():
    assert PolymarketUsFeeModel().venue_id == "polymarket_us"


def test_sportsbook_fee_is_zero_since_vig_in_price():
    m = SportsbookFeeModel(venue_id="dks")
    assert m.fee(price=Decimal("0.55"), qty=Decimal("100"), is_buy=True) == 0


def test_leg_unit_cost_includes_per_unit_fee():
    # Kalshi taker fee is 0.07 * p * (1-p): 0.07 * 0.47 * 0.53 = 0.017437
    cost = leg_unit_cost(Decimal("0.47"), KalshiFeeModel(), is_buy=True)
    assert cost > Decimal("0.47")
    assert cost == Decimal("0.47") + KalshiFeeModel().fee(
        price=Decimal("0.47"), qty=Decimal("1"), is_buy=True
    )


def test_net_edge_per_contract_is_one_minus_total_cost():
    k = leg_unit_cost(Decimal("0.47"), KalshiFeeModel())
    p = leg_unit_cost(Decimal("0.525"), PolymarketUsFeeModel())
    edge = net_edge_per_contract([k, p])
    assert edge == Decimal("1") - (k + p)
    # Measured 2026-08-22 on nfl-GB-MIN: gross +0.5c, net negative.
    assert edge < Decimal("0")


def test_net_edge_positive_when_legs_are_cheap():
    edge = net_edge_per_contract([Decimal("0.40"), Decimal("0.50")])
    assert edge == Decimal("0.10")


def test_novig_fee_peaks_at_a_coin_flip():
    """Novig taker fee = 0.03 * C * p * (1-p) — the same shape as Kalshi and
    Polymarket US at half the coefficient.

    Max at p=0.50 -> 0.03 * 0.25 = 0.0075/contract = $0.75 per 100, which is
    the "capped at $0.0075 per contract" figure the venue advertises.
    """
    m = NovigFeeModel()
    assert m.fee(price=Decimal("0.50"), qty=Decimal("100"), is_buy=True) == Decimal("0.75")


def test_novig_fee_vanishes_at_the_extremes():
    m = NovigFeeModel()
    assert m.fee(price=Decimal("0"), qty=Decimal("100"), is_buy=True) == 0
    assert m.fee(price=Decimal("1"), qty=Decimal("100"), is_buy=True) == 0


def test_novig_fee_is_zero_for_nonpositive_qty():
    m = NovigFeeModel()
    assert m.fee(price=Decimal("0.5"), qty=Decimal("0"), is_buy=True) == 0
    assert m.fee(price=Decimal("0.5"), qty=Decimal("-5"), is_buy=True) == 0


def test_novig_venue_id():
    assert NovigFeeModel().venue_id == "novig"


def test_novig_is_the_cheapest_of_the_three_at_every_price():
    """0.03 < 0.06 < 0.07, same shape — so Novig is strictly cheaper all-in at
    an identical ask.

    This is the tie-break dynamic from CLAUDE.md pointed the other way: 42.7% of
    head-to-head ask comparisons between venues are exact price ties and are
    decided purely by the fee coefficient. Polymarket US wins every one of those
    against Kalshi today; a Novig leg would win every one against both.
    """
    qty = Decimal("100")
    for price in (Decimal("0.05"), Decimal("0.25"), Decimal("0.50"), Decimal("0.75")):
        novig = NovigFeeModel().fee(price=price, qty=qty, is_buy=True)
        poly = PolymarketUsFeeModel().fee(price=price, qty=qty, is_buy=True)
        kalshi = KalshiFeeModel().fee(price=price, qty=qty, is_buy=True)
        assert novig < poly < kalshi


def test_a_novig_leg_brings_two_leg_drag_under_the_measured_divergence():
    """The whole reason this venue is interesting.

    Measured 2026-08-11, gross divergence between Kalshi and Polymarket US
    topped out at 2.75c/contract across 34 matched sides. At a coin flip the
    existing pair drags 3.25c — *more* than the widest disagreement ever
    observed — which is why 12 groups were gross-positive on 2026-08-22 and 0
    were net-positive. Substituting a Novig leg drags 2.25c, under the ceiling,
    so a 50/50 market becomes net-positive-capable at all.

    Fees are the only term here that we can move; divergence is the venues'
    to decide.
    """
    coin_flip, one = Decimal("0.50"), Decimal("1")
    novig = NovigFeeModel().fee(price=coin_flip, qty=one, is_buy=True)
    poly = PolymarketUsFeeModel().fee(price=coin_flip, qty=one, is_buy=True)
    kalshi = KalshiFeeModel().fee(price=coin_flip, qty=one, is_buy=True)

    max_observed_divergence = Decimal("0.0275")

    assert kalshi + poly > max_observed_divergence
    assert novig + poly < max_observed_divergence
    assert novig + kalshi < max_observed_divergence
