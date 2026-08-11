# Polymarket US migration — design

**Date:** 2026-08-11
**Status:** approved, not yet implemented

## Problem

Arbys reads Polymarket's international CLOB (`gamma-api.polymarket.com`,
`clob.polymarket.com`). That book is **not tradeable from the US**. Polymarket
now runs a separate CFTC-regulated exchange, Polymarket US, as its own
Designated Contract Market with its own order book. The books are not connected
and shares are not fungible between them.

Every edge the scanner currently publishes against the international book is
therefore untakeable. The scanner must read the book we can actually trade.

### Measured divergence (2026-08-11)

34 MLB moneyline sides matched on start time across both books:

| Statistic | Value |
| --- | --- |
| Median mid divergence | 0.25¢ |
| Largest divergence | 2.75¢ (SEA/NYY — intl 0.470/0.480 vs US 0.445/0.450) |
| Tick size | US 0.5¢; international 1–2¢ |
| Gross-crossable pairs | 3 (SEA +2.0¢, CLE +1.0¢, PHI +0.5¢) |

The two books are close but genuinely independent, and the US book quotes
tighter.

## Scope

The end state is full market-type coverage across both venues. That is too
large for one change, so it is split into three sequenced specs. **This document
specifies Phase 1 only**; Phases 2 and 3 are recorded here as the roadmap and
get their own specs.

The split exists because the work has two independent axes — swapping the venue,
and widening the market types. Bundled, a zero-arb result is unattributable
between the two. Separated, Phase 1 is mechanical and live-verifiable, and
Phase 2 isolates the genuinely risky part (spread sign normalization).

| Phase | Delivers | Risk |
| --- | --- | --- |
| **1 (this spec)** | Venue swap at parity + fee fix + taxonomy groundwork | Low — mechanical |
| 2 | MLB + NFL full-game spreads | High — sign/anchor normalization |
| 3 | First-five, halves, quarters | Low once Phase 2 lands |

## API facts (verified live, 2026-08-11)

Two hosts. Only the first is used in Phase 1.

- **`gateway.polymarket.us`** — public. No API key, no KYC, no wallet.
- **`api.polymarket.us`** — authenticated (Ed25519). Orders, portfolio, and the
  market WebSocket. Requires completed identity verification.

Endpoints used:

| Endpoint | Purpose |
| --- | --- |
| `GET /v2/sports` | League catalogue |
| `GET /v2/leagues/{slug}/events` | Discovery — every market type for a league |
| `GET /v1/markets/{slug}/bbo` | Quotes — best bid/ask, depth, open interest |
| `GET /v1/markets/{slug}/book` | Full ladder (not used in Phase 1) |

Measured: 53 concurrent `/bbo` calls returned in **1.46s, all HTTP 200, no rate
limiting**.

`GET /v1/markets` is **not** usable for bulk quotes — it ignores a `slugs`
filter, returns closed markets, and its `marketSides[].price` is ambiguous
between the long-side bid and ask. Always use `/bbo`.

### Coverage

Polymarket US carries everything currently discovered, and more:

| | Polymarket US | Used in Phase 1 |
| --- | --- | --- |
| MLB / NFL / NBA moneyline | yes | yes |
| ATP / WTA tennis | yes | yes |
| NFL totals | yes | yes |
| MLB totals | yes | **no** — Phase 2+ |
| Spreads, first-five, halves, quarters | yes | no — Phases 2, 3 |

Kalshi has counterparts for all of it: `KXMLBSPREAD`, `KXMLBF5SPREAD`,
`KXMLBF5TOTAL`, `KXNFL1HSPREAD`, `KXNFL1HTOTAL`, `KXNBA1QSPREAD`, and so on.

Two-sided orderbook liquidity, sampled 12 markets per series on 2026-08-11 —
all twelve two-sided in every series: `KXMLBGAME`, `KXMLBTOTAL`, `KXMLBSPREAD`,
`KXMLBF5TOTAL`, `KXMLBF5SPREAD`, `KXNFLTOTAL`, `KXNFLSPREAD`.

> Kalshi's `GET /markets` list endpoint returns `yes_bid`/`yes_ask` as `null`
> even for markets with live books. Liquidity must be measured through
> `GET /markets/{ticker}/orderbook`, which is what the adapter already does.
> Hammering the list endpoint concurrently also returns spurious empty results;
> sample sequentially.

### Two traps that disappear

Both are documented in CLAUDE.md as Polymarket-international problems and do not
exist on the US gateway:

- **The 100-row cap.** The international flat `/markets` endpoint caps at 100
  rows ordered by 24h volume, where league games never outrank politics.
  `/v2/leagues/{slug}/events` is league-scoped, so there is nothing to outrank.
