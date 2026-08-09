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

Quotes carry no timestamp of their own (they are plain value objects), so the
book stamps arrival time. A monotonic clock is used so a system clock
adjustment cannot make quotes look fresh or ancient.
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
        with self._lock:
            self._quotes[quote.outcome_id] = (quote, self._clock())

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
