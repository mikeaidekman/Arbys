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

# Polymarket US tennis.
#
# The old version scanned international's flat /markets for tennis-ish slugs,
# because that endpoint had no league filter and capped at 100 rows by volume.
# The US gateway is league-scoped, so ATP and WTA are just two direct calls.
for league in ("atp", "wta"):
    print(f"\n=== POLYMARKET US {league.upper()} ===")
    r = httpx.get(
        f"https://gateway.polymarket.us/v2/leagues/{league}/events",
        params={"limit": 10},
        timeout=20,
    )
    events = r.json().get("events", [])
    print(f"{len(events)} events")
    for e in events[:6]:
        print(f"  {e.get('slug')}  start={e.get('startTime')}")
        print(f"    players={[t.get('name') for t in e.get('teams') or []]}")
        for m in e.get("markets") or []:
            if m.get("sportsMarketType") == "tennis_match_winner":
                sides = [
                    (s.get("long"), (s.get("team") or {}).get("name"))
                    for s in m.get("marketSides") or []
                ]
                print(f"    winner slug={m.get('slug')} sides={sides}")
