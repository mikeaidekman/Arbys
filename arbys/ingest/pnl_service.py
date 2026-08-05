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
from decimal import Decimal

from ..db import repositories as repo
from ..db.session import session_scope
from ..shared.paper_broker import PaperExecutionAdapter
from ..shared.quotebook import QuoteBook

log = logging.getLogger(__name__)


def _mid(q) -> Decimal:
    return (q.bid + q.ask) / Decimal(2)


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
            cash_total = Decimal("0")
            mtm_total = Decimal("0")
            for broker in self._brokers.values():
                cash, positions = broker.account_snapshot(account_id)
                cash_total += cash
                for outcome_id, (qty, avg_price, _realized) in positions.items():
                    q = self._book.get(outcome_id)
                    price = _mid(q) if q is not None else avg_price
                    mtm_total += price * qty
            try:
                async with session_scope() as session:
                    await repo.insert_paper_pnl_snapshot(
                        session,
                        account_id=account_id,
                        cash=cash_total,
                        mtm_positions=mtm_total,
                        total_equity=cash_total + mtm_total,
                    )
            except Exception:
                log.exception("pnl snapshot write failed for %s", account_id)

    async def _run(self) -> None:
        while True:
            try:
                await self.snapshot_once()
            except Exception:
                log.exception("pnl snapshot iteration failed")
            await asyncio.sleep(self._interval_s)
