# Arbys operator runbook

Practical instructions for running Arbys locally and extending it. Assumes
you've already followed the setup in the root README.

## 1. Daily startup

```powershell
# Terminal 1 — backend
cd C:\Users\MichaelAidekman\PycharmProjects\Arbys
venv\Scripts\activate
uvicorn arbys.backend.app:app --reload
# → http://127.0.0.1:8000/docs  (Swagger UI)

# Terminal 2 — frontend
cd C:\Users\MichaelAidekman\PycharmProjects\Arbys\frontend
npm run dev
# → http://localhost:5173
```

By default the backend uses the Postgres URL in `ARBYS_DB_URL`
(`postgresql+asyncpg://arbys:arbys@localhost:5432/arbys`). For a zero-infra
smoke test, override to SQLite:

```powershell
$env:ARBYS_DB_URL = "sqlite+aiosqlite:///./arbys-local.db"
uvicorn arbys.backend.app:app --reload
```

On startup `AppState.bootstrap()` will:

1. Create tables if missing (`Base.metadata.create_all`).
2. Ensure the three seed venues exist (`polymarket_us`, `kalshi`, `draftkings`).
3. Ensure the `default` paper account exists.
4. Hydrate event groups, balances, and positions from the DB.
5. Seed `DEFAULT_STARTING_BALANCE = $1000` for any venue not previously funded.
6. Start the periodic PnL snapshot service.

## 1.1 Upgrading an existing database

**Run `alembic upgrade head` before starting the backend on a new version —
not after.** Step 1 above (`create_all`) only creates *missing tables*; it
never adds a *column* to a table that already exists. On a database sitting
at migration `0005`, starting this version's backend first will create the
new `paper_ticket` and `paper_settlement` tables but leave
`paper_order.ticket_id` absent, and every ticket endpoint then 500s with
`no such column: paper_order.ticket_id`. Worse, `alembic_version` still says
`0005` at that point, so the ticket-history migration (`0006`) looks
unapplied — but running it now fails with `table paper_ticket already
exists`, because `create_all` already made the table with the wrong shape.
(CLAUDE.md documents the same trap for migration `0002`.)

**Recovery**, if you already hit this: stop the backend, drop the two tables
`create_all` created (this does not touch anything from `0001`–`0005`), then
run the migration properly.

```powershell
# Postgres
psql $env:ARBYS_DB_URL -c "DROP TABLE paper_settlement; DROP TABLE paper_ticket;"
alembic upgrade head

# SQLite
venv\Scripts\python.exe -c "import sqlite3; c = sqlite3.connect('arbys-local.db'); c.execute('DROP TABLE paper_settlement'); c.execute('DROP TABLE paper_ticket'); c.commit()"
alembic upgrade head
```

Then start the backend as normal.

## 2. Adding a new event group (curated allowlist)

An "event group" is Arbys' term for a real-world event whose outcomes are
listed on 2+ venues. All arb detection is scoped to a registered event group.

**Via the admin UI:**

1. Open http://localhost:5173/admin.
2. Under **Add event group**, fill:
   - `ID` — a stable identifier you'll use in the DB and logs (e.g. `nfl-sb-2027-chiefs`).
   - `Title` — human-readable label.
   - Legs — one row per venue outcome. Each leg needs:
     - `outcome_id` — the venue's native outcome identifier (e.g. Polymarket US
       token id, Kalshi ticker + side, DraftKings selection id).
     - `Venue` — polymarket_us / kalshi / draftkings.
     - `Side` — YES (the outcome as-listed) or NO (the complementary side).
3. Click **Create**.

**Via API:**

```powershell
curl -X POST http://localhost:8000/event-groups `
  -H "content-type: application/json" `
  -d '{
    "id": "nfl-sb-2027-chiefs",
    "title": "Chiefs win Super Bowl 2027",
    "legs": [
      {"outcome_id": "aec-mlb-cle-det-2026-08-11:LONG", "venue_id": "polymarket_us", "is_yes_side": true},
      {"outcome_id": "KXSB-27-KC",     "venue_id": "kalshi",     "is_yes_side": true},
      {"outcome_id": "dk-selection-42", "venue_id": "draftkings", "is_yes_side": true}
    ]
  }'
```

**Rules of thumb:**

- All legs of a cross-venue group should refer to the same real-world outcome.
  For a 2-leg group where you plan to buy YES on one venue and NO on another,
  represent the NO leg with `is_yes_side: false` — the arb engine handles the
  complement math.
- For a complementary-set arb within a single venue (buy every outcome), all
  legs sit on the same `venue_id` with `is_yes_side: true`.
- The outcome IDs must match exactly what the venue adapter emits on quotes,
  or the engine will never see prices for them. The admin page's **Push
  quote** tool is the fastest way to verify wiring end-to-end.

**Verify:** after creating the group, push mock quotes and watch the
**Opportunities** page:

```powershell
curl -X POST http://localhost:8000/quotes -H "content-type: application/json" `
  -d '{"outcome_id":"poly-token-abc","bid":"0.40","ask":"0.40"}'
