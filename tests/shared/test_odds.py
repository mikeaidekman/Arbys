from decimal import Decimal

import pytest

from arbys.shared.odds import (
    american_to_decimal,
    american_to_implied_prob,
    decimal_to_implied_prob,
    devig_two_way,
)


def test_american_to_decimal_positive():
    assert american_to_decimal(150) == Decimal("2.5")


def test_american_to_decimal_negative():
    assert american_to_decimal(-200) == Decimal("1.5")


def test_american_to_decimal_zero_raises():
    with pytest.raises(ValueError):
        american_to_decimal(0)


def test_decimal_to_implied_prob():
    assert decimal_to_implied_prob(Decimal("2")) == Decimal("0.5")


def test_decimal_to_implied_prob_invalid():
    with pytest.raises(ValueError):
        decimal_to_implied_prob(Decimal("1"))


def test_american_to_implied_prob_favorite():
    p = american_to_implied_prob(-200)
    assert p == Decimal("2") / Decimal("3")


def test_devig_two_way_sums_to_one():
    a, b = devig_two_way(Decimal("0.55"), Decimal("0.50"))
    assert a + b == Decimal("1")
    assert a > b


def test_devig_two_way_invalid():
    with pytest.raises(ValueError):
        devig_two_way(Decimal("0"), Decimal("0"))
