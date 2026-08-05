"""CLI: run one MLB discovery pass; print or upsert cross-venue event groups."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from arbys.db import repositories as repo
from arbys.db.session import configure_engine, create_all, session_scope
from arbys.discovery.service import discover_all_event_groups


async def _main(dry_run: bool) -> int:
    groups = await discover_all_event_groups()
    print(f"discovered {len(groups)} cross-venue group(s):")
    for g in groups:
        print(f"  - {g.id}  ({g.title})  legs={len(g.legs)}")
        for leg in g.legs:
            print(f"      {leg.venue_id:12} {leg.outcome_id}  yes={leg.is_yes_side}")

    if dry_run:
        return 0
    if not groups:
        return 0

    configure_engine()
    await create_all()
    async with session_scope() as session:
        for g in groups:
            await repo.upsert_event_group(session, g)
    print(f"upserted {len(groups)} event group(s) into DB")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print but do not persist")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    sys.exit(asyncio.run(_main(dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
