# Deploying Arbys

24/7 hosting on Cloudflare + a single small VM, with git auto-deploy for the
frontend and gated deploys for the backend.

Companion to [RUNBOOK.md](RUNBOOK.md), which covers day-to-day operation
(adding event groups, adding a venue, resetting data). This document covers
getting the thing running somewhere that isn't your laptop.

---

## The invariant that shapes everything

**Run exactly one backend instance. Ever.**

`AppState.__init__` ([arbys/backend/state.py:147](../arbys/backend/state.py))
holds the `QuoteBook`, the three `paper_brokers`, `_opps_by_group`, and the WS
subscriber queues in ordinary process memory. Nothing is shared out-of-process.
Two instances means:

- `ARBYS_MAX_OUTCOME_STAKE` is enforced against in-memory broker state, so the
  per-game capital cap silently doubles.
- Both instances detect the same edge and both execute it.

This is a correctness bug, not a scaling inefficiency. Consequences:

| Don't | Because |
|---|---|
| `uvicorn --workers 2` (or any value >1) | Each worker is a full independent `AppState` |
| Autoscaling / min-instances >1 | Same |
| Rolling or blue-green deploys | The overlap window *is* two live instances |
| Serverless (Workers, Lambda, Cloud Run scale-to-zero) | Persistent venue WS + monotonic-clock state has no request to hang off |

Deploys must be **stop, then start**. Accept the brief downtime.

### Restarts are not free

`QuoteBook` stamps arrival on `time.monotonic`
([arbys/shared/quotebook.py:42](../arbys/shared/quotebook.py)), so a fresh
process starts with an empty book. Venues push only on change, and observed
median quote age is 50–90s — so after a restart the terminal is partially blank
for up to a minute or two while quotes refill.

Optimize for **fewer** restarts, not faster ones. This is why we don't
auto-deploy the backend.

---

## Topology

```mermaid
flowchart LR
  U[Browser] -->|HTTPS| CF[Cloudflare Access<br/>SSO gate]
  CF --> W[Worker<br/>static assets + router]
  W -->|"/ (SPA)"| A[(Static assets)]
  W -->|"/api/*, /ws/*<br/>strip prefix"| T[Cloudflare Tunnel]
  T --> V["VM (us-east)"]
  subgraph V
    U2[uvicorn :8000<br/>1 worker] --> PG[(Postgres)]
  end
  U2 -.->|outbound WS| K[Kalshi]
  U2 -.->|outbound WS| P[Polymarket]
```

**Why a Worker rather than plain Pages:** the frontend calls relative,
same-origin paths — `BASE = "/api"` at
[client.ts:10](../frontend/src/api/client.ts) and `window.location.host` at
[useOpportunityStream.ts:28](../frontend/src/hooks/useOpportunityStream.ts).
Keeping one hostname means **zero frontend code changes and no CORS** (the
backend has no CORS middleware and no auth of its own). A Worker serving static
assets can also do the `/api` prefix strip in five lines you control.

**Put the VM in us-east.** You're latency-sensitive against both venues and
that's where their infrastructure lives.

> **Simpler alternative:** mount `frontend/dist` on the FastAPI app with
> `StaticFiles` and tunnel the whole thing — one origin, no Worker, no Pages.
> You trade the frontend's git auto-deploy for one less moving part. Reasonable
> if the Worker routing turns into a fight.

---

## Part 1 — The box

Any always-on VM works. Hetzner CX22 or a DigitalOcean basic droplet is plenty
— this is one Python process and a Postgres.

```bash
# Debian 12 / Ubuntu 24.04
sudo apt-get update
sudo apt-get install -y build-essential git curl postgresql
```

### Python 3.14

