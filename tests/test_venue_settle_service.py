"""VenueSettleService: positions stranded on finished games settle at the
venue's own published result.

The failure this pins was reported on 2026-10-03: open positions on games
that ended long ago. ``AutoSettleService`` reaches a finished game only
through in-memory state -- a restart empties it, and a dropped write
resurrects a position it had already settled -- and the market itself quotes
nothing once it is over. These brokers are real (no sink, so no database);
the venues are fake resolvers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from arbys.ingest.auto_settle_service import AutoSettleService
from arbys.ingest.venue_settle_service import VenueSettleService
from arbys.shared.fees import KalshiFeeModel, PolymarketUsFeeModel
from arbys.shared.paper_broker import PaperExecutionAdapter
from arbys.shared.quotebook import QuoteBook
from arbys.shared.types import EventGroup, EventGroupLeg

D = Decimal
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def _brokers() -> dict[str, PaperExecutionAdapter]:
    book = QuoteBook()
    out = {
        "kalshi": PaperExecutionAdapter(
            venue_id="kalshi", quotebook=book, fee_model=KalshiFeeModel()
        ),
        "polymarket_us": PaperExecutionAdapter(
            venue_id="polymarket_us", quotebook=book, fee_model=PolymarketUsFeeModel()
        ),
    }
    for b in out.values():
        b.deposit("default", D("1000"))
    return out


def _hold(broker: PaperExecutionAdapter, outcome_id: str, qty: str, avg: str) -> None:
    broker.hydrate_position("default", outcome_id, qty=D(qty), avg_price=D(avg), realized_pnl=D("0"))


class _Venue:
    """Answers from a fixed table; anything absent is not final yet."""

    def __init__(self, results: dict[str, Decimal]) -> None:
        self.results = results
        self.asked: list[str] = []

    async def __call__(self, outcome_id: str) -> Decimal | None:
        self.asked.append(outcome_id)
        return self.results.get(outcome_id)


def _service(brokers, kalshi: _Venue, pm: _Venue, groups=None, **kw) -> VenueSettleService:
    return VenueSettleService(
        brokers=brokers,
        account_ids=["default"],
        resolvers={"kalshi": kalshi, "polymarket_us": pm},
        event_groups=groups if groups is not None else {},
        request_spacing_s={},
        now=lambda: NOW,
        **kw,
    )


async def test_an_orphaned_matched_pair_settles_at_the_venues_results():
    # The group was retired before a restart: nothing in the registry knows it.
    brokers = _brokers()
    _hold(brokers["kalshi"], "KXATP-GAU:NO", "100", "0.40")
    _hold(brokers["polymarket_us"], "aec-atp-gau:LONG", "100", "0.58")
    kalshi = _Venue({"KXATP-GAU:NO": D("1")})
    pm = _Venue({"aec-atp-gau:LONG": D("0")})

    r = await _service(brokers, kalshi, pm).settle_once()

    assert len(r.settled) == 2
    assert (await brokers["kalshi"].get_positions("default"))["KXATP-GAU:NO"] == 0
    assert (await brokers["polymarket_us"].get_positions("default"))["aec-atp-gau:LONG"] == 0
    # The winning leg paid $1 a contract, the losing one nothing.
    assert brokers["kalshi"].cash("default") == D("1100")
    assert brokers["polymarket_us"].cash("default") == D("1000")


async def test_a_market_the_venue_has_not_finalised_stays_open():
    brokers = _brokers()
    _hold(brokers["kalshi"], "KXATP-GAU:NO", "100", "0.40")

    r = await _service(brokers, _Venue({}), _Venue({})).settle_once()

    assert r.settled == []
    assert r.unresolved == [("kalshi", "KXATP-GAU:NO")]
    assert (await brokers["kalshi"].get_positions("default"))["KXATP-GAU:NO"] == D("100")
    assert brokers["kalshi"].cash("default") == D("1000")


async def test_games_not_yet_started_are_not_even_asked_about():
    brokers = _brokers()
    _hold(brokers["kalshi"], "KXNFL-SUN:YES", "10", "0.50")
    group = EventGroup(
        id="nfl-A-B-2026-10-05",
        title="A vs B",
        legs=(EventGroupLeg(outcome_id="KXNFL-SUN:YES", venue_id="kalshi", is_yes_side=True),),
        start_time=NOW + timedelta(days=2),
        source="discovery",
    )
    kalshi = _Venue({"KXNFL-SUN:YES": D("1")})

    r = await _service(brokers, kalshi, _Venue({}), groups={group.id: group}).settle_once()

    assert kalshi.asked == []
    assert r.skipped_not_started == 1
    assert (await brokers["kalshi"].get_positions("default"))["KXNFL-SUN:YES"] == D("10")


async def test_a_registered_group_it_settles_is_closed_to_auto_settle_and_trading():
    brokers = _brokers()
    _hold(brokers["kalshi"], "KXATP-GAU:NO", "10", "0.40")
    group = EventGroup(
        id="atp-GAU-WEN-2026-10-02",
        title="Gaubas vs Wendelken",
        legs=(EventGroupLeg(outcome_id="KXATP-GAU:NO", venue_id="kalshi", is_yes_side=False),),
        start_time=NOW - timedelta(hours=20),
        source="discovery",
    )
    groups = {group.id: group}
    auto = AutoSettleService(
        event_groups=groups, brokers=brokers, quotebook=QuoteBook(), now=lambda: NOW
    )

    await _service(
        brokers,
        _Venue({"KXATP-GAU:NO": D("1")}),
        _Venue({}),
        groups=groups,
        mark_group_settled=auto.mark_settled,
    ).settle_once()

    assert auto.is_settled(group.id)


async def test_closed_positions_are_not_looked_up():
    brokers = _brokers()
    _hold(brokers["kalshi"], "KXATP-OLD:YES", "0", "0")
    kalshi = _Venue({})

    await _service(brokers, kalshi, _Venue({})).settle_once()

    assert kalshi.asked == []


async def test_a_closed_outcome_missing_its_record_gets_one_and_no_cash():
    # Paid and zeroed, but the settlement record was dropped, so /account
    # reads every ticket on it as open forever.
    brokers = _brokers()
    kalshi = _Venue({"KXATP-GAU:NO": D("1")})

    async def unrecorded() -> list[tuple[str, str]]:
        return [("kalshi", "KXATP-GAU:NO")]

    r = await _service(
        brokers, kalshi, _Venue({}), unrecorded_outcomes=unrecorded
    ).settle_once()

    assert r.recorded == [("kalshi", "KXATP-GAU:NO", D("1"))]
    assert brokers["kalshi"].cash("default") == D("1000")


async def test_a_held_outcome_missing_its_record_is_paid_once_not_twice():
    brokers = _brokers()
    _hold(brokers["kalshi"], "KXATP-GAU:NO", "100", "0.40")
    kalshi = _Venue({"KXATP-GAU:NO": D("1")})

    async def unrecorded() -> list[tuple[str, str]]:
        return [("kalshi", "KXATP-GAU:NO")]

    r = await _service(
        brokers, kalshi, _Venue({}), unrecorded_outcomes=unrecorded
    ).settle_once()

    assert len(r.settled) == 1
    assert r.recorded == []
    assert kalshi.asked == ["KXATP-GAU:NO"]
    assert brokers["kalshi"].cash("default") == D("1100")


async def test_trigger_runs_one_pass_in_the_background():
    import asyncio

    brokers = _brokers()
    _hold(brokers["kalshi"], "KXATP-GAU:NO", "100", "0.40")
    svc = _service(brokers, _Venue({"KXATP-GAU:NO": D("1")}), _Venue({}))

    assert svc.trigger() is True
    assert svc.trigger() is False  # already running
    for _ in range(50):
        await asyncio.sleep(0)
        if svc.last_result is not None:
            break
    assert svc.last_result is not None
    assert svc.settled_total == 1


async def test_a_position_already_settled_once_is_closed_without_paying_again():
    # Settlement paid the cash and wrote its record; only the position upsert
    # was dropped, so the restart hydrated it as held.
    brokers = _brokers()
    _hold(brokers["kalshi"], "KXATP-GAU:NO", "100", "0.40")
    kalshi = _Venue({"KXATP-GAU:NO": D("1")})

    async def already(outcome_id: str) -> bool:
        return outcome_id == "KXATP-GAU:NO"

    r = await _service(brokers, kalshi, _Venue({}), already_settled=already).settle_once()

    assert r.settled == []
    assert r.ghosts_closed == [("kalshi", "KXATP-GAU:NO")]
    assert kalshi.asked == []
    assert (await brokers["kalshi"].get_positions("default"))["KXATP-GAU:NO"] == 0
    assert brokers["kalshi"].cash("default") == D("1000")


async def test_settlement_lands_balance_position_and_record_together(seed_reference_rows):
    from sqlalchemy import select

    from arbys.db import models as m
    from arbys.db.session import get_engine, session_scope
    from arbys.shared.persistence import AccountScopedSink, DbPaperPersistenceSink

    async with get_engine().begin() as conn:
        await conn.run_sync(m.Base.metadata.create_all)
    await seed_reference_rows()

    broker = PaperExecutionAdapter(
        venue_id="kalshi",
        quotebook=QuoteBook(),
        fee_model=KalshiFeeModel(),
        sink=AccountScopedSink(DbPaperPersistenceSink(), "default"),
    )
    broker.hydrate_balance("default", D("1000"))
    _hold(broker, "KXATP-GAU:NO", "100", "0.40")

    await broker.settle_outcome_async("KXATP-GAU:NO", D("1"), source="venue_result")

    async with session_scope() as s:
        bal = (
            await s.execute(select(m.PaperBalance.amount).where(m.PaperBalance.venue_id == "kalshi"))
        ).scalar_one()
        pos = (
            await s.execute(
                select(m.PaperPosition.qty).where(m.PaperPosition.outcome_id == "KXATP-GAU:NO")
            )
        ).scalar_one()
        rec = (await s.execute(select(m.PaperSettlement.source))).scalars().all()
    assert bal == D("1100")
    assert pos == 0
    assert rec == ["venue_result"]


async def test_unrecorded_outcomes_lists_exactly_what_account_reads_as_open(
    seed_reference_rows,
):
    from arbys.backend.state import _unrecorded_outcomes
    from arbys.db import models as m
    from arbys.db import repositories as repo
    from arbys.db.session import get_engine, session_scope

    async with get_engine().begin() as conn:
        await conn.run_sync(m.Base.metadata.create_all)
    await seed_reference_rows()

    async def ticket(tid: str, outcome: str, *, status: str = "filled", fill: bool = True):
        async with session_scope() as s:
            await repo.insert_paper_ticket(
                s, ticket_id=tid, account_id="default", event_group_id="g",
                title_snapshot="g", source="auto", status=status,
            )
            await s.flush()
            await repo.insert_paper_order(
                s, order_id=f"o-{tid}", account_id="default", venue_id="kalshi",
                outcome_id=outcome, is_buy=True, qty=D("1"), limit_price=D("0.5"),
                status="filled", ticket_id=tid,
            )
            if fill:
                await repo.insert_paper_fill(
                    s, order_id=f"o-{tid}", qty=D("1"), price=D("0.5"), fee=D("0")
                )

    await ticket("t1", "K-OPEN:YES")
    await ticket("t2", "K-DONE:YES")
    await ticket("t3", "K-REJ:YES", status="rejected", fill=False)
    async with session_scope() as s:
        await repo.insert_paper_settlement(
            s, outcome_id="K-DONE:YES", venue_id="kalshi", resolved_value=D("1")
        )

    assert await _unrecorded_outcomes() == [("kalshi", "K-OPEN:YES")]
