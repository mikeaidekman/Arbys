"""Ingest worker.

Runs one asyncio task per configured adapter, streams quotes into a shared
`QuoteBook`, and forwards each update to a callback (used by the engine
runtime to trigger arb detection).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from ..adapters.base import MarketDataAdapter
from ..shared.quotebook import QuoteBook
from ..shared.types import Quote

log = logging.getLogger(__name__)

QuoteHandler = Callable[[Quote], None]


class IngestWorker:
    def __init__(
        self,
        *,
        adapters: list[MarketDataAdapter],
        quotebook: QuoteBook,
        on_quote: QuoteHandler | None = None,
    ) -> None:
        self._adapters = adapters
        self._book = quotebook
        self._on_quote = on_quote or (lambda _q: None)
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        for adapter in self._adapters:
            self._tasks.append(asyncio.create_task(self._pump(adapter)))

    async def _pump(self, adapter: MarketDataAdapter) -> None:
        try:
            async for quote in adapter.stream_quotes():
                self._book.upsert(quote)
                try:
                    self._on_quote(quote)
                except Exception:
                    log.exception("on_quote handler raised for %s", quote.outcome_id)
        except Exception:
            log.exception("adapter %s crashed", adapter.venue_id)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
