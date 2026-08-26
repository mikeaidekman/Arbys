# Hosting without a server — design

**Date:** 2026-08-25
**Status:** proposed
**Depends on:** [the trustworthy ledger design](2026-08-25-trustworthy-ledger-design.md),
Parts B–D
**Changes the premise of:** [docs/DEPLOY.md](../../DEPLOY.md), which assumes a VM
you administer and never restart
**Precedes:** real-venue execution, which is a separate decision and a separate spec

## Why now

The proof of concept is outperforming expectations and the stated intent is to
go live "pretty soon." The stated constraint is that there is no appetite for
administering a box — no apt, no systemd, no Postgres upgrades on a Tuesday.

Those two pull in opposite directions under the current deploy plan. DEPLOY.md
is built around a VM precisely *because* it treats a restart as expensive, and
every managed platform restarts you on its own schedule. This spec resolves
that, and the resolution turns out to be smaller than expected: one finding in
the adapters removes most of the cost of a restart, and three changes of maybe
a hundred lines make the app safe on a platform whose deploy machinery we do
not control.

One thing to say plainly before the rest, because it is the same caveat the
ledger spec raised and the timing now matters more: **"working better than
expected" is currently a reading of a ledger that is known to drop writes.**
The measured 18 `database is locked` errors, 17 `OperationalError`s and 6
`QueuePool` timeouts across roughly one day all landed on write paths that
swallow their exception, and the $132 broker-vs-DB divergence is what that
looks like from the outside. Nothing here contradicts the conclusion. But the
sequencing below puts the ledger fix first, because hosting exists to run
unattended, and an unattended bot writing to a lossy ledger produces a number
nobody can defend when it is time to size real money against it.

## Findings

### Restarts are cheap on the credentialed path

This is the finding the rest of the spec rests on, and it should be measured
before it is trusted.

DEPLOY.md states that a fresh process starts with an empty `QuoteBook` and the
terminal is "partially blank for up to a minute or two," concluding: optimize
for **fewer** restarts. That is true of the REST-poll path, where the book
refills one 5s sweep at a time against a Kalshi public tier calibrated at
~6 req/s.

It is not true of the WebSocket path. Both adapters request and handle a full
snapshot at connect:

| adapter | evidence |
| --- | --- |
| Kalshi | "Kalshi sends one `orderbook_snapshot` up front and then incremental `orderbook_delta` messages" (`adapters/kalshi_ws.py:12-14`); handled at line 282 |
| Polymarket US | "Subscribing yields an immediate snapshot per market" (`adapters/polymarket_us_ws.py:12`); re-subscribes from scratch on every connect *specifically* to re-request one (line 119) |

So with `KALSHI_API_KEY_ID` and `POLYMARKET_US_API_KEY_ID` both set, a restart
refills the book in roughly one round trip per subscription batch — Polymarket
US batches at `MAX_SLUGS_PER_SUBSCRIPTION = 100` — rather than one poll per leg.
Seconds, not minutes.

**Measure it before building on it:** time from process start to N legs quoted,
credentialed on both venues, and record the number here. If it comes back in
minutes rather than seconds this spec's platform choice is wrong and the VM
plan stands. Everything downstream is contingent on this one number.

Note the coupling this creates: the low-maintenance hosting story now *requires*
credentials on both venues. The un-credentialed REST path still works, but it
restores the expensive-restart problem that argues for a box you never touch.

### The single-instance invariant is enforced by nothing

DEPLOY.md is emphatic that exactly one backend instance may ever run, and it is
right: `AppState.__init__` (`backend/state.py:203-247`)
holds the `QuoteBook`, the three paper brokers, `_opps_by_group` and the
subscriber queues in ordinary process memory. Two instances means both detect
the same edge, both execute it, and `ARBYS_MAX_OUTCOME_QTY` silently doubles.
It is a correctness bug, not a scaling inefficiency.

But the invariant is currently maintained by **discipline** — a comment in a
systemd unit and a warning in a document. Nothing in the process detects a
second copy of itself. On a VM you administer, discipline is adequate. On a
platform where a dashboard toggle, a default deploy strategy, or a failed
health check can produce a second instance without anyone typing a command, it
is not.

This is the actual blocker to using a managed platform, and it is fifteen
lines to remove.

### Nothing in the repo is deployable as an image

- **No `Dockerfile`, no `.dockerignore`.** `docker-compose.yml`
  defines Postgres for local dev only; the app itself is not containerized.
