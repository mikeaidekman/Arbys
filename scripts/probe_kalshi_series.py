import httpx

# Search Kalshi series for anything tennis-related
r = httpx.get(
    "https://api.elections.kalshi.com/trade-api/v2/series",
    params={"limit": 200},
    timeout=15,
)
data = r.json()
series = data.get("series", [])
print(f"total series returned: {len(series)}")
hits = [s for s in series if any(k in (s.get("ticker") or "").lower() or k in (s.get("title") or "").lower() for k in ("tennis", "atp", "wta", "wimb", "open", "us_open", "french"))]
for s in hits[:30]:
    print(f"  ticker={s.get('ticker')} title={s.get('title')!r}")

# Also try direct series lookup
for guess in ["KXATP", "KXWTA", "KXTENNIS", "KXATPMATCH", "KXTOUR"]:
    r = httpx.get(
        "https://api.elections.kalshi.com/trade-api/v2/events",
        params={"series_ticker": guess, "status": "open", "limit": 5},
        timeout=10,
    )
    events = r.json().get("events", [])
    print(f"{guess}: {len(events)} open events")
    for e in events[:3]:
        print(f"    {e.get('event_ticker')} title={e.get('title')!r}")