curl -X POST http://localhost:8000/quotes -H "content-type: application/json" `
  -d '{"outcome_id":"KXSB-27-KC","bid":"0.50","ask":"0.50"}'
```

Sum of 0.40 + (1 − 0.50) = 0.90 < 1 → an opportunity appears in the table.

## 2.1 Auto-discovery (cross-venue MLB)

Instead of curating each event group by hand, the backend can scan Kalshi and
Polymarket US every N minutes and register any MLB games it finds on both venues.

Enable in `.env`:

```
ARBYS_ENABLE_INGEST=1
ARBYS_ENABLE_DISCOVERY=1
ARBYS_DISCOVERY_INTERVAL_S=600
```

One-off from the CLI:

```powershell
.\venv\Scripts\python.exe scripts\discover_mlb.py --dry-run   # just print
.\venv\Scripts\python.exe scripts\discover_mlb.py             # upsert into DB
```

Discovery matches by `(sport, game_date, unordered team pair)` so opening-day
tickers with unusual home/away conventions still align across venues.

## 3. Adding a new venue

Adding a fourth venue is a scoped change; the arb engine, paper broker, and
UI don't need to know venue-specific details.

### 3.1 Register the venue id

Edit `arbys/backend/state.py`:

```python
self.fees: FeeModelRegistry = {
    "polymarket_us": PolymarketUsFeeModel(),
    "kalshi": KalshiFeeModel(),
    "draftkings": SportsbookFeeModel("draftkings"),
    "manifold": ManifoldFeeModel(),   # ← new
}
```

The `AppState.bootstrap()` upsert loop will create the venue row on next
startup.

### 3.2 Implement a fee model

Add a class in `arbys/shared/fees.py` implementing the `FeeModel` protocol.
Both `PolymarketUsFeeModel` (0.06) and `KalshiFeeModel` (0.07) are
`rate * p * (1-p) * qty`; `SportsbookFeeModel` returns zero because a
sportsbook's vig is already inside the quoted price.

**Write tests first** in `tests/shared/test_fees.py` — the fee model directly
affects whether the engine calls something an arbitrage, so it must be
correct before it goes live.

### 3.3 Implement a MarketDataAdapter

Add `arbys/adapters/<venue>.py` implementing `MarketDataAdapter` from
`arbys/adapters/base.py`. Two methods matter:

- `poll_quotes(outcome_ids) -> Iterable[Quote]` — one-shot fetch used by the
  ingest worker.
- `stream_quotes(outcome_ids) -> AsyncIterator[Quote]` — optional WS stream;
  fall back to polling if the venue has no WS.

Two templates, depending on what the venue offers:

- **REST-poll** — `PolymarketUsAdapter` in `arbys/adapters/polymarket_us.py`.
  Sweeps `gateway.polymarket.us/v1/markets/{slug}/bbo` every
  `ARBYS_POLYMARKET_US_POLL_S` seconds (default 5). Polymarket US has an
  authenticated WebSocket at `wss://api.polymarket.us/v1/ws/markets`, but it
  needs completed KYC plus an Ed25519 key pair and is **not wired**.
- **WS-first with REST fallback** — `KalshiWebSocketAdapter` /
  `KalshiAdapter`, selected by `_kalshi_factory` in `state.py` based on
  whether credentials are present. Copy that shape when adding a WS path to a
  venue that already polls.

If the venue quotes one binary contract with two sides rather than a token per
side, derive the second side rather than fetching it — see `quotes_from_bbo`.
**Both sides' asks must sum to more than 1**; if they don't, the inversion is
backwards, which is otherwise silent because the prices still look plausible.

Tests should use `httpx.MockTransport` for REST paths and an in-process
`websockets.serve` server for WS paths (see
`tests/adapters/test_polymarket_us.py` and `tests/adapters/test_kalshi_ws.py`)
— do **not** hit the real venue in tests. For a live check, add a script under
`scripts/` following `smoke_polymarket_us.py`.

### 3.4 Wire the adapter into ingest

`AppState.bootstrap()` builds one adapter per venue from
`AppState.adapter_factories` (defaults registered in `state.py`) and starts a
single `IngestWorker` covering all outcomes registered on any event group.
Whenever an event group is created or deleted via the REST API, ingest is
restarted so new outcome subscriptions take effect.

**Ingest is off by default** — set `ARBYS_ENABLE_INGEST=1` in `.env` to
enable it. When disabled, quotes must be pushed via `POST /quotes` (useful
for demos and tests).

To register a new venue's factory, mutate `self.adapter_factories` on the
`AppState` instance before `bootstrap()` runs, or add it directly to
`_default_adapter_factories()` in `state.py`.

### 3.5 (Later) Implement a LiveExecutionAdapter

v1 keeps `PaperExecutionAdapter` for every venue. When you're ready for live
execution, implement `ExecutionAdapter` for the venue, then swap it into
`ExecutionRouter` in `state.py`. The router and engine don't change.

## 4. Operating the paper broker