- **`requirements.txt` is a live trap.** It contains exactly `pandas`, `numpy`,
  `requests`, `python-dotenv` — leftovers from an earlier incarnation. No
  fastapi, no uvicorn, no sqlalchemy, no httpx, no websockets, no cryptography.
  DEPLOY.md already warns a human away from it. A buildpack autodetecting a
  Python project will not read the warning, and produces a build that dies at
  first import.
- **`frontend/dist/` is gitignored**, so an image that serves the SPA has to
  build it, not copy a committed bundle.

### Shutdown is not a drain

`AppState.shutdown()` (`backend/state.py:402-408`) stops discovery,
ingest, the auto-settler and the snapshotter. It does not refuse new tickets and
does not wait for in-flight ones.

Under paper that is harmless: `_commit_atomically` fills every leg with no
`await` between them, so nothing can interleave and there is no in-flight
window to interrupt. Once a real `ExecutionAdapter` exists the router falls to
`_commit_sequentially` (`shared/execution_router.py:158-180`),
which awaits `place_order` per leg — and a platform-initiated SIGTERM landing
between leg one and leg two leaves a naked position. That is the one outcome
the project exists to avoid, and a managed platform sends that signal on its own
schedule rather than ours.

## What a platform has to provide

| requirement | why |
| --- | --- |
| Exactly one instance, permanently | The invariant above. Not a scaling knob. |
| No overlapping deploys | A rolling or blue-green overlap window *is* two live instances |
| Always-allocated CPU | Long-lived outbound WS to both venues; no request to hang work off |
| us-east | Latency against both venues' infrastructure |
| SIGTERM plus a real grace period | The drain in Part B needs somewhere to run |
| Managed Postgres, same region | Backups and PITR under a real-money ledger |

Scale-to-zero, autoscaling, and revision-based traffic shifting are all
disqualifying rather than merely unnecessary.

## External services

Three vendors, plus a domain that is already held.

| slot | vendor | notes |
| --- | --- | --- |
| Compute | Fly.io | Runs the process. The only one that cannot be dropped — Cloudflare and Neon do not execute code. |
| Database | Neon (or Supabase) | The ledger. SQLite on the Part D volume would work, since the app is single-instance by design, but backup and restore then become manual — which is the maintenance this spec exists to avoid. |
| Auth | Cloudflare Access | SSO in front of an app that has none of its own. |

**The domain is already on Cloudflare**, which removes the one onboarding step
with a lead time: Access needs a hostname whose DNS you control, and
`*.fly.dev` is not one.

Roughly $5–10/month — a `shared-cpu-1x` Machine, a Postgres branch that never
idles (the PnL snapshotter writes every 30s), and Access at no cost at this
user count. Check the database tier's backup-retention window before live
money; free tiers keep history for a short period, which is not what you want
under a ledger you are sizing positions against.

Not vendors, but prerequisites with their own lead time: **funded venue
accounts with API credentials**. Kalshi needs an API key; Polymarket US order
placement needs completed identity verification plus an Ed25519 key pair.
Neither is a hosting task, and both should be started before the
infrastructure rather than after it.

## Part A — enforce single-instance with a database lock

At the top of `bootstrap()`, take a session-scoped Postgres advisory lock on a
constant key. If it is already held, **refuse to start**.

```python
# db/session.py
async def acquire_singleton_lock() -> bool:
    """True if this process is the only one. Session-scoped: released on
    disconnect, so a hard-killed instance does not wedge the lock."""
```

`pg_try_advisory_lock` rather than `pg_advisory_lock` — block and you turn a
misconfiguration into a hang that looks like a slow boot. Fail loudly instead,
naming the invariant, so a platform health check reports it.

Two details that matter:

- **Session-scoped, not transaction-scoped.** The lock must be held for the
  process lifetime and released automatically when the connection drops. A
  transaction-scoped lock releases at the first commit and protects nothing.
- **The lock connection must be dedicated**, held outside the pool, or a pool
  recycle drops it silently. That is the whole failure mode this part exists to
  remove, so it should not be reintroduced by pool configuration.

On SQLite this is a no-op — gate on the dialect, exactly as the ledger spec's
Part A gates its pragmas, and for the same reason. Local dev and the test suite
must not require Postgres.

This converts the invariant from a document into a property of the system, and
it is what makes every remaining choice in this spec safe.

## Part B — a shutdown that drains

Extend `AppState.shutdown()` in order:

1. Set a `_draining` flag. `submit_arb_ticket` refuses immediately while set,
   with a distinct rejection reason so drained attempts are legible in the
   ticket log rather than looking like vanished edges.
