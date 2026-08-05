"""Smoke: bootstrap AppState with both Polymarket (WS) and Kalshi (poll) ingest.

Uses today's LAD vs Cubs game as a real cross-venue event group.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

# --- Real IDs discovered 2026-08-05 for the 2:20 PM ET LAD vs CHC game.
POLY_LAD = "71414359369770981117526636017076928924157945364412016891553818347898844703026"
POLY_CHC = "36186989822412722102385176384866805022593594175942416373309242053628802093800"
KALSHI_LAD_YES = "KXMLBGAME-26AUG051420LADCHC-LAD:YES"
KALSHI_CHC_YES = "KXMLBGAME-26AUG051420LADCHC-CHC:YES"


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "arbys-cross.db"
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
        id="lad-chc-26aug05",
        title="LAD vs CHC (2026-08-05 2:20pm ET)",
        legs=(
            # Polymarket direct token IDs
            EventGroupLeg(outcome_id=POLY_LAD, venue_id="polymarket", is_yes_side=True),
            EventGroupLeg(outcome_id=POLY_CHC, venue_id="polymarket", is_yes_side=False),
            # Kalshi: TICKER:SIDE format; the LAD market YES == LAD wins.
            EventGroupLeg(outcome_id=KALSHI_LAD_YES, venue_id="kalshi", is_yes_side=True),
            EventGroupLeg(outcome_id=KALSHI_CHC_YES, venue_id="kalshi", is_yes_side=False),
        ),
    )
    async with session_scope() as session:
        await repo.upsert_event_group(session, group)
    s.event_groups[group.id] = group
    s.engine.register_group(group)
    await s.restart_ingest()

    print("Streaming for 25s...", flush=True)
    await asyncio.sleep(25)

    print("\n--- QUOTEBOOK ---", flush=True)
    for leg in group.legs:
        q = s.quotebook.get(leg.outcome_id)
        short = leg.outcome_id if len(leg.outcome_id) < 30 else leg.outcome_id[:20] + "..."
        print(f"  {leg.venue_id:11}  {short:35}  {q}", flush=True)

    print(f"\n--- OPPORTUNITIES ({len(s.opportunities)}) ---", flush=True)
    for opp in list(s.opportunities)[:5]:
        print(f"  {opp.event_group_id}  profit_bps={opp.profit_bps}  legs={len(opp.legs)}", flush=True)

    await s.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