- **Question parsing.** `parse_vs_question` existed because the international
  payload only identified teams in prose. Polymarket US returns
  `teams[].name` (`"Arizona Diamondbacks"`) and `teams[].abbreviation`
  structured. The existing `TeamResolver.by_polymarket_name` resolves those
  names unchanged.

`startTime` is a clean UTC instant, so the 90-minute start-time matcher works
directly with no Polymarket-side date heuristics.

## Design

### 1. Venue identity

`venue_id = "polymarket_us"` throughout.

Deleted outright, not left dormant:

```
arbys/adapters/polymarket.py
arbys/discovery/polymarket_sports.py
arbys/discovery/polymarket_tennis.py
arbys/discovery/polymarket_totals.py
tests/adapters/test_polymarket.py
tests/discovery/test_polymarket_sports.py
scripts/smoke_polymarket_ws.py
```

Rationale for a rename over repointing `"polymarket"` in place: `venue_id` is
carried alongside `outcome_id` on every leg, position, and fill precisely
because outcome ids are venue-native and not portable. Leaving the string
`"polymarket"` pointing at a different exchange makes that identifier lie, and
it is the identifier live execution will route on.

### 2. Adapter — `arbys/adapters/polymarket_us.py`

A Polymarket US market is a single binary contract with a long and a short side,
structurally like a Kalshi market rather than like Polymarket international's
two-token pair. So `outcome_id` follows the established Kalshi convention
(`{ticker}:YES` / `{ticker}:NO`, see `arbys/adapters/kalshi.py`):

```
{market_slug}:LONG
{market_slug}:SHORT
```

Each `/bbo` response produces two `Quote`s:

```
bbo: bestBid = 0.4550, bestAsk = 0.4600

  {slug}:LONG    bid = 0.4550          ask = 0.4600
  {slug}:SHORT   bid = 1 - 0.4600      ask = 1 - 0.4550
                     = 0.5400              = 0.5450
```

This inversion is the only real arithmetic in the adapter, and a side error is
silent — it produces plausible prices that invent edges. It gets a dedicated
unit test with hand-computed expectations.

All prices parse to `Decimal`, never float, per the project convention.
`Quote.__post_init__` already enforces `[0,1]` and `ask >= bid`; the inversion
preserves both.

Polling interval: `ARBYS_POLYMARKET_US_POLL_S`, default `5.0`, matching the
Kalshi REST fallback. Slugs are deduplicated per poll, since `:LONG` and
`:SHORT` share one HTTP call.

**No WebSocket in Phase 1.** No credential-reading code, no WS adapter, no
`polymarket_us_ws.py`. The factory in `arbys/backend/state.py` constructs the
REST adapter unconditionally:

```python
factories["polymarket_us"] = lambda oids: PolymarketUsAdapter(outcome_ids=oids)
```

The seam it will later occupy already exists and is proven — `_kalshi_factory`
selects `KalshiWebSocketAdapter` over `KalshiAdapter` when
`kalshi_ws_creds_from_env()` returns credentials. Adding the Polymarket US WS
means following that pattern; it does not mean changing anything written in
Phase 1. Nothing is stubbed ahead of need here.

### 3. Discovery — `arbys/discovery/polymarket_us.py`

One module replaces three. `/v2/leagues/{slug}/events` returns every market type
for a league in a single call, so the team-sports, totals, and tennis paths
differ only in which league slugs they request and which `sportsMarketType`
values they keep.

League slug table:

```python
LEAGUE_SLUGS = {"mlb": "mlb", "nfl": "nfl", "nba": "nba",
                "atp": "atp", "wta": "wta"}
```

Market types kept in Phase 1:

```python
MONEYLINE = {"baseball_team_full_game_winner",
             "football_team_full_game_winner",
             "basketball_team_full_game_winner",
             "tennis_match_winner"}
TOTAL     = {"football_team_full_game_total"}   # NFL only in Phase 1
```

Every other `sportsMarketType` is ignored. Unknown types are logged at debug and
skipped, never raised — a new market type appearing upstream must not break
discovery.

> `basketball_team_full_game_winner` was verified against **WNBA**, which had 4
> open events on 2026-08-11. `/v2/leagues/nba/events` returned **zero** events —
> the NBA offseason, the same condition that left `KXNBAGAME` unverified per
> CLAUDE.md. Recheck the NBA league slug and its type strings when the season
> opens; both venues remain unverified for NBA.

The Kalshi side of discovery is untouched.

### 4. Taxonomy groundwork