2. Stop the auto-trader first, so nothing new arrives.
3. Await in-flight tickets to a terminal state, bounded — 20s is a reasonable
   start and must sit comfortably inside the platform's grace period.
4. Then the existing teardown: discovery, ingest, auto-settle, PnL.

Set the platform kill timeout to **60s**, well above the bound, so the drain
finishes rather than being cut off mid-unwind.

A hard kill gives no drain at all, so pair this with a **boot-time
reconciliation** against venue-reported positions once a live adapter exists.
Until then it is paper-only and the reconciliation has nothing to compare
against — but the drain should land now, because retrofitting it under live
money means testing it under live money.

## Part C — one artifact

Serve the SPA from FastAPI rather than from a separate Worker or Pages project.
There is no `StaticFiles`, no `mount()` and no `CORSMiddleware` anywhere in
`arbys/backend/` today, so this is additive.

This is DEPLOY.md's own "simpler alternative," and against a no-maintenance
brief it is now the primary: one origin, one deploy, no CORS, and it removes
the `/api` prefix-strip failure mode entirely — the rewrite in
`frontend/vite.config.ts` is dev-server only, and every
plan that keeps a separate frontend has to reimplement it somewhere and 404s
completely if it is missed.

Mount it **after** all API routes, with an SPA fallback so client-side routes
(`/admin`, `/account`) return `index.html` rather than 404.

Multi-stage `Dockerfile`, because `frontend/dist/` is gitignored:

```dockerfile
FROM node:22-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build          # tsc -b && vite build — the real frontend typecheck

FROM python:3.14-slim
WORKDIR /app
COPY pyproject.toml ./
COPY arbys/ ./arbys/
RUN pip install --no-cache-dir -e .    # NOT requirements.txt — see Findings
COPY --from=frontend /app/frontend/dist ./frontend/dist
CMD ["uvicorn", "arbys.backend.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

Python **3.14-slim**, matching the dev venv (3.14.3) rather than the
`requires-python = ">=3.11"` floor, which is not what the suite has been run
against.

`--workers 1` is the default; state it anyway, as the systemd unit does, so
nobody optimizes it later.

While here: delete or regenerate `requirements.txt`. It has now been flagged in
two documents and the correct fix is to stop it existing.

## Part D — the platform

**One Fly Machine in `iad`, plus managed Postgres in the same region.**

```toml
# fly.toml
app = "arbys"
primary_region = "iad"

[build]

[http_service]
  internal_port = 8000
  auto_stop_machines = false
  auto_start_machines = false
  min_machines_running = 1

[[vm]]
  size = "shared-cpu-1x"
  memory = "1gb"

[[mounts]]
  source = "arbys_data"
  destination = "/data"

kill_timeout = 60
```

Four deliberate choices:

- **`auto_stop_machines = false`.** Scale-to-zero has nothing to restart the
  venue websockets from.
- **Deploy in place, never alongside.** The default canary/rolling strategies
  create a second Machine before retiring the first, which is the overlap
  window. `fly deploy --strategy immediate` updates the existing Machine.
  **Confirm this against the running app** — read `fly status` mid-deploy and
  check that the machine count never exceeds one. Part A turns a mistake here
  into a refused boot rather than a double-executed edge, but the deploy should
  be correct on its own.
- **A volume, even though nothing needs one.** A single-attach volume is a
  *mechanical* lock: it makes scaling to two and overlapping deploys impossible
  rather than merely discouraged. Belt and braces with Part A, and the same
  trick is what forces serialization on other platforms.
- **`kill_timeout = 60`** for Part B's drain.

Postgres from Neon or Supabase pinned to us-east (Fly's managed offering is
also fine — check region availability). `ARBYS_DB_URL` must use the
`postgresql+asyncpg://` scheme; both drivers are already declared in
`pyproject.toml`, so there is no extra install step.

Note the interaction with the ledger spec: **on Postgres its Part A is a
no-op**, since `journal_mode` and `busy_timeout` are SQLite-only and the
dialect gate skips them. Parts B, C and D — retry, `dropped_writes` counter,
discovery batching, and recording every attempt — all still apply and are all
still required. Its Part A remains correct for local dev, which stays on SQLite.

### Platforms rejected, and why

| platform | verdict |
| --- | --- |
| Cloud Run, Azure Container Apps | Revision-based traffic shifting *is* an overlap window; scale-to-zero fights a persistent WS |
| Heroku | Cycles dynos roughly daily — an unavoidable restart on someone else's schedule |
| Render | Zero-downtime deploys overlap by default. Workable, since attaching a disk forces serialization, but it is fighting the platform |
| Railway | Reasonable second choice. Confirm its deploy-overlap behaviour before trusting it |
| Lambda, Workers | No persistent process. Not applicable |

