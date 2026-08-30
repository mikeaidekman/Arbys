# Fly.io Hosting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run this on a platform nobody administers, safely restartable by someone else's scheduler, with the single-instance invariant enforced by the system rather than by a paragraph.

**Spec:** [docs/superpowers/specs/2026-08-25-hosting-without-a-server-design.md](../specs/2026-08-25-hosting-without-a-server-design.md)

**Architecture:** One Fly Machine in `iad` with managed Postgres, serving the API *and* the built SPA from one origin. A Postgres advisory lock makes a second instance refuse to boot. A drain on shutdown makes a platform-initiated restart safe. Nothing about detection, sizing or the paper broker changes.

**Tech Stack:** Python 3.14 (matching the dev venv, not the `>=3.11` floor), asyncio, SQLAlchemy 2 async + asyncpg, FastAPI, Docker, Fly.io.

## The spec's blocking question is answered

The spec made everything contingent on one measurement: *does a credentialed cold start refill the quote book in seconds or minutes?* If minutes, Part D was wrong and the VM plan stood.

**Measured 2026-08-29 on the running backend: six Polymarket shards connected at +0s and the first 30-second heartbeat reported `live 100/100 slugs` at +31s.** Seconds, as predicted. Part D stands and this plan proceeds.

## Access is Cloudflare Access — decided 2026-08-30

The spec specified it, the owner confirmed it, and the deciding fact is that
**the domain is already on Cloudflare**: this is an Access application and a
CNAME, not a domain migration or a new dependency.

Tailscale was floated in conversation on 2026-08-29 before this spec was
found, and is now closed off. It would have meant running `tailscaled` inside
the container — a sidecar, which is the thing a "nothing to administer" brief
exists to avoid. Recorded so the option is not reopened by someone reading the
older conversation.

Task 6 implements it. **Verify, do not merely front:** a direct request to the
Fly hostname carries no signed assertion and must be rejected, or the origin is
quietly reachable around Access.

## The hosted instance trades; this machine only watches — decided 2026-08-30

`ARBYS_ENABLE_AUTO_TRADE=1` in `fly.toml`, alongside `ARBYS_ENABLE_INGEST=1`
and `ARBYS_ENABLE_DISCOVERY=1`. Three consequences follow, and the third is a
hazard rather than a detail.

**Watching means opening the hosted URL.** Task 4 serves the SPA from the same
origin as the API, so the terminal and the performance dashboard are just
`https://arbys.<domain>` behind Access. **No local process is involved at
all** — not a backend, not a Vite dev server. That is what makes the split
clean: there is exactly one thing running and it is not on this laptop.

**Counting means a read-only database role.** Every analysis in this project so
far has read the ledger directly rather than through the API, because the
interesting questions (settled-only realized net, fee drag, per-leg staleness,
duplicate fills) are joins the API does not expose. Against hosted Postgres
that wants a **separate read-only role**, not the application's credentials:
ad-hoc analysis should be incapable of writing to a live ledger, and a
half-finished query should be incapable of holding a lock on it.

**The local bot has to stop, and the advisory lock will not stop it.** Task 2's
lock is scoped to a *database*. A backend on this laptop runs against local
SQLite, so it takes a different lock and boots happily — two bots, two
ledgers, one set of venue credentials, and an accounting picture that agrees
with neither. Harmless while both are paper and actively dangerous once either
is not. The cutover is therefore explicit, in Task 7 Step 8, and it is the last
step rather than the first: keep collecting locally until the hosted instance
is proven.

## Prerequisites — what a human has to prepare

Everything below needs an account, a card, or a decision, and none of it can be
done from the repo. Grouped by the task it unblocks, so nothing is set up
earlier than it is needed.

Checked on the dev machine 2026-08-30: `node`/`npm` present; **`docker`,
`flyctl` and `psql` all absent.** Only `flyctl` is genuinely required — see the
note on Docker below.

### Before Task 1 — a throwaway Postgres

Task 1 replays the Alembic chain against real Postgres. It needs a database
you do not care about, not the production one.

- [ ] **Create a Neon account** (neon.tech) and a project in a US-east region.
      Its free tier and instant branching make a disposable database trivial:
      branch, run the test, delete the branch. A local Docker container also
      works — see the Docker note.
- [ ] Copy the connection string and set `ARBYS_TEST_PG_URL`, rewriting the
      scheme to **`postgresql+asyncpg://`**. The `postgres://` form Neon shows
      by default will not work: SQLAlchemy picks the driver from the scheme.
- [ ] Nothing else in the suite reads it, so this is safe to leave set.

