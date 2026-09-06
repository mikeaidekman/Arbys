# Spreads (Phase 2) and a discovery horizon — design

**Date:** 2026-09-06
**Status:** approved, building

## Goal

Discover full-game spread markets on Kalshi and Polymarket US for MLB, NFL and
college football, register them as cross-venue event groups, and let the
engine, broker and auto-trader treat them exactly like every other binary
group. Alongside it, bound how far into the future discovery looks, for every
market type, so capital is never locked in a game days from kickoff.

Two decisions were made during design and are fixed here:

- **All three leagues at once** — MLB, NFL and NCAAF. Not staged.
- **Spreads trade immediately.** No auto-trader allowlist, no holdout. The
  paper account is the validation harness and the plausible-edge ceiling
  already refuses a badly inverted pair.

## Why the horizon is part of this work

Discovery has no time bound today; it registers every game both venues list.
Kalshi lists NFL a week or more ahead, so on 2026-09-03 the local database held
174 upcoming groups of which **127 started more than 7 days out** — beyond the
fill rule (`ARBYS_MAX_DAYS_TO_START`, 7), so they could never trade and were
pure subscription cost. Spreads triple the group count, which turns that waste
from tolerable into the dominant load.

The trading value forgone by a 3-day horizon is negligible. Local ledger,
1,248 filled tickets, 2026-08-28 to 2026-09-03:

| lead time before game | fills | expected profit |
| --- | --- | --- |
| game day | 1,177 | $174.68 |
| 1 day | 21 | $0.59 |
| 2–3 days | 32 | $1.35 |
| 4–7 days | 16 | $0.66 |
| > 7 days | 2 | $0.01 |

A 3-day horizon forgoes 1.4% of fills and 0.4% of expected profit. It does
**not** lower the weekend peak — a Saturday CFB slate and a Sunday NFL slate
both fall inside 3 days of each other by Thursday — so it is a scope reducer,
not the protection against a socket ceiling. That protection is the kill
switch and post-deploy measurement (§13).

With the horizon at 3 days nothing can be filled more than 3 days out, because
the group does not exist yet. The horizon is therefore what bounds capital
lock; the 7-day fill rule stays as the chokepoint backstop for hand-registered
groups and is not changed.

## What the venues publish (measured 2026-09-06)

### Kalshi

Series `KXMLBSPREAD`, `KXNFLSPREAD`, `KXNCAAFSPREAD` (`KXWNBASPREAD` and
`KXNBASPREAD` exist and return zero events in the off-season). Event tickers
share the totals stem: `KXNFLSPREAD-26SEP09NESEA`, date (and `HHMM` for MLB)
then the two team codes concatenated. **One market per (team, line)**:

```
KXNFLSPREAD-26SEP09NESEA-SEA4
  yes_sub_title  "Seattle wins by over 3.5 points"
  floor_strike   3.5
  strike_type    "greater"
```

The market ticker suffix is `<CODE><N>` where `CODE` is one of the event's two
team codes and `N = floor_strike + 0.5` on all 1,480 markets observed. `N` is
not used; `floor_strike` is the line. `strike_type` was `greater` on all 1,480.

| series | open events | markets per event |
| --- | --- | --- |
| KXMLBSPREAD | 15 | 6 (±1.5, ±2.5, ±3.5 per team) |
| KXNFLSPREAD | 16 | 24–27 |
| KXNCAAFSPREAD | 51 (36 with both codes in our table) | 16–32 |

Two Kalshi codes are missing from our tables: `AZ` (Diamondbacks, ours `ARI`)
and `JAC` (Jaguars, ours `JAX`). `split_team_codes` fails on `AZHOU` and
`CLEJAC`, so those games are already dropped from totals today.

### Polymarket US

Types `baseball_team_full_game_spread`, `football_team_full_game_spread`
(`basketball_team_full_game_spread` presumed, unverified). First-five, half and
quarter spreads are distinct types (`baseball_team_first_five_spread`,
`football_team_first_half_spread`, …) and are Phase 3. **One market per signed
line on the first-listed team**:

```
slug     asc-nfl-ne-sea-2026-09-09-pos-3pt5
line     3.5                       ← signed; neg-3pt5 carries -3.5
question "Will the New England Patriots cover 3.5 vs the Seattle Seahawks …"
title    "Seattle Seahawks wins by over 3.5 points"
sides    long  team=New England Patriots
         short team=Seattle Seahawks
```

Invariants checked across every full-game spread market in the MLB, NFL and
CFB league feeds (1,250 + 4,017 markets), **zero violations**:

