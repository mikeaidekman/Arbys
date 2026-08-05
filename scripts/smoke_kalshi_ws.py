"""Live smoke test for the Kalshi authenticated WebSocket adapter.

Requires ``KALSHI_API_KEY_ID`` and ``KALSHI_PRIVATE_KEY_PATH`` in your env
(or ``.env``). Connects to production Kalshi, subscribes to the given market
tickers, prints the first N quote events, then exits.

Usage:
    python scripts/smoke_kalshi_ws.py TICKER1 [TICKER2 ...] [--count 5]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Load .env from the repo root before importing anything that reads env vars.
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from arbys.adapters.kalshi_ws import (  # noqa: E402
    KalshiWebSocketAdapter,
    kalshi_ws_creds_from_env,
)


async def main(tickers: list[str], count: int) -> int:
    creds = kalshi_ws_creds_from_env()
    if creds is None:
        print(
            "missing KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY_PATH — see .env.example",
            file=sys.stderr,
        )
        return 2
    key_id, private_key = creds
    outcome_ids = [f"{t}:YES" for t in tickers]
    a = KalshiWebSocketAdapter(
        outcome_ids=outcome_ids, api_key_id=key_id, private_key=private_key
    )
    print(f"connecting: {len(tickers)} ticker(s) -> {tickers}")
    got = 0
    async for q in a.stream_quotes():
        print(f"  {q.outcome_id}  bid={q.bid}  ask={q.ask}")
        got += 1
        if got >= count:
            break
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("tickers", nargs="+")
    p.add_argument("--count", type=int, default=5)
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.tickers, args.count)))
