"""In-memory top-of-book cache shared between adapters, engine, and paper broker.

Ingest workers write into a `QuoteBook`; the engine reads from it to detect
opportunities; the paper broker reads from it to determine fill prices. Making
this a single object keeps the whole system consistent about "what did we think
the price was at time T?"
"""

from __future__ import annotations

import threading
from dataclasses import replace

from .types import Quote


class QuoteBook:
    def __init__(self) -> None:
        self._quotes: dict[str, Quote] = {}
        self._lock = threading.Lock()

    def upsert(self, quote: Quote) -> None:
        with self._lock:
            self._quotes[quote.outcome_id] = quote

    def get(self, outcome_id: str) -> Quote | None:
        with self._lock:
            q = self._quotes.get(outcome_id)
            return replace(q) if q is not None else None

    def snapshot(self) -> dict[str, Quote]:
        with self._lock:
            return dict(self._quotes)