- slug team order equals `teams[]` order equals (long side team, short side team)
- `line` sign equals the slug's `pos`/`neg`
- `title` names the team that must win by more than `|line|`: the first team
  when `line < 0`, the second when `line > 0`; and the title's number equals
  `|line|`

| league | events | full-game spreads per event |
| --- | --- | --- |
| mlb | 56 | 4 (±1.5, ±2.5) |
| nfl | 32 | 32–33 (0.5 … 21.5 per side) |
| cfb | 247 | 14–42 |

### The sign convention, pinned by price

Long on a Polymarket spread is "the first-listed team covers the signed
line". Live top-of-book, 2026-09-06 16:20Z:

| Polymarket market | meaning | PM long bid/ask | Kalshi market | Kalshi side | bid/ask |
| --- | --- | --- | --- | --- | --- |
| `ne-sea … pos-3pt5` | NE +3.5 | 0.51 / 0.52 | `NESEA-SEA4` | **NO** | 0.51 / 0.52 |
| `ne-sea … neg-3pt5` | NE −3.5 | 0.26 / 0.27 | `NESEA-NE4` | **YES** | 0.25 / 0.27 |
| `sf-lar … pos-10pt5` | SF +10.5 | 0.72 / 0.74 | `SFLAR-LAR11` | **NO** | 0.72 / 0.74 |
| `sf-lar … neg-10pt5` | SF −10.5 | 0.08 / 0.11 | `SFLAR-SF11` | **YES** | 0.08 / 0.14 |

So: **`line < 0` → LONG ≡ Kalshi YES on `<first team><|line|+0.5>`;
`line > 0` → LONG ≡ Kalshi NO on `<second team><|line|+0.5>`.** Getting this
backwards near even money produces a 2–4¢ phantom edge that the 15¢
plausible-edge ceiling cannot see, which is why the parser cross-checks it at
runtime (§5) and why the end-to-end test (§11) encodes it with these prices.

### Projected scale

Shared (anchor, line) pairs on games both venues list, today:

| league | games on both | spread groups |
| --- | --- | --- |
| mlb | 15 | 60 |
| nfl | 15 | 341 |
| cfb | 36 | 769 |
| **total** | | **~1,170** |

On top of ~570 groups today. With the 3-day horizon the Sunday-afternoon count
is ~140 and the Friday/Saturday peak ~1,050.

## 1. Canonical form

Every spread on either venue becomes a `VenueGame` with:

- `market_type = "spread"`
- `line` — **positive** `Decimal`
- `anchor` — the **code** of the team that must win by more than `line`
- `outcome_ids` keyed by **team code**, like a moneyline: the anchor's key maps
  to the side that pays if the anchor covers, the other team's key to its
  complement

The proposition "anchor wins by more than line" is the group's TRUE side.
`CrossVenueMatch.yes_key()` already returns `self.anchor` for spreads and
`_pair_key` already buckets on `(market_type, line, anchor)`, so **no matcher
logic changes** beyond id and title (§6). `CLE −2.5` and `DET −2.5` land in
different buckets by construction.

## 2. Kalshi parser — `arbys/discovery/kalshi_spreads.py`

Mirrors `kalshi_totals.py`. Registry:

```python
SPREADS_SERIES = {
    "mlb": "KXMLBSPREAD",
    "nfl": "KXNFLSPREAD",
    "ncaaf": "KXNCAAFSPREAD",
    # Unverified: both return zero events in the off-season (2026-09-06).
    "nba": "KXNBASPREAD",
    "wnba": "KXWNBASPREAD",
}
```

`fetch_kalshi_spreads(*, resolver, sport, series_ticker=None, http_client=None,
limit=100, horizon_days=None)`:

1. `GET /events?series_ticker=…&status=open`.
2. Per event: match the totals `_TICKER_RE`, `_parse_ticker_date`,
   `split_team_codes` → `(team_a, team_b)`. Unsplittable → skip (debug log, as
   totals does).