- **Starting balances** live in `DEFAULT_STARTING_BALANCE` in
  `arbys/backend/state.py`. They only apply to venues never previously funded
  — hydrated balances always win, so bumping the constant won't retroactively
  top up an existing account.
- **Slippage / latency** are configured on `PaperExecutionAdapter` construction
  (in `state.py`). Defaults are conservative for local demo; tune per venue
  once you have real quote history.
- **Resetting a paper account** — `POST /paper/{account_id}/reset` (wired to
  the admin UI's reset button) calls `delete_paper_history`, which wipes
  `paper_balance`, `paper_position`, `paper_order`, `paper_fill`,
  `paper_pnl_snapshot`, and `paper_ticket` for the account, **and every row**
  of `paper_settlement` — that table has no `account_id` column (settlement
  is keyed by `outcome_id`, since on a real exchange it's global), so on this
  single-account simulator "reset the account" clears it entirely rather than
  filtering it. Leaving `paper_ticket` or `paper_settlement` behind after a
  reset is what makes a fresh account report phantom "open tickets" forever,
  or score a brand-new ticket against a stale resolution. For a clean slate
  by hand instead, wipe the SQLite/Postgres DB or delete rows from all seven
  of those tables.
- **Multi-leg atomicity** — `ExecutionRouter.submit()` fills all legs or
  none. A rejection never writes a `paper_fill` row. `paper_order` rows are
  written one per attempted leg on a router-level rejection (limit price,
  balance, depth — see 4.1), but not on the pre-router
  `ARBYS_MAX_OUTCOME_QTY` cap rejection, which stops before an
  `ExecutionIntent` ever exists. Check `rejection_reason` on the ticket, and
  on its legs, for why.

## 4.1 Trade history and ticket statuses

Every submission attempt — `POST /paper/execute` today, the auto-trader
later — goes through `submit_arb_ticket` and writes one `paper_ticket` row.
Read the log with `GET /paper/{account_id}/tickets` (`?status=` and
`?source=` filter it). This is the only place "the bot tried and failed" is
distinguishable from "the bot never saw an edge" — a detector run that finds
nothing writes no row at all.

A ticket's `status` is one of three terminal values:

- **`filled`** — every leg executed. `paper_order` rows exist and each leg in
  the response carries a real `fill_price`.
- **`rejected`** — two different paths, and whether `legs` is populated
  tells you which:
  - **Cap breach** — `ARBYS_MAX_OUTCOME_QTY` (enforced in
    `ticket_service.py`, not the endpoint — see CLAUDE.md) stops the ticket
    before an `ExecutionIntent` is ever built. `legs` is empty; nothing was
    attempted.
  - **Router-level rejection** — a limit-price breach, insufficient balance,
    or insufficient book depth. `legs` is populated, one row per attempted
    leg: the leg that actually failed carries its own `rejection_reason`,
    every other leg is marked `ticket_rejected` (it previewed fine but the
    ticket failed as a whole), and `fill_price` is null on all of them —
    nothing filled. This is the more useful of the two for debugging: it
    names the specific leg that broke the ticket.
- **`missed`** — the opportunity was already gone by the time the ticket
  tried to act on it (quotes moved between detection and submission). This is
  the number that tells you whether faster execution would actually help —
  `rejected` means the edge was still there and something else stopped you.

`pending` is not one of the three — it's a transient state written the
instant the ticket id is minted, before the router runs. A ticket stuck on
`pending` means the process died mid-submission, not that anything is broken
in normal operation.

`paper_settlement` rows come from `AutoSettleService`'s heuristic settlement,
not a real exchange feed — Arbys has no settlement API to poll. If the
heuristic calls a game wrong, the tell is a ticket whose `realized_profit`
(computed at read time from that ticket's own fills) looks nothing like its
`expected_profit` from detection.

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Opportunities page shows "reconnecting…" indefinitely | Backend not running, or Vite proxy misconfigured | Confirm `uvicorn` on :8000; check `frontend/vite.config.ts` proxy block |
| POST /event-groups returns 500 with FK error | Adapter emitted an `outcome_id` not previously seen | Placeholder creation happens automatically in `repositories.ensure_outcome_placeholder`; if this still fails, check that the seed venues exist |
| Opportunity created but "Paper execute" returns 409 | One or more legs moved past their limit price between detection and execution | Re-scan; the opportunity is stale. Widen slippage tolerance if this happens often |
| Equity curve stays at 0 after trades | PnL snapshot service hasn't ticked yet, or no quotes for held outcomes | Wait ~30 s; verify quotes are flowing for the outcomes you hold |
| Test suite fails with "NOT NULL constraint failed" on autoincrement id | New table added with `BigInteger` PK on SQLite | Use `BigInteger().with_variant(Integer(), "sqlite")` (see `Quote` and `PaperPnlSnapshot`) |

## 6. Data reset

Nuke everything and start fresh:

```powershell
# SQLite (dev)
Remove-Item .\arbys-local.db -ErrorAction SilentlyContinue

# Postgres
docker compose down -v
docker compose up -d postgres
alembic upgrade head
```

Restart the backend and the seed data + `$1000` per venue will be recreated
on the first request.
