# Arbys — working notes for Claude

Prediction-market arbitrage scanner + paper trading. Detects guaranteed-profit
opportunities across Polymarket US / Kalshi / DraftKings and validates them with a
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
venv\Scripts\python.exe -m pytest -q            # 177 tests, must stay green
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
  `ExecutionAdapter` ABCs in `base.py`. Kalshi is WS-first with REST-poll
  fallback; Polymarket US is REST-poll only (see **Venues** below).
- `arbys/discovery/` — scans Kalshi + Polymarket US for the same real-world
  game and auto-registers cross-venue event groups. Matching is by
  `(sport, game_date, unordered team pair)`, then split into date clusters so
  a team pair meeting on consecutive days (MLB series) yields one group per
  game. Team sports share one path: `fetch_kalshi_team_games` +
  `SERIES_TICKERS` on the Kalshi side, `/v2/leagues/<slug>/events` +
  `LEAGUE_SLUGS` on the Polymarket US side. Adding a league means adding a
  team table, a series ticker, and a league slug.

  One live trap: Kalshi sends a **bare city** (`"Atlanta"`) except where a city
  fields two teams (`"New York Y"`), which `TeamResolver` handles. Two others
  are now **historical**, both belonging to Polymarket *international*: its
  flat `/markets` endpoint capped at 100 rows ordered by 24h volume, where
  league games never outranked politics; and it identified teams only in prose,
  which is what `parse_vs_question` existed for. The US gateway is
  league-scoped and returns `teams[].name` structured, so neither applies.

  NBA is **unverified on both venues** — `KXNBAGAME` had no open events in the
  offseason, and `/v2/leagues/nba/events` returned zero on 2026-08-11 for the
  same reason. The Polymarket US type string
  `basketball_team_full_game_winner` was confirmed against **WNBA** instead.
  Recheck both when the season starts.

  **Market types.** A group is identified by `(sport, teams, date, market_type,
  line)`. `market_type="moneyline"` keys `outcome_ids` by team code and the
  canonical TRUE side is `team_a`; `market_type="total"` keys it by
  `OVER`/`UNDER` with TRUE = OVER, and the group id carries the line
  (`nfl-ARI-LAC-2026-09-13-total-44.5`). **The line must stay in the matcher's
  bucket key** — without it, one venue's Over 44.5 pairs against the other's
  Over 47.5 and invents an arb. Totals are binary, so the engine, broker and
  UI needed no changes.

  A `market_type="spread"` also carries an **anchor** — the participant its
  signed line is stated for — and the anchor is in the bucket key for the same
  reason the line is. Nothing sets it yet; every wired market type leaves it
  `None`. It landed early so adding spreads is registering a market type
  rather than reopening the matcher. See **Phase 2** below.

  Only NFL totals are wired, and that is now a **choice, not a limit**:
  Polymarket US carries `baseball_team_full_game_total` and Kalshi lists
  `KXMLBTOTAL`, so adding `("mlb", MLB_RESOLVER)` to `TOTALS_SPORTS` should
  work. It is held back so the Polymarket US port had exactly one behavioural
  variable. (Polymarket *international* genuinely carried no baseball totals,
  which is why this used to read as impossible.)

  Kalshi puts the strike in `yes_sub_title` and the team codes only in the
  event ticker, concatenated and variable-width, so `split_team_codes` tries
  every split; the line itself comes from the structured `floor_strike`.
  Polymarket US reports `line` as a structured field and is filtered on
  `sportsMarketType == "football_team_full_game_total"` — there is no slug to
  parse on that side.

  Observed 2026-08-09: totals price near 50/50 with a 1–2¢ spread on both
  venues and showed **no** arbs, while moneyline did. A totals line is set to
  be a coin flip and both venues model it similarly; disagreement lives in who
  wins, not in the score.

  **Kalshi publishes no live score or game clock** (checked 2026-08-08): it
  has no score field, and its `settlement_sources` just point at ESPN. The same
  was true of Polymarket international, whose payload contained the words
  `score` and `inning` only in prose — a prop market's question, a link to
  mlb.com/scores, a narrative blurb, and `teams[].record`, which is the
  *season* record, not the game score.

  **Polymarket US does publish them.** Its `/v2/leagues/<slug>/events` payload
  carries `score`, `period`, `live` and `ended`. Verified 2026-08-11:
  `wtt-diychi-sreaku-2026-08-11` returned `score: "11-8, 11-6, 5-5"`,
  `period: "S3"`, `live: true`. We still rely on the countdown alone, but that
  is now a choice rather than a data limitation — and a score is only half a
  picture when the Kalshi leg has none.

  Both venues report an exact start (`occurrence_datetime` / `startTime`),
  which is what `event_group.start_time` and the card countdown use.
