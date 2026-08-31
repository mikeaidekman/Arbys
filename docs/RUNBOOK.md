# Arbys operator runbook

Practical instructions for running Arbys and extending it. Assumes you've
already followed the setup in the root README.

**The instance that trades is the hosted one.** Since 2026-08-31 Arbys runs as
a single Fly machine (`arbys-dekman`) against a Neon Postgres with
`ARBYS_ENABLE_AUTO_TRADE=1`. This laptop is for development. Running a second
backend against the same venue credentials gives you two quote books, two
opportunity sets and two ledgers agreeing with each other about nothing, and it
doubles the WebSocket connections against the per-connection shedding ceiling
CLAUDE.md documents. Local runs default to ingest **off** partly for that
reason. See [section 7](#7-operating-the-hosted-instance) before touching
production.

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

`ARBYS_DB_URL` defaults to **SQLite** (`sqlite+aiosqlite:///./arbys-local.db`),
so a fresh checkout needs no database service at all. Set it to Postgres only
when you want to rehearse what production does:

```powershell
$env:ARBYS_DB_URL = "postgresql+asyncpg://arbys:arbys@localhost:5432/arbys"
uvicorn arbys.backend.app:app --reload
```

**Start uvicorn from the repo root.** The SQLite default is a *relative* path,
so launching from anywhere else silently creates a second, empty database
rather than failing. Alembic reads the same default, so `alembic upgrade head`
with nothing set migrates that same file.

Note SQLite here enforces foreign keys (`PRAGMA foreign_keys=ON`, set per
connection in `db/session.py`). That is not SQLite's default — constraints are
normally parsed and then not checked — and it is on so dev can fail on a write
Postgres would reject. It is not decoration: its absence is what let discovery
ship code that could not write a single event group to Postgres.

On startup `AppState.bootstrap()` will:

1. Take a Postgres **advisory lock**, and refuse to start if another instance
   holds it. This is what makes running two of these a loud failure instead of
   two ledgers quietly diverging. (No-op on SQLite.)
2. Create tables if missing (`Base.metadata.create_all`).
3. Ensure the three seed venues exist (`polymarket_us`, `kalshi`, `draftkings`).
   These are reference rows every placeholder market points at by foreign key,
   so a schema built without them cannot be written to.
4. Ensure the `default` paper account exists.
5. Hydrate event groups, balances, and positions from the DB.
6. Seed `DEFAULT_STARTING_BALANCE = $1000` for any venue not previously funded.
7. Start the periodic PnL snapshot and auto-settle services.
8. Start `AutoTradeService`, but only if `ARBYS_ENABLE_AUTO_TRADE=1`.
9. Start ingest, but only if `ARBYS_ENABLE_INGEST=1`.

## 1.1 Upgrading an existing database

**Run `alembic upgrade head` before starting the backend on a new version —
not after.** Step 2 above (`create_all`) only creates *missing tables*; it
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

### Turning the auto-trader on

**On the hosted instance it is already on** — `ARBYS_ENABLE_AUTO_TRADE = "1"`
in `fly.toml`'s `[env]`, since 2026-08-31. Changing it there means a commit and
a deploy, which is the point: those three flag lines are the difference between
an idle web app and a bot trading around the clock, and they belong somewhere
reviewable rather than in a secret store.

Locally, set `ARBYS_ENABLE_AUTO_TRADE=1` in `.env` and restart the backend.
There is no UI toggle by design. It trades paper only — `PaperExecutionAdapter`
is the only `ExecutionAdapter` in the repo, and paper fills are atomic, so it
cannot end up holding one naked leg.

Turning it on was gated on Cloudflare Access **verifying** rather than merely
fronting the app. `POST /quotes` lets a caller inject arbitrary prices and so
manufacture an arbitrage out of nothing; publicly writable *and* trading is the
dangerous combination, either alone is minor. Confirm the gate still holds with
the 403 check in section 7.4 before assuming it does.

To see what it did:

    select source, status, count(*) from paper_ticket group by source, status;

On the hosted instance you cannot run that from here — the Neon connection
string exists only in Fly's secret store, deliberately. Use the `/account` page
in the browser, which reads the same ticket ledger.

`source='auto'` rows are the bot's. The `filled` / `missed` / `rejected` split
is the point: `missed` counts edges that died between publication and
submission, which is what decides whether latency work is worth anything.
Cross-check `GET /health` for `dropped_writes` first — non-zero makes every
count above a lower bound.

To stop it, set the flag back to 0 and restart. To clear its cooldowns without
a restart, reset the paper account (`POST /paper/{account_id}/reset`), which
calls `clear_cooldowns()`.

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Opportunities page shows "reconnecting…" indefinitely | Backend not running, or Vite proxy misconfigured | Confirm `uvicorn` on :8000; check `frontend/vite.config.ts` proxy block |
| POST /event-groups returns 500 with FK error | Adapter emitted an `outcome_id` not previously seen | Placeholder creation happens automatically in `repositories.ensure_outcome_placeholder`; if this still fails, check that the seed venues exist |
| Opportunity created but "Paper execute" returns 409 | One or more legs moved past their limit price between detection and execution | Re-scan; the opportunity is stale. Widen slippage tolerance if this happens often |
| Equity curve stays at 0 after trades | PnL snapshot service hasn't ticked yet, or no quotes for held outcomes | Wait ~30 s; verify quotes are flowing for the outcomes you hold |
| Test suite fails with "NOT NULL constraint failed" on autoincrement id | New table added with `BigInteger` PK on SQLite | Use `BigInteger().with_variant(Integer(), "sqlite")` (see `Quote` and `PaperPnlSnapshot`) |

### 5.1 Hosted-only symptoms

Every one of these was invisible on the laptop and none of them raised. Expect
that shape from this environment.

| Symptom | Likely cause | Fix |
|---|---|---|
| `persistence write abandoned (discovery.groups)`, `dropped_writes` climbing | A foreign key Postgres enforces and SQLite historically did not | Flush a parent row before adding the child; SQLAlchemy orders inserts by *relationship*, not by raw `ForeignKey` |
| Hosted page renders blank, every API call 404s | Something is serving `/api/...` without the strip | `_StripApiPrefix` in `app.py`. The vite proxy is dev-server only |
| Hosted page renders but unstyled | A build *directory* is not mounted | `_mount_spa` mounts directories, not just root-level files. `public/design/industry/` is the whole design system |
| Table updates but the live feed is dead | The websocket dependency failed to resolve | An app-level `Depends()` must take `HTTPConnection`, never `Request` — FastAPI cannot hand a `Request` to a socket |
| Both venues disconnect together every ~60s, `keepalive ping timeout` | Our event loop is starved — not a venue outage, though the logs read like one | Check `loop_lag` in `/health`. Seconds there means it is ours |
| `adapters` shows `rest` for a venue | Its credentials are absent | A *broken* key raises instead; `rest` means unset |
| `adapters` is `{}` | Ingest is off, or no event groups are registered yet | Normal for the first minute or two after a deploy — discovery has to find groups before ingest builds adapters |

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

**On the hosted instance, don't reach for the database.** The Neon connection
string exists only in Fly's secret store and that is deliberate. Reset the
ledger through the app instead — `POST /paper/{account_id}/reset` from the
browser (it is behind Access), which wipes paper history, re-seeds balances,
and clears the auto-trader's cooldowns and the auto-settle memo. It leaves
event groups and the quote book alone, which is what you want: those are
rediscovered, not authored.

Note what a reset cannot do. `paper_position.realized_pnl` written before
2026-08-25 is understated by roughly $132 because lock contention dropped
`on_position` upserts while every write path swallowed its exception. `run_write`
stops further loss but nothing can reconstruct what was never written, so a
resync from broker state would overwrite rather than recover. Treat realized
PnL from a pre-existing database as a lower bound.

## 7. Operating the hosted instance

One Fly machine, `arbys-dekman`, region `ewr`, against a Neon Postgres. It is
deliberately singular: two instances would each hold their own quote book, each
publish opportunities, and each submit tickets against the same venue
credentials. Three separate things hold that line — `min_machines_running = 1`
with autoscaling off, a Fly volume that attaches to one machine at a time, and
the Postgres advisory lock in `bootstrap()` that makes a second process refuse
to start rather than quietly double up.

The VM is `performance-1x`, and that is a **correctness** decision rather than a
speed one. See 7.4.

### 7.1 Deploying

There is no flyctl on the dev box. The `Deploy` GitHub Action is the only way
in, and it runs on every push to `main`:

```powershell
gh workflow run "Deploy" --repo mikeaidekman/Arbys
```

`alembic upgrade head` runs as the `release_command` — in a temporary machine
holding the app's secrets, before the new version goes live — so a broken
migration aborts the deploy instead of reaching a running app. That is also why
the production connection string never has to exist on anyone's laptop.

Deploys are serialised (`concurrency: fly-deploy`, `cancel-in-progress: false`).
**Don't cancel one mid-flight.** It can interrupt the shutdown drain, which is
what makes a restart safe for in-flight tickets, and two overlapping deploys are
exactly the condition the advisory lock exists to refuse.

### 7.2 Running one-off Fly commands

The `Fly admin (manual)` workflow runs an arbitrary `fly <command>`:

```powershell
gh workflow run "Fly admin (manual)" --repo mikeaidekman/Arbys -f command="logs --no-tail"
gh workflow run "Fly admin (manual)" --repo mikeaidekman/Arbys -f command="config show"
gh workflow run "Fly admin (manual)" --repo mikeaidekman/Arbys -f command="machines list"
```

**Never pass a secret through it.** It echoes its input into the workflow log,
**this repository is public**, and Actions logs on a public repo are readable by
anyone. Anything typed there must be treated as disclosed. Use it for `logs`,
`config show`, `machines list`, `volumes` — never `secrets set`.

`config show` is the honest way to answer "did that flag actually reach the
machine?", which is a different question from "is it in `fly.toml`" and a
different question again from "did the log say so".

### 7.3 Secrets

Set them in the Fly dashboard (app → Secrets). Not in `fly.toml`: the repo is
public.

| name | what it does |
|---|---|
| `ARBYS_DB_URL` | the Neon connection string |
| `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY` | selects the credentialed Kalshi WS path |
| `POLYMARKET_US_API_KEY_ID` + `POLYMARKET_US_PRIVATE_KEY` | same, for Polymarket US |
| `CF_ACCESS_TEAM_DOMAIN` + `CF_ACCESS_AUD` | Cloudflare Access verification |

A new secret lands **Staged**, not Deployed. It does nothing until the next
deploy, so trigger one.

Both halves of every pair matter, and they fail differently on purpose. With
only one `CF_ACCESS_*` set, Access is treated as unconfigured and becomes a
no-op — which is loud, because every request then succeeds. A
`KALSHI_API_KEY_ID` set alongside a broken key **raises** instead of falling
back to REST: a silent downgrade would report healthy while quote freshness
quietly degraded, and cheap restarts on the WS path are the premise the whole
hosting design rests on.

The inline `*_PRIVATE_KEY` forms exist because no platform secret store hands
you a *file*, which is what the path-based loaders want. A PEM survives being
flattened, CRLF'd, or pasted as one base64 blob; it does not survive being
truncated to its first line, and that fails loudly.

### 7.4 Checking on it

`GET /health` is the one path exempt from Access, so it answers from a plain
terminal — and keeps answering when Access is itself the thing misbehaving:

```powershell
Invoke-RestMethod https://arbys-dekman.fly.dev/health
```

| field | healthy | meaning |
|---|---|---|
| `adapters` | `{"kalshi":"websocket","polymarket_us":"websocket"}` | which data path each venue is *actually* on |
| `dropped_writes` | `0` | non-zero means the ledger is incomplete — treat every figure derived from it as a lower bound until it is back to zero |
| `loop_lag` | p95 under ~50ms | below |

**`loop_lag` is how you tell our fault from the venue's.** Both WS adapters run
`ping_timeout=20`, so a loop held that long drops every venue connection at once
and reads in the logs as a venue outage. Worse:
`ARBYS_POLYMARKET_US_PRIORITY_DARK_AFTER_S` is **6 seconds**, so a stall past
that marks live in-play markets dark *by itself*, which escalates to a
resubscribe and then a shard rebuild — and every rebuild replays cached books
that can be hours old. A starved loop manufactures exactly the stale-quote
condition the freshness rules exist to prevent.

That is not hypothetical, and it is why the VM is `performance-1x`. Measured
2026-08-31:

| | shared-cpu-1x | performance-1x |
|---|---|---|
| `/monitored`, ~865 groups | 23.9 s | 588 ms |
| loop lag p50 / p95 | 2,278 / 5,641 ms | 0 / 24 ms |
| disconnects per log buffer | 22 | 1 |

The same request takes 0.43s locally, so the gap was contention, not an
algorithm. Watch `loop_lag` after any change to VM size or per-tick work.

Everything other than `/health` needs a Cloudflare Access assertion, so use the
browser on the custom domain. To confirm the origin is **verified** and not
merely fronted — `<app>.fly.dev` stays publicly reachable, so a proxy-only
arrangement would leave it wide open:

```powershell
# expect 403 on both of these, and 200 only on /health
Invoke-WebRequest https://arbys-dekman.fly.dev/monitored
Invoke-WebRequest https://arbys-dekman.fly.dev/quotes -Method POST -Body '{}' -ContentType 'application/json'
```

### 7.5 Reading the logs

```powershell
gh workflow run "Fly admin (manual)" --repo mikeaidekman/Arbys -f command="logs --no-tail"
```

The line worth reading is the Polymarket shard heartbeat, once per shard per
30s:

```
polymarket_us WS shard 4: 24 frames/s, 49 quotes/s, live 100/100 slug(s), 47 stale-on-arrival, 0 unknown-slug, 0 non-market frame(s)
```

- **`live N/M`** is the *only* symptom the venue's silent market-shedding has.
  Don't read one sub-100% window as a fault — plenty of pre-game markets are
  genuinely quiet, and the first window after a reconnect is always low. Read it
  as a trend across windows, and cross-check actual quote ages. A shard that
  stays low while its games are in play is the fault.
- **`stale-on-arrival`** is high immediately after a (re)connect and should fall
  to near zero. The venue answers a fresh subscription with a *cached* book, so
  a burst there is expected and handled: those quotes are back-dated by
  `source_age_s` and age out instead of being traded on. A count that stays high
  on a settled connection is worth chasing.

These are `INFO`. Nothing called `configure_logging()` until 2026-08-31, so the
root logger sat at its default and every `log.info` in the process was
discarded — while warnings still appeared via logging's last-resort handler,
which is what made the gap look like working logging rather than absent
logging. If these lines vanish again, check that first.

### 7.6 Stopping trading in a hurry

Set `ARBYS_ENABLE_AUTO_TRADE = "0"` in `fly.toml`, commit, push. The deploy
drains rather than cutting off: `kill_timeout = 60`, and
`AutoTradeService.stop()` waits up to `STOP_TIMEOUT_S` for an in-flight
submission instead of cancelling it — a `CancelledError` mid-submission is a
`BaseException`, so every layer built to notice a dropped write catches
`Exception` and never sees it, which could strand a `paper_ticket` at `pending`
with nothing logged and nothing counted.

To stop the *feed* as well, set `ARBYS_ENABLE_INGEST = "0"` in the same commit.
Leaving ingest on with trading off is the intended "watch only" state.