**On Docker:** the plan's Task 4 Step 5 suggests building the image locally to
check it. You can skip installing Docker entirely by using
`fly deploy --remote-only`, which builds on Fly's builders. Given this laptop
was at **97% RAM with VS Code and browsers using 6.7 GB**, adding Docker
Desktop is a real cost for a step you can get remotely. The trade is that you
cannot inspect the image before it ships — acceptable here, since Task 4's
tests cover the behaviour that matters.

### Before Task 7 — Fly.io

- [ ] **Create a Fly.io account** and add a payment method. A machine this size
      plus a small volume is single-digit dollars a month, but confirm current
      pricing yourself rather than trusting a figure written here.
- [ ] **Install `flyctl`** — on Windows: `iwr https://fly.io/install.ps1 -useb | iex`
- [ ] `fly auth login`
- [ ] **Pick an app name.** It is globally unique across all of Fly, so `arbys`
      is likely taken. Whatever you choose goes in `fly.toml` and becomes
      `<name>.fly.dev`.
- [ ] `fly apps create <name>` and `fly volumes create arbys_data --size 1 --region iad`.
      The volume is not for storage — it is a *mechanical* single-attach lock
      that makes running two machines impossible rather than merely discouraged.

### Before Task 7 — production Postgres

- [ ] **Decide the provider.** Neon or Supabase pinned to us-east, or Fly's own
      Postgres. Note Fly Postgres is *unmanaged* — you would be administering
      it, which is the thing this whole spec exists to avoid. Neon or Supabase
      fit the brief better.
- [ ] Provision it in a **US-east** region, near the Fly machine in `iad`.
      Cross-region database round trips would sit inside every write path.
- [ ] Have the `postgresql+asyncpg://` connection string ready for
      `fly secrets set`.
- [ ] **Create a second, read-only role** and keep its connection string
      separately. This is the one you use for analysis. The application's
      credentials should never be the ones sitting in a query window: every
      interesting question about this ledger is a read, and nothing about
      answering it should be able to write.

### Before Task 6 — Cloudflare Access

The domain is already on Cloudflare, which is why this is small.

- [ ] **Enable Zero Trust** on the Cloudflare account if it is not already.
      It is free at single-user scale; confirm the current limit.
- [ ] **Choose a hostname**, e.g. `arbys.<your-domain>`.
- [ ] **Create an Access application** for that hostname, with a policy
      allowing your own email and nothing else.
- [ ] Note two values — both go into Fly secrets:
      - the **team domain**, `<team>.cloudflareaccess.com`
      - the application **AUD tag**, from the application's settings
- [ ] Point the hostname at the Fly app with a **CNAME to `<name>.fly.dev`**,
      proxied (orange cloud). Not proxied, Access never sees the request and
      the origin is open.

### Before Task 5 — venue credentials in inline form

Both keys currently live as files on this laptop, which a container has no
equivalent of. Task 5 makes the inline form work; you need the material.

- [ ] **Kalshi:** `KALSHI_API_KEY_ID`, plus the **contents** of the `.pem` at
      `KALSHI_PRIVATE_KEY_PATH`. It is multi-line, which is exactly what secret
      stores mangle — Task 5 normalises a flattened `
` form, so either
      survives, but paste it carefully.
- [ ] **Polymarket US:** `POLYMARKET_US_API_KEY_ID`, plus the base64 secret
      from the file at `POLYMARKET_US_PRIVATE_KEY_PATH`. Single-line, so it
      travels unchanged.
- [ ] **Keep both out of the repo and out of `fly.toml`.** They go in
      `fly secrets set`, never in a file that is committed. `.dockerignore`
      from Task 4 keeps `*.pem` out of the build context as a second line.

### Decisions worth making before you start

- [ ] **Does the hosted ledger start clean?** The plan assumes yes — see *What
      this plan deliberately leaves undone*. `arbys-local.db` stays on the
      laptop.
- [ ] **Region.** `iad` is the spec's choice. Both venues are US-based, so
      us-east is right unless you know otherwise.

## Global Constraints

- **All money and all prices are `Decimal`. Never float.**
- Run everything from the repo root with `venv\Scripts\python.exe`.
- `venv\Scripts\python.exe -m pytest -q` must stay green — **386 tests** before this plan.
- `venv\Scripts\python.exe -m ruff check .` must stay clean.
- **The test suite must never require a Postgres server.** Every Postgres-specific path is dialect-gated, exactly as the WAL pragmas in `db/session.py` already are. Getting this wrong makes the suite unrunnable on a laptop, which is worse than the bug it would catch.
- **Local dev stays on SQLite and stays working unchanged.** `ARBYS_DB_URL` defaults to `./arbys-local.db` and that must not change.
- Do not touch the developer's `arbys-local.db`.
- **Nothing in this plan enables live execution.** Hosting a paper trader is the goal; `ARBYS_ENABLE_LIVE_EXECUTION` is not part of it.