**Match the dev venv, which is 3.14.3.** `pyproject.toml` claims
`requires-python = ">=3.11"`, but 3.11 is not what the test suite has been run
against. Use `uv` or `pyenv` rather than the distro Python:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.14
```

---

## Part 2 — Postgres

The repo's [docker-compose.yml](../docker-compose.yml) defines Postgres for
*local dev only* — the app itself is not containerized. On the VM, either run
Postgres natively or point at a managed instance.

```bash
sudo -u postgres psql <<'SQL'
CREATE USER arbys WITH PASSWORD 'CHANGE_ME';
CREATE DATABASE arbys OWNER arbys;
SQL
```

The SQLAlchemy async URL needs the `asyncpg` driver in the scheme:

```
ARBYS_DB_URL=postgresql+asyncpg://arbys:CHANGE_ME@localhost:5432/arbys
```

`asyncpg` and `psycopg[binary]` are both already declared in
`pyproject.toml`, so no extra install step.

---

## Part 3 — Install the app

```bash
sudo useradd -r -m -d /opt/arbys -s /bin/bash arbys
sudo -u arbys git clone https://github.com/mikeaidekman/Arbys.git /opt/arbys/app
cd /opt/arbys/app
sudo -u arbys uv venv --python 3.14
sudo -u arbys uv pip install -e .
```

> ### ⚠️ Do not `pip install -r requirements.txt`
>
> That file lists `pandas`, `numpy`, `requests`, `python-dotenv` — leftovers
> from an earlier incarnation of this project. It does **not** contain FastAPI,
> uvicorn, SQLAlchemy, asyncpg, httpx, websockets, or cryptography. A deploy
> built from it fails at first import.
>
> `pyproject.toml` is the real dependency list; install with `pip install -e .`.
> Worth deleting or regenerating `requirements.txt` so nobody trips on this
> again.

### Migrations

Alembic is the source of truth in prod (`bootstrap()` calling `create_all()` is
a dev convenience). Current head is `0004_event_group_source`.

```bash
sudo -u arbys /opt/arbys/app/.venv/bin/alembic upgrade head
```

Migrations must never build DDL from `Base.metadata` — see the note in
[CLAUDE.md](../CLAUDE.md) about why `0001` was rewritten, and
`tests/db/test_migrations_match_models.py` which now guards it.

### Secrets

`load_dotenv()` runs at [app.py:13](../arbys/backend/app.py), so a `.env` file
beside the app works — but prefer a systemd `EnvironmentFile` outside the git
checkout so a `git clean` can't take it out:

```bash
sudo install -d -m 0750 -o arbys -g arbys /etc/arbys
sudo -u arbys tee /etc/arbys/env >/dev/null <<'ENV'
ARBYS_DB_URL=postgresql+asyncpg://arbys:CHANGE_ME@localhost:5432/arbys
ARBYS_ENABLE_INGEST=1
ARBYS_ENABLE_POLYMARKET=1
ARBYS_ENABLE_KALSHI=1
ARBYS_ENABLE_DRAFTKINGS=0
ARBYS_ENABLE_DISCOVERY=1
ARBYS_DISCOVERY_INTERVAL_S=60
ARBYS_MAX_OUTCOME_STAKE=500
ARBYS_QUOTE_MAX_AGE_S=600
KALSHI_API_KEY_ID=...
KALSHI_PRIVATE_KEY_PATH=/etc/arbys/kalshi.pem
ENV
sudo chmod 0640 /etc/arbys/env
```

Copy the Kalshi `.pem` to `/etc/arbys/kalshi.pem` (mode `0400`, owner `arbys`).
**Keep it out of the repo**, same rule as locally.

---

## Part 4 — systemd

```ini
# /etc/systemd/system/arbys.service
[Unit]
Description=Arbys arbitrage scanner
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=exec
User=arbys
WorkingDirectory=/opt/arbys/app
EnvironmentFile=/etc/arbys/env
ExecStart=/opt/arbys/app/.venv/bin/uvicorn arbys.backend.app:app \
  --host 127.0.0.1 --port 8000 --workers 1
Restart=always
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

Three deliberate choices:

