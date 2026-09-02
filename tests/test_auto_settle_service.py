"""AutoSettleService: when a paper position is allowed to resolve.

Everything is injected — a fake broker, a real QuoteBook on a fake clock — so
these need no AppState, no database and no venue.

The behaviour under test is a pair of opposite failures the price-only
heuristic produced. It settled *too late and permanently* once a finished
market went dark (39 of 204 hosted positions were on games already played,
holding ~$2,130 of a $2,883 book), and *too early* on a heavy pre-game
favourite (908 of 2,013 local settlements fired before the game date).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from arbys.ingest.auto_settle_service import AutoSettleService
from arbys.shared.quotebook import QuoteBook
from arbys.shared.types import EventGroup, EventGroupLeg, Quote

NOW = datetime(2026, 9, 2, 18, 0, tzinfo=UTC)


class _Broker:
    """Records settlements instead of moving money."""

    def __init__(self, venue_id: str) -> None:
        self.venue_id = venue_id
        self.settled: dict[str, Decimal] = {}

    async def settle_outcome_async(
        self, outcome_id: str, resolved_value: Decimal, *, source: str = "heuristic"
    ) -> None:
        self.settled[outcome_id] = resolved_value


def _group(
    group_id: str = "atp-A-B-2026-09-01",
    *,
    start_time: datetime | None = None,
    ended: bool | None = None,
) -> EventGroup:
    return EventGroup(
        id=group_id,
        title="A vs B",
        legs=(
            EventGroupLeg(outcome_id="p-long", venue_id="polymarket_us", is_yes_side=True),
            EventGroupLeg(outcome_id="k-no", venue_id="kalshi", is_yes_side=False),
        ),
        start_time=start_time if start_time is not None else NOW - timedelta(hours=3),
        ended=ended,
        source="discovery",
    )


class _Fixture:
    """A book on a fake clock, so staleness is controllable."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.book = QuoteBook(max_age_s=600, clock=lambda: self.t)
        self.groups: dict[str, EventGroup] = {}
        self.brokers = {"polymarket_us": _Broker("polymarket_us"), "kalshi": _Broker("kalshi")}
        self.service = AutoSettleService(
            event_groups=self.groups,
            brokers=self.brokers,  # type: ignore[arg-type]
            quotebook=self.book,
            now=lambda: NOW,
        )

    def quote(self, outcome_id: str, ask: str) -> None:
        px = Decimal(ask)
        self.book.upsert(
            Quote(outcome_id=outcome_id, bid=max(px - Decimal("0.01"), Decimal("0")), ask=px)
        )

    def add(self, group: EventGroup) -> None:
        self.groups[group.id] = group

    @property
    def settled(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for b in self.brokers.values():
            out.update(b.settled)
        return out


async def test_venue_ended_settles_a_market_that_has_gone_dark():
    """The regression that stranded $2,130 of a $2,883 hosted book.

    A finished game is delisted, its quotes stop, and `QuoteBook.get` withholds
    them past max_age_s — so the price heuristic reads nothing, forever, while
    discovery retires the group. Reading the venue's own `ended` flag and the
    last known book settles it instead.
    """
    f = _Fixture()
    f.quote("p-long", "0.99")
    f.quote("k-no", "0.01")
    # Long past the staleness horizon: the market is dark.
    f.t += 100_000
    assert f.book.get("p-long") is None, "precondition: the live path sees nothing"

    f.add(_group(ended=True))
    await f.service.tick()

    assert f.settled == {"p-long": Decimal("1"), "k-no": Decimal("0")}


async def test_dark_market_without_an_ended_flag_is_not_settled():
    """Kalshi publishes no lifecycle, so `ended` is None — abstain, not guess."""
    f = _Fixture()
    f.quote("p-long", "0.99")
    f.quote("k-no", "0.01")
    f.t += 100_000
    f.add(_group(ended=None))
    for _ in range(5):
        await f.service.tick()
    assert f.settled == {}


async def test_retirement_settles_from_the_final_book():
    """Discovery dropping a group is the last moment anything knows it existed."""
    f = _Fixture()
    f.quote("p-long", "0.02")
    f.quote("k-no", "0.97")
    group = _group()
    f.add(group)
    await f.service.tick()
    assert f.settled == {}, "still listed, and no side is at 0.99"

    del f.groups[group.id]
    f.t += 100_000  # dark by the time we notice it is gone
    await f.service.tick()

    # NO side won, so the group's canonical proposition resolved FALSE.
    assert f.settled == {"p-long": Decimal("0"), "k-no": Decimal("1")}


async def test_a_fixture_retired_before_kickoff_is_never_settled():
    """A game that never started has no result, whatever its price was doing.

    Retirement before kickoff means the fixture moved or the venue pulled the
    market — settling would invent a winner for a game nobody played.
    """
    f = _Fixture()
    f.quote("p-long", "0.99")
    f.quote("k-no", "0.01")
    group = _group(start_time=NOW + timedelta(days=3))
    f.add(group)
    await f.service.tick()
    del f.groups[group.id]
    await f.service.tick()
    assert f.settled == {}


async def test_price_heuristic_will_not_fire_before_kickoff():
    """908 of 2,013 local settlements fired early; a 0.99 favourite is not a result."""
    f = _Fixture()
    f.quote("p-long", "0.995")
    f.quote("k-no", "0.005")
    f.add(_group(start_time=NOW + timedelta(days=2)))
    for _ in range(10):
        await f.service.tick()
    assert f.settled == {}


async def test_price_heuristic_still_fires_once_the_game_is_under_way():
    f = _Fixture()
    f.quote("p-long", "0.995")
    f.quote("k-no", "0.005")
    f.add(_group(start_time=NOW - timedelta(minutes=30)))
    await f.service.tick()
    await f.service.tick()
    assert f.settled == {}, "needs CONSECUTIVE_HITS confirmations"
    await f.service.tick()
    assert f.settled == {"p-long": Decimal("1"), "k-no": Decimal("0")}


async def test_an_indecisive_final_book_is_left_open_rather_than_guessed():
    """An unsettled position is visible and recoverable; a wrong one is not."""
    f = _Fixture()
    f.quote("p-long", "0.55")
    f.quote("k-no", "0.47")
    f.t += 100_000
    f.add(_group(ended=True))
    await f.service.tick()

    assert f.settled == {}
    assert f.service.unresolved_groups() == ("atp-A-B-2026-09-01",)
    assert not f.service.is_settled("atp-A-B-2026-09-01")


async def test_a_group_is_settled_once_and_reports_itself_settled():
    """`is_settled` is what stops the group being traded again."""
    f = _Fixture()
    f.quote("p-long", "0.99")
    f.quote("k-no", "0.01")
    f.t += 100_000
    group = _group(ended=True)
    f.add(group)
    await f.service.tick()
    assert f.service.is_settled(group.id)
    # engine_runtime publishes a synthetic `<group>:<venue>` id for an
    # intra-venue edge; a settled game is settled whichever id names it.
    assert f.service.is_settled(f"{group.id}:kalshi")

    f.brokers["kalshi"].settled.clear()
    f.brokers["polymarket_us"].settled.clear()
    await f.service.tick()
    assert f.settled == {}, "settled twice"