- `arbys/ingest/` — async services: quote `worker`, `engine_runtime` (arb
  detection, triggered only on affected event groups), `pnl_service`,
  `auto_settle_service`.
- `arbys/backend/` — FastAPI app + `AppState`. `state.py` is the wiring hub:
  fee registry, adapter factories, broker construction, bootstrap/hydration.
- `arbys/db/` — SQLAlchemy models, repos, Alembic migrations.

**Event group** is the core concept: one real-world proposition whose outcomes
are listed on 2+ venues. All arb detection is scoped to a registered event
group. A leg's `is_yes_side` says whether buying that leg long is a bet the
group's canonical proposition resolves TRUE — that's how a Polymarket US LONG pairs
with Kalshi-NO on the same question.

## Conventions

- **All money and all prices are `Decimal`.** Never float. Prices are
  probabilities in `[0, 1]`; the UI renders them as cents. `Quote.__post_init__`
  enforces range and `ask >= bid`.
- Domain types are `@dataclass(frozen=True)`; enums are `StrEnum`.
- `outcome_id` values are **venue-native and not portable** — a Polymarket US
  market slug is meaningless to Kalshi. Never key cross-venue logic on `outcome_id`
  alone; carry `venue_id` with it.
- **Tests never hit a real venue.** REST paths are mocked with
  `httpx.MockTransport`; WS paths use an in-process `websockets.serve`. See
  `tests/adapters/test_polymarket_us.py` as the template.
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
- `ARBYS_POLYMARKET_US_POLL_S` — seconds between `/bbo` sweeps, default 5,
  clamped to a 1s floor. Polymarket US has no WS path yet (see **Venues**).
- `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH` — when both set, the
  authenticated WS adapter is used instead of 5s REST polling. **Keep the .pem
  outside this repo.**

`.env.example` also lists `ARBYS_ENABLE_DRAFTKINGS`, which *is* read. Note
that `ARBYS_ENABLE_POLYMARKET` / `ARBYS_ENABLE_KALSHI` / `POLYMARKET_API_KEY`
were **dead config** — nothing ever read them — and have been removed.

Run the backend **from the repo root**: `ARBYS_DB_URL` defaults to a relative
`./arbys-local.db`, so starting uvicorn from elsewhere silently creates a
second, empty database rather than failing.

**Alembic reads the same default.** `migrations/env.py` used to hardcode a
Postgres fallback despite its docstring promising otherwise, so
`alembic upgrade head` with no `ARBYS_DB_URL` set hung on `localhost:5432`
instead of migrating the SQLite database sitting right there. It now imports
`DEFAULT_DB_URL` from `arbys.db.session`.

`arbys-local.db` is a gitignored local artifact (~11 MB as of 2026-08-11).
Don't read it wholesale or commit it; query it if you need to inspect state.

### Time is the matching key, not the date

`game_date` is **not comparable across venues**: Kalshi's ticker carries a
local trading day, Polymarket US reports UTC. A night game is Aug 11 on one and
Aug 12 on the other — and Kalshi's Aug 11 night game collides with
Polymarket US's Aug 10 night game on `2026-08-11`. Date tolerance cannot fix
that, because the dates already agree wrongly; it paired Monday's game with
Tuesday's and invented an arb between two fixtures. `match_games` now compares
actual start times (90-minute window) whenever both venues report one.

**Kalshi's `occurrence_datetime` is expected settlement, ~3h after first
pitch — never use it as a start time.** The true start is in the ticker, in
Eastern: `KXMLBGAME-26AUG10`**`2210`**`KCLAD` → Aug 10 22:10 ET → 02:10Z,
matching Polymarket US exactly. `parse_ticker_start` does this. NFL tickers carry
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
constant while divergence varies. **Both** venues charge the same shape of fee,
peaking at a coin flip and vanishing at the extremes, which is why the
outline-without-button case clusters on ~50/50 markets:

| Venue | Taker fee | Max per contract |
| --- | --- | --- |
| Kalshi | `0.07 × p × (1-p)` | 1.75¢ at p=0.50 |
| Polymarket US | `0.06 × p × (1-p)` | 1.50¢ at p=0.50 |