- **No `--reload`.** Every file save would restart the process and empty the
  QuoteBook.
- **`--workers 1`, explicitly.** It's the default, but state it so nobody
  "optimizes" it later. See the invariant above.
- **`WorkingDirectory=/opt/arbys/app`.** `ARBYS_DB_URL` has a relative SQLite
  default; starting from elsewhere with a missing env var silently creates a
  second empty database rather than failing.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now arbys
sudo journalctl -u arbys -f
```

---

## Part 5 — Cloudflare Tunnel

No inbound ports, no public IP, no firewall rules. The backend stays private.

```bash
curl -L https://pkg.cloudflare.com/cloudflared-linux-amd64.deb -o cfd.deb
sudo dpkg -i cfd.deb
cloudflared tunnel login
cloudflared tunnel create arbys
```

```yaml
# /etc/cloudflared/config.yml
tunnel: <TUNNEL-UUID>
credentials-file: /root/.cloudflared/<TUNNEL-UUID>.json

ingress:
  - hostname: arbys-origin.example.com
    service: http://127.0.0.1:8000
  - service: http_status:404
```

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

`arbys-origin.example.com` is an internal name the Worker talks to — not the
hostname you browse. Lock it down with an Access policy too (Part 6) so it
isn't reachable directly.

---

## Part 6 — Cloudflare Access (do not skip)

**The app has no authentication.** There is no CORS middleware, no bearer
token, and no API-key dependency anywhere in [arbys/backend/](../arbys/backend/),
and `/admin` can execute trades. Today that's paper money; the stated goal is
live trading.

Access puts SSO in front without you writing a login:

1. Zero Trust → Access → Applications → **Add a self-hosted application**
2. Domain: your public hostname (e.g. `arbys.example.com`)
3. Policy: **Allow**, rule `Emails` → your address
4. Add a second application for `arbys-origin.example.com` with the same policy

For the GitHub Action to reach the origin later, create a **service token** and
add a policy rule accepting it — don't widen the human policy.

---

## Part 7 — Frontend on Cloudflare (auto-deploy)

A Worker with a static-assets binding serves the SPA and routes API traffic.

```jsonc
// wrangler.jsonc  (repo root)
{
  "name": "arbys",
  "main": "worker/index.ts",
  "compatibility_date": "2026-01-01",
  "assets": { "directory": "frontend/dist", "binding": "ASSETS" },
  "vars": { "ORIGIN": "https://arbys-origin.example.com" }
}
```

```ts
// worker/index.ts
export default {
  async fetch(req: Request, env: any): Promise<Response> {
    const url = new URL(req.url);

    // /api/monitored -> ORIGIN/monitored   (mirrors the dev proxy's rewrite)
    if (url.pathname.startsWith("/api/")) {
      const target = new URL(env.ORIGIN);
      target.pathname = url.pathname.slice(4);
      target.search = url.search;
      return fetch(new Request(target, req));
    }

    // /ws/* passes through unchanged, Upgrade header intact
    if (url.pathname.startsWith("/ws/")) {
      const target = new URL(env.ORIGIN);
      target.pathname = url.pathname;
      return fetch(new Request(target, req));
    }

    return env.ASSETS.fetch(req); // SPA
  },
};
```

**The `/api` strip is the detail people miss.** `server.proxy` in
[vite.config.ts](../frontend/vite.config.ts) rewrites `/api/x` → `/x` before
proxying, and that block is **dev-server only** — it does not exist in a built
bundle. Without the equivalent strip in the Worker, every call 404s.

Wire git auto-deploy by connecting the repo in the Cloudflare dashboard
(Workers → Builds), with build command `cd frontend && npm ci && npm run build`.
Push to `main` deploys the frontend. That's safe — it never restarts the
backend.

---

## Part 8 — Backend deploy, gated

Auto-deploying the backend on every push is wrong for this app: each deploy
empties the QuoteBook, and once it's trading live, a deploy landing between two
legs of an open arb is exactly the legging risk the project exists to avoid.

Trigger on a **tag**, not on `main`:

```yaml
# .github/workflows/deploy-backend.yml
name: deploy-backend
on:
  push:
    tags: ["deploy-*"]
jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: production   # add a required reviewer here
    steps:
      - uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.VM_HOST }}
          username: arbys
          key: ${{ secrets.VM_SSH_KEY }}
          script: |
            set -euo pipefail
            cd /opt/arbys/app
            git fetch --tags origin
            git checkout ${{ github.ref_name }}
            uv pip install -e .
            .venv/bin/alembic upgrade head
            sudo systemctl restart arbys
```

Deploy with `git tag deploy-$(date +%Y%m%d-%H%M) && git push --tags`.

Run the green-build bar before tagging — 310 tests and `ruff` must be clean.
(`mypy` is *not* part of the bar; it has known errors. See CLAUDE.md.)

---

## Part 9 — Fix the health check first

`/health` at [app.py:104-106](../arbys/backend/app.py) returns
`{"status": "ok", "dropped_writes": ..., "last_dropped_write": ...}` — the
trustworthy-ledger branch added the write-reliability counters, so a
supervisor can now tell whether persistence is silently losing rows. It still
unconditionally reports `"status": "ok"` whenever the process is alive, so it
still misses the actual observed failure mode: **on both restarts on
2026-08-10, both venue websockets timed out on the opening handshake while
uvicorn happily served 200s.** The retry recovered, but a supervisor watching
this endpoint would never notice a feed that stayed down.

Behind a tunnel this is even harder to spot. Make `/health` assert quote
freshness — the app already computes `is_stale` per leg, so "every leg stale"
is a ready-made unhealthy signal — then point a Cloudflare health check or an
uptime monitor at it.

This is the change that makes "24/7" mean the app is *working*, not merely
*running*.

---

## Operational notes

**Cloudflare drops idle WebSockets.** `/ws/opportunities` only pushes when the
opportunity set changes, so it will sit idle and get closed. The frontend
auto-reconnects with exponential backoff
([useOpportunityStream.ts:38-41](../frontend/src/hooks/useOpportunityStream.ts)),
so it self-heals — but silently. Confirm a reconnect re-fetches state rather
than assuming it resumed, and consider a server-side keepalive ping.

**Venue geography.** Before moving real money through a hosted box, check each
venue's stance on datacenter IPs and jurisdiction. Paper trading won't surface
a restriction that live trading would.

**Backups.** `arbys-local.db` is ~177 MB locally; the Postgres equivalent holds
your fill history and PnL snapshots. `pg_dump` on a cron to R2.

**What green outlines mean.** Unchanged by hosting, but worth remembering while
staring at a remote instance: card outlines and the nav's arb count are **gross
of fees**; the buy button requires a net-of-fees engine opportunity. Outline
without a button is expected behavior, not a deployment problem.

---

## Checklist

- [ ] VM in us-east, Python 3.14 via `uv`
- [ ] Postgres created, `ARBYS_DB_URL` uses `postgresql+asyncpg://`
- [ ] Installed with `pip install -e .` — **not** `requirements.txt`
- [ ] `alembic upgrade head` → `0004_event_group_source`
- [ ] Kalshi `.pem` at `/etc/arbys/kalshi.pem`, mode `0400`, outside the repo
- [ ] systemd unit: no `--reload`, `--workers 1`, `Restart=always`
- [ ] Tunnel up, backend not reachable on any public port
- [ ] **Access policy on both the public and origin hostnames**
- [ ] Worker deployed, `/api` prefix strip verified against `/api/monitored`
- [ ] WebSocket verified end-to-end (`/ws/opportunities` accepts and survives idle)
- [ ] Backend deploys gated on tag + reviewer
- [ ] `/health` reports feed staleness, monitor pointed at it
- [ ] `pg_dump` cron to R2
