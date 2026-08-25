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


# --- the book must never go backwards in time ------------------------------


def _timed_quote(oid: str, bid: str, ask: str, source_age_s: float | None = None) -> Quote:
    return Quote(
        outcome_id=oid,
        bid=Decimal(bid),
        ask=Decimal(ask),
        source_age_s=source_age_s,
    )


def test_a_replayed_snapshot_cannot_overwrite_a_newer_quote():
    """Frames do not arrive in book order.

    Every fresh subscription is answered with a *cached* book, so a
    resubscribe can hand us an hours-old snapshot after live prices are
    already flowing. Applied blindly it would overwrite good data and blank a
    market that was streaming perfectly - turning a routine reconnect into a
    market-wide outage.
    """
    clock = [1000.0]
    book = QuoteBook(max_age_s=600.0, clock=lambda: clock[0])

    book.upsert(_timed_quote("m:LONG", "0.40", "0.41"))  # live, now
    assert book.get("m:LONG").ask == Decimal("0.41")

    # A resubscribe replays a book from six hours ago.
    book.upsert(_timed_quote("m:LONG", "0.90", "0.91", source_age_s=21_600))
    got = book.get("m:LONG")
    assert got is not None, "a stale replay blanked a live market"
    assert got.ask == Decimal("0.41"), "a stale replay overwrote a newer quote"


def test_a_genuinely_newer_quote_still_replaces_an_older_one():
    """The guard must not freeze the book - only reject backwards steps."""
    clock = [1000.0]
    book = QuoteBook(max_age_s=600.0, clock=lambda: clock[0])

    book.upsert(_timed_quote("m:LONG", "0.40", "0.41", source_age_s=30))
    clock[0] += 1
    book.upsert(_timed_quote("m:LONG", "0.50", "0.51", source_age_s=0))
    assert book.get("m:LONG").ask == Decimal("0.51")


def test_equal_timestamps_still_apply():
    """The venue repeats a transactTime across consecutive frames; those are
    real updates and must not be dropped."""
    clock = [1000.0]
    book = QuoteBook(max_age_s=600.0, clock=lambda: clock[0])

    book.upsert(_timed_quote("m:LONG", "0.40", "0.41", source_age_s=5))
    book.upsert(_timed_quote("m:LONG", "0.44", "0.45", source_age_s=5))
    assert book.get("m:LONG").ask == Decimal("0.45")


def test_a_replay_still_lands_when_the_market_has_nothing_yet():
    """With no newer quote to protect, an old book is better than none - it is
    withheld by the age check, but it is what `get_with_age` explains."""
    clock = [1000.0]
    book = QuoteBook(max_age_s=600.0, clock=lambda: clock[0])

    book.upsert(_timed_quote("m:LONG", "0.90", "0.91", source_age_s=21_600))
    assert book.get("m:LONG") is None  # correctly withheld
    aged = book.get_with_age("m:LONG")
    assert aged is not None and aged[1] > 21_000


def test_ordinary_timestamp_jitter_is_not_treated_as_a_regression():
    """The guard targets hours-old replays, not sub-second noise.

    A venue stamps each frame with its own clock, and that lag jitters: frames
    arrive every ~100ms carrying a 0.15-0.45s transactTime lag, so a frame
    routinely looks slightly older than the one before it. A zero-tolerance
    guard discarded 24% of perfectly good updates that way - on a fast in-play
    book that is throwing away real ticks for nothing.
    """
    clock = [1000.0]
    book = QuoteBook(max_age_s=600.0, clock=lambda: clock[0])

    book.upsert(_timed_quote("m:LONG", "0.40", "0.41", source_age_s=0.15))
    clock[0] += 0.10
    # Older by its own clock than the one before it, but only just.
    book.upsert(_timed_quote("m:LONG", "0.44", "0.45", source_age_s=0.45))

    got = book.get("m:LONG")
    assert got.ask == Decimal("0.45"), "jitter was mistaken for a stale replay"