## Part E — authentication

**The app has no authentication and `/admin` executes trades.** There is no
bearer token and no API-key dependency anywhere in `arbys/backend/`. On a VM
behind a Cloudflare Tunnel that is survivable because the origin has no public
address. On a platform the hostname is public by construction, so this becomes
load-bearing.

Put Cloudflare Access on the hostname and **verify the
`Cf-Access-Jwt-Assertion` header in one FastAPI dependency**, validating
against the team's public keys and checking `aud`. Verifying rather than merely
fronting is the point: a direct request to the platform hostname carries no
signed assertion and is rejected, so the origin is not quietly reachable around
Access.

This runs no tunnel and no sidecar, which is the whole brief. Exempt `/health`
so the platform can probe it.

The domain is already on Cloudflare, so this is an Access application and a
CNAME, not a domain migration.

## Part F — secrets and key material

Both venue integrations load their private key **from a file path**, and no
platform secret store hands you a file.

| venue | variable | loader |
| --- | --- | --- |
| Kalshi | `KALSHI_PRIVATE_KEY_PATH` | `load_kalshi_private_key(pem_path)` — `adapters/kalshi_ws.py:98` |
| Polymarket US | `POLYMARKET_US_PRIVATE_KEY_PATH` | `Path(key_path).read_text(...)` — `adapters/polymarket_us_auth.py:119` |

The file convention is deliberate, and the docstring says why
(`adapters/polymarket_us_auth.py:111-112`): a file keeps the key **outside this
repo**, where an env var invites a `.env` that eventually reaches git. That is
right on a laptop and on the DEPLOY.md VM. It does not transfer to a container,
where there is no repo on disk, no `.env`, and the platform secret store *is*
the outside-the-repo location.

### Accept the key inline, keep the path for dev

Add `KALSHI_PRIVATE_KEY` and `POLYMARKET_US_PRIVATE_KEY` holding the key
material directly, taking precedence over the `_PATH` form when both are set.
Keep `_PATH` — local dev then works exactly as documented, and the existing
convention stays right where it was right.

Inline rather than writing the secret to disk at boot: the Part D volume is
**persistent**, so a key written there outlives the process that wrote it, and
a file adds failure modes (mount timing, permissions) an env var does not have.

One trap, and it is Kalshi-only. Polymarket US's secret is already a
single-line base64 string — the key file "holds the base64 secret exactly as
the portal displayed it" — so it survives any secret store unchanged. Kalshi's
is a multi-line PEM, and multi-line values are the classic way this gets
mangled between a shell and a secret store. The loader should normalise a
literal `\n` sequence back to a newline before parsing, so a key that arrives
flattened still works rather than failing as "malformed."

### Absent is a configuration; broken is a bug

Today they are the same code path. `kalshi_ws.py:99-101` and
`polymarket_us_auth.py:121-123` both catch, log, and return `None`, and the
factories at `backend/state.py:180-193` then build the un-credentialed REST
adapter.

Returning `None` is **correct when credentials are genuinely absent.** That is
the documented no-KYC path and it must keep working. It is **wrong when
credentials are present but unusable** — key id set, key missing, unreadable or
malformed. That should raise at boot.

This distinction matters more than anywhere else in the spec, because the whole
hosting argument rests on the credentialed WebSocket path having cheap restarts
(see Findings). A broken key silently selects the REST path, where they are
not — so the premise dissolves, the app still reports healthy, and the only
evidence is one log line on a box nobody is tailing.

The rule: if the `_ID` variable is set and the key cannot be loaded, fail the
boot. If neither is set, fall back exactly as today.

### Report the resolved mode on /health

Add the adapter mode per venue, alongside the `dropped_writes` field the ledger
spec adds:

```json
{
  "status": "ok",
  "dropped_writes": 0,
  "adapters": {"kalshi": "websocket", "polymarket_us": "websocket"}
}
```

"Am I actually on the fast path?" then has an answer from outside the box,
which is the point of hosting it at all. It composes with DEPLOY.md's Part 9
request that `/health` assert feed staleness rather than merely proving the
process is alive.

### What goes in the secret store

| key | value |
| --- | --- |
| `ARBYS_DB_URL` | Postgres connection string, `postgresql+asyncpg://` scheme |
| `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY` | Kalshi credentials |
| `POLYMARKET_US_API_KEY_ID`, `POLYMARKET_US_PRIVATE_KEY` | Polymarket US credentials |
| `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` | Part E's JWT validation |

