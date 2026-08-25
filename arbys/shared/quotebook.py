"""In-memory top-of-book cache shared between adapters, engine, and paper broker.

Ingest workers write into a `QuoteBook`; the engine reads from it to detect
opportunities; the paper broker reads from it to determine fill prices. Making
this a single object keeps the whole system consistent about "what did we think
the price was at time T?"

The book records *when* each quote arrived and treats anything older than
``max_age_s`` as absent. Without that, a venue that stops publishing an outcome
— a delisted market, a rotated token, a dropped subscription — is
indistinguishable from a quiet one, and its last price quotes forever. That
produced a real phantom arb: a Polymarket token that no longer existed kept
showing 50c against a live Kalshi leg.

The book stamps arrival time on a monotonic clock, so a system clock
adjustment cannot make quotes look fresh or ancient. Where a quote reports its
own `source_age_s` -- how far behind the venue said the book already was --
that stamp is back-dated by it, because on a replayed snapshot arrival time
says nothing about whether the prices are current.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace

from .types import Quote

# Long enough that a genuinely quiet market is not discarded — most legs do not
# tick for minutes at a time — but short enough that a dead feed stops being
# treated as tradeable. Kalshi/Polymarket websockets send deltas only on change,
# so there is no heartbeat to distinguish "quiet" from "gone" more precisely
# than this.
DEFAULT_MAX_AGE_S = 600.0

# How much older than the stored entry an incoming quote may be and still be
# accepted. The regression guard exists to stop an *hours*-old replayed
# snapshot from overwriting live prices; it must not fight the sub-second
# jitter in a venue's own transactTime. Measured against real frame timing -
# a frame every ~100ms carrying a 0.15-0.45s lag - a zero-tolerance guard
# discarded 24% of perfectly good updates, which on a fast-moving in-play book
# is throwing away ticks for nothing.
REGRESSION_TOLERANCE_S = 5.0


class QuoteBook:
    def __init__(
        self,
        *,
        max_age_s: float | None = DEFAULT_MAX_AGE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._quotes: dict[str, tuple[Quote, float]] = {}
        self._lock = threading.Lock()
        self._max_age_s = max_age_s
        self._clock = clock

    @property
    def max_age_s(self) -> float | None:
        return self._max_age_s

    def upsert(self, quote: Quote) -> None:
        """Record a quote, ageing it from when the *venue* said it was current.

        A quote that arrived carrying ``source_age_s`` is back-dated by that
        much, so ``max_age_s`` withholds it on the age of the data rather than
        the age of the delivery. Without this a replayed snapshot - Polymarket
        US serves hours-old books on subscribe - is indistinguishable from a
        live one, because both arrived just now.

        Back-dating can make an entry stale on arrival. That is the intended
        outcome: it is exactly the quote that must not be traded on.
        """
        with self._lock:
            at = self._clock()
            age = quote.source_age_s
            if age is not None and age > 0:
                at -= age
            existing = self._quotes.get(quote.outcome_id)
            if existing is not None and at < existing[1] - REGRESSION_TOLERANCE_S:
                # Never replace a newer book with a materially older one.
                # Frames do not arrive in book order: a venue answers every
                # fresh subscription with a cached snapshot, so a resubscribe
                # can deliver an hours-old book *after* live prices are
                # already flowing. Without this guard that snapshot would
                # overwrite good data and blank a market that was streaming
                # fine. The tolerance keeps it from rejecting ordinary
                # timestamp jitter - see REGRESSION_TOLERANCE_S.
                return
            self._quotes[quote.outcome_id] = (quote, at)

    def get(self, outcome_id: str) -> Quote | None:
        """Latest quote, or None if there isn't one or it has gone stale."""
        with self._lock:
            entry = self._quotes.get(outcome_id)
            if entry is None:
                return None
            quote, at = entry
            if self._is_stale(at):
                return None
            return replace(quote)

    def age_s(self, outcome_id: str) -> float | None:
        """Seconds since this outcome last updated, regardless of staleness."""
        with self._lock:
            entry = self._quotes.get(outcome_id)
            return None if entry is None else self._clock() - entry[1]

    def get_with_age(self, outcome_id: str) -> tuple[Quote, float] | None:
        """Latest quote and its age even when stale — for reporting *why*."""
        with self._lock:
            entry = self._quotes.get(outcome_id)
            if entry is None:
                return None
            quote, at = entry
            return replace(quote), self._clock() - at

    def snapshot(self) -> dict[str, Quote]:
        """Fresh quotes only, so callers can't accidentally act on stale ones."""
        with self._lock:
            now = self._clock()
            return {
                oid: replace(q)
                for oid, (q, at) in self._quotes.items()
                if not self._is_stale(at, now)
            }

    def purge_stale(self) -> int:
        """Drop stale entries outright. Returns how many were removed."""
        with self._lock:
            now = self._clock()
            dead = [oid for oid, (_q, at) in self._quotes.items() if self._is_stale(at, now)]
            for oid in dead:
                del self._quotes[oid]
            return len(dead)

    def _is_stale(self, at: float, now: float | None = None) -> bool:
        if self._max_age_s is None:
            return False
        return ((self._clock() if now is None else now) - at) > self._max_age_s