## File Structure

**Created:**

| File | Responsibility |
| --- | --- |
| `Dockerfile` | Multi-stage: build the SPA, then the Python image. |
| `.dockerignore` | Keep `.env`, `*.pem`, `*.db`, `venv/` out of the build context. |
| `fly.toml` | One machine, no autostop, a volume as a mechanical lock. |
| `arbys/backend/access.py` | Cloudflare Access JWT verification as a FastAPI dependency. |
| `tests/db/test_singleton_lock.py` | The lock's dialect gate and its refusal path. |
| `tests/test_spa_serving.py` | SPA fallback, and that it does not shadow the API. |
| `tests/test_access_auth.py` | Assertion required, `/health` exempt. |

**Modified:**

| File | Change |
| --- | --- |
| `arbys/db/session.py` | `acquire_singleton_lock()`, dialect-gated. |
| `arbys/backend/state.py` | Take the lock in `bootstrap()`; `_draining`; drain in `shutdown()`; fail-loud credential loading. |
| `arbys/backend/ticket_service.py` | Refuse while draining, with a distinct reason. |
| `arbys/backend/app.py` | Mount the SPA after the API; `/health` reports adapter mode; wire the Access dependency. |
| `arbys/adapters/kalshi_ws.py`, `arbys/adapters/polymarket_us_auth.py` | Accept the key inline; normalise flattened PEMs; distinguish absent from broken. |
| `requirements.txt` | **Delete.** Flagged in two documents; the fix is to stop it existing. |

---

### Task 1: Prove the migration chain applies to a real Postgres

**Do this first, and do not skip it.** `bootstrap()` builds the schema with `create_all()` and dev has therefore **never run a migration**. The Alembic chain is exercised only by `tests/db/test_migrations_match_models.py`, which replays it against SQLite. A hosted deploy is the first time it runs against Postgres, on a database that matters.

**Files:**
- Test: `tests/db/test_migrations_postgres.py` (skipped unless `ARBYS_TEST_PG_URL` is set)

- [ ] **Step 1: Write the test** — `alembic upgrade head` from empty against `ARBYS_TEST_PG_URL`, then diff against `Base.metadata`, mirroring the SQLite version. `pytest.skip` when the variable is unset so the default suite is unaffected.

- [ ] **Step 2: Run it against a throwaway Postgres** — a local container, or a free Neon branch. Expect failures: `BigInteger().with_variant(Integer(), "sqlite")`, `JSON` vs `JSONB`, and `NUMERIC(28,12)` are the three most likely to differ.

- [ ] **Step 3: Fix what it finds**, in the migrations, not in `models.py`. A migration describes the change *it* made and is frozen at that point in history.

- [ ] **Step 4: Green locally (skipped) and against Postgres (passing). Commit.**

---

### Task 2: Single-instance advisory lock

Two instances both streaming both venues and both auto-trading is the bug this whole spec exists to prevent. Today nothing enforces it.

**Files:**
- Modify: `arbys/db/session.py`, `arbys/backend/state.py`
- Test: `tests/db/test_singleton_lock.py`

- [ ] **Step 1: Write the failing tests**

```python
async def test_the_lock_is_a_no_op_on_sqlite():
    """The suite must never require a Postgres server. This is the assertion
    that keeps it runnable on a laptop."""
    assert await acquire_singleton_lock() is True


@pytest.mark.skipif(not os.environ.get("ARBYS_TEST_PG_URL"), reason="needs Postgres")
async def test_a_second_process_refuses_to_boot():
    """Replaces a paragraph of documentation with a guarantee."""


@pytest.mark.skipif(not os.environ.get("ARBYS_TEST_PG_URL"), reason="needs Postgres")
async def test_the_lock_survives_a_pool_recycle():
    """The lock connection is held OUTSIDE the pool. A pool recycle dropping
    it silently is the exact failure this part exists to remove."""
```

- [ ] **Step 2: Run and watch them fail.**

- [ ] **Step 3: Implement.** `pg_try_advisory_lock` — never the blocking form, which turns a misconfiguration into a hang that reads as a slow boot. Session-scoped on a **dedicated connection held outside the pool**. Gate on `dialect.name == "postgresql"`, beside the existing WAL-pragma gate.

