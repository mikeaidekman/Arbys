# Polymarket US market WebSocket — design

**Date:** 2026-08-12
**Status:** approved, not yet implemented
**Changes the premise of:**
[2026-08-12-in-game-divergence-design.md](2026-08-12-in-game-divergence-design.md)

## Why now

Credentials exist and are verified working — `GET /v1/portfolio/positions`
returned **HTTP 200** on 2026-08-12, so identity verification is approved and
the authenticated API is reachable.

That obsoletes the premise of the in-game divergence spec. Three of its eight
tasks exist solely to compensate for Polymarket US being polled while Kalshi
pushes. Building elaborate mitigation for a problem that can now simply be
fixed is the wrong order, so the WebSocket lands first and the in-game spec is
trimmed afterwards.

| In-game task | Purpose | After this spec |
| --- | --- | --- |
| 3 — two-tier concurrent polling | narrow the latency gap | largely obsolete |
| 5 — skew labelling | detect the remaining gap | rare, not routine |
| 7 — execute-time verification | verify a stale leg | keep; fires seldom |
| 1, 2, 4, 6, 8 | liveness, MLB totals, docs | unaffected |

## Protocol facts, established by probing

The published documentation omits the handshake specifics, unsubscribe,
heartbeat and connection limits. These were resolved against the live venue on
2026-08-12 rather than guessed:

| Question | Finding |
| --- | --- |
| Handshake auth | Same three headers as REST, supplied as connection headers |
| **Path covered by the signature** | **`/v1/ws/markets`** — signing `/` is rejected with **HTTP 401** |
| Subscribe → response | An immediate snapshot per market, then live deltas |
| Message envelope | `{requestId, subscriptionType, marketData: {...}}` |
| Markets per subscription | 100 (documented) |
| Unsubscribe / heartbeat | Not documented and not needed — see **Reconnection** |

Sample inbound frame, trimmed:

```json
{"requestId": "…", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
 "marketData": {"marketSlug": "aec-mlb-phi-stl-2026-08-12",
                "bids": [],
                "offers": [{"px": {"value": "0.0050"}, "qty": "419882.6700"}]}}
```

## Design

### 1. The adapter

New file `arbys/adapters/polymarket_us_ws.py`, holding
`PolymarketUsWebSocketAdapter`. It follows `kalshi_ws.py`, which already solves
the same problems: signed handshake, subscribe on connect, reconnect with
capped backoff, `max_size=2**22`, `ping_interval=20` / `ping_timeout=20`.

`venue_id = "polymarket_us"` and `outcome_id` stays `{slug}:LONG` /
`{slug}:SHORT`, unchanged — this is a transport swap, not a data model change.

### 2. Full market data, not LITE — because of `qty`

The venue offers `SUBSCRIPTION_TYPE_MARKET_DATA_LITE` (best bid/ask plus depth
counters) and `SUBSCRIPTION_TYPE_MARKET_DATA` (the ladder, `px` + `qty` per
level). **We use the full type**, for one specific reason developed below:
LITE cannot report resting size, and neither can REST `/bbo`.

Only the top level is read — `bids[0]` and `offers[0]`. The rest of the ladder
is ignored, so the extra payload buys correctness rather than depth we do not
use.

The SHORT side is derived exactly as today:

```
short.bid = 1 - long.ask     short.bid_size = long.ask_size
short.ask = 1 - long.bid     short.ask_size = long.bid_size
```

Existing tests already pin this inversion, including the both-asks-exceed-one
invariant, and they must keep passing against the WS parse path.

### 3. Adapter selection

`state.py` gains the creds gate `_kalshi_factory` already demonstrates:

```python
def _polymarket_us_factory(oids: list[str]) -> MarketDataAdapter:
    creds = creds_from_env()
    if creds is not None:
        return PolymarketUsWebSocketAdapter(outcome_ids=oids, creds=creds)
    return PolymarketUsAdapter(outcome_ids=oids, poll_interval_s=polymarket_us_poll_s())
```

The REST adapter is **kept, not deleted**. It is the fallback when credentials
are absent or revoked, and it remains the only path that works without KYC —
which matters for anyone else running this, and for local work with the key
file unavailable.

### 4. Reconnection

Reconnect with capped exponential backoff on any close or error, resubscribing
from scratch. There is no unsubscribe and no application-level heartbeat
because neither is needed: subscriptions die with the connection, and the
`websockets` library's own ping/pong (20s) detects a dead peer.

A reconnect re-requests a snapshot for every subscribed market, so the quote
book self-heals rather than carrying stale prices across the gap. This matters
more than usual here — `ARBYS_QUOTE_MAX_AGE_S` would otherwise keep serving
pre-disconnect prices for up to ten minutes.

