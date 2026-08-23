from dataclasses import dataclass
from decimal import Decimal

from arbys.shared.arb_engine import (
    DEFAULT_QTY_TICK,
    detect_complementary_set,
    detect_cross_venue_two_leg,
)
from arbys.shared.fees import FeeModelRegistry, SportsbookFeeModel, ZeroFeeModel
from arbys.shared.types import EventGroup, EventGroupLeg, Quote


@dataclass(frozen=True)
class _FlatFeeModel:
    """Test-only: a fixed cost per trade, regardless of price or size.

    These are engine tests — they need *some* fee that eats a known edge, not
    any particular venue's schedule. This used to borrow PolymarketFeeModel's
    ``gas_flat``, which went away when Polymarket US's real percentage fee
    replaced the old zero-fee model. Keeping a local stub means the engine
    tests no longer move when a venue's fee schedule changes.
    """

    venue_id: str
    amount: Decimal

    def fee(self, *, price: Decimal, qty: Decimal, is_buy: bool) -> Decimal:
        return Decimal("0") if qty <= 0 else self.amount


def _q(
    oid: str, ask: str, bid: str | None = None, ask_size: str | None = None
) -> Quote:
    bid_d = Decimal(bid) if bid is not None else Decimal(ask)
    return Quote(
        outcome_id=oid,
        bid=bid_d,
        ask=Decimal(ask),
        ask_size=Decimal(ask_size) if ask_size is not None else None,
    )


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
    # 0.45 + 0.50 = 0.95 -> 5c guaranteed profit per contract. Depth is stated
    # explicitly so the ticket size is the book's, not a fallback constant's.
    quotes = {
        "poly_yes": _q("poly_yes", "0.45", ask_size="10"),
        "kals_no": _q("kals_no", "0.50", ask_size="10"),
    }
    fees = {"poly": ZeroFeeModel("poly"), "kals": ZeroFeeModel("kals")}
    opp = detect_cross_venue_two_leg(eg, quotes, fees)
    assert opp is not None
    assert all(leg.qty == Decimal("10") for leg in opp.legs)
    assert opp.guaranteed_profit == Decimal("0.50")  # 10 contracts * 5c
    assert opp.total_stake == Decimal("9.50")
    assert len(opp.legs) == 2


