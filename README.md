# Arbys

Prediction market arbitrage scanner with paper trading. Surfaces guaranteed-profit
opportunities across Polymarket, Kalshi, and DraftKings, and lets you validate
them with a paper broker that fills against real live odds.

## Status: v0.1 — working end-to-end backend slice ✅

Full read-scan-detect-paper-execute loop is implemented and tested (51 tests
green, ruff clean):

* **Shared arb math** — types, odds converters, per-venue fee models,
  cross-venue and complementary-set detectors, stake sizing.
* **Adapters** — Polymarket, Kalshi, DraftKings market-data adapters (polling
  today; WS in a follow-up). Mocked via `httpx.MockTransport` in tests.
* **Ingest worker + engine runtime** — asyncio quote pump, event-driven arb
  detection triggered only on affected event groups.
* **Paper broker** — full `ExecutionAdapter` implementation with slippage,
  fees, position tracking, settlement/PnL. `ExecutionRouter` enforces
  all-or-nothing multi-leg tickets.
* **FastAPI backend** — REST endpoints for event groups, quotes,
  opportunities, paper accounts, and paper-execute; live WebSocket for
  opportunities.
* **Backtest harness** — replay any quote sequence and optionally auto-paper-
  execute detected opportunities.
* **Persistence layer** — SQLAlchemy 2 models + Alembic initial migration
  covering venues, markets, outcomes, event groups, quotes, opportunities,
  and full paper-trading tables. Docker Compose for local Postgres.
* **Observability** — structlog setup util.

Remaining work is the React frontend, wiring the persistence layer into the
running backend (currently all in-memory), and operator docs.

## Layout

```
arbys/
  shared/     # Pure domain: types, odds, fees, arb_engine, sizing (no I/O)
  adapters/   # Venue integrations (MarketData + Execution ABCs)
  ingest/     # Async workers writing normalized quotes to Postgres
  backend/    # FastAPI app
  db/         # SQLAlchemy models + Alembic migrations (TBD)
tests/
  shared/     # Unit tests for arb math
```

## Setup

```bash
python -m venv venv
venv\Scripts\activate            # Windows
source venv/bin/activate         # macOS/Linux
pip install -e ".[dev]"
```

## Run tests

```bash
pytest
```

## Roadmap

See the full plan in `~/.copilot/session-state/<session-id>/plan.md`. Progress:

1. **Phase 0 — Foundations** ✅
2. **Phase 1 — Arb math** ✅
3. **Phase 2 — Adapters** ✅ (polling; WS streams TBD)
4. **Phase 3 — Event-group mapping + engine runtime** ✅
5. **Phase 4 — Paper trading** ✅ (in-memory; DB persistence TBD)
6. **Phase 5 — API + frontend** — backend done; React frontend TBD
7. **Phase 6 — Hardening** — backtest harness ✅, observability ✅, operator docs TBD

## Running locally

```bash
# Postgres (only needed once you wire the persistence layer in)
docker compose up -d postgres
alembic upgrade head

# Backend
uvicorn arbys.backend.app:app --reload
# → http://127.0.0.1:8000/docs
```

Push a mock quote (no real venues needed):

```bash
curl -X POST localhost:8000/event-groups -H "content-type: application/json" -d '{
  "id":"eg1","title":"demo",
  "legs":[
    {"outcome_id":"y","venue_id":"polymarket","is_yes_side":true},
    {"outcome_id":"n","venue_id":"kalshi","is_yes_side":false}
  ]
}'
curl -X POST localhost:8000/quotes -H "content-type: application/json" -d '{"outcome_id":"y","bid":"0.40","ask":"0.40"}'
curl -X POST localhost:8000/quotes -H "content-type: application/json" -d '{"outcome_id":"n","bid":"0.50","ask":"0.50"}'
curl localhost:8000/opportunities
curl -X POST localhost:8000/paper/execute -H "content-type: application/json" -d '{"opportunity_index":0}'
curl localhost:8000/paper/default
```