3. **Horizon check before the market call** (§8). Out of horizon → skip.
4. `GET /markets?event_ticker=…&limit=60`.
5. Per market:
   - `rest = ticker[len(event_ticker) + 1:]`. Exactly one of the two codes must
     satisfy `rest.startswith(code) and rest[len(code):].isdigit()`; that code is
     the anchor. Zero or two matches → skip. This enforces "the anchor is one
     of the event's teams" in the same step, with no regex over variable-width
     codes.
   - `strike_type` must be `"greater"`. Anything else inverts the proposition
     → skip with a warning.
   - `line = Decimal(str(floor_strike))`; missing, invalid or `<= 0` → skip.
   - `VenueGame(sport, venue_id="kalshi", game_date, teams=(team_a, team_b),
     outcome_ids={anchor: f"{ticker}:YES", other: f"{ticker}:NO"}, ref=ticker,
     market_type="spread", line=line, anchor=anchor,
     start_time=parse_ticker_start(event_ticker))`.

The anchor code is the **resolved team's canonical code** (`team.code`), not
the raw ticker text, so `AZ2` yields anchor `ARI` (§9).

## 3. Polymarket US parser — `fetch_polymarket_us_spreads` in `polymarket_us.py`

Beside `fetch_polymarket_us_totals`. Registry:

```python
SPREAD_TYPES = frozenset({
    "baseball_team_full_game_spread",
    "football_team_full_game_spread",
    "basketball_team_full_game_spread",  # unverified, off-season
})
```

Per event: `startTime` (skip if missing), `_live_flags`, two `teams[]` both
resolved via `_resolve_team` (else skip). Per market with type in
`SPREAD_TYPES`:

1. `slug`, `line = _line(market)` (**signed**). Missing or `0` → skip.
2. `_sides(market)` → `(long, short)`; not binary → skip.
3. **Structural guard.** `_resolve_team(long["team"])` must be `teams[0]` and
   `_resolve_team(short["team"])` must be `teams[1]`. Otherwise skip with a
   warning — the derivation below assumes long is the first-listed team.
4. `anchor_idx = 0 if line < 0 else 1`; `anchor = teams[anchor_idx]`;
   `abs_line = abs(line)`.
5. **Title guard.** Match `title` against
   `^(?P<team>.+?) wins by over (?P<line>\d+(?:\.\d+)?) (?:runs|points)$`.
   - Match, and `team` equals the anchor's raw `teams[anchor_idx]["name"]`
     string and `Decimal(line) == abs_line` → accept.
   - Match, but team or line disagrees → **skip with a warning** naming the
     slug, title and derived anchor. Two venue fields contradicting each other
     means the convention moved.
   - No match → **accept**, and count. The fetcher logs one warning per pass
     with the count of unparseable titles, so a venue rewording titles disables
     the guard *visibly* rather than silently zeroing the league.
   The comparison is string equality on `teams[].name`, not resolution:
   the title uses the same string the event's `teams[]` carries, including
   the bare mascot on CFB (`"Tar Heels wins by over 20.5 points"`), and
   matched it on all 5,267 markets.
6. `VenueGame(sport, venue_id="polymarket_us", game_date=_eastern_date(start),
   teams=(t0, t1), outcome_ids={t0.code: f"{slug}:LONG", t1.code: f"{slug}:SHORT"},
   ref=slug, market_type="spread", line=abs_line, anchor=anchor.code,
   start_time, live, ended)`.

