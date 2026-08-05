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
2. Ensure the three seed venues exist (`polymarket`, `kalshi`, `draftkings`).
3. Ensure the `default` paper account exists.
4. Hydrate event groups, balances, and positions from the DB.
5. Seed `DEFAULT_STARTING_BALANCE = $1000` for any venue not previously funded.
6. Start the periodic PnL snapshot service.

## 2. Adding a new event group (curated allowlist)

An "event group" is Arbys' term for a real-world event whose outcomes are
listed on 2+ venues. All arb detection is scoped to a registered event group.

**Via the admin UI:**

1. Open http://localhost:5173/admin.
2. Under **Add event group**, fill:
   - `ID` — a stable identifier you'll use in the DB and logs (e.g. `nfl-sb-2027-chiefs`).
   - `Title` — human-readable label.
   - Legs — one row per venue outcome. Each leg needs:
     - `outcome_id` — the venue's native outcome identifier (e.g. Polymarket
       token id, Kalshi ticker + side, DraftKings selection id).
     - `Venue` — polymarket / kalshi / draftkings.
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
      {"outcome_id": "poly-token-abc", "venue_id": "polymarket", "is_yes_side": true},
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

## 3. Adding a new venue

Adding a fourth venue is a scoped change; the arb engine, paper broker, and
UI don't need to know venue-specific details.

### 3.1 Register the venue id

Edit `arbys/backend/state.py`:

```python
self.fees: FeeModelRegistry = {
    "polymarket": PolymarketFeeModel(),
    "kalshi": KalshiFeeModel(),
    "draftkings": SportsbookFeeModel("draftkings"),
    "manifold": ManifoldFeeModel(),   # ← new
}
```

The `AppState.bootstrap()` upsert loop will create the venue row on next
startup.

### 3.2 Implement a fee model

Add a class in `arbys/shared/fees.py` implementing the `FeeModel` protocol.
Look at `PolymarketFeeModel` for a percentage-of-notional example and
`KalshiFeeModel` for a piecewise/rounded example.

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

Follow the Polymarket adapter as a template. `PolymarketAdapter` uses a
WebSocket (`wss://ws-subscriptions-clob.polymarket.com/ws/market`) as its
primary transport with automatic REST-poll fallback if the WS connection fails
repeatedly (default: 3 failures within 60s → switches to REST until WS
recovers). Set `use_websocket=False` on construction to force REST-only.
Tests should use `httpx.MockTransport` for REST paths and an in-process
`websockets.serve` server for WS paths (see `tests/adapters/test_polymarket.py`) —
do **not** hit the real venue in tests.

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
- **Resetting a paper account** — for a clean slate, wipe the SQLite/Postgres
  DB or delete rows from `paper_balance`, `paper_position`, `paper_order`,
  `paper_fill`, `paper_pnl_snapshot` for the account.
- **Multi-leg atomicity** — `ExecutionRouter.submit()` fills all legs or
  none. If any leg would exceed its limit price or lack balance, the entire
  ticket rejects; nothing is persisted for rejected tickets. Check server
  logs for the rejection reason.

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
