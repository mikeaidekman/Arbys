from decimal import Decimal

import pytest

from arbys.ingest.engine_runtime import EngineRuntime
from arbys.shared.arb_engine import ArbOpportunity
from arbys.shared.fees import ZeroFeeModel
from arbys.shared.quotebook import QuoteBook
from arbys.shared.types import EventGroup, EventGroupLeg, Quote


def _q(oid, ask, bid=None):
    bid_d = Decimal(bid) if bid else Decimal(ask)
    return Quote(outcome_id=oid, bid=bid_d, ask=Decimal(ask))


@pytest.mark.asyncio
async def test_engine_emits_opportunity_on_qualifying_quote_update():
    book = QuoteBook()
    fees = {"poly": ZeroFeeModel("poly"), "kals": ZeroFeeModel("kals")}
    opps: list[ArbOpportunity] = []
    engine = EngineRuntime(quotebook=book, fees=fees, on_opportunity=opps.append)

    engine.register_group(
        EventGroup(
            id="eg",
            title="X",
            legs=(
                EventGroupLeg(outcome_id="Y", venue_id="poly", is_yes_side=True),
                EventGroupLeg(outcome_id="N", venue_id="kals", is_yes_side=False),
            ),
        )
    )

    # First quote alone: no opp because the other leg has no quote yet.
    book.upsert(_q("Y", "0.45"))
    engine.on_quote(book.get("Y"))
    assert opps == []

    # Second quote closes the arb (0.45 + 0.50 = 0.95).
    book.upsert(_q("N", "0.50"))
    engine.on_quote(book.get("N"))
    assert len(opps) == 1
    assert opps[0].guaranteed_profit == Decimal("0.05")


@pytest.mark.asyncio
async def test_engine_only_reevaluates_affected_groups():
    book = QuoteBook()
    fees = {"poly": ZeroFeeModel("poly")}
    calls = {"n": 0}

    def handler(_):
        calls["n"] += 1

    engine = EngineRuntime(quotebook=book, fees=fees, on_opportunity=handler)
    engine.register_group(
        EventGroup(
            id="eg",
            title="X",
            legs=(EventGroupLeg(outcome_id="A", venue_id="poly", is_yes_side=True),),
        )
    )
    # Quote for outcome not in any group -> no evaluation.
    q = _q("Z", "0.99")
    book.upsert(q)
    engine.on_quote(q)
    assert calls["n"] == 0
