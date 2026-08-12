"""Live smoke test for the Polymarket US integration.

Hits the real gateway. Not part of the test suite — the suite never touches a
real venue. Run from the repo root:

    venv\\Scripts\\python.exe scripts/smoke_polymarket_us.py
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from arbys.adapters.polymarket_us import PolymarketUsAdapter
from arbys.discovery.polymarket_us import fetch_polymarket_us_games
from arbys.discovery.teams import MLB_RESOLVER


async def main() -> None:
    games = await fetch_polymarket_us_games(resolver=MLB_RESOLVER, sport="mlb")
    print(f"discovered {len(games)} MLB moneyline games")
    if not games:
        print(
            "NO GAMES — check the league slug against "
            "https://gateway.polymarket.us/v2/sports before touching the "
            "parser. In the offseason, zero is the correct answer."
        )
        return

    for game in games[:3]:
        print(f"  {game.ref}  start={game.start_time}  {game.outcome_ids}")

    outcome_ids = [oid for g in games[:3] for oid in g.outcome_ids.values()]
    adapter = PolymarketUsAdapter(outcome_ids=outcome_ids, poll_interval_s=1.0)
    print(f"\npolling {len(outcome_ids)} outcomes...")

    seen: dict[str, tuple[Decimal, Decimal]] = {}
    async for quote in adapter.stream_quotes():
        seen[quote.outcome_id] = (quote.bid, quote.ask)
        if len(seen) >= len(outcome_ids):
            break
    await adapter.close()

    for oid, (bid, ask) in sorted(seen.items()):
        print(f"  {oid:55} bid={bid} ask={ask}")

    # Both sides of one market must cost more than a dollar. If this trips,
    # the LONG/SHORT inversion is backwards — which is otherwise silent,
    # since the individual prices still look perfectly plausible.
    print()
    by_slug: dict[str, list[Decimal]] = {}
    for oid, (_bid, ask) in seen.items():
        by_slug.setdefault(oid.rpartition(":")[0], []).append(ask)

    bad = False
    for slug, asks in sorted(by_slug.items()):
        if len(asks) != 2:
            continue
        total = sum(asks)
        if total <= Decimal("1"):
            bad = True
            print(f"BAD: {slug} asks sum to {total} <= 1 — inversion is wrong")
        else:
            print(f"ok:  {slug} asks sum to {total}")
    if not bad:
        print("\nall markets sane")


if __name__ == "__main__":
    asyncio.run(main())
