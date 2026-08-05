import httpx

for series in ["KXATPGAME", "KXWTAGAME"]:
    r = httpx.get(
        "https://api.elections.kalshi.com/trade-api/v2/events",
        params={"series_ticker": series, "status": "open", "limit": 10},
        timeout=15,
    )
    events = r.json().get("events", [])
    print(f"\n=== KALSHI {series}: {len(events)} open events ===")
    for e in events[:6]:
        print(f"  {e.get('event_ticker')} | title={e.get('title')!r}")
    if events:
        et = events[0]["event_ticker"]
        mr = httpx.get(
            "https://api.elections.kalshi.com/trade-api/v2/markets",
            params={"event_ticker": et, "limit": 20},
            timeout=15,
        )
        print(f"  markets for {et}:")
        for m in mr.json().get("markets", []):
            print(
                f"    ticker={m.get('ticker')} yes_sub_title={m.get('yes_sub_title')!r}"
            )

# Polymarket tennis
print("\n=== POLYMARKET (tennis filter) ===")
r = httpx.get(
    "https://gamma-api.polymarket.com/markets",
    params={
        "closed": "false",
        "active": "true",
        "order": "volume24hr",
        "ascending": "false",
        "limit": 400,
    },
    timeout=20,
)
markets = r.json()
tennis = [
    m
    for m in markets
    if any(
        s in (m.get("slug") or "").lower() or s in (m.get("question") or "").lower()
        for s in ("tennis", "atp", "wta", "-us-open", "-french-open", "-wimbledon")
    )
]
print(f"found {len(tennis)} tennis-ish markets in top 400")
for m in tennis[:8]:
    print(f"  q={m.get('question')!r}")
    print(f"    slug={m.get('slug')}  gameStartTime={m.get('gameStartTime')}")
    print(
        f"    outcomes={m.get('outcomes')}  tokens={m.get('clobTokenIds')}"
    )