`matcher._pair_key` currently buckets on `(sport, market_type, line, team-pair)`.
That is correct for moneyline and totals and **wrong for spreads**, because a
spread's line is meaningless without the team it is stated for. The two venues
anchor it differently:

```
Kalshi   KXMLBSPREAD-26AUG112145HOUSF-SF3
         yes_sub_title "San Francisco wins by over 2.5 runs"
         floor_strike 2.5
         → team named in the ticker suffix, threshold structured

PolyUS   asc-mlb-cle-det-2026-08-11-neg-2pt5     line = -2.5
         long  = CLE -2.50     short = DET +2.50
         → signed line ALWAYS anchored to the first team in the slug
```

`CLE -2.5` is equivalent to `CLE wins by over 2.5` (half-run line, no push), so
Polymarket's long side is Kalshi's YES and its short side is Kalshi's NO — the
markets are genuinely the same binary. But bucketing on `(sport, start,
market_type, line)` alone pairs `CLE -2.5` against `DET -2.5` and invents an
arb. This is the identical failure mode CLAUDE.md already records for the totals
line, which is why the line must stay in the bucket key.

Phase 1 therefore adds, ahead of need:

- an `anchor: str | None` field on `VenueGame` and `CrossVenueMatch`, included
  in `_pair_key`. `None` for moneyline and totals, so Phase 1 behavior is
  bit-identical to today.
- `CrossVenueMatch.yes_key()` generalized from its current
  `OVER if market_type == "total" else team_a.code` into a per-market-type
  dispatch keyed on `market_type`.

This is roughly 20 speculative lines. It is included because the alternative is
refactoring the matcher and debugging spread sign conventions in the same
change, and the matcher is the component where a mistake silently fabricates
guaranteed profit.

### 5. Fees

`PolymarketFeeModel` currently returns **zero**. That is wrong for both books
and is a live correctness defect independent of this migration: every net edge
published on a Polymarket leg is overstated.

Official schedules, both the same shape as Kalshi's:

| Venue | Taker | Rounding |
| --- | --- | --- |
| Kalshi | `0.07 · C · p · (1-p)` | up, to the cent, per contract |
| Polymarket US | `0.06 · C · p · (1-p)` | banker's, to the cent |
| Polymarket intl (sports) | `0.05 · C · p · (1-p)` | 5 decimal places |

Phase 1 adds:

```python
@dataclass(frozen=True)
class PolymarketUsFeeModel:
    venue_id: str = "polymarket_us"
    rate: Decimal = Decimal("0.06")

    def fee(self, *, price, qty, is_buy):
        if qty <= 0:
            return Decimal("0")
        return self.rate * price * (Decimal("1") - price) * qty
```

Per CLAUDE.md, the test in `tests/shared/test_fees.py` is written **first**.

Expect published net edges to fall relative to today. That is the defect being
fixed, not a regression, and it should be stated plainly in the commit message
so a future reader does not "restore" the old numbers.

Maker rebates (-0.0125 on Polymarket US) are **not** modelled — the paper broker
fills against the ask as a taker, so a maker rebate would never apply. Volume
tiers are likewise out of scope.

Not fixed here, and left as a known understatement consistent with the existing
Kalshi note: neither venue's rounding is modelled, so fees come out slightly low
and marginal edges look slightly better than they are.

### 6. Persistence

Paper rows under `venue_id='polymarket'` are **deleted**, not remapped. Their
`outcome_id`s are Polymarket international CLOB token ids, which do not identify
anything on the US book; remapping them is not possible even in principle. This
is a paper account, so the cost is discarded simulated history.

Affected: `paper_position`, `paper_balance`, `paper_fill`, `paper_order`,
`opportunity`, and `event_group` rows whose legs reference the venue.

The migration describes the change in explicit `op.*` calls, frozen at this
point in history. It must **not** build DDL from `Base.metadata` — CLAUDE.md
records that `0001_initial` did exactly that and broke every later revision.
`tests/db/test_migrations_match_models.py` replays the chain from empty and will
catch a wrong or missing revision.

`bootstrap()` re-seeds `DEFAULT_STARTING_BALANCE` for `polymarket_us` on first
run through the existing unfunded-venue path, so no special-casing is needed.

### 7. Frontend

Four hardcoded strings:

| File | Change |
| --- | --- |
| `frontend/src/pages/TerminalPage.tsx:19` | `VENUES` → `"Polymarket US"` |
| `frontend/src/pages/TerminalPage.tsx:130` | connected tag label |
| `frontend/src/pages/AdminPage.tsx:7` (+ 3 defaults) | `"polymarket"` → `"polymarket_us"` |
| `frontend/src/components/OpportunityCard.tsx:105` | leg lookup by `venue_id` |

