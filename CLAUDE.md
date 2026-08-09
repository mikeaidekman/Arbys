# Arbys — working notes for Claude

Prediction-market arbitrage scanner + paper trading. Detects guaranteed-profit
opportunities across Polymarket / Kalshi / DraftKings and validates them with a
paper broker that fills against real live odds.

Python 3.11+ FastAPI backend (`arbys/`), Vite + React 19 + TS frontend
(`frontend/`), SQLAlchemy 2 async persistence (SQLite for dev, Postgres for real).

## Scope

This is a **standalone personal project**, fully separate from any work
codebase on this machine. Nothing here shares conventions, issue tracking,
credentials, or git identity with anything under `~\Projects\`. Don't import
practices from other repos on this box, don't file issues to any work project
board, and don't reconcile this repo's git identity with the global config —
the repo-local identity is intentional (see **Repo facts**).

## Commands

Run everything from the repo root with the venv Python — `venv\Scripts\python.exe`
— rather than a bare `python`.

```powershell
venv\Scripts\python.exe -m pytest -q            # 128 tests, must stay green
venv\Scripts\python.exe -m ruff check .         # must stay clean
venv\Scripts\python.exe -m mypy arbys           # see caveat below — NOT clean today
```

```powershell
cd frontend
npm run lint      # oxlint (not eslint)
npm run build     # tsc -b && vite build — the real typecheck for frontend code
npm run dev       # :5173, proxies /api and /ws to :8000
```

Backend: `uvicorn arbys.backend.app:app --reload` → http://127.0.0.1:8000/docs

Full operational detail — adding event groups, adding a venue, resetting data,
troubleshooting table — lives in [docs/RUNBOOK.md](docs/RUNBOOK.md). Read it
before changing ingest, discovery, or the paper broker.

### mypy caveat

`pyproject.toml` sets `strict = true`, but the codebase currently has **47 errors
across 17 files** and mypy is not part of the green-build bar. Do not claim
"mypy clean", and do not embark on a 47-error cleanup as a side effect of an
unrelated task. Adding annotations to code you're already touching is welcome.

Known false positive: `arbys/backend/state.py:191-194` reports `PaperBalance has
no attribute outcome_id/qty/...`. That's mypy binding the loop variable `row`
from the earlier `balances` loop; the runtime types are correct.

## Architecture

Layers, strictly inward-depending:

- `arbys/shared/` — **pure domain, no I/O and no framework imports.** Types,
  odds conversion, per-venue fee models, `arb_engine` detectors, stake sizing,
  `paper_broker`, `execution_router`. Safe to import anywhere. Keep it that way:
  no `httpx`, no SQLAlchemy, no FastAPI in here.
- `arbys/adapters/` — venue integrations behind the `MarketDataAdapter` /
  `ExecutionAdapter` ABCs in `base.py`. WS-first with REST-poll fallback.
- `arbys/discovery/` — scans Kalshi + Polymarket for the same real-world game
  and auto-registers cross-venue event groups. Matching is by
  `(sport, game_date, unordered team pair)`, then split into date clusters so
  a team pair meeting on consecutive days (MLB series) yields one group per
  game. Team sports share one path: `fetch_kalshi_team_games` +
  `SERIES_TICKERS` on the Kalshi side, `/events?tag_slug=<league>` +
  `SPORT_TAG_SLUGS` on the Polymarket side. Adding a league means adding a
  team table, a series ticker, and a tag slug.

  Two traps, both of which silently returned zero groups rather than erroring:
  Kalshi sends a **bare city** (`"Atlanta"`) except where a city fields two
  teams (`"New York Y"`), and Polymarket's flat `/markets` endpoint **caps at
  100 rows** ordered by 24h volume, where league games never outrank politics.
  NBA is wired but **unverified** — `KXNBAGAME` had no open events in the
  offseason, so recheck its codes and title format when the season starts.

  **Neither venue publishes a live score or game clock** (checked 2026-08-08).
  Kalshi has no score field; its `settlement_sources` just point at ESPN.
  Polymarket's payload does contain the words `score` and `inning`, but only in
  prose — a prop market's question, a link to mlb.com/scores, a narrative
  blurb, and `teams[].record`, which is the *season* record, not the game
  score. Both do report an exact start (`occurrence_datetime` /
  `gameStartTime`), which is what `event_group.start_time` and the card
  countdown use. Live scores would need a third-party feed; decided against
  in favour of the countdown alone.
- `arbys/ingest/` — async services: quote `worker`, `engine_runtime` (arb
  detection, triggered only on affected event groups), `pnl_service`,
  `auto_settle_service`.
- `arbys/backend/` — FastAPI app + `AppState`. `state.py` is the wiring hub:
  fee registry, adapter factories, broker construction, bootstrap/hydration.
- `arbys/db/` — SQLAlchemy models, repos, Alembic migrations.

**Event group** is the core concept: one real-world proposition whose outcomes
are listed on 2+ venues. All arb detection is scoped to a registered event
group. A leg's `is_yes_side` says whether buying that leg long is a bet the
group's canonical proposition resolves TRUE — that's how Polymarket-YES pairs
with Kalshi-NO on the same question.

## Conventions

- **All money and all prices are `Decimal`.** Never float. Prices are
  probabilities in `[0, 1]`; the UI renders them as cents. `Quote.__post_init__`
  enforces range and `ask >= bid`.
- Domain types are `@dataclass(frozen=True)`; enums are `StrEnum`.
- `outcome_id` values are **venue-native and not portable** — a Polymarket token
  id is meaningless to Kalshi. Never key cross-venue logic on `outcome_id`
  alone; carry `venue_id` with it.
- **Tests never hit a real venue.** REST paths are mocked with
  `httpx.MockTransport`; WS paths use an in-process `websockets.serve`. See
  `tests/adapters/test_polymarket.py` as the template.
- `pytest` runs with `asyncio_mode = "auto"` — async tests need no decorator.
- Fee models gate whether something is called an arbitrage. Write the test in
  `tests/shared/test_fees.py` **first** when adding one.
- SQLite gotcha: autoincrement PKs need
  `BigInteger().with_variant(Integer(), "sqlite")` or inserts fail on a NOT NULL
  constraint. See `Quote` and `PaperPnlSnapshot`.

### Frontend

Single-page terminal at `/`, with `/admin` as the secondary route. The UI is
built on an external design system copied in verbatim at
`frontend/public/design/industry/styles.css`.

Style via that system's semantic classes (`.card.blueprint`, `.btn.btn-primary`,
`.tag`, `.table`, `.field`, `.input`) and its CSS custom properties
(`--color-bg`, `--color-text`, `--color-accent`, `--space-*`, `--font-heading`).
**Do not introduce new hex colors, radii, or type scales** — take them from the
tokens. Tailwind stays installed as a layout escape hatch (grid/flex helpers)
but is not the styling engine for color, border, radius, or type.

The design system is a light-ground brief with no dark mode. Don't add one
casually.

## Config

Feature flags in `.env` (copy from `.env.example`; `.env` is gitignored):

- `ARBYS_DB_URL` — defaults to SQLite `./arbys-local.db`.
- `ARBYS_ENABLE_INGEST` — **0 by default.** When off, no venue is contacted and
  quotes must be pushed via `POST /quotes`. Tests and demos rely on this.
- `ARBYS_ENABLE_DISCOVERY` / `ARBYS_DISCOVERY_INTERVAL_S` — auto-registration of
  cross-venue games. Needs ingest on to actually stream.
- `ARBYS_MAX_OUTCOME_QTY` — max open units per outcome per paper account,
  default 500, `0` disables. An edge stays published while it exists, so
  without this repeat executions stack without bound.
- `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH` — when both set, the
  authenticated WS adapter is used instead of 5s REST polling. **Keep the .pem
  outside this repo.**

Run the backend **from the repo root**: `ARBYS_DB_URL` defaults to a relative
`./arbys-local.db`, so starting uvicorn from elsewhere silently creates a
second, empty database rather than failing.

`arbys-local.db` is a ~177 MB gitignored local artifact. Don't read it wholesale
or commit it; query it if you need to inspect state.

## Known defects

None currently tracked.

Previously listed here and now fixed (migration `0002`): `paper_position` had no
`venue_id`, so restart hydration fanned every row out to all three brokers and
`GET /paper/{account_id}` reported qty and realized PnL 3× inflated. The table is
now keyed on `account_id` + `venue_id` + `outcome_id`, `bootstrap()` routes each
row to its owning broker only, and `test_open_positions_hydrate_once_per_venue`
in [tests/test_backend_e2e.py](tests/test_backend_e2e.py) covers the restart
path. **Keep `venue_id` in that key** — dropping it silently reintroduces the
inflation, which no fresh-process test can catch.

## Repo facts

- Single `main` branch, remote is `mikeaidekman/Arbys` on the personal GitHub
  account.
- Git identity is set **repo-locally on purpose**: `Michael Aidekman
  <mikeaidekman@users.noreply.github.com>`, with
  `credential.https://github.com.username = mikeaidekman`. The machine's global
  git identity is a different, unrelated account, and this override keeps it out
  of this repo's history. Leave it alone.
- The README's roadmap points at `~/.copilot/session-state/<id>/plan.md`, a dead
  pointer now that this project has moved off Copilot. Historical plans and
  per-round UI feedback still exist under that directory if you need archaeology.
