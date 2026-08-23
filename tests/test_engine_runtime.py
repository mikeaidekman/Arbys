from decimal import Decimal

import pytest

from arbys.ingest.engine_runtime import EngineRuntime
from arbys.shared.arb_engine import ArbOpportunity
from arbys.shared.fees import ZeroFeeModel
from arbys.shared.quotebook import QuoteBook
from arbys.shared.types import EventGroup, EventGroupLeg, Quote


def _q(oid, ask, bid=None, ask_size="10"):
    """Quotes carry an explicit depth so ticket sizing is deterministic.

    Sizing is now min(depth, budget / unit_cost); leaving depth unknown would
    make every asserted qty a function of the default stake budget and the
    price, which is not what these tests are about.
    """
    bid_d = Decimal(bid) if bid else Decimal(ask)
    return Quote(
        outcome_id=oid,
        bid=bid_d,
        ask=Decimal(ask),
        ask_size=Decimal(ask_size) if ask_size is not None else None,
    )


@pytest.mark.asyncio
async def test_engine_emits_opportunity_on_qualifying_quote_update():
    book = QuoteBook()
    fees = {"poly": ZeroFeeModel("poly"), "kals": ZeroFeeModel("kals")}
    opps: list[ArbOpportunity] = []
    engine = EngineRuntime(
        quotebook=book,
        fees=fees,
        on_opportunity=opps.append,
        max_ticket_stake=Decimal("200"),
    )

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

    # Second quote closes the arb (0.45 + 0.50 = 0.95 -> 5c/contract).
    book.upsert(_q("N", "0.50"))
    engine.on_quote(book.get("N"))
    assert len(opps) == 1
    # 10 contracts (the thinner book's depth) * 5c
    assert all(leg.qty == Decimal("10") for leg in opps[0].legs)
    assert opps[0].guaranteed_profit == Decimal("0.50")


def _two_leg_group():
    return EventGroup(
        id="eg",
        title="X",
        legs=(
            EventGroupLeg(outcome_id="Y", venue_id="poly", is_yes_side=True),
            EventGroupLeg(outcome_id="N", venue_id="kals", is_yes_side=False),
        ),
    )


@pytest.mark.asyncio
async def test_opportunity_set_handler_reports_empty_when_edge_disappears():
    """The set handler must fire with [] so consumers can drop stale entries.

    A per-detection callback can only ever say "here is an arb"; it has no way
    to say "the arb you were told about is gone".
    """
    book = QuoteBook()
    fees = {"poly": ZeroFeeModel("poly"), "kals": ZeroFeeModel("kals")}
    sets: list[tuple[str, int]] = []

    engine = EngineRuntime(
        quotebook=book,
        fees=fees,
        on_opportunities=lambda gid, opps: sets.append((gid, len(opps))),
        max_ticket_stake=Decimal("200"),
    )
    engine.register_group(_two_leg_group())

    book.upsert(_q("Y", "0.45"))
    engine.on_quote(book.get("Y"))
    book.upsert(_q("N", "0.50"))
    engine.on_quote(book.get("N"))
    assert sets[-1] == ("eg", 1), sets

    # Price moves against us: 0.45 + 0.60 = 1.05, no edge left.
    book.upsert(_q("N", "0.60"))
    engine.on_quote(book.get("N"))
    assert sets[-1] == ("eg", 0), sets


@pytest.mark.asyncio
async def test_evaluate_now_is_pure_and_reflects_current_quotes():
    book = QuoteBook()
    fees = {"poly": ZeroFeeModel("poly"), "kals": ZeroFeeModel("kals")}
    fired: list[ArbOpportunity] = []
    engine = EngineRuntime(
        quotebook=book,
        fees=fees,
        on_opportunity=fired.append,
        max_ticket_stake=Decimal("200"),
    )
    engine.register_group(_two_leg_group())
    book.upsert(_q("Y", "0.45"))
    book.upsert(_q("N", "0.50"))

    found = engine.evaluate_now("eg")
    assert len(found) == 1
    assert all(leg.qty == Decimal("10") for leg in found[0].legs)
    assert found[0].guaranteed_profit == Decimal("0.50")  # 10 contracts * 5c
    # Pure: querying must not emit to handlers.
    assert fired == []

    # Reflects the book as it stands now, not as it was.
    book.upsert(_q("N", "0.60"))
    assert engine.evaluate_now("eg") == []

    assert engine.evaluate_now("no-such-group") == []


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
