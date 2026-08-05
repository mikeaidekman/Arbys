"""Smoke: bootstrap AppState with real Polymarket ingest and observe quotebook + opportunities."""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from decimal import Decimal
from pathlib import Path


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "arbys-smoke.db"
    os.environ["ARBYS_DB_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ["ARBYS_ENABLE_INGEST"] = "1"

    from arbys.backend import state as state_module
    from arbys.db import repositories as repo
    from arbys.db.session import session_scope
    from arbys.shared.types import EventGroup, EventGroupLeg

    state_module.reset_state()
    s = state_module.get_state()
    await s.bootstrap()

    group = EventGroup(
        id="clarity-act",
        title="Clarity Act 2026",
        legs=(
            EventGroupLeg(
                outcome_id="35198549486569600595408965368290795524428922595732253580266052028158040373233",
                venue_id="polymarket",
                is_yes_side=True,
            ),
            EventGroupLeg(
                outcome_id="29633826701713552120011280893309822625942861174174976276686142480636225243753",
                venue_id="polymarket",
                is_yes_side=False,
            ),
        ),
    )
    async with session_scope() as session:
        await repo.upsert_event_group(session, group)
    s.event_groups[group.id] = group
    s.engine.register_group(group)
    await s.restart_ingest()

    print("Waiting 15s for WS quotes...", flush=True)
    await asyncio.sleep(15)

    yes_q = s.quotebook.get(group.legs[0].outcome_id)
    no_q = s.quotebook.get(group.legs[1].outcome_id)
    print(f"YES quote in book: {yes_q}", flush=True)
    print(f"NO  quote in book: {no_q}", flush=True)
    print(f"Opportunities seen: {len(s.opportunities)}", flush=True)

    if yes_q is not None and no_q is not None:
        # A real arb would need yes.ask + no.ask < 1. On healthy books that's rare.
        combined = yes_q.ask + no_q.ask
        print(f"YES ask + NO ask = {combined}  ({'ARB!' if combined < Decimal('1') else 'no arb'})")

    await s.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