The `ARBYS_*` feature flags are **not** secrets and belong in `fly.toml` as
plain env, where they are visible in review rather than hidden in a secret
store. `ARBYS_ENABLE_INGEST=1` and `ARBYS_ENABLE_DISCOVERY=1` are the two that
must be set for a hosted instance to do anything at all.

`load_dotenv()` runs at `backend/app.py:13` and is harmless in a container
because no `.env` is present — provided none is ever copied in. The Part C
Dockerfile copies only `pyproject.toml`, `arbys/` and `frontend/`, so it cannot
happen by accident, but add the `.dockerignore` noted in Findings listing
`.env`, `*.pem`, `*.db` and `venv/`. `.env` being gitignored protects git, not
a local build context.

Rotation is a secret update plus a restart. Part A makes a botched restart
refuse to boot rather than trade twice, and the cost of the restart itself is
the number measured in Findings.

## Testing

- The advisory lock is taken on Postgres and **skipped on SQLite** — the
  dialect gate is the part worth pinning, since getting it wrong makes the test
  suite require a Postgres server.
- A second process against the same Postgres URL **fails to boot**, with the
  invariant named in the error. This is the test that replaces a paragraph of
  documentation with a guarantee.
- The lock survives a pool recycle: force one, assert the lock is still held.
- A killed instance releases the lock — the next boot succeeds without manual
  intervention.
- `submit_arb_ticket` refuses while `_draining`, with the distinct reason.
- Shutdown awaits an in-flight ticket to terminal state, and gives up at the
  bound rather than hanging.
- The SPA fallback returns `index.html` for `/account` and a **404 for an
  unknown `/api`-shaped path** — the regression that catches a catch-all mounted
  ahead of the API routes.
- `/health` is reachable without an Access assertion; every other route is not.
- Image build: `pip install -e .` resolves fastapi and uvicorn, and the built
  image does not contain `requirements.txt`.
- An inline `KALSHI_PRIVATE_KEY` takes precedence over `KALSHI_PRIVATE_KEY_PATH`
  when both are set, and `_PATH` alone still works unchanged — the local dev
  path must not regress.
- A Kalshi PEM arriving with literal `\n` sequences instead of newlines loads
  successfully.
- **`_ID` set with an unloadable key fails the boot**, and neither set falls
  back to REST without raising. This is the pair that matters: it is the
  difference between a configuration and a silent downgrade to the slow path.
- `/health` reports `websocket` per venue when credentialed and `rest` when
  not, matching the adapter the factory actually built.

## What deliberately does not change

Detection, sizing, fee models, the paper broker, discovery, and the frontend's
behaviour. This spec moves where the process runs and makes it safe to be
restarted by someone else. It changes no arithmetic.

DEPLOY.md's core invariant is unchanged and is now *enforced* rather than
documented. Its VM instructions remain valid as a fallback if the restart
measurement comes back badly.

## Non-goals

- **Real-venue execution.** Part B prepares for it and Part A makes it safe to
  host, but the router's `isinstance(adapter, PaperExecutionAdapter)` gate —
  which silently skips the preview phase and drops to `_commit_sequentially`
  for a live leg — is its own spec and its own decision.
- **Horizontal scale, multi-region, HA.** All three are the bug this spec
  exists to prevent.
- **Removing the restart cost entirely.** A warm-start REST sweep on boot would
  make the un-credentialed path viable too. If the measurement in Findings comes
  back in seconds, it is unnecessary; revisit only if it does not.
- **Migrating existing history off the local SQLite file.** `arbys-local.db` is
  known-incomplete for the reasons in the ledger spec. Starting the hosted
  ledger clean is more honest than importing rows nobody can vouch for.
- **A runtime kill switch.** Wanted before live money — an env flag plus a
  restart is a poor way to stop a bot at 2am, and Part A means a botched restart
  refuses to boot rather than trading twice. It belongs with the auto-trader,
  which is where the flag lives.

## Open questions

**One blocking, and it is the first task:** the restart measurement in
Findings. Everything about the platform choice follows from whether a
credentialed cold start refills the book in seconds or minutes. If minutes,
this spec's Part D is wrong and DEPLOY.md's VM stands.

Non-blocking: whether the drain bound of 20s is right. It is a guess, and the
right value is a function of live venue acknowledgement latency, which is
unmeasurable until a real `ExecutionAdapter` exists. Revisit it there rather
than tuning it against paper fills, which are instant and will make any bound
look generous.