Note `outcome_ids` is **always** first team → LONG, second team → SHORT;
only the anchor flips with the sign. That is the same shape as the
moneyline (each team's key is the side that pays if that team "wins" the bet).

## 4. Identity and display — `matcher.py`

- `event_group_id()`: for `market_type == "spread"`,
  `f"{base}-spread-{anchor}-{_fmt_line(line)}"`, e.g.
  `nfl-NE-SEA-2026-09-09-spread-SEA-3.5`. Totals and moneyline unchanged.
  Both id parsers (`backend/performance.py:_GROUP_ID`,
  `frontend/src/lib/performance.ts:GROUP_ID`) read the segment after the date
  as the market type and the remainder as opaque, so `by_market_type` gains a
  `spread` row with no change to either.
- `event_group_title()`: for spreads,
  `f"{matchup} — {anchor_full_name} -{_fmt_line(line)} ({game_date})"`, e.g.
  `New England Patriots vs Seattle Seahawks — Seattle Seahawks -3.5 (2026-09-09)`.
  The anchor's full name is whichever of `team_a`/`team_b` has `code == anchor`.
  `splitTitle` in the frontend splits on the spaced em-dash already.

## 5. Discovery service — `service.py`

- `SPREADS_SPORTS = (("nfl", NFL_RESOLVER), ("mlb", MLB_RESOLVER), ("ncaaf", CFB_RESOLVER))`.
- The three existing pair functions (`discover_team_sport_event_groups`,
  `discover_totals_event_groups`, and what would be a fourth for spreads) are
  identical apart from their two fetchers and the log label. They collapse into
  one `_discover_pair(label, kalshi_coro, poly_coro)` helper; the three named
  functions remain as thin wrappers so existing callers and tests are
  untouched.
- `discover_all_event_groups` adds
  `*(discover_spreads_event_groups(s, r) for s, r in SPREADS_SPORTS)` **when
  `ARBYS_ENABLE_SPREADS` is on** (default `1`). The flag is the kill switch for
  a scale problem found in production: `fly secrets set` and a restart, rather
  than a redeploy through the Action. Read in `service.py`, where
  `ARBYS_DISCOVERY_CONCURRENCY` is already read.

## 6. Discovery horizon — new `arbys/discovery/horizon.py`

```python
DEFAULT_HORIZON_DAYS = 3

def discovery_horizon_days() -> int          # ARBYS_DISCOVERY_HORIZON_DAYS, 0 disables
def eastern_today() -> date
def within_horizon(game_date: date, *, days: int, today: date | None = None) -> bool
    # days <= 0 -> True; else game_date <= today + days
def filter_horizon(games: list[VenueGame], *, days: int, today: date | None = None) -> list[VenueGame]
```

Judged on **`game_date`**, which both venues already carry as an Eastern
calendar date (Kalshi from the ticker, Polymarket via `_eastern_date`). Using
the date rather than `start_time` means the two venues always agree on whether
a game is in, so a game never has one leg inside the window and one outside,
and there is no boundary flapping. Day granularity means "3 days" admits a
game up to the end of the third calendar day.

Applied in two places:

1. **Once in the service**, to every venue's game list before `match_games`,
   inside `_discover_pair` and in the tennis and UFC passes. This is the rule.
2. **Inside the Kalshi event parsers before the per-event `/markets` call** —
   team sport, totals and spreads — because the date is in the event ticker
   and the market call is what costs a request. On a Sunday this skips the
   majority of Kalshi requests, which is where the 429s that halved coverage
   came from. This is an optimisation of the same rule, not a second rule.

`horizon_days` is a keyword parameter on each fetcher, `None` meaning "read
the config", so tests pass an explicit value and never touch the environment.

Groups already registered are unaffected: a game can only move toward the
window, never out of it, so nothing is retired by the horizon that was found
by it. A group is retired only when a complete pass no longer finds it, as
today.

## 7. Team code aliases — `teams.py`

`TeamResolver.__init__` gains `code_aliases: dict[str, str] | None = None`
(alias code → canonical code). `by_code` checks `_by_code` first, then the
alias map. `MLB_RESOLVER` gets `{"AZ": "ARI"}`, `NFL_RESOLVER` gets
`{"JAC": "JAX"}`. Everything that resolves a Kalshi code — `split_team_codes`,
the spread suffix — goes through `by_code`, so totals and spreads are both
fixed. The moneyline path resolves `yes_sub_title` by city and was never
affected. The returned `Team.code` is canonical, so outcome keys and anchors
read `ARI`/`JAX` and match Polymarket's.

## 8. Config

Two new variables, documented in `.env.example` and CLAUDE.md's **Config**:

| variable | default | meaning |
| --- | --- | --- |
| `ARBYS_ENABLE_SPREADS` | `1` | run the spreads sub-passes; `0` removes them from discovery (existing spread groups then retire on the next complete pass) |
| `ARBYS_DISCOVERY_HORIZON_DAYS` | `3` | furthest Eastern game date discovery will register, all market types; `0` disables |

Unchanged and deliberately so: `ARBYS_MAX_DAYS_TO_START` (7) — the fill-time
backstop for groups the horizon did not create (manual registration).

## 9. What does not change

Engine, broker, settlement, ticket service, auto-trader, WebSocket adapters,
DB schema, migrations, frontend. Spreads are binary contracts with a YES/LONG
and NO/SHORT side, which is all any of those layers know about a leg.

## 10. Tests

All venue I/O mocked with `httpx.MockTransport`, fixtures modelled on the
payloads above.

`tests/discovery/test_spreads.py`:

- Kalshi: one game per (team, line); anchor is the ticker code; `YES` maps to
  the anchor's key; a `strike_type` other than `greater` is skipped; a suffix
  that is neither team is skipped; a missing `floor_strike` is skipped;
  `AZHOU` splits and yields anchor `ARI`; an event outside the horizon makes
  **no** `/markets` request (assert on the mock transport's call log).
- Polymarket: `neg-2pt5` → anchor first team, line `2.5`, first team → `:LONG`;
  `pos-2pt5` → anchor second team, same outcome ids; a title naming the wrong
  team is skipped; a title with the wrong line is skipped; an unparseable
  title is accepted; a long side whose team is not `teams[0]` is skipped;
  `line: 0` is skipped; first-five and quarter types are ignored.
- Matcher: spread id carries anchor and line; title reads `<anchor> -<line>`;
  `match_to_event_group` marks the anchor's legs `is_yes_side=True` on both
  venues; opposite anchors on the same line do not match (already covered by
  `test_opposite_anchors_on_the_same_line_do_not_match`).
- **End to end, the convention test.** Kalshi `KXMLBSPREAD-…MILCIN-CIN3`
  (`floor_strike 2.5`) and Polymarket `asc-mlb-mil-cin-…-pos-2pt5`
  (`line 2.5`, long = Brewers) form **one** group,
  `mlb-CIN-MIL-…-spread-CIN-2.5`, whose TRUE legs are `…-CIN3:YES` and
  `…-pos-2pt5:SHORT`. Docstring cites the four measured price pairs.

`tests/discovery/test_horizon.py`: `within_horizon` at the boundary, `0`
disables, `filter_horizon` keeps near games and drops far ones,
`discover_all_event_groups` drops a far-out game from both venues and keeps a
near one, tennis/UFC passes filtered too.

`tests/discovery/test_teams.py`: `by_code("AZ")` is the Diamondbacks and its
`.code` is `ARI`; `by_code("JAC")` likewise; unknown codes still `None`.

`tests/discovery/test_service.py`: `ARBYS_ENABLE_SPREADS=0` removes the
sub-passes; the wrappers still return what they did.

Existing `test_spread_markets_are_skipped_in_phase_1` is renamed to say what
it now asserts — the moneyline fetcher yields only moneyline games — and kept.

## 11. Docs

- **CLAUDE.md**: rewrite **Phase 2 — spreads** under *Phase 2 and beyond* as
  wired, with the canonical form, the sign convention table, the title guard
  and the `AZ`/`JAC` fix; add the two config variables; note the horizon
  under *Only-tradeable invariants* beside the fill rule, with the fills table
  as the evidence for 3 days; update the market-types paragraph in
  *Architecture* (it says nothing sets `anchor` yet); update the test count.
- **RUNBOOK.md**: the discovery section gains spreads and the horizon.
- **`.env.example`**: both variables with one-line comments.

## 12. Rollout and what to watch

1. `venv\Scripts\python.exe scripts\discover_cross_venue.py --dry-run` locally:
   expect spread groups with ids of the new shape, TRUE legs on the anchor,
   and nothing beyond three days out.
2. Full suite, ruff, `npm run build` (no frontend change, but the build is the
   typecheck and the id parser is exercised by its tests).
3. Deploy. The first pass triples the group count and rebuilds every socket
   once, ~3 minutes of stale quotes as documented. Then watch, over the first
   weekend:
   - `/health` `loop_lag` p95 — the performance-1x figure was 24ms at 867
     groups; this is the first time the count passes 1,000
   - each Polymarket shard's `live N/M` — expect ~18 shards
   - Kalshi quote ages — **one socket carries every ticker and has no known
     ceiling**; the Polymarket ceiling was found only by measuring, so a
     Kalshi age creeping up on in-play markets while Polymarket stays fresh is
     the symptom to look for
   - the `spread` row in `by_market_type` on `/account`
4. Transitional effect to expect once, at deploy: hosted positions on games
   more than 3 days out lose their group on the first complete pass and sit
   unresolved (`unresolved_groups()`) until the game re-enters the window and
   is re-registered under the same deterministic id.
5. If the socket count or lag misbehaves: `ARBYS_ENABLE_SPREADS=0` first
   (targeted), `ARBYS_DISCOVERY_HORIZON_DAYS=1` second (blunt).

## 13. Non-goals

- Period markets (first-five, halves, quarters) — Phase 3; the types are
  known and excluded.
- Verifying NBA/WNBA spreads — off-season on both venues; series and types are
  registered but the sports are not.
- An auto-trader market-type allowlist — decided against.
- Changing `ARBYS_MAX_DAYS_TO_START`.
- Sharing one Polymarket league payload across the moneyline, totals and
  spreads sub-passes — three fetches of the same feed per league is bytes,
  not requests, and the gateway showed no rate limiting at 53 concurrent calls.
- Spread-specific sizing, edge floors or display changes.
