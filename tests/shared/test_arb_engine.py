from decimal import Decimal

from arbys.shared.arb_engine import (
    detect_complementary_set,
    detect_cross_venue_two_leg,
)
from arbys.shared.fees import PolymarketFeeModel, SportsbookFeeModel, ZeroFeeModel
from arbys.shared.types import EventGroup, EventGroupLeg, Quote


def _q(oid: str, ask: str, bid: str | None = None) -> Quote:
    bid_d = Decimal(bid) if bid is not None else Decimal(ask)
    return Quote(outcome_id=oid, bid=bid_d, ask=Decimal(ask))


def _event_group(legs):
    return EventGroup(id="eg1", title="Will X happen?", legs=tuple(legs))


def test_cross_venue_no_arb_when_sum_gte_one():
    eg = _event_group(
        [
            EventGroupLeg(outcome_id="poly_yes", venue_id="poly", is_yes_side=True),
            EventGroupLeg(outcome_id="kals_no", venue_id="kals", is_yes_side=False),
        ]
    )
    quotes = {"poly_yes": _q("poly_yes", "0.55"), "kals_no": _q("kals_no", "0.50")}
    fees = {"poly": ZeroFeeModel("poly"), "kals": ZeroFeeModel("kals")}
    assert detect_cross_venue_two_leg(eg, quotes, fees) is None


def test_cross_venue_finds_arb_when_sum_lt_one():
    eg = _event_group(
        [
            EventGroupLeg(outcome_id="poly_yes", venue_id="poly", is_yes_side=True),
            EventGroupLeg(outcome_id="kals_no", venue_id="kals", is_yes_side=False),
        ]
    )
    # 0.45 + 0.50 = 0.95 -> 5c guaranteed profit per $1 payoff
    quotes = {"poly_yes": _q("poly_yes", "0.45"), "kals_no": _q("kals_no", "0.50")}
    fees = {"poly": ZeroFeeModel("poly"), "kals": ZeroFeeModel("kals")}
    opp = detect_cross_venue_two_leg(eg, quotes, fees)
    assert opp is not None
    assert opp.guaranteed_profit == Decimal("0.05")
    assert opp.total_stake == Decimal("0.95")
    assert len(opp.legs) == 2


def test_cross_venue_respects_fees_eating_edge():
    eg = _event_group(
        [
            EventGroupLeg(outcome_id="poly_yes", venue_id="poly", is_yes_side=True),
            EventGroupLeg(outcome_id="kals_no", venue_id="kals", is_yes_side=False),
        ]
    )
    # 0.48 + 0.50 = 0.98 -> 2c edge, but gas of 0.03 kills it.
    quotes = {"poly_yes": _q("poly_yes", "0.48"), "kals_no": _q("kals_no", "0.50")}
    fees = {
        "poly": PolymarketFeeModel(gas_flat=Decimal("0.03")),
        "kals": ZeroFeeModel("kals"),
    }
    assert detect_cross_venue_two_leg(eg, quotes, fees) is None


def test_cross_venue_picks_best_pair_across_multiple_legs():
    eg = _event_group(
        [
            EventGroupLeg(outcome_id="poly_yes", venue_id="poly", is_yes_side=True),
            EventGroupLeg(outcome_id="dks_yes", venue_id="dks", is_yes_side=True),
            EventGroupLeg(outcome_id="kals_no", venue_id="kals", is_yes_side=False),
        ]
    )
    quotes = {
        "poly_yes": _q("poly_yes", "0.48"),  # 0.48 + 0.50 = 0.98 -> 2c
        "dks_yes": _q("dks_yes", "0.40"),    # 0.40 + 0.50 = 0.90 -> 10c
        "kals_no": _q("kals_no", "0.50"),
    }
    fees = {
        "poly": ZeroFeeModel("poly"),
        "kals": ZeroFeeModel("kals"),
        "dks": SportsbookFeeModel("dks"),
    }
    opp = detect_cross_venue_two_leg(eg, quotes, fees)
    assert opp is not None
    assert opp.guaranteed_profit == Decimal("0.10")
    venue_ids = {leg.venue_id for leg in opp.legs}
    assert venue_ids == {"dks", "kals"}


def test_cross_venue_returns_none_if_missing_quote():
    eg = _event_group(
        [
            EventGroupLeg(outcome_id="poly_yes", venue_id="poly", is_yes_side=True),
            EventGroupLeg(outcome_id="kals_no", venue_id="kals", is_yes_side=False),
        ]
    )
    quotes = {"poly_yes": _q("poly_yes", "0.40")}
    fees = {"poly": ZeroFeeModel("poly"), "kals": ZeroFeeModel("kals")}
    assert detect_cross_venue_two_leg(eg, quotes, fees) is None


def test_cross_venue_returns_none_if_no_no_side_leg():
    eg = _event_group(
        [EventGroupLeg(outcome_id="a", venue_id="v", is_yes_side=True)]
    )
    assert detect_cross_venue_two_leg(eg, {}, {}) is None


def test_complementary_set_arb():
    legs = [
        EventGroupLeg(outcome_id="a", venue_id="poly", is_yes_side=True),
        EventGroupLeg(outcome_id="b", venue_id="poly", is_yes_side=True),
        EventGroupLeg(outcome_id="c", venue_id="poly", is_yes_side=True),
    ]
    quotes = {
        "a": _q("a", "0.30"),
        "b": _q("b", "0.30"),
        "c": _q("c", "0.30"),
    }
    fees = {"poly": ZeroFeeModel("poly")}
    opp = detect_complementary_set("egc", legs, quotes, fees)
    assert opp is not None
    assert opp.total_stake == Decimal("0.90")
    assert opp.guaranteed_profit == Decimal("0.10")


def test_complementary_set_no_arb_when_sum_exceeds_one():
    legs = [
        EventGroupLeg(outcome_id="a", venue_id="poly", is_yes_side=True),
        EventGroupLeg(outcome_id="b", venue_id="poly", is_yes_side=True),
    ]
    quotes = {"a": _q("a", "0.55"), "b": _q("b", "0.50")}
    fees = {"poly": ZeroFeeModel("poly")}
    assert detect_complementary_set("egc", legs, quotes, fees) is None


def test_complementary_set_requires_at_least_two_legs():
    legs = [EventGroupLeg(outcome_id="a", venue_id="poly", is_yes_side=True)]
    quotes = {"a": _q("a", "0.10")}
    fees = {"poly": ZeroFeeModel("poly")}
    assert detect_complementary_set("egc", legs, quotes, fees) is None