Together that is up to **3.25¢/contract** of drag on a two-leg ticket at
even money. Measured 2026-08-11, gross divergence between the venues on MLB
moneyline topped out at 2.75¢ across 34 matched sides, so expect the
outline-without-button case to be the norm near 50/50 rather than the
exception.

`PolymarketFeeModel` used to return **zero**, which overstated every net edge
on a Polymarket leg. If net arbs look scarcer than you remember, that is the
correction, not a regression — do not loosen the fee model to bring them back.

Known understatement: Kalshi rounds fees **up** to the cent per contract,
Polymarket US uses banker's rounding, and nothing in our fee path rounds at
all — so modelled fees are slightly low and marginal edges look slightly
better than they are. Polymarket US's maker rebate (-0.0125) is not modelled
either, but the paper broker always takes, so it would never apply.

## Venues

**Polymarket US is a different exchange from Polymarket international**, not a
different endpoint for the same one. It is a CFTC-regulated DCM with its own
order book; shares are not fungible between the two and prices diverge. Only
the US book is tradeable from here, so the international integration was
deleted outright in favour of `venue_id = "polymarket_us"` (migration `0005`
purges its rows — the stored `outcome_id`s were CLOB token ids that identify
nothing on the US book, so they could not be remapped even in principle).

Two hosts, only the first of which we use:

- **`gateway.polymarket.us`** — public. No API key, no KYC, no wallet.
  `/v2/leagues/{slug}/events` for discovery, `/v1/markets/{slug}/bbo` for
  quotes. Measured 2026-08-11: 53 concurrent `/bbo` calls in 1.46s, no rate
  limiting.
- **`api.polymarket.us`** — authenticated (Ed25519, needs completed identity
  verification). Orders, portfolio, and the market WebSocket. **Not wired.**

So the adapter is **REST-poll only** (`ARBYS_POLYMARKET_US_POLL_S`, default
5s). When the WS lands, follow `_kalshi_factory` in `state.py`, which already
selects a WS adapter over a polling one when credentials are present.

A Polymarket US market is **one binary contract with a long and a short side**
— structurally like a Kalshi market, not like international's two-token pair.
So `outcome_id` follows the Kalshi convention: `{market_slug}:LONG` /
`{market_slug}:SHORT`. The short side is derived, not fetched:

```
short.bid = 1 - long.ask     short.bid_size = long.ask_size
short.ask = 1 - long.bid     short.ask_size = long.bid_size
```

**Get that inversion backwards and it is silent** — the prices still look
plausible and it invents edges. The invariant that catches it: both sides'
asks must sum to **more than** 1. `scripts/smoke_polymarket_us.py` asserts
exactly that against the live gateway.

`GET /v1/markets` is **not** usable for bulk quotes: it ignores a `slugs`
filter, returns closed markets, and its `marketSides[].price` is ambiguous
between the long-side bid and ask. Always use `/bbo`.

### Phase 2 and beyond

The venue swap was Phase 1 of three. Design and plan live in
[docs/superpowers/specs/](docs/superpowers/specs/) and
[docs/superpowers/plans/](docs/superpowers/plans/).

- **Phase 2 — spreads** (MLB + NFL). Both venues have deep books
  (`KXMLBSPREAD`, `KXNFLSPREAD`; `baseball_team_full_game_spread`). The work
  is sign normalisation: Kalshi names the team in its ticker
  (`KXMLBSPREAD-…-SF3`, *"San Francisco wins by over 2.5 runs"*, threshold in
  `floor_strike`), while Polymarket US anchors the signed line to **slug
  position** (`asc-mlb-cle-det-…-neg-2pt5` means CLE −2.5). `CLE −2.5` and
  `CLE wins by over 2.5` are the same binary — long side ≡ Kalshi YES — but
  pairing `CLE −2.5` against `DET −2.5` invents an arb. The matcher's `anchor`
  field already guards this.
- **Phase 3 — period markets**: first-five, halves, quarters. Kalshi has
  `KXMLBF5SPREAD`, `KXNFL1HTOTAL`, `KXNBA1QSPREAD` and friends; Polymarket US
  has the matching `sportsMarketType`s. Mechanically the same as Phase 2 once
  the taxonomy exists.

## Only-tradeable invariants

Three layers can each go stale independently, and each has bitten. A phantom
8¢ arb on 2026-08-09 came from a delisted Polymarket market quoting forever
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
