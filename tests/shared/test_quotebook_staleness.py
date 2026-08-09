"""A quote that stopped arriving must stop counting as tradeable.

Venue websockets push deltas only on change, so "quiet" and "gone" look
identical in the data. Without an age limit the last price for a delisted
market or a rotated token quotes forever — which produced a phantom 8c arb
between a dead Polymarket token and a live Kalshi leg.
"""

from decimal import Decimal

from arbys.shared.quotebook import QuoteBook
from arbys.shared.types import Quote


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _q(oid="A", bid="0.40", ask="0.45"):
    return Quote(outcome_id=oid, bid=Decimal(bid), ask=Decimal(ask))


def test_fresh_quote_is_returned():
    clock = FakeClock()
    book = QuoteBook(max_age_s=60, clock=clock)
    book.upsert(_q())
    clock.advance(59)
    assert book.get("A") is not None


def test_stale_quote_is_withheld():
    clock = FakeClock()
    book = QuoteBook(max_age_s=60, clock=clock)
    book.upsert(_q())
    clock.advance(61)
    assert book.get("A") is None, "a quote past max_age must not be tradeable"


def test_refresh_resets_the_clock():
    """A quiet market that eventually ticks becomes tradeable again."""
    clock = FakeClock()
    book = QuoteBook(max_age_s=60, clock=clock)
    book.upsert(_q())
    clock.advance(61)
    assert book.get("A") is None
    book.upsert(_q(ask="0.46"))
    assert book.get("A") is not None
    assert book.get("A").ask == Decimal("0.46")


def test_age_and_get_with_age_still_report_a_stale_quote():
    """Staleness must be explainable, not just invisible."""
    clock = FakeClock()
    book = QuoteBook(max_age_s=60, clock=clock)
    book.upsert(_q())
    clock.advance(120)
    assert book.get("A") is None
    assert book.age_s("A") == 120
    aged = book.get_with_age("A")
    assert aged is not None
    quote, age = aged
    assert quote.ask == Decimal("0.45")
    assert age == 120


def test_snapshot_excludes_stale_entries():
    clock = FakeClock()
    book = QuoteBook(max_age_s=60, clock=clock)
    book.upsert(_q("OLD"))
    clock.advance(61)
    book.upsert(_q("NEW"))
    assert set(book.snapshot()) == {"NEW"}


def test_purge_stale_drops_them():
    clock = FakeClock()
    book = QuoteBook(max_age_s=60, clock=clock)
    book.upsert(_q("OLD"))
    clock.advance(61)
    book.upsert(_q("NEW"))
    assert book.purge_stale() == 1
    assert book.age_s("OLD") is None
    assert book.get("NEW") is not None


def test_expiry_can_be_disabled():
    """Backtests replay faster than wall clock; expiry would void the run."""
    clock = FakeClock()
    book = QuoteBook(max_age_s=None, clock=clock)
    book.upsert(_q())
    clock.advance(10_000)
    assert book.get("A") is not None
    assert set(book.snapshot()) == {"A"}


def test_unknown_outcome_reports_nothing():
    book = QuoteBook(max_age_s=60, clock=FakeClock())
    assert book.get("nope") is None
    assert book.age_s("nope") is None
    assert book.get_with_age("nope") is None