No styling changes. Per CLAUDE.md, no new hex colors, radii, or type scales —
label text only.

### 8. Config

`.env.example` gains:

```
ARBYS_POLYMARKET_US_POLL_S=5
# Phase 1 ships REST-only; these are read but unused until the WS adapter lands.
# POLYMARKET_US_API_KEY_ID=
# POLYMARKET_US_PRIVATE_KEY_PATH=
```

As with the Kalshi key, any `.pem` stays **outside** this repo.

## Error handling

- A failed `/bbo` call yields no quote for that slug on that cycle. Quotes then
  age out under `ARBYS_QUOTE_MAX_AGE_S` (default 600s) rather than going stale
  silently — the existing only-tradeable invariant covers this unchanged.
- A failed league fetch raises out of that sub-pass. `discover_all_event_groups`
  already marks the pass incomplete and **skips retirement**, so a Polymarket US
  outage is not read as every game being delisted. This behavior must be
  preserved; it is load-bearing.
- Unknown `sportsMarketType` values are skipped, not raised.
- Retirement must continue to call `clear_group_opportunities` — an unregistered
  group is never re-evaluated, so nothing else would empty its opportunity set.

## Testing

Per CLAUDE.md, **tests never hit a real venue**. REST paths mock with
`httpx.MockTransport` against captured real payloads;
`tests/adapters/test_polymarket.py` is the template (it is deleted, but its
shape is the model for the replacement).

| Test | Covers |
| --- | --- |
| `tests/shared/test_fees.py` | `PolymarketUsFeeModel` — written first |
| `tests/adapters/test_polymarket_us.py` | `/bbo` parse; **LONG/SHORT inversion with hand-computed values**; poll loop; HTTP failure yields no quote |
| `tests/discovery/test_polymarket_us.py` | league events parse; moneyline + NFL totals kept; other `sportsMarketType` skipped; team resolution via `by_polymarket_name` |
| `tests/discovery/test_matcher.py` | extended: `anchor` in bucket key; `anchor=None` matches today's behavior byte-for-byte |
| `tests/db/test_migrations_match_models.py` | existing replay covers the new revision |
| `tests/test_backend_e2e.py` | existing restart-hydration test, retargeted at `polymarket_us` |

Green-build bar is unchanged and must hold: `pytest` (128 tests today, more
after), `ruff check .`, and `npm run build` in `frontend/`. mypy is **not** part
of the bar — 47 pre-existing errors across 17 files. Annotating new code is
welcome; a cleanup pass is out of scope.

Manual verification: `scripts/smoke_polymarket_us.py`, mirroring the existing
smoke scripts, hitting the live gateway to confirm discovery and quotes end to
end. Run from the repo root — `ARBYS_DB_URL` defaults to a relative path and
starting elsewhere silently creates a second empty database.

## Non-goals for Phase 1

Explicitly deferred, each to a named phase:

- Spreads, first-five, halves, quarters — Phases 2 and 3.
- **MLB totals** — available on Polymarket US, one line in `TOTALS_SPORTS`, held
  back so the port has exactly one behavioral variable.
- The authenticated WebSocket feed and Ed25519 credential handling.
- Live (non-paper) execution.
- Maker rebates, volume tiers, and fee rounding.
- The 47 outstanding mypy errors.

## Documentation to update

CLAUDE.md carries three statements this change falsifies. All were true when
written and are true of the *international* book; they must not be silently
left to mislead:

1. **"Polymarket carries no baseball totals"** — false for Polymarket US, which
   carries `baseball_team_full_game_total`. Update to say MLB totals are now
   possible and deliberately not yet wired.
2. **"Neither venue publishes a live score or game clock"** — false for
   Polymarket US. Verified 2026-08-11: `wtt-diychi-sreaku-2026-08-11` returned
   `score: "11-8, 11-6, 5-5"`, `period: "S3"`, `live: true`. Still true for
   Kalshi and for Polymarket international. The decision to rely on the
   countdown alone is unchanged, but the stated reason no longer holds.
3. **The 100-row `/markets` cap and `parse_vs_question`** — both
   international-only. Reframe as history rather than live constraints.

`docs/RUNBOOK.md` needs its adding-a-venue and troubleshooting sections
retargeted.

## Open questions

None. All decisions are settled:

| Decision | Resolution |
| --- | --- |
| Venue identity | Rename to `polymarket_us`; delete international |
| Quote feed | REST-poll `/bbo`; WS deferred behind creds |
| Market-type scope | Full coverage as the destination, in three phases |
| Existing paper data | Deleted by migration, not remapped |
| Taxonomy groundwork | Included in Phase 1 |
