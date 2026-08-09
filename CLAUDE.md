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

  **Market types.** A group is identified by `(sport, teams, date, market_type,
  line)`. `market_type="moneyline"` keys `outcome_ids` by team code and the
  canonical TRUE side is `team_a`; `market_type="total"` keys it by
  `OVER`/`UNDER` with TRUE = OVER, and the group id carries the line
  (`nfl-ARI-LAC-2026-09-13-total-44.5`). **The line must stay in the matcher's
  bucket key** — without it, one venue's Over 44.5 pairs against the other's
  Over 47.5 and invents an arb. Totals are binary, so the engine, broker and
  UI needed no changes.

  Only NFL totals are wired: Kalshi lists `KXMLBTOTAL` but Polymarket carries
  no baseball totals (moneyline, NRFI and player props only). Kalshi puts the
  strike in `yes_sub_title` and the team codes only in the event ticker,
  concatenated and variable-width, so `split_team_codes` tries every split;
  the line itself comes from the structured `floor_strike`. Polymarket encodes
  it in the slug (`…-total-37pt5`) and is filtered on
  `sportsMarketType == "totals"`.

  Observed 2026-08-09: totals price near 50/50 with a 1–2¢ spread on both
  venues and showed **no** arbs, while moneyline did. A totals line is set to
  be a coin flip and both venues model it similarly; disagreement lives in who
  wins, not in the score.

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
- **Migrations must never build DDL from `Base.metadata`.** Each revision
  describes the change *it* makes, in explicit `op.*` calls, frozen at that
  point in history. `0001_initial` originally called
  `Base.metadata.create_all()`, which reads `models.py` as it exists *today* —
  so once `0002` existed, `alembic upgrade head` on an empty database died with
  `duplicate column name: venue_id`, and every later revision double-applied.
  Dev never noticed because `bootstrap()` builds the schema with `create_all()`
  and never runs a migration.
  [tests/db/test_migrations_match_models.py](tests/db/test_migrations_match_models.py)
  now replays the chain from empty and diffs it against `create_all`, so a
  missing or wrong migration fails the suite instead of the next deploy.

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

### Time is the matching key, not the date

`game_date` is **not comparable across venues**: Kalshi's ticker carries a
local trading day, Polymarket reports UTC. A night game is Aug 11 on one and
Aug 12 on the other — and Kalshi's Aug 11 night game collides with
Polymarket's Aug 10 night game on `2026-08-11`. Date tolerance cannot fix
that, because the dates already agree wrongly; it paired Monday's game with
Tuesday's and invented an arb between two fixtures. `match_games` now compares
actual start times (90-minute window) whenever both venues report one.

**Kalshi's `occurrence_datetime` is expected settlement, ~3h after first
pitch — never use it as a start time.** The true start is in the ticker, in
Eastern: `KXMLBGAME-26AUG10`**`2210`**`KCLAD` → Aug 10 22:10 ET → 02:10Z,
matching Polymarket exactly. `parse_ticker_start` does this. NFL tickers carry
a date with no `HHMM`, so those fall back to date matching — safe, since NFL
pairs never meet on consecutive days.

### Gross vs net is deliberate in the UI

A card's **green outline** (and the nav's "N arbs" count, and `arb_edge` in
`/monitored`) is **gross of fees** — just `yes_ask + no_ask < 1`. The **green
buy button** requires a live engine opportunity, which is **net of fees**. So a
green outline with a disabled button is expected, not a bug: the two asks sum
under a dollar but fees eat the difference.

**This is intentional — do not "fix" it.** The gross edge is a divergence signal
between the venues, which is worth seeing on its own; fee drag is roughly
constant while divergence varies. Kalshi's fee peaks at a coin flip
(`0.07 × p × (1-p)`, max 1.75¢/contract at p=0.50) and vanishes at the
extremes, which is why the outline-without-button case clusters on ~50/50
markets.

Known understatement: Kalshi rounds fees **up** to the cent per contract and
nothing in our fee path rounds, so modelled fees are slightly low and marginal
edges look slightly better than they are.

## Only-tradeable invariants

Three layers can each go stale independently, and each has bitten. A phantom
8¢ arb on 2026-08-09 came from a delisted Polymarket token quoting forever
against a live Kalshi leg.

- **Quotes expire.** `QuoteBook` stamps arrival on a monotonic clock and
  `get()` withholds anything older than `ARBYS_QUOTE_MAX_AGE_S` (default 600s,
  `0` disables). Venue websockets push only on change, so a dead feed and a
  quiet market are indistinguishable without this. `get_with_age()` still
  returns stale entries so the UI can explain rather than blank.
- **Groups are retired.** Discovery removes `source="discovery"` groups a
  *complete* pass no longer finds; `source="manual"` is never touched, and
  retirement is skipped when any sub-pass raised so a venue outage isn't read
  as delisting.
- **Opportunities follow the group.** Retiring must call
  `clear_group_opportunities` — unregistering from the engine means no further
  evaluation, so nothing else would ever empty that group's set.

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
