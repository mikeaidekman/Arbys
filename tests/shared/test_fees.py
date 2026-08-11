from decimal import Decimal

from arbys.shared.fees import (
    KalshiFeeModel,
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
