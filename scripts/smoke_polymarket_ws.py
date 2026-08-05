"""Live smoke test: connect to real Polymarket WS, print quotes for 20s."""
from __future__ import annotations

import asyncio

from arbys.adapters.polymarket import PolymarketAdapter

TOKENS = [
    # Abdul El-Sayed 2026 MI Dem primary (YES, NO)
    "74768395815166461619548348007728690058055087254143355558596876906836785272025",
    "20535370510756332462433575263109694145574231213604117747907874615324086102913",
    # Clarity Act signed into law 2026 (YES, NO)
    "35198549486569600595408965368290795524428922595732253580266052028158040373233",
    "29633826701713552120011280893309822625942861174174976276686142480636225243753",
]

DURATION_S = 20.0


async def main() -> None:
    adapter = PolymarketAdapter(outcome_ids=TOKENS, use_websocket=True)
    print(f"Subscribing to {len(TOKENS)} tokens on Polymarket WS...", flush=True)

    n = 0
    seen: dict[str, tuple[str, str]] = {}
    stream = adapter.stream_quotes()

    async def pump() -> None:
        nonlocal n
        async for q in stream:
            n += 1
            seen[q.outcome_id] = (str(q.bid), str(q.ask))
            if n <= 10 or n % 25 == 0:
                short = q.outcome_id[:12] + "..."
                print(f"  #{n:4d}  {short}  bid={q.bid}  ask={q.ask}", flush=True)

    try:
        await asyncio.wait_for(pump(), timeout=DURATION_S)
    except asyncio.TimeoutError:
        pass
    finally:
        await stream.aclose()
        await adapter.close()

    print(f"\nTotal quotes received: {n}")
    print(f"Distinct tokens with quotes: {len(seen)} / {len(TOKENS)}")
    for tid, (bid, ask) in seen.items():
        print(f"  {tid[:16]}...  bid={bid}  ask={ask}")


if __name__ == "__main__":
    asyncio.run(main())