- [ ] **Step 4: `bootstrap()` refuses to start when the lock is held**, naming the invariant in the error so a platform health check surfaces it. Green, ruff, commit.

---

### Task 3: A shutdown that drains

**Files:**
- Modify: `arbys/backend/state.py`, `arbys/backend/ticket_service.py`
- Test: `tests/test_backend_e2e.py`, `tests/test_ticket_service.py`

- [ ] **Step 1: Write the failing tests** — `submit_arb_ticket` refuses while `_draining` with a distinct reason (so drained attempts are legible in the ticket log rather than looking like vanished edges); shutdown awaits an in-flight ticket to terminal state; shutdown gives up at the bound rather than hanging.

- [ ] **Step 2: Run and watch them fail.**

- [ ] **Step 3: Implement** in the spec's order: set `_draining`, stop the auto-trader first so nothing new arrives, await in-flight tickets bounded at 20s, then the existing teardown.

  Note the existing `AutoTradeService.stop()` already waits for an in-flight `handle()` rather than cancelling it, for closely related reasons — reuse that discipline rather than inventing a second one.

- [ ] **Step 4: Green, ruff, commit.**

---

### Task 4: One artifact — serve the SPA from FastAPI

**Files:**
- Modify: `arbys/backend/app.py`
- Create: `Dockerfile`, `.dockerignore`
- Delete: `requirements.txt`
- Test: `tests/test_spa_serving.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_a_client_route_returns_index_html():
    """/account is a client-side route; it must not 404."""


def test_an_unknown_api_shaped_path_still_404s():
    """The regression that catches a catch-all mounted AHEAD of the API
    routes — which would swallow every endpoint and return index.html."""


def test_missing_dist_does_not_break_the_api():
    """frontend/dist is gitignored, so a dev who has never run `npm run build`
    must still get a working API rather than a boot failure."""
```

- [ ] **Step 2: Run and watch them fail.**

- [ ] **Step 3: Mount the SPA after every API route**, with an SPA fallback. Guard on `dist/` existing.

- [ ] **Step 4: Write the Dockerfile** exactly as the spec gives it — `node:22-slim` build stage, `python:3.14-slim` runtime, `pip install -e .` (not `requirements.txt`), `--workers 1` stated explicitly. `.dockerignore` lists `.env`, `*.pem`, `*.db`, `venv/`: `.env` being gitignored protects git, not a local build context.

- [ ] **Step 5: Build the image locally and run it against SQLite.** Assert the API answers, the SPA loads, and `requirements.txt` is absent from the image. Commit.

---

### Task 5: Secrets that work in a container

**Files:**
- Modify: `arbys/adapters/kalshi_ws.py`, `arbys/adapters/polymarket_us_auth.py`, `arbys/backend/state.py`, `arbys/backend/app.py`
- Test: `tests/adapters/test_credentials.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_inline_key_takes_precedence_over_path():
    """Both set -> inline wins. The _PATH form must keep working alone, or
    local dev regresses."""


def test_a_flattened_pem_still_loads():
    r"""Kalshi's key is a multi-line PEM and multi-line values are the classic
    thing a secret store mangles. A key arriving with literal \n sequences
    must load rather than failing as 'malformed'."""


def test_id_set_with_an_unloadable_key_fails_the_boot():
    """THE test of this task. Today absent and broken share a code path: both
    log and return None, and the factory quietly builds the REST adapter. But
    the entire hosting argument rests on the credentialed WebSocket path
    having cheap restarts — so a broken key silently dissolves the premise
    while the app still reports healthy."""


def test_neither_set_still_falls_back_to_rest():
    """The documented no-KYC path. Absent is a configuration, not a bug."""
```

- [ ] **Step 2: Run and watch them fail.**

- [ ] **Step 3: Implement.** `KALSHI_PRIVATE_KEY` / `POLYMARKET_US_PRIVATE_KEY` inline, taking precedence over `_PATH`. Normalise literal `\n` before parsing. Split absent from broken: `_ID` set with an unloadable key raises at boot; neither set falls back exactly as today.

- [ ] **Step 4: `/health` reports the resolved adapter mode per venue** (`websocket` / `rest`), so "am I on the fast path?" is answerable from outside the box — which is most of the point of hosting it. Green, ruff, commit.

---

### Task 6: Cloudflare Access

**Files:**
- Create: `arbys/backend/access.py`, `tests/test_access_auth.py`
- Modify: `arbys/backend/app.py`, `arbys/backend/state.py`