def test_cross_venue_respects_fees_eating_edge():
    eg = _event_group(
        [
            EventGroupLeg(outcome_id="poly_yes", venue_id="poly", is_yes_side=True),
            EventGroupLeg(outcome_id="kals_no", venue_id="kals", is_yes_side=False),
        ]
    )
    # 0.48 + 0.50 = 0.98 -> 2c edge, but a flat cost of 0.03 kills it.
    quotes = {"poly_yes": _q("poly_yes", "0.48"), "kals_no": _q("kals_no", "0.50")}
    fees = {
        "poly": _FlatFeeModel("poly", Decimal("0.03")),
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
    # Equal depth on every leg, so the winning pair is chosen on edge rather
    # than on which book happens to be deeper.
    quotes = {
        # 0.48 + 0.50 = 0.98 -> 2c/contract
        "poly_yes": _q("poly_yes", "0.48", ask_size="10"),
        # 0.40 + 0.50 = 0.90 -> 10c/contract
        "dks_yes": _q("dks_yes", "0.40", ask_size="10"),
        "kals_no": _q("kals_no", "0.50", ask_size="10"),
    }
    fees = {
        "poly": ZeroFeeModel("poly"),
        "kals": ZeroFeeModel("kals"),
        "dks": SportsbookFeeModel("dks"),
    }
    opp = detect_cross_venue_two_leg(eg, quotes, fees)
    assert opp is not None
    assert all(leg.qty == Decimal("10") for leg in opp.legs)
    assert opp.guaranteed_profit == Decimal("1.00")  # 10 contracts * 10c
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
    # Sizing is now depth-derived rather than a flat payoff, so each quote
    # states an explicit ask_size (5) to keep the resulting qty deterministic.
    legs = [
        EventGroupLeg(outcome_id="a", venue_id="poly", is_yes_side=True),
        EventGroupLeg(outcome_id="b", venue_id="poly", is_yes_side=True),
        EventGroupLeg(outcome_id="c", venue_id="poly", is_yes_side=True),
    ]
    quotes = {
        "a": _q("a", "0.30", ask_size="5"),
        "b": _q("b", "0.30", ask_size="5"),
        "c": _q("c", "0.30", ask_size="5"),
    }
    fees = {"poly": ZeroFeeModel("poly")}
    opp = detect_complementary_set("egc", legs, quotes, fees)
    assert opp is not None
    assert all(leg.qty == Decimal("5") for leg in opp.legs)
    assert opp.total_stake == Decimal("4.50")  # 5 contracts * 3 legs * 0.30
    assert opp.guaranteed_profit == Decimal("0.50")  # 5 contracts * 10c edge


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


# --- depth-aware sizing -------------------------------------------------


def _two_venue_group() -> EventGroup:
    return _event_group(
        [
            EventGroupLeg(outcome_id="y", venue_id="v1", is_yes_side=True),
            EventGroupLeg(outcome_id="n", venue_id="v2", is_yes_side=False),
        ]
    )


def _fees() -> FeeModelRegistry:
    # FeeModelRegistry is just dict[str, FeeModel], and every fee model is a
    # frozen dataclass whose first field is venue_id. There is no register().
    return {"v1": ZeroFeeModel("v1"), "v2": ZeroFeeModel("v2")}


def test_sizes_to_the_thinnest_leg():
    quotes = {
        "y": Quote(outcome_id="y", bid=Decimal("0.40"), ask=Decimal("0.45"),
                   ask_size=Decimal("1000")),
        "n": Quote(outcome_id="n", bid=Decimal("0.45"), ask=Decimal("0.50"),
                   ask_size=Decimal("7")),
    }
    opp = detect_cross_venue_two_leg(
        _two_venue_group(), quotes, _fees(), max_ticket_stake=Decimal("200")
    )
    assert opp is not None
    assert all(leg.qty == Decimal("7") for leg in opp.legs)
    # 7 contracts * 0.05 edge
    assert opp.guaranteed_profit == Decimal("0.35")


def test_stake_budget_caps_a_deep_book():
    quotes = {
        "y": Quote(outcome_id="y", bid=Decimal("0.40"), ask=Decimal("0.45"),
                   ask_size=Decimal("100000")),
        "n": Quote(outcome_id="n", bid=Decimal("0.45"), ask=Decimal("0.50"),
                   ask_size=Decimal("100000")),
    }
    opp = detect_cross_venue_two_leg(
        _two_venue_group(), quotes, _fees(), max_ticket_stake=Decimal("95")
    )
    assert opp is not None
    # unit cost 0.95, budget 95 -> 100 contracts
    assert all(leg.qty == Decimal("100") for leg in opp.legs)


def test_known_empty_leg_yields_no_opportunity():
    quotes = {
        "y": Quote(outcome_id="y", bid=Decimal("0.40"), ask=Decimal("0.45"),
                   ask_size=Decimal("1000")),
        "n": Quote(outcome_id="n", bid=Decimal("0.45"), ask=Decimal("0.50"),
                   ask_size=Decimal("0")),
    }
    assert detect_cross_venue_two_leg(
        _two_venue_group(), quotes, _fees(), max_ticket_stake=Decimal("200")
    ) is None


def test_unknown_depth_still_produces_an_opportunity():
    # Hand-pushed quotes via POST /quotes carry no sizes at all.
    quotes = {
        "y": Quote(outcome_id="y", bid=Decimal("0.40"), ask=Decimal("0.45")),
        "n": Quote(outcome_id="n", bid=Decimal("0.45"), ask=Decimal("0.50")),
    }
    opp = detect_cross_venue_two_leg(
        _two_venue_group(), quotes, _fees(), max_ticket_stake=Decimal("200")
    )
    assert opp is not None
    assert all(leg.qty > 0 for leg in opp.legs)


def test_no_edge_is_rejected_regardless_of_size():
    # The gate is per-contract and size-independent: 0.55 + 0.50 > 1.
    quotes = {
        "y": Quote(outcome_id="y", bid=Decimal("0.50"), ask=Decimal("0.55"),
                   ask_size=Decimal("1000")),
        "n": Quote(outcome_id="n", bid=Decimal("0.45"), ask=Decimal("0.50"),
                   ask_size=Decimal("1000")),
    }
    assert detect_cross_venue_two_leg(
        _two_venue_group(), quotes, _fees(), max_ticket_stake=Decimal("200")
    ) is None


def test_budget_sized_qty_is_floored_to_the_default_tick():
    """A division must not publish a size no venue would accept.

    Both venues quote resting size to two decimals, so 0.01 is the floor
    granularity; it also keeps the value inside the qty column's 12 decimal
    places, so a stored ticket reloads as the same number.
    """
    quotes = {
        "y": Quote(outcome_id="y", bid=Decimal("0.40"), ask=Decimal("0.45")),
        "n": Quote(outcome_id="n", bid=Decimal("0.45"), ask=Decimal("0.50")),
    }
    opp = detect_cross_venue_two_leg(
        _two_venue_group(), quotes, _fees(), max_ticket_stake=Decimal("200")
    )
    assert opp is not None
    # 200 / 0.95 = 210.526315789..., floored to the 0.01 tick.
    assert all(leg.qty == Decimal("210.52") for leg in opp.legs)
    assert all(
        leg.qty == leg.qty.quantize(DEFAULT_QTY_TICK) for leg in opp.legs
    )


def test_tick_by_venue_overrides_the_default_tick():
    quotes = {
        "y": Quote(outcome_id="y", bid=Decimal("0.40"), ask=Decimal("0.45")),
        "n": Quote(outcome_id="n", bid=Decimal("0.45"), ask=Decimal("0.50")),
    }
    opp = detect_cross_venue_two_leg(
        _two_venue_group(),
        quotes,
        _fees(),
        max_ticket_stake=Decimal("200"),
        tick_by_venue={"v2": Decimal("1")},
    )
    assert opp is not None
    # The coarser of the pair's ticks wins: whole contracts, not 210.52.
    assert all(leg.qty == Decimal("210") for leg in opp.legs)


def test_complementary_set_sizes_to_thinnest_leg():
    legs = [
        EventGroupLeg(outcome_id="a", venue_id="v1", is_yes_side=True),
        EventGroupLeg(outcome_id="b", venue_id="v1", is_yes_side=False),
    ]
    quotes = {
        "a": Quote(outcome_id="a", bid=Decimal("0.40"), ask=Decimal("0.45"),
                   ask_size=Decimal("900")),
        "b": Quote(outcome_id="b", bid=Decimal("0.45"), ask=Decimal("0.50"),
                   ask_size=Decimal("11")),
    }
    opp = detect_complementary_set(
        "eg:v1", legs, quotes, _fees(), max_ticket_stake=Decimal("200")
    )
    assert opp is not None
    assert all(leg.qty == Decimal("11") for leg in opp.legs)


def test_complementary_set_blocked_by_known_empty_leg():
    legs = [
        EventGroupLeg(outcome_id="a", venue_id="v1", is_yes_side=True),
        EventGroupLeg(outcome_id="b", venue_id="v1", is_yes_side=False),
    ]
    quotes = {
        "a": Quote(outcome_id="a", bid=Decimal("0.40"), ask=Decimal("0.45"),
                   ask_size=Decimal("900")),
        "b": Quote(outcome_id="b", bid=Decimal("0.45"), ask=Decimal("0.50"),
                   ask_size=Decimal("0")),
    }
    assert detect_complementary_set(
        "eg:v1", legs, quotes, _fees(), max_ticket_stake=Decimal("200")
    ) is None
