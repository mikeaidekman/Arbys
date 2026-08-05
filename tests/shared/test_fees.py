from decimal import Decimal

from arbys.shared.fees import (
    KalshiFeeModel,
    PolymarketFeeModel,
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


def test_polymarket_fee_is_flat_gas_per_trade():
    m = PolymarketFeeModel(gas_flat=Decimal("0.10"))
    assert m.fee(price=Decimal("0.5"), qty=Decimal("100"), is_buy=True) == Decimal("0.10")
    assert m.fee(price=Decimal("0.9"), qty=Decimal("1"), is_buy=False) == Decimal("0.10")


def test_polymarket_fee_zero_qty():
    m = PolymarketFeeModel(gas_flat=Decimal("0.10"))
    assert m.fee(price=Decimal("0.5"), qty=Decimal("0"), is_buy=True) == 0


def test_sportsbook_fee_is_zero_since_vig_in_price():
    m = SportsbookFeeModel(venue_id="dks")
    assert m.fee(price=Decimal("0.55"), qty=Decimal("100"), is_buy=True) == 0
