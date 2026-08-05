from decimal import Decimal

from arbys.shared.arb_engine import ArbLeg, ArbOpportunity
from arbys.shared.sizing import size_to_bankroll, size_to_max_stake


def _mk_opp(y_price="0.45", n_price="0.50", qty="1") -> ArbOpportunity:
    y_qty = Decimal(qty)
    n_qty = Decimal(qty)
    y_cost = Decimal(y_price) * y_qty
    n_cost = Decimal(n_price) * n_qty
    total = y_cost + n_cost
    profit = y_qty - total
    return ArbOpportunity(
        event_group_id="eg",
        legs=(
            ArbLeg(
                outcome_id="y",
                venue_id="poly",
                is_buy=True,
                price=Decimal(y_price),
                qty=y_qty,
                fee=Decimal("0"),
            ),
            ArbLeg(
                outcome_id="n",
                venue_id="kals",
                is_buy=True,
                price=Decimal(n_price),
                qty=n_qty,
                fee=Decimal("0"),
            ),
        ),
        total_stake=total,
        guaranteed_profit=profit,
        guaranteed_profit_bps=(profit / total) * Decimal(10_000),
    )


def test_size_to_bankroll_scales_up_when_bankroll_allows():
    opp = _mk_opp()  # unit stake 0.95, profit 0.05
    scaled = size_to_bankroll(
        opp,
        bankroll_by_venue={"poly": Decimal("45"), "kals": Decimal("50")},
    )
    assert scaled is not None
    # Both venues fully consumed at scale=100 -> qty=100 each side.
    assert scaled.legs[0].qty == Decimal("100")
    assert scaled.legs[1].qty == Decimal("100")
    assert scaled.guaranteed_profit == Decimal("5")


def test_size_to_bankroll_limited_by_smaller_venue_bankroll():
    opp = _mk_opp()
    scaled = size_to_bankroll(
        opp,
        bankroll_by_venue={"poly": Decimal("4.5"), "kals": Decimal("50")},
    )
    assert scaled is not None
    assert scaled.legs[0].qty == Decimal("10")
    assert scaled.legs[1].qty == Decimal("10")


def test_size_to_bankroll_respects_tick_size():
    opp = _mk_opp()
    scaled = size_to_bankroll(
        opp,
        bankroll_by_venue={"poly": Decimal("4.5"), "kals": Decimal("50")},
        tick_by_venue={"poly": Decimal("3"), "kals": Decimal("3")},
    )
    assert scaled is not None
    # scale=10 -> round down to nearest 3 -> qty=9
    assert scaled.legs[0].qty == Decimal("9")


def test_size_to_bankroll_none_if_bankroll_too_small_for_tick():
    opp = _mk_opp()
    scaled = size_to_bankroll(
        opp,
        bankroll_by_venue={"poly": Decimal("0.10"), "kals": Decimal("50")},
        tick_by_venue={"poly": Decimal("1"), "kals": Decimal("1")},
    )
    assert scaled is None


def test_size_to_max_stake_returns_original_when_under_cap():
    opp = _mk_opp()
    assert size_to_max_stake(opp, Decimal("100")) is opp


def test_size_to_max_stake_scales_down_over_cap():
    opp = _mk_opp(qty="100")  # total_stake 95, profit 5
    scaled = size_to_max_stake(opp, Decimal("9.5"))
    assert scaled is not None
    # scale ratio 0.1 -> qty 10 per leg, profit 0.5
    assert scaled.legs[0].qty == Decimal("10")
    assert scaled.guaranteed_profit == Decimal("0.5")
