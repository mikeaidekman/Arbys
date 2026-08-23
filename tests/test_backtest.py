from decimal import Decimal

import pytest

from arbys.backtest import run_backtest
from arbys.shared.fees import ZeroFeeModel
from arbys.shared.types import EventGroup, EventGroupLeg, Quote


def _q(oid, ask, bid=None, ask_size="10"):
    # Explicit depth: sizing is min(depth, budget / unit_cost), so without it
    # the ticket size — and whether the $10 balances below can pay for it —
    # would depend on the default stake budget.
    return Quote(
        outcome_id=oid,
        bid=Decimal(bid or ask),
        ask=Decimal(ask),
        ask_size=Decimal(ask_size),
    )


@pytest.mark.asyncio
async def test_backtest_detects_and_optionally_executes():
    group = EventGroup(
        id="eg",
        title="X",
        legs=(
            EventGroupLeg(outcome_id="Y", venue_id="poly", is_yes_side=True),
            EventGroupLeg(outcome_id="N", venue_id="kals", is_yes_side=False),
        ),
    )
    fees = {"poly": ZeroFeeModel("poly"), "kals": ZeroFeeModel("kals")}

    quotes = [
        _q("Y", "0.50"),
        _q("N", "0.50"),   # sum = 1.00 -> no arb
        _q("Y", "0.40"),   # sum = 0.90 -> arb
    ]
    r = await run_backtest(quotes=quotes, event_groups=[group], fees=fees)
    assert len(r.opportunities) == 1
    assert r.orders == []

    r2 = await run_backtest(
        quotes=quotes,
        event_groups=[group],
        fees=fees,
        starting_balances={"poly": Decimal("10"), "kals": Decimal("10")},
        execute=True,
    )
    assert len(r2.opportunities) == 1
    # 10 contracts a side: $4.00 on poly + $5.00 on kals, inside the balances.
    assert all(leg.qty == Decimal("10") for leg in r2.opportunities[0].legs)
    assert len(r2.orders) == 2
    assert r2.rejections == []