- [ ] **Step 1: Write the failing tests** — a request with no `Cf-Access-Jwt-Assertion` is rejected; one with a validly-signed assertion for the configured `aud` passes; a valid signature for the **wrong `aud`** is rejected; `/health` is reachable without any assertion so the platform can probe it. Mint test JWTs locally against a throwaway key; never contact Cloudflare from a test.

- [ ] **Step 2: Run and watch them fail.**

- [ ] **Step 3: Implement** as one FastAPI dependency validating against the team's public keys and checking `aud`. **Verify, don't merely front:** a direct request to the Fly hostname carries no signed assertion and must be rejected, so the origin is not quietly reachable around Access.

- [ ] **Step 4: Off when unconfigured.** With `CF_ACCESS_TEAM_DOMAIN` unset the dependency is a no-op, so local dev and the suite are unaffected. Green, ruff, commit.

---

### Task 7: Deploy

- [ ] **Step 1: Write `fly.toml`** exactly as the spec gives it: `auto_stop_machines = false` (scale-to-zero has nothing to restart the venue websockets from), `min_machines_running = 1`, a `[[mounts]]` volume as a *mechanical* single-attach lock, `kill_timeout = 60` for Task 3's drain.

- [ ] **Step 2: Provision Postgres** (Neon, Supabase or Fly's own) in `us-east`. Set `ARBYS_DB_URL` with the `postgresql+asyncpg://` scheme.

- [ ] **Step 3: `alembic upgrade head` against it** — the first time this has ever run for real. Task 1 is what makes this safe.

- [ ] **Step 4: Set secrets** per the spec's table. The `ARBYS_*` feature flags go in `fly.toml` as plain env, visible in review rather than hidden in a store. `ARBYS_ENABLE_INGEST=1` and `ARBYS_ENABLE_DISCOVERY=1` are the two without which a hosted instance does nothing at all.

- [ ] **Step 5: Deploy with `--strategy immediate`.** The default rolling strategy creates a second Machine before retiring the first, which is the overlap window Task 2 exists to refuse. **Read `fly status` mid-deploy and confirm the machine count never exceeds one** — Task 2 turns a mistake here into a refused boot rather than a double-traded edge, but the deploy should be correct on its own.

- [ ] **Step 6: Verify against the running instance.** `/health` reports `websocket` for both venues; the SPA loads through Access; a direct hit on the Fly hostname is rejected; clock skew is under the 30s Polymarket signing tolerance (`scripts/verify_polymarket_us_creds.py` reports it, and a skewed clock fails every request in a way that looks exactly like a bad key).

- [ ] **Step 7: Watch one deploy cycle end to end.** Confirm the drain runs, the lock releases, and the book refills — the +31s measurement should reproduce on the platform. If it does not, that is the spec's Findings premise failing on real infrastructure and the VM fallback is still valid.

- [ ] **Step 8: Cut over — stop the local bot.** Set `ARBYS_ENABLE_AUTO_TRADE=0` in this machine's `.env` and stop the local backend. Until this step there are two bots on one set of venue credentials, writing two ledgers that agree with each other about nothing; after it there is one. Do it **last**, once the hosted instance has been observed filling tickets, so a failed deploy does not leave nothing running.

- [ ] **Step 9: Confirm the split holds.** The hosted `/health` reports `websocket` for both venues and its ticket count is climbing; nothing on this laptop is listening on `:8000`. Watching is now a browser tab, and counting is the read-only role.

---

### Task 8: Documentation

- [ ] `CLAUDE.md`: the lock, the drain, inline secrets, the SPA mount, and that dev is unchanged on SQLite.
- [ ] `docs/RUNBOOK.md`: deploy, roll back, rotate a key, read `/health`.
- [ ] `docs/DEPLOY.md`: mark superseded, keep the VM instructions as the documented fallback.
- [ ] Mark the spec implemented; record the +31s measurement in its Findings, replacing the "measure it before building on it" instruction.

---

## What this plan deliberately leaves undone

**Real-venue execution.** Hosting a paper trader is the goal. The [legging-safety plan](2026-08-30-legging-safety.md) is a separate track and neither depends on the other.

**Migrating the existing SQLite history.** `arbys-local.db` is known-incomplete for the reasons in the ledger spec, and `arbys-2026-08-28-pre-reset.db` is an archive. Starting the hosted ledger clean is more honest than importing rows nobody can vouch for.

**A runtime kill switch.** Wanted before live money, but an env flag plus a restart is a poor way to stop a bot at 2am. It belongs with the auto-trader.
