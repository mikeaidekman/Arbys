"""Periodic levelling of free cash across the per-venue paper books.

Why this exists at all is in `shared/cash.py`: a matched pair costs ~$1.00
all-in but splits it lopsidedly and unpredictably between the two venues, so
fixed per-venue funding strands cash on whichever side a run of tickets did
not lean on. **2,835 of the local ledger's 6,316 rejected tickets (44.9%) had
one venue out of cash and the other leg previewing clean** -- 2.3x the whole
filled book, refused for the location of the money rather than its absence.

It follows `PnlSnapshotService`'s shape: one interval task, `sweep_once`
exposed so a test or a caller can run a single pass. Like every service in
this package it must not import `arbys/backend/` -- `backend/state.py` imports
`ingest`, so the reverse is a cycle.

The one thing worth being careful about: the whole plan is applied to memory
**before** anything is awaited. Persisting mid-plan would let a fill or a
settlement interleave and read a half-levelled account.

It levels across every broker in the map, which is every venue in
`AppState.fees` -- so switching `ARBYS_ENABLE_DRAFTKINGS` on would park a
third of the account's cash on a venue that has never carried a leg, starving
the two that do. That is the same untradeable-cash problem the flag was made
to gate; watch the first sweep if DraftKings is ever enabled.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from ..db import repositories as repo
from ..db.session import run_write
from ..shared.cash import CashTransfer, apply_transfers, plan_transfers
from ..shared.paper_broker import PaperExecutionAdapter

log = logging.getLogger(__name__)


class CashSweepService:
    def __init__(
        self,
        *,
        brokers: dict[str, PaperExecutionAdapter],
        account_ids: list[str],
        min_transfer: Decimal,
        interval_s: float = 60.0,
        enabled: Callable[[], bool] = lambda: True,
    ) -> None:
        self._brokers = brokers
        self._account_ids = list(account_ids)
        self._min_transfer = min_transfer
        self._interval_s = interval_s
        self._enabled = enabled
        self._task: asyncio.Task[None] | None = None
        # Cumulative, for /health. A sweep that never fires on a persistently
        # lopsided account is the failure this counter makes visible.
        self.transfers = 0
        self.moved = Decimal("0")

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

    async def sweep_once(self) -> tuple[CashTransfer, ...]:
        """Level every account once. Returns what moved, for logs and tests."""
        if not self._enabled():
            return ()
        applied: list[CashTransfer] = []
        for account_id in self._account_ids:
            balances = {v: b.cash(account_id) for v, b in self._brokers.items()}
            plan = plan_transfers(balances, min_transfer=self._min_transfer)
            if not plan:
                continue
            moved = apply_transfers(self._brokers, account_id, plan)
            if not moved:
                continue
            applied.extend(moved)
            self.transfers += len(moved)
            self.moved += sum((t.amount for t in moved), Decimal("0"))
            for t in moved:
                log.info(
                    "cash sweep: $%s %s -> %s (account %s, was %s/%s)",
                    t.amount,
                    t.from_venue,
                    t.to_venue,
                    account_id,
                    balances[t.from_venue],
                    balances[t.to_venue],
                )
            await self._persist(account_id, moved)
        return tuple(applied)

    async def _persist(self, account_id: str, moved: tuple[CashTransfer, ...]) -> None:
        """One transaction for the audit rows and every touched balance.

        Both halves in one write so a drop cannot leave the ledger claiming a
        transfer that did not land, or a balance with no row explaining it.
        In-memory state has already moved either way -- a dropped write is
        counted, not silent, and `/health` says so.
        """
        touched = {t.from_venue for t in moved} | {t.to_venue for t in moved}
        amounts = {v: self._brokers[v].cash(account_id) for v in touched}

        async def work(session: AsyncSession) -> None:
            for t in moved:
                await repo.insert_paper_transfer(
                    session,
                    account_id=account_id,
                    from_venue_id=t.from_venue,
                    to_venue_id=t.to_venue,
                    amount=t.amount,
                )
            for venue_id, amount in amounts.items():
                await repo.upsert_paper_balance(
                    session, account_id=account_id, venue_id=venue_id, amount=amount
                )

        await run_write("cash.sweep", work)

    async def _run(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except Exception:
                log.exception("cash sweep iteration failed")
            await asyncio.sleep(self._interval_s)