## Two Phase 1 defects this surfaced

Both were found by probing, not by the test suite, and both are worth fixing
inside this change because the WS path is where they become consequential.

### `bidDepth` / `askDepth` are level counts, not sizes

Measured on `aec-mlb-tb-ath-2026-08-12`: `/bbo` reported `bidDepth 49`, and
`/book` returned exactly **49 bid levels** whose best level held **287,926.98**
contracts. The current adapter maps `bidDepth → bid_size`, so it reports 49
where the true size is 287,927.

Blast radius today is **display only**: sizes reach `/monitored`, the schemas
and the `quote` table, but nothing in `arb_engine` or stake sizing consumes
them. No bad trade has resulted. It stops being cosmetic the moment sizing
uses depth, which live trading requires.

Fix:

- **WS path**: sizes come from `bids[0].qty` / `offers[0].qty` — correct by
  construction.
- **REST path**: `/bbo` structurally cannot report top-of-book size. It must
  report **`0`**, meaning "unknown", rather than a level count that looks like
  a size and is not. Fetching `/book` per market to recover size would double
  REST load for a fallback path and is not worth it.

### One-sided books are dropped entirely

`quotes_from_bbo` returns `[]` when either side is missing, so a market with a
live ask and no bids disappears from the scanner — taking a genuinely
tradeable buy with it. This is live right now: `aec-mlb-phi-stl-2026-08-12`
has `bids: []` against 419,882 contracts offered at 0.0050.

The deleted international adapter synthesised the missing side at zero size;
Phase 1 lost that behaviour.

Fix, in two halves — **both halves are required**:

1. Synthesise the missing side at the present side's price with **size 0**, so
   the real ask stays visible to the detector.
2. **`paper_broker._preview_fill` must reject a fill whose side has size 0**,
   returning a `"no_liquidity"` rejection.

Half 2 is not optional. `_preview_fill` currently reads `quote.bid` for sells
with no size check, so a synthesised bid would let the broker report selling
into a book with no buyers — inflating paper P&L in the same direction the
stale-quote problem does. Restoring half 1 alone would reintroduce a phantom
in the process of fixing another.

## Error handling

- A failed handshake (401, network) logs and retries under backoff. It must
  **not** fall back to the REST adapter mid-flight: silently swapping
  transports would hide a revoked credential indefinitely.
- Credentials absent at construction is the normal no-KYC case and selects
  REST without complaint.
- A malformed frame is logged at debug and skipped; one bad message must not
  drop the connection.
- Frames for slugs we did not subscribe to are ignored.
- `Quote.__post_init__` rejects crossed or out-of-range books; those are
  dropped per-market, as the REST path already does.

## Testing

Tests never hit a real venue. WS paths use an in-process `websockets.serve`,
the pattern `tests/adapters/test_kalshi_ws.py` established.

| Test | Covers |
| --- | --- |
| `tests/adapters/test_polymarket_us_ws.py` | handshake sends the three headers and signs **`/v1/ws/markets`**; subscribe frame shape; snapshot parses to LONG+SHORT quotes; **sizes come from `qty`, not a depth counter**; malformed frame is skipped without dropping the connection; reconnect resubscribes |
| `tests/adapters/test_polymarket_us.py` | extended: `/bbo` reports size **0**, never the depth counter; a one-sided book yields a quote with the missing side at size 0 |
| `tests/shared/test_paper_broker.py` | **a fill against a zero-size side is rejected** — the half that stops a synthesised bid becoming a phantom sale |
| `tests/test_ingest_wiring.py` | factory picks WS when credentials are present, REST when absent |

The zero-size rejection test is the one that must not be skipped. Everything
else here is a transport improvement; that one prevents a new way for paper
P&L to lie.

Green-build bar unchanged: `pytest` (**190 today**, more after this),
`ruff check .`, `npm run build`.

## Non-goals

- **Order placement.** Same credentials, entirely separate concern, and gated
  behind legging risk which nothing here addresses.
- **Deleting the REST adapter.** It is the no-credentials fallback.
- **Using the rest of the ladder.** Only level 0 is read. Depth-aware sizing is
  a real future improvement and out of scope.
- **The private user WebSocket** (`/v1/ws/private`), which carries order and
  position updates and matters only once orders are real.

## Open questions

None. Decisions settled:

| Decision | Resolution |
| --- | --- |
| Subscription type | Full `MARKET_DATA` — LITE cannot report `qty` |
| REST adapter | Kept as the no-credentials fallback |
| Depth counters | REST reports size `0`; WS reports true `qty` |
| One-sided books | Emit with the missing side at size 0, **and** make the broker refuse zero-size fills |
| Fallback on auth failure | None — retry under backoff, never silently downgrade |
