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
venv\Scripts\python.exe -m pytest -q            # 361 tests, must stay green
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

`pyproject.toml` sets `strict = true`, but the codebase currently has **71 errors
across 24 files** and mypy is not part of the green-build bar. Do not claim
"mypy clean", and do not embark on a 71-error cleanup as a side effect of an
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
  `ExecutionAdapter` ABCs in `base.py`. Both Kalshi and Polymarket US are
  WS-first with a REST-poll fallback, each gated on credentials being present
  (see **Venues** below).
- `arbys/discovery/` — scans Kalshi + Polymarket US for the same real-world
  game and auto-registers cross-venue event groups. Matching is by
  `(sport, game_date, unordered team pair)`, then split into date clusters so
  a team pair meeting on consecutive days (MLB series) yields one group per
  game. Team sports share one path: `fetch_kalshi_team_games` +
  `SERIES_TICKERS` on the Kalshi side, `/v2/leagues/<slug>/events` +
  `LEAGUE_SLUGS` on the Polymarket US side. Adding a league means adding a
  team table, a series ticker, and a league slug. Individual sports (tennis,
  UFC) share a second path built on `players.py` instead of a roster table;
  `fetch_kalshi_tennis_matches(series=...)` and
  `fetch_polymarket_us_tennis(leagues=..., winner_types=...)` are
  parameterised, so a new head-to-head sport is registry entries, not a new
  parser.

  Wired as of 2026-08-24, with live group counts from one full pass (567
  total): **nfl** 268, **mlb** 69, **atp** 68, **wta** 62, **ncaaf** 59,
  **wnba** 34, **ufc** 7. Note `ncaaf` is Kalshi's name and `cfb` is
  Polymarket's — `SERIES_TICKERS` and `LEAGUE_SLUGS` are separate dicts
  precisely so a league can be named differently per venue.

  **`teams[].name` on Polymarket US holds a different thing per league.** This
  is the trap that cost the most time here, because it fails silently — the
  league discovers zero groups while both venues are visibly quoting it:

  | league | `teams[].name` | resolves via |
  | --- | --- | --- |
  | NFL / MLB | `"Arizona Cardinals"` (full name) | `_by_full` |
  | WNBA | `"Golden State"` (bare city) | `_by_city_unique` |
  | CFB | `"Tar Heels"` (mascot only) | **not usable** |

  A college mascot is not an identity: 28 repeat across the 88 CFB games
  observed, `"Tigers"` being seven different schools, covering 81 of 176
  team-slots. `TeamResolver` therefore refuses a bare *ambiguous* nickname,
  and `_resolve_team` prefers the two better fields the payload carries —
  `displayAbbreviation` (equals Kalshi's ticker code for 154 of 168 CFB teams)
  then `safeName` (equals Kalshi's title) — before falling back to `name`.
  Polymarket writes `"Arizona State"` where Kalshi writes `"Arizona St."` on
  39 of the shared codes, so both spellings are indexed.

  **`game_date` is Eastern on both sides, deliberately.** Kalshi's comes from
  its ticker, which carries a local Eastern trading day; Polymarket's is
  derived from a UTC `startTime` and is converted by `_eastern_date`. Without
  that conversion an evening game lands on different dates per venue, and
  `_same_fixture` falls back to comparing dates whenever either side lacks an
  exact start — which is every sport whose Kalshi ticker omits `HHMM` (WNBA,
  NFL, CFB). Reading UTC there silently dropped every evening fixture: NFL
  totals found 151 groups where the Eastern read finds 252. Do **not** "fix"
  this class of miss by widening `date_tolerance_days` — that also fuses
  Monday's game with Tuesday's and invents an arb between two fixtures.

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

  Totals are wired for **nfl, mlb, wnba and ncaaf** (`TOTALS_SPORTS`), each
  with a `TOTALS_SERIES` ticker and a `TOTAL_TYPES` entry. NBA stays out of
  both `TEAM_SPORTS`-in-practice and `TOTALS_SPORTS` verification until its
  season opens, for the same reason its moneyline is unverified.

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
  `auto_settle_service`, `auto_trade_service`.

  `engine_runtime` runs **two** detectors per evaluation:
  `detect_cross_venue_two_leg` across venues, and `detect_complementary_set`
  once per venue. Every group carries 2 legs per venue (`:YES`/`:NO`,
  `:LONG`/`:SHORT`), so the complementary detector always has a candidate set —
  meaning **intra-venue arbs are already detected**. A Kalshi book crossed at
  `YES 0.47 + NO 0.52 = 0.99` was observed on 2026-08-22 (1 of 245 groups); it
  produced no opportunity only because fees put it at 1.0249. Both detectors
  are now depth-aware and share the same tick handling (see **Sizing has a
  tick, and it isn't 1** below).
- `arbys/backend/` — FastAPI app + `AppState`. `state.py` is the wiring hub:
  fee registry, adapter factories, broker construction, bootstrap/hydration.
  `ticket_service.py` is the **only** way a live arb ticket is submitted —
  both `POST /paper/execute` and (later) the auto-trader call
  `submit_arb_ticket`. (The backtest harness, `arbys/backtest/__init__.py`,
  builds an `ExecutionIntent` directly and calls the router itself, skipping
  both the ticket log and the cap — correctly, since it runs its own
  throwaway brokers with no DB sink, so there is nothing to log and no
  shared position to cap.) It mints the ticket id, enforces
  `ARBYS_MAX_OUTCOME_QTY`, and writes the `paper_ticket` row. **The cap used
  to live in the endpoint**, so any non-HTTP caller bypassed it silently and
  stacked without bound; keep it in the service.
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

Single-page terminal at `/`, with `/admin` and `/account` as secondary routes.
`/account` replaced the old sidebar — `AccountPanel.tsx` no longer exists;
`AccountStrip` (full-width, above the opportunity table) and `TicketHistory`
took its place. The UI is built on an external design system copied in
verbatim at `frontend/public/design/industry/styles.css`.

Style via that system's semantic classes (`.card.blueprint`, `.btn.btn-primary`,
`.tag`, `.table`, `.field`, `.input`) and its CSS custom properties
(`--color-bg`, `--color-text`, `--color-accent`, `--space-*`, `--font-heading`).
**Do not introduce new hex colors, radii, or type scales** — take them from the
tokens. Tailwind stays installed as a layout escape hatch (grid/flex helpers)
but is not the styling engine for color, border, radius, or type.

The one sanctioned exception is `frontend/src/index.css`'s small `--vt-*` set
(`--vt-green`, `--vt-green-dark`, `--vt-green-tint`, `--vt-red-dark`) — the
design system has no profit/loss color pair, so the terminal defines its own
rather than smuggling in a raw hex value at the call site.

**The `--space-*` scale skips 5 and 7** (`--space-4` then `--space-6`,
`--space-8`; see the design system CSS). `var(--space-5)` is not an error, not
a fallback to the nearest defined step, and not a browser warning — it
silently resolves to nothing, collapsing whatever margin or gap it was set on.
This has shipped twice during the account-page work and nothing in the test
or lint suite catches it; check the actual token list before using one.

The design system is a light-ground brief with no dark mode. Don't add one
casually.

## Config

Feature flags in `.env` (copy from `.env.example`; `.env` is gitignored):

- `ARBYS_DB_URL` — defaults to SQLite `./arbys-local.db`. SQLite databases are
  opened in **WAL** journal mode with `synchronous=NORMAL` and
  `busy_timeout=15000`, applied per connection by a `connect` hook in
  `db/session.py` and gated on the dialect — issuing `PRAGMA` against
  Postgres is an error. The default `delete` journal mode allows one writer
  *and blocks readers*, which with five concurrent writers produced 18
  `database is locked` errors and 6 QueuePool timeouts in a day. WAL leaves
  `-wal` and `-shm` sidecar files beside the database.
- `ARBYS_ENABLE_INGEST` — **0 by default.** When off, no venue is contacted and
  quotes must be pushed via `POST /quotes`. Tests and demos rely on this.
- `ARBYS_DISCOVERY_CONCURRENCY` — how many discovery sub-passes may hit the
  venues at once, **1 by default**. `_REQUEST_SPACING_S` is 0.15s, calibrated
  as ~6 req/s for Kalshi's public tier, and that assumes one pass at a time.
  Going from 5 sub-passes to 11 (adding WNBA, CFB, UFC) asked for ~66 req/s
  and earned a 429 that dropped MLB and CFB from the pass — coverage halving
  in a way that looks like "those games ended". A serial pass takes ~128s
  against the 600s default interval. Raise it to trade reliability for latency.
- `ARBYS_ENABLE_DISCOVERY` / `ARBYS_DISCOVERY_INTERVAL_S` — auto-registration of
  cross-venue games. Needs ingest on to actually stream.
- `ARBYS_MAX_OUTCOME_QTY` — max open units per outcome per paper account,
  default 500, `0` disables. An edge stays published while it exists, so
  without this repeat executions stack without bound.
- `ARBYS_MAX_TICKET_STAKE` — max total capital in one arb ticket, default 200,
  `0` disables. Sizing is depth-driven and one Polymarket US level has shown
  419,882 contracts resting, so without this a single ticket would consume the
  book. **This does not replace `ARBYS_MAX_OUTCOME_QTY`** — that caps
  cumulative open units per outcome per account at execute time, this caps one
  ticket at detection time. At ~$1.00 all-in per contract pair, $200 is ~198
  contracts, so roughly 2.5 tickets on one outcome before the position cap
  binds. Both apply.
- `ARBYS_POLYMARKET_US_POLL_S` — seconds between `/bbo` sweeps, default 5,
  clamped to a 1s floor. This is the **credential-less fallback** path only;
  with credentials set the WebSocket is used instead (see **Venues**).
- `ARBYS_POLYMARKET_US_WS_SHARD_SIZE` — markets per Polymarket US WebSocket
  connection, default 100. One connection silently stops streaming a large
  fraction of its markets past some ceiling, so the subscription is split
  across sockets; see **Venues** for the measurement. Lower it if a shard's
  `live` count sags, raise it to trade that margin for fewer connections.
- `ARBYS_POLYMARKET_US_DARK_AFTER_S` / `..._PRIORITY_DARK_AFTER_S` — socket
  silence before a subscribed market is treated as **dark**, default 120s and
  6s respectively, the tighter one applying where the venue says the game is
  under way. Dark is a *diagnosis*, not a data source: the adapter re-sends a
  subscribe for that market, and rebuilds the connection if that fails for a
  live game. See **The feed abandons markets** below.
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

`/monitored` gained six pair-level fields: `net_edge`, `max_tradeable_qty`,
`net_max_profit`, `capital_required`, `best_pair_yes_outcome_id`,
`best_pair_no_outcome_id` — plus `uncapped_qty` / `uncapped_capital`, which
answer "how much size is really there?" by sizing the **same winning pair**
with `max_stake=None`. The terminal shows that as the **Book** column beside
**Size**, so the gap between them is exactly what `ARBYS_MAX_TICKET_STAKE` is
holding back — measured 2026-08-26, binding on 258 of 548 groups and by as
much as 1165x (192 contracts allowed against 224,111 resting).

**Ranking still uses the capped figure and must keep doing so** — it is what
is actually tradeable, and the objective is shared with
`detect_cross_venue_two_leg` (see below). `uncapped_qty` is display only.
`None` there means *unknown*, never unlimited: with neither leg reporting
depth and no stake cap, `tradeable_qty` falls back to `LEGACY_UNBOUNDED_QTY`,
a placeholder that would read on screen as a real hundred contracts the venue
never offered. `best_yes_ask`/`best_no_ask` above are each the
cheapest ask **independently** across venues — they can both come from the
same venue, in which case they name no single tradeable pair. The six new
fields instead evaluate every real (yes, no) leg combination and report the
best one, so they can actually be filled together. `net_edge` is frequently
**negative**, and that is correct and must be displayed as such, not
suppressed: measured 2026-08-22, 12 groups had a gross-positive pair
(`arb_edge > 0`) and **0** had a net-positive one. A negative `net_edge` on
the best pair is the normal state near a coin flip, not a bug in the new
fields.

**The ranking objective is shared between the engine and `/monitored` on
purpose.** Both `detect_cross_venue_two_leg` and `/monitored`'s pair search
rank candidate (yes, no) pairs by highest absolute net profit
(`net_edge × qty`), which is depth-scaled — a deep 1¢ pair beats a thin 10¢
one. They have to stay in agreement: the frontend matches the pair it
displays to a published opportunity by leg `outcome_id`, so if the two ever
picked different pairs for the same group, the Fill button would sit disabled
at "waiting" on a row that has a live, executable arb underneath it.

### Sizing has a tick, and it isn't 1

Budget-bound sizing (`tradeable_qty` in `arbys/shared/qty.py`, called from
both detectors and from `/monitored`) is a division: stake budget divided by
per-contract cost. Left unrounded that produces numbers like
`214.615302071037664985513467` — not an order size any venue would accept,
and more decimal places than the DB `qty` column (12) can round-trip, so the
value would not even survive a save/reload. `DEFAULT_QTY_TICK = Decimal("0.01")`
in `arbys/shared/arb_engine.py` floors every derived quantity to that
granularity; `tick_by_venue` overrides it per venue where a finer or coarser
one is known.

**0.01 is deliberate, not a stand-in for "whole contracts, fix later."**
Measured 2026-08-23, 104 of 404 Kalshi legs and 225 of 336 Polymarket US legs
report a *fractional* `ask_size` — e.g. Kalshi `1865.53`, Polymarket US
`2616.69`. Contracts are not whole units on these venues. Rounding the tick up
to `1` would silently discard real, currently-tradeable size, not just tidy up
formatting.

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
  verification). The **market WebSocket is wired and is the live quote path**
  (see below); orders and portfolio are not.

The adapter is **WebSocket-first with a REST-poll fallback**, chosen by
`_polymarket_us_factory` on whether `POLYMARKET_US_API_KEY_ID` +
`POLYMARKET_US_PRIVATE_KEY_PATH` are set — the same shape as
`_kalshi_factory`. With credentials, both legs of a cross-venue group push.
Without them the REST path still works (`ARBYS_POLYMARKET_US_POLL_S`, default
5s); it is the only path that needs no KYC.

The handshake signature must cover **`/v1/ws/markets`** — signing `/` is
rejected with 401. Check credentials with
`scripts/verify_polymarket_us_creds.py`, which also reports clock skew: the
30s signing tolerance means a skewed clock fails every request in a way that
looks exactly like a bad key.

We subscribe to the **full** `SUBSCRIPTION_TYPE_MARKET_DATA`, not the lite
variant, because `bidDepth`/`askDepth` count price **levels**, not contracts —
measured 49 against a true best-bid size of 287,926.98 — and only the full
ladder carries real `qty`. REST `/bbo` cannot report size at all and reports
`None`, meaning unknown.

**There is no runtime fallback from WS to REST.** A failing handshake retries
under backoff; downgrading silently would hide a revoked credential
indefinitely, visible only as degraded fill quality.

**One connection will not stream every market, and it fails silently** — the
worst trap found here so far, because it produces confident wrong prices
rather than an error. Measured 2026-08-25 with 573 markets on a single socket:
the connection stayed healthy (no error, no disconnect, ~250 frames/s arriving)
while delivering deltas for only ~400 of them per 30s window, and what it
dropped included the live in-play markets that reprice fastest. In-play
Polymarket legs sat at a **median age of 416s** while their Kalshi
counterparts were at **0.3s**.

An A/B/A test separated this from "the market just went quiet": of nine slugs
proven to be streaming both immediately before *and* immediately after,
**eight were shed** while subscribed alongside 564 others, and all nine
streamed when subscribed alone. A second connection on the same API key had
full service throughout, so the ceiling is per **connection**, not per key.

So the subscription is **sharded across connections** —
`ARBYS_POLYMARKET_US_WS_SHARD_SIZE` slugs each, default 100, six sockets for
573 markets. Sharding fixed the *shedding*: the markets that stream, stream
promptly, and the six shards together report the venue's full-book heartbeat
of ~127 frames/s.

**It did not fix staleness, and an early note here claiming "every leg reads
under 4.5s" was wrong** — that was measured moments after a resubscribe, when
every market has just been handed a snapshot and nothing has had time to age.
Measured 96s after a resubscribe instead, 191 of 571 markets had received
nothing at all. The remaining cause is a different failure, below, and no
shard size addresses it: re-subscribing the stale markets 20 to a socket left
44 of 74 still hours out of date.

**Each shard logs `live N/M` every 30s, and that count is the only symptom
this failure has.** Don't read a single sub-100% value as a fault — plenty of
pre-game markets are genuinely quiet — read it as a trend, and cross-check
against actual quote ages. What went wrong before was not that the number was
bad but that nobody was counting: stale quotes are indistinguishable from a
quiet market, right up until a stale leg invents an arbitrage against a live
one on the other venue. Our book held Mochizuki/Milavsky at 0.58/0.59 while
the venue was at 0.87/0.88 and Kalshi at 0.94/0.95.

### The feed abandons markets, and said so only in a field we ignored

**The WebSocket is push-only, so a quote is replaced only when the venue sends
a frame or we resubscribe. For a persistent subset of markets it does
neither** — and until 2026-08-25 nothing noticed, because a frame's own
timestamp was never read.

Every `marketData` frame carries `transactTime`, the venue's own clock for the
book it is describing. On subscribe the venue replays a **cached** snapshot,
and for a large minority of markets that snapshot is hours old:

| first-frame lag | markets | ≥2¢ wrong vs live `/bbo` |
| --- | --- | --- |
| < 10s | 304 | 2.3% |
| 10s–60min | 66 | ~2% |
| **> 1 hour** (median ~5.7h) | **199** | **58.3%**, worst 97¢ |

We stamped **arrival** time, so a six-hour-old book entered the quote book
reading 0.2s old. `ARBYS_QUOTE_MAX_AGE_S` was powerless against it by
construction — the entry really had just arrived. A leg frozen at a price the
venue left hours ago then sat in the opportunity set against a live Kalshi
leg, which is precisely the false arbitrage the age check exists to prevent:
our book held `tsc-nfl-ari-gb-…-total-48pt5` at 0.19/**0.50** while the venue
was at 0.19/**0.23**.

Two things follow, and both are now in the code:

- **`Quote.source_age_s`** carries how stale the venue said the data already
  was, and `QuoteBook.upsert` **back-dates its arrival stamp by it**. A
  replayed snapshot can therefore be stale on arrival — which is the intended
  outcome, because it is exactly the quote that must not be traded on. Ageing
  now describes the data rather than the delivery.
- **Nothing else may write a price.** The socket is the only quote source.
  When a market goes dark the adapter escalates *on the socket* — re-subscribe
  that market, then rebuild the connection if that fails for a live game — and
  if neither works the quote ages out and is withheld. A withheld leg is safe;
  a wrong one is not.

**In-play markets get a far tighter deadline, and the venue says which they
are.** `/v2/leagues/<slug>/events` carries `live` and `ended` per event;
discovery records them on `VenueGame` and the matcher resolves them onto
`EventGroup.in_play`, which `AppState.in_play_slugs` reads. All three states
matter and are real — verified 2026-08-25 on the ATP feed:

| `live` | `ended` | `period` | meaning |
| --- | --- | --- | --- |
| `True` | `False` | `S3` | on court now |
| `False` | `True` | `FT` | finished |
| `False` | `False` | `` | scheduled, not started |
| `None` | `None` | `NS` | venue not tracking it yet |

**`None` is not `False`.** Kalshi publishes no live state at all, so a group
seen only there reports `None`, and `CrossVenueMatch.in_play` therefore
ignores venues that said nothing rather than counting them as "not playing".
Where nobody said, `in_play_slugs` falls back to a start-time window bounded
by `MAX_EVENT_DURATION_S` (6h). `EventGroup.in_play` is deliberately **not
persisted**: it flips as games start and end, so a value rehydrated from the
database would be a confident lie where `None` correctly means "ask again".

Before this, "started" was inferred from `start_time` alone and was true
forever — finished tennis matches were polled at in-play rates and
re-subscribed every 15s for the life of the process, 13 per shard.

**In-play markets get a far tighter deadline.** A market whose game is under
way reprices on every point, so 120s of silence is a fault where the same
silence on next week's game is normal: measured 2026-08-25, three of thirty
in-play markets went unserved and drifted from 39s to 122s stale, one of them
11¢ from the live book. Those count as dark after
`ARBYS_POLYMARKET_US_PRIORITY_DARK_AFTER_S` (6s), and they are the **only**
markets whose darkness justifies dropping an otherwise-healthy connection — a
pre-game book that never answers is usually finished or delisted, and
reconnecting for it would thrash the socket forever.

**A REST read is not an acceptable substitute, and this was tried.** A `/bbo`
backstop briefly filled these gaps and had to be removed, for two compounding
reasons:

- **`/bbo` lags the WebSocket.** Measured on an in-play ATP match, the ladder
  read `0.55/0.56` while `/bbo` still said `0.60/0.61`, converging seconds
  later. Substituting it hands the engine a price the venue has already moved
  past — the exact false positive this section exists to prevent.
- **`/bbo` reports no size, and unknown depth is not harmless.** Its
  `bidDepth`/`askDepth` count price *levels*, not contracts, so a quote built
  from it carries `ask_size=None`. `tradeable_qty` treats `None` as **no
  ceiling** (correctly — that rule is what keeps hand-pushed quotes tradeable),
  so sizing falls through to the stake budget. Observed live: a ticket sized at
  **200 contracts** off a backstop leg while two legs built from real ladder
  depth were capped at **25**.

So a substituted quote is wrong twice over — a price that may be behind, sized
against a book we cannot see. **Withholding is the correct failure mode**, and
`/bbo` is for asking *whether* a market is being served, never for what it is
worth.

**A subscription is acknowledged as a whole, never per market — so verify it
is actually arriving.** If the venue registers 97 of the 100 names in a
subscribe, nothing says so, and those three stay dark for the life of the
connection, because the adapter subscribes once at connect and never checks.
Measured 2026-08-25: a market delivering 165 frames/30s subscribed *alone* —
and 149 inside a fresh 100-slug shard — was getting nothing on the running
backend's socket, and the dark set was the **same three markets** across a 90s
sample. A venue shedding markets on throughput would churn; a subscription
that silently failed to register would not, which is why this is read as a
registration failure rather than the per-connection ceiling sharding
addresses.

`_repair_subscriptions` re-sends a subscribe for just those markets, on the
existing connection — subscribing is additive, so it costs one small message
and no reconnect. Two kinds qualify: **never delivered on this connection**
(a subscribed market answers immediately, if only with a snapshot), and
**in-play and silent past `REPAIR_AFTER_S`** (a book being played reprices
constantly, so 20s of quiet is abnormal in a way it is not for next week's
game). A merely quiet pre-game market is deliberately excluded — it answered
at connect, it is behaving normally, and re-subscribing all of those every
sweep would be hundreds of pointless messages a minute, each one making the
venue replay a snapshot. `MAX_REPAIR_ATTEMPTS` bounds the cost for a market
that is genuinely finished, since a settled book never streams again however
often it is asked for.

This is only safe because the quote book refuses to go backwards: a repair
makes the venue replay a possibly hours-old snapshot, which on a live market
would otherwise clobber current prices.

**`close()` on the WS adapter must actually close the shards.** A shard runs
in its own task, and the event loop holds a reference to a task, so dropping
the `stream_quotes` generator does not end it. Cancelling the consumer happens
to work — the `CancelledError` lands inside the generator's own `await` and
runs its cleanup — but `_stop_ingest` calls `close()`, and `restart_ingest()`
rebuilds the adapters on every discovery pass that changes anything, so a leak
here compounds into sockets that hold markets subscribed against the very
ceiling the sharding exists to respect. Both paths are pinned by tests, each
verified to fail when its own cleanup is removed.

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

### Quote sizes have three states, not two

`Quote.bid_size` / `ask_size` are `Decimal | None`:

| value | meaning | broker |
| --- | --- | --- |
| `None` | unknown — the venue did not report depth | fills |
| `0` | **known empty** — nothing is resting there | **rejects** |
| `> 0` | a real quantity | fills |

Conflating the first two is what makes this worth stating. `POST /quotes` and
most test fixtures omit sizes entirely, so a naive `size <= 0` guard would
reject every hand-pushed quote while looking like a safety improvement.

**One-sided books are kept**, with the missing side synthesised at the present
side's price and size `0`, so a live ask stays tradeable — there really are
markets with 419,882 contracts offered and no bids at all. `paper_broker`
refuses to fill against a known-empty side; without that guard the synthesised
bid would let it report selling into a book with no buyers.

### Rebuilding ingest costs the whole book, so don't

`AppState.sync_ingest` is what discovery and the event-group endpoints call.
**It rebuilds the venue adapters only when some venue needs a market it is not
already subscribed to.** A retirement, a title change, a start time moving —
none of those touch the sockets.

Neither venue supports unsubscribing, so the only way to *drop* a market is to
hang up every connection and redial with a shorter list. But dropping costs
nothing if we simply stop caring: frames for markets we no longer hold are
already ignored, and the group is gone from `event_groups` so nothing
evaluates it. Redialing is what is expensive — the venue answers a fresh
subscription by replaying a cached book that can be hours old, so every
rebuild floods the quote book with stale prices and takes ~3 minutes to
recover.

Discovery retires a finished match on nearly every pass, so treating that as a
reason to rebuild meant **all 569 subscriptions across both venues went stale
for ~3 minutes every ~12 minutes**, in order to stop watching one tennis game
that had ended. `restart_ingest` still exists for the unconditional rebuild;
prefer `sync_ingest`.

## Only-tradeable invariants

Three layers can each go stale independently, and each has bitten. A phantom
8¢ arb on 2026-08-09 came from a delisted Polymarket market quoting forever
against a live Kalshi leg.

- **Quotes expire.** `QuoteBook` stamps arrival on a monotonic clock and
  `get()` withholds anything older than `ARBYS_QUOTE_MAX_AGE_S` (default 600s,
  `0` disables). Venue websockets push only on change, so a dead feed and a
  quiet market are indistinguishable without this. `get_with_age()` still
  returns stale entries so the UI can explain rather than blank.

  **`/bbo` is not ground truth during fast play.** Measured 2026-08-25 on an
  in-play ATP match, the WebSocket ladder led `/bbo` by a full tick for
  seconds at a time (WS `0.55/0.56` while `/bbo` still read `0.60/0.61`,
  converging a few seconds later). The socket is the faster, more granular
  feed; `/bbo` is a periodic summary. Use `/bbo` to check whether a market is
  *being served at all* — that is what it is good for — but do **not** grade
  live prices against it and conclude the book is stale. A three-way read
  (our book vs our own WS vs `/bbo`) settles it, and our book has matched our
  own WS on every sample.

  **The book never goes backwards.** `upsert` drops a quote whose effective
  time is older than the entry it would replace. Frames do not arrive in book
  order — every fresh subscription is answered with a *cached* book, so a
  resubscribe can deliver an hours-old snapshot after live prices are already
  flowing. Without the guard that snapshot overwrites good data and blanks a
  market that was streaming fine, turning a routine reconnect into an outage.
  `REGRESSION_TOLERANCE_S` (5s) keeps it aimed at that and not at ordinary
  jitter: a venue's own `transactTime` lag wanders 0.15–0.45s frame to frame,
  so a zero-tolerance guard discarded **24% of legitimate updates** — real
  ticks thrown away on a fast in-play book.

  **Arrival time is not always data time.** Where a quote carries
  `source_age_s` — how far behind the venue said its own book already was —
  `upsert` back-dates the arrival stamp by it, so an entry can be stale the
  moment it lands. Without that, a replayed snapshot defeats this whole
  invariant while satisfying it on paper: it genuinely *did* just arrive. See
  **The feed abandons markets** under **Venues**.
- **Groups are retired.** Discovery removes `source="discovery"` groups a
  *complete* pass no longer finds; `source="manual"` is never touched, and
  retirement is skipped when any sub-pass raised so a venue outage isn't read
  as delisting.
- **Opportunities follow the group.** Retiring must call
  `clear_group_opportunities` — unregistering from the engine means no further
  evaluation, so nothing else would ever empty that group's set.

## Trade history is ticket-level

`paper_ticket` gives an arb ticket a durable identity and `paper_order.ticket_id`
groups its legs. Three things about it are deliberate:

- **`event_group_id` is not a foreign key, and `title_snapshot` is frozen at
  submit time.** Discovery retires groups routinely and `delete_event_group`
  takes the legs with it, so a live join to `event_group.title` blanks the name
  of every finished game — exactly the rows worth auditing.
- **Rejected and missed tickets are recorded, not just fills.** A preview
  rejection never builds an `Order`, so before this nothing reached the DB and
  a bot attempting 400 tickets looked identical to one attempting 3. `missed`
  means the edge vanished between detection and submission, which is the
  measurement that decides whether latency work is worth anything.
- **A manual click is always an attempt.**
  `submit_arb_ticket_for_descriptor` resolves the descriptor itself and writes
  a `missed` ticket when no live edge matches, so a click on a row whose edge
  just died leaves a record. The endpoint no longer resolves anything on
  the `event_group_id` path the UI actually uses (the legacy
  `opportunity_index` branch still indexes `s.opportunities` itself). The
  narrower rule still holds for `submit_arb_ticket`, which the auto-trader
  calls: a detector finding nothing is not an attempt, or a bot would write
  thousands of rows a night saying nothing happened.

`paper_settlement` records resolution events, which `settle_outcome_async`
previously did not — a settled winner was indistinguishable from a position
sold out at market. A ticket's realized profit is computed at read time from
its **own** fills, because settlement uses an `avg_price` blended across every
ticket on that outcome.

## The auto-trader fills what the engine published

`ARBYS_ENABLE_AUTO_TRADE` (**0 by default**) starts `AutoTradeService`, which
consumes `AppState.subscribe_opportunities()` and calls `submit_arb_ticket`
with `source="auto"` for every opportunity it receives. There is no separate
threshold: both detectors already refuse to publish anything with
`net_edge_per_contract(...) <= 0`, so everything on that queue is net-positive
of fees. **Do not add an edge floor or a gross-edge mode** — they are explicit
non-goals, and the honest gate being quiet is the finding, not a bug.

It adds no sizing logic. `ARBYS_MAX_TICKET_STAKE` (detection) and
`ARBYS_MAX_OUTCOME_QTY` (execution) both still bind, and `submit_arb_ticket`
stays the authoritative enforcement point for the latter. The service
*additionally* pre-checks the cap via the shared `cap_breach` and skips
**silently** when it would bind: opportunities republish on fingerprint change,
so a capped-out group would otherwise write a `rejected` ticket on every tick
all night, filling the audit log with rows saying only "still capped".

`ARBYS_AUTO_TRADE_COOLDOWN_S` (default 60) ignores a group for a window after a
**fill**. Rejects and misses deliberately do not start one — a miss means the
edge was gone, which is no reason to stop watching. Beware the interaction with
dust: a fill of 0.01 contracts starts the same 60s cooldown as a real one, so a
half-cent fill can mask a genuine edge on that group. Measured 2026-08-27, 5 of
496 groups were net-positive worth ~18c in total, three of them sized 0.01-0.03
contracts against off-market orders — so this is not hypothetical.

**The cooldown key is the published opportunity id, not the real-world
game.** It keys on `opp.event_group_id`, and `engine_runtime` publishes a
synthetic `<group>:<venue>` id for each venue's intra-venue complementary edge
alongside the cross-venue one on the same evaluation — so a single pass on
`eg-1` can yield `eg-1`, `eg-1:kalshi` and `eg-1:polymarket_us`, three
independent cooldowns on the same game. Up to three tickets can therefore land
back to back on a shared leg; the position cap, not the cooldown, is what
bounds total exposure.

**The service must not import `arbys/backend/`.** `backend/state.py` imports
`ingest`, and `backend/ticket_service.py` imports `backend/state.py`, so the
reverse is a cycle. Submission and the cap pre-check are injected as callables
by `AppState`, whose own `ticket_service` imports sit inside the method bodies
for the same reason. That boundary is also why the service's tests need no
database.

Backpressure is a known, bounded limitation: `subscribe_opportunities` returns a
queue of `maxsize=100` and publishers drop with `put_nowait` under
`suppress(QueueFull)`, so a slow consumer loses opportunities with no error.
The service logs `auto-trade backpressure` above 50 queued. Processing is
serial on purpose — concurrent tickets would race on both the cash balance and
the position cap, and a lost race there is a real oversized position rather
than a missed trade.

**`stop()` does not cancel an in-flight `handle()` — it waits for it.** The
plan as written called for a straight `task.cancel()`, but an in-flight call is
a submission already past the cap pre-check, and `CancelledError` is a
`BaseException` that every layer built to notice a dropped write (see **A
dropped write is counted, not silent** below) catches as `Exception` and so
never sees — cancelling there could leave a `paper_ticket` row stuck at
`pending` with nothing logged and nothing counted. `stop()` instead sets an
event the run loop checks before picking up its next item, then waits up to
`STOP_TIMEOUT_S` (5s) for the current iteration to finish on its own, and only
cancels as a last resort if that grace period is exceeded. It also logs rather
than swallows a consumer task that ended on a real exception, and `start()`
checks whether the previous task actually finished before deciding whether a
restart is needed — otherwise a crashed consumer would look identical to a
running one and never come back.

Account-level equity is computed by `shared/equity.py:account_equity`.
`PnlSnapshotService` and `GET /paper/{account_id}` both call it; if they diverged, the
account strip and the equity curve would disagree on the same page.
`GET /paper/{account_id}/positions` (`arbys/backend/app.py:418-428`) does
**not** go through it — it recomputes a mark and per-position unrealized
inline, because it needs the per-outcome breakdown `account_equity` doesn't
return. Keep its mark logic (mid, falling back to `avg_price` when there's no
live quote) in step with `account_equity`'s if that ever changes.

## A dropped write is counted, not silent

Every persistence path in the paper broker and ticket service swallows its
exception on purpose: a broken trade is worse than an unrecorded one. The flaw
that cost real data was that a swallowed write was **indistinguishable from a
successful one**. On 2026-08-25 that left a ticket stuck at `pending` and
`paper_position.realized_pnl` $132 adrift from the broker's own figure, with
nothing anywhere saying rows had been lost.

All of those writes now go through `db/session.py:run_write`, which allows
up to three attempts when it hits `database is locked` and, if it still
fails, counts it.
`GET /health` reports `dropped_writes` and `last_dropped_write`. **Non-zero
means the ledger on screen is incomplete** — treat any figure derived from it
as a lower bound until the count is back to zero.

Only `database is locked` is retried. Any other exception is counted once and
logged as itself, so a real bug is never filed as contention.

## Known defects

**`paper_position.realized_pnl` is understated for rows written before
2026-08-25.** Lock contention dropped an unknown number of `on_position`
upserts while every write path swallowed its exception, so the stored
figure sits $132 below the in-memory broker's own total — the whole gap on
Polymarket, with Kalshi agreeing to the cent. `run_write` stops further
loss but cannot reconstruct what was never written, and there is no record
of what was lost. Treat realized PnL derived from `paper_position` on a
pre-existing database as a lower bound. A resync from broker state would
overwrite rather than reconstruct, so it is deliberately not done.

Previously listed here and now fixed (migration `0002`): `paper_position` had no
`venue_id`, so restart hydration fanned every row out to all three brokers and
`GET /paper/{account_id}` reported qty and realized PnL 3× inflated. The table is
now keyed on `account_id` + `venue_id` + `outcome_id`, `bootstrap()` routes each
row to its owning broker only, and `test_open_positions_hydrate_once_per_venue`
in [tests/test_backend_e2e.py](tests/test_backend_e2e.py) covers the restart
path. **Keep `venue_id` in that key** — dropping it silently reintroduces the
inflation, which no fresh-process test can catch.

Also previously listed here and now fixed (2026-08-22/23), all three found
while building the dense opportunity table:

**Sizing ignored book depth.** `arb_engine` set `qty = target_payoff` with
`DEFAULT_TARGET_PAYOFF = 100`, so every opportunity was sized at a flat 100
contracts whether the book held 3 or 419,882. Sizing is now
`min(depth, stake_budget)` via `shared/qty.py:tradeable_qty`.

**The paper broker filled more than was resting.** `_preview_fill` blocked
only an explicit size `0` and never compared `qty` to `resting`, so an order
for 100 filled completely against a book with 3 available. It now returns
`insufficient_liquidity`, distinct from `no_liquidity`. **Reject, not
partial-fill** — a partial on one leg of a two-leg arb leaves an unhedged
position, the one outcome the design exists to avoid.

**The detection gate was dimensionally wrong.** `detect_cross_venue_two_leg`
compared a per-contract cost against a total payoff
(`total_unit_cost >= target_payoff` with `target_payoff = 100`), so it never
fired; the downstream `profit <= 0` check happened to reduce to the correct
test. Never a live defect, but it broke the moment `qty` stopped equalling
payoff. The gate is now an explicit per-contract
`net_edge_per_contract(...) <= 0`.

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
