"""Periodic paper PnL mark-to-market snapshotter.

Writes one `paper_pnl_snapshot` row per account per interval. The equity
computation uses last-known mid prices from the shared `QuoteBook`; if an
outcome has no live quote we fall back to the position's average price (i.e.
we assume flat MTM rather than zero).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from ..db import repositories as repo
from ..db.session import run_write
from ..shared.equity import account_equity
from ..shared.paper_broker import PaperExecutionAdapter
from ..shared.quotebook import QuoteBook

log = logging.getLogger(__name__)


class PnlSnapshotService:
    def __init__(
        self,
        *,
        brokers: dict[str, PaperExecutionAdapter],
        quotebook: QuoteBook,
        account_ids: list[str],
        interval_s: float = 30.0,
    ) -> None:
        self._brokers = brokers
        self._book = quotebook
        self._account_ids = list(account_ids)
        self._interval_s = interval_s
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None

    async def snapshot_once(self) -> None:
        for account_id in self._account_ids:
            eq = account_equity(self._brokers, self._book, account_id)
            await run_write(
                "pnl.snapshot",
                lambda s, account_id=account_id, eq=eq: repo.insert_paper_pnl_snapshot(
                    s,
                    account_id=account_id,
                    cash=eq.cash,
                    mtm_positions=eq.position_value,
                    total_equity=eq.equity,
                ),
            )

    async def _run(self) -> None:
        while True:
            try:
                await self.snapshot_once()
            except Exception:
                log.exception("pnl snapshot iteration failed")
            await asyncio.sleep(self._interval_s)
