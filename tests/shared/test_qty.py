from decimal import Decimal

from arbys.shared.qty import LEGACY_UNBOUNDED_QTY, tradeable_qty


def test_tradeable_qty_capped_by_thinnest_leg():
    # $200 budget would allow ~200 contracts, but one leg has only 3 resting.
    qty = tradeable_qty(
        unit_cost=Decimal("0.99"),
        depths=[Decimal("412"), Decimal("3")],
        max_stake=Decimal("200"),
    )
    assert qty == Decimal("3")


def test_tradeable_qty_capped_by_stake_budget():
    qty = tradeable_qty(
        unit_cost=Decimal("1.00"),
        depths=[Decimal("5000"), Decimal("5000")],
        max_stake=Decimal("200"),
    )
    assert qty == Decimal("200")


def test_known_empty_leg_blocks_entirely():
    # 0 is *known empty*, not unknown. Nothing is tradeable at any budget.
    qty = tradeable_qty(
        unit_cost=Decimal("0.98"),
        depths=[Decimal("1000"), Decimal("0")],
        max_stake=Decimal("200"),
    )
    assert qty == Decimal("0")


def test_unknown_depth_imposes_no_ceiling():
    # None = the venue did not report depth. POST /quotes omits sizes entirely,
    # so treating None as 0 would silence every hand-pushed quote.
    qty = tradeable_qty(
        unit_cost=Decimal("1.00"),
        depths=[None, None],
        max_stake=Decimal("200"),
    )
    assert qty == Decimal("200")


def test_mixed_known_and_unknown_uses_the_known_one():
    qty = tradeable_qty(
        unit_cost=Decimal("0.50"),
        depths=[None, Decimal("17")],
        max_stake=Decimal("200"),
    )
    assert qty == Decimal("17")


def test_disabled_budget_and_unknown_depth_falls_back_to_legacy():
    # ARBYS_MAX_TICKET_STAKE=0 disables the budget cap. With no depth known
    # either, there is no ceiling at all, so reproduce today's flat sizing
    # rather than emitting an unbounded ticket.
    qty = tradeable_qty(
        unit_cost=Decimal("0.98"),
        depths=[None, None],
        max_stake=None,
    )
    assert qty == LEGACY_UNBOUNDED_QTY == Decimal("100")


def test_tick_floors_the_result():
    qty = tradeable_qty(
        unit_cost=Decimal("1.00"),
        depths=[Decimal("7.6"), None],
        max_stake=Decimal("200"),
        tick=Decimal("1"),
    )
    assert qty == Decimal("7")


def test_zero_unit_cost_does_not_divide_by_zero():
    qty = tradeable_qty(
        unit_cost=Decimal("0"),
        depths=[Decimal("42")],
        max_stake=Decimal("200"),
    )
    assert qty == Decimal("42")
