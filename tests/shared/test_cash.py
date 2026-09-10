"""The cash-levelling planner.

The case that matters is the measured one: two venues, one drained by a run of
tickets that all leaned on it, the other idle. See `shared/cash.py`.
"""

from decimal import Decimal

import pytest

from arbys.shared.cash import CashTransfer, apply_transfers, plan_transfers
from arbys.shared.equity import account_equity
from arbys.shared.fees import KalshiFeeModel, PolymarketUsFeeModel
from arbys.shared.paper_broker import PaperExecutionAdapter
from arbys.shared.quotebook import QuoteBook

D = Decimal
MIN = D("25")


def test_levels_two_venues_to_the_midpoint():
    plan = plan_transfers({"kalshi": D("100"), "polymarket_us": D("0")}, min_transfer=MIN)
    assert plan == (CashTransfer("kalshi", "polymarket_us", D("50.00")),)


def test_already_level_moves_nothing():
    assert plan_transfers({"a": D("500"), "b": D("500")}, min_transfer=MIN) == ()


def test_gap_under_the_floor_moves_nothing():
    # A $10 gap is a $5 transfer. Writing an audit row to move $5 every
    # interval is the bookkeeping-for-dust problem the floor exists to stop.
    assert plan_transfers({"a": D("505"), "b": D("495")}, min_transfer=MIN) == ()


def test_a_single_venue_has_nowhere_to_send_it():
    assert plan_transfers({"kalshi": D("4000")}, min_transfer=MIN) == ()


def test_both_venues_dry_is_not_a_transfer_problem():
    # 751 of the ledger's rejections were this: real capital exhaustion, which
    # levelling cannot fix and must not pretend to.
    assert plan_transfers({"a": D("1.77"), "b": D("3.75")}, min_transfer=MIN) == ()


def test_zero_total_is_not_a_division():
    assert plan_transfers({"a": D("0"), "b": D("0")}, min_transfer=MIN) == ()


def test_three_venues_drain_the_largest_surplus_first():
    plan = plan_transfers(
        {"rich": D("900"), "mid": D("300"), "poor": D("0")}, min_transfer=MIN
    )
    # 1200/3 = 400, so 'mid' at 300 is short too -- the deepest hole is
    # filled first and the one surplus funds both.
    assert plan == (
        CashTransfer("rich", "poor", D("400.00")),
        CashTransfer("rich", "mid", D("100.00")),
    )


def test_two_sources_can_fund_one_sink():
    plan = plan_transfers({"a": D("300"), "b": D("300"), "c": D("0")}, min_transfer=MIN)
    assert plan == (
        CashTransfer("a", "c", D("100.00")),
        CashTransfer("b", "c", D("100.00")),
    )


def test_amounts_floor_to_the_cent_so_a_source_is_never_overdrawn():
    # 100/3 = 33.333... A transfer has to be a real number of cents, and
    # rounding up would ask for money that is not there.
    plan = plan_transfers({"a": D("100"), "b": D("0"), "c": D("0")}, min_transfer=MIN)
    assert all(t.amount == D("33.33") for t in plan)
    assert sum(t.amount for t in plan) <= D("100")


def _brokers(**cash: str) -> dict[str, PaperExecutionAdapter]:
    book = QuoteBook()
    fees = {"kalshi": KalshiFeeModel(), "polymarket_us": PolymarketUsFeeModel()}
    out = {}
    for venue, amount in cash.items():
        b = PaperExecutionAdapter(
            venue_id=venue, quotebook=book, fee_model=fees.get(venue, KalshiFeeModel())
        )
        b.deposit("acct", D(amount))
        out[venue] = b
    return out, book


def test_apply_moves_the_cash():
    brokers, _ = _brokers(kalshi="4000", polymarket_us="0")
    plan = plan_transfers(
        {v: b.cash("acct") for v, b in brokers.items()}, min_transfer=MIN
    )
    assert apply_transfers(brokers, "acct", plan) == plan
    assert brokers["kalshi"].cash("acct") == D("2000.00")
    assert brokers["polymarket_us"].cash("acct") == D("2000.00")


def test_a_sweep_is_equity_neutral():
    """The invariant. A transfer is not a deposit.

    `account_equity` sums cash across brokers, so levelling must leave equity
    untouched -- otherwise every return figure moves when cash does, and the
    sweep would read as funding the account.
    """
    brokers, book = _brokers(kalshi="3800", polymarket_us="200")
    before = account_equity(brokers, book, "acct")
    plan = plan_transfers(
        {v: b.cash("acct") for v, b in brokers.items()}, min_transfer=MIN
    )
    apply_transfers(brokers, "acct", plan)
    after = account_equity(brokers, book, "acct")
    assert plan  # the fixture really is imbalanced
    assert after.equity == before.equity == D("4000")
    assert after.cash == before.cash


def test_withdraw_refuses_to_overdraw_and_moves_nothing():
    brokers, _ = _brokers(kalshi="10", polymarket_us="0")
    assert brokers["kalshi"].withdraw("acct", D("10.01")) is False
    assert brokers["kalshi"].cash("acct") == D("10")


@pytest.mark.parametrize("amount", [D("0"), D("-5")])
def test_withdraw_refuses_a_nonpositive_amount(amount):
    brokers, _ = _brokers(kalshi="10")
    assert brokers["kalshi"].withdraw("acct", amount) is False
    assert brokers["kalshi"].cash("acct") == D("10")


def test_apply_skips_a_leg_whose_source_moved_underneath_it():
    brokers, _ = _brokers(kalshi="1000", polymarket_us="0")
    stale = (CashTransfer("kalshi", "polymarket_us", D("2000")),)
    assert apply_transfers(brokers, "acct", stale) == ()
    assert brokers["kalshi"].cash("acct") == D("1000")


def test_apply_skips_an_unknown_venue():
    brokers, _ = _brokers(kalshi="1000")
    plan = (CashTransfer("kalshi", "draftkings", D("100")),)
    assert apply_transfers(brokers, "acct", plan) == ()
    assert brokers["kalshi"].cash("acct") == D("1000")
