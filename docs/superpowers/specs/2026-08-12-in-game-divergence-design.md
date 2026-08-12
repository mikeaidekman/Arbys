# In-game divergence — design

**Date:** 2026-08-12
**Status:** approved, not yet implemented
**Supersedes:** the "Phase 2 — spreads" item in
[2026-08-11-polymarket-us-migration-design.md](2026-08-11-polymarket-us-migration-design.md)

## Why this replaced spreads

Phase 1 shipped a correct fee model, and the board it produced is the reason
this spec exists rather than the spreads one.

Measured 2026-08-12 on a live 326-group board: **12 gross arbs, 1 net.** The one
that cleared is the tell:

| Group | prices | fee drag | gross | net |
| --- | --- | --- | --- | --- |
| `nfl-BAL-IND-…-total-30.5` | 0.88 / 0.10 | 1.28¢ | 2.00¢ | **+0.72¢** |
| `nfl-MIN-NYG-…-total-44.5` | 0.36 / 0.62 | 3.03¢ | 2.00¢ | −1.03¢ |

Identical gross edge; opposite outcome. Both venues charge `rate · p · (1-p)`,
so drag is ~3.25¢ at a coin flip and under 1.3¢ at the extremes.

Spreads exist to *make a bet a coin flip*. A `−1.5` or `−2.5` line sits by
construction at the worst point on that curve. Adding a large,
sign-normalisation-heavy market type whose prices live exactly where edges
cannot survive is poor value, so spreads move behind this work.

**The real thesis is in-game.** Pre-game the two venues agree closely — that is
why net arbs are rare. During live play, prices move fast and a genuine
divergence can exceed 3.25¢ for a short window. Coin-flip totals, useless
pre-game, are the *most* interesting in-play: a 8.5-run total is what moves
hardest when runs actually score.

## The problem this must solve

The current plumbing cannot tell a real in-game divergence from a latency
artifact.

| Leg | Transport | Latency |
| --- | --- | --- |
| Kalshi | authenticated WebSocket (credentials are set) | real-time push |
| Polymarket US | REST poll | up to **5s** stale |
| Quote staleness ceiling | `ARBYS_QUOTE_MAX_AGE_S` | **600s** |

When a run scores, Kalshi reprices immediately while Polymarket US is still
showing a price up to five seconds old. The engine reads that gap as an edge.
It is not one: it is the same market observed at two different moments, and it
closes before anything could be executed against it.

So simply widening coverage would make the scanner *noisier*, not better.
Coverage and trustworthiness have to land together.

## Scope

In scope:

1. MLB totals (+36 groups, measured).
2. Liveness detection.
3. Two-tier adaptive polling, with concurrent fetch for the live tier.
4. Per-outcome staleness ceilings.
5. An age-skew guard on in-play groups.

Out of scope, deliberately:

- **Spreads** — deferred, not cancelled. See "Phase 3" below.
- **The Polymarket US WebSocket.** Needs completed KYC and an Ed25519 key pair
  from `polymarket.us/developer`. Items 3 and 5 are the mitigation for its
  absence; when it lands, skew collapses to near zero and the guard stops
  rejecting anything. The env placeholders already exist.
- **UI changes.** `/monitored` already exposes per-leg `quote_age_s` and
  `is_stale`, so skew is inspectable without new surface.
- **Live score display.** Polymarket US publishes `score`/`period`, Kalshi does
  not. A score on one leg only is half a picture; the countdown stays.

## Design

### 1. MLB totals

Configuration only:

```python
# arbys/discovery/polymarket_us.py
TOTAL_TYPES = frozenset({
    "football_team_full_game_total",
    "baseball_team_full_game_total",   # new
})

# arbys/discovery/service.py
TOTALS_SPORTS = (("nfl", NFL_RESOLVER), ("mlb", MLB_RESOLVER))   # new entry
```

Measured 2026-08-12: Kalshi 154 MLB total markets across 14 games, Polymarket
US 39 across 13, **12 shared games → 36 matched groups**.

Polymarket US quotes only the middle three strikes per game (e.g. 7.5/8.5/9.5)
against Kalshi's eleven (2.5–12.5). That is not a fetch limitation on either
side; the venues simply list different ladders, and only shared strikes can
match. Those three are precisely the in-play-interesting ones.

The CLAUDE.md note that MLB totals are "held back so the port had exactly one
behavioural variable" is now discharged and must be updated.

### 2. Liveness — derived from the clock

New pure module, `arbys/shared/liveness.py` (no I/O, safe to import anywhere):

```python
IN_PLAY_WINDOWS: dict[str, timedelta]   # per sport, generous
def is_in_play(start_time: datetime | None, sport: str, now: datetime) -> bool
```

A group is in play when `start_time <= now < start_time + window(sport)`.
`start_time is None` returns False.

**Why not read Polymarket US's `live` / `ended` flags**, which exist and are
accurate? Because discovery refreshes only every `ARBYS_DISCOVERY_INTERVAL_S`
(600s in practice), so a stored flag would be up to ten minutes stale — worse,
for this purpose, than a clock that is always current. Deriving also fails in
the safe direction: it keeps fast-polling a finished game (harmless, its
markets close) rather than slow-polling a live one (the failure that matters).

Windows are generous rather than tight, for the same reason. Extra innings,
overtime and rain delays all extend real games; over-polling a finished market
costs a few HTTP calls.

The sport is recoverable from the event group id prefix (`mlb-…`, `nfl-…`,
`atp-…`), which `frontend/src/lib/combo.ts` already relies on via
`CATEGORY_LABELS`.

#### Getting from a group to an outcome id

Liveness is a property of an **event group** (it has `start_time`), but the
adapter and `QuoteBook` both work in **outcome ids**. `AppState` owns
`event_groups` and is the only place that can bridge them, so it exposes:

```python
def live_outcome_ids(self) -> frozenset[str]
```

which walks `event_groups`, keeps groups where `is_in_play(...)`, and unions
their legs' `outcome_id`s.

**This must be memoised.** The `QuoteBook.max_age_for` hook fires on *every*
`get()` — once per leg per evaluation, and evaluation runs on every inbound
quote. Recomputing a ~1300-entry set (326 groups × 4 legs) at that rate would
put a full scan of the group table on the hottest path in the system. The
cached set is rebuilt at most once per second and invalidated whenever
`event_groups` changes. One second of staleness is irrelevant against
in-play windows measured in hours.

Both consumers read the same cache: the adapter's `live_outcome_ids` callback
and the `QuoteBook` hook.

### 3. Two-tier adaptive polling

`PolymarketUsAdapter` gains:

```python
live_poll_interval_s: float = 1.0
live_outcome_ids: Callable[[], set[str]] | None = None
```

Each live tick polls the live slugs; every `poll_interval_s / live_poll_interval_s`
ticks it also sweeps everything else. Slugs continue to be deduplicated —
`:LONG` and `:SHORT` share one HTTP call.

**The live tier must fetch concurrently.** This is a change to the existing
sequential loop and it is forced by arithmetic, not preference: a full MLB
slate is ~15 concurrent games × ~4 markets ≈ 60 slugs. Sequentially at ~30ms
each that is ~1.8s, so a 1s tier is unachievable. Concurrent fetch under a
bounded semaphore (default 20) fits comfortably — measured 2026-08-11, 53
concurrent `/bbo` calls returned in 1.46s with no rate limiting.

The base (non-live) tier stays sequential. It has no deadline and sequential
polling is gentler on the gateway.

`ARBYS_POLYMARKET_US_LIVE_POLL_S` configures the live tier, default 1.0,
clamped to a 0.5s floor.

### 4. Per-outcome staleness

`QuoteBook.__init__` gains an optional hook:

```python
max_age_for: Callable[[str], float | None] | None = None
```

`_is_stale` consults it per outcome, falling back to the existing global
`max_age_s` when absent or when it returns `None`. Default behaviour with no
hook is byte-identical to today.

`AppState` wires it to return `ARBYS_LIVE_QUOTE_MAX_AGE_S` (default 15s) for
outcomes belonging to an in-play group, and the global 600s otherwise.

600s is right for a quiet pre-game market that legitimately does not tick for
minutes. It is indefensible in play, where a 10-minute-old quote is not a
price, it is a memory.

### 5. Age-skew guard

In `EngineRuntime.evaluate_now`, for groups that are **in play only**:

```
if max(leg_ages) - min(leg_ages) > max_skew_s:   # default 3.0
    return []          # publish nothing for this group
```

Ages come from `QuoteBook.get_with_age`, which already exists.

This is the piece that makes in-game divergence trustworthy. Kalshi pushes,
Polymarket US polls; when the two disagree it is either because the market
genuinely moved or because one leg is looking at an older moment. Age skew is
what separates those, and nothing else in the system can.

**It applies only in play.** Pre-game, legs sit unticked for minutes at a time
and skew carries no information — applying it there would suppress the
legitimate edges the scanner finds today, including the one net arb currently
on the board.

3¢ of tolerance is chosen against the polling design: with a 1s live tier and
a push Kalshi feed, honest skew is 0–1s, so 3s absorbs jitter while rejecting
the ≥5s artifacts that motivated the guard.

`ARBYS_MAX_QUOTE_SKEW_S` configures it; `0` disables.

Items 3 and 5 are complementary rather than redundant. 3 narrows the gap so the
guard passes often enough to be useful; 5 rejects what still slips through.
Either alone is insufficient: without 3 the guard would reject nearly every
in-play group, and without 5 a narrowed gap is still an unverified one.

## Error handling

- A failed live-tier fetch yields no quote for that slug that tick; the next
  tick retries. Concurrent fetch uses `return_exceptions=True` so one failure
  cannot abort the whole sweep.
- Existing invariants are preserved and must not regress: discovery marks a
  pass incomplete when any sub-pass raises and **skips retirement**; retiring a
  group must still call `clear_group_opportunities`.
- The skew guard returning `[]` flows through the existing empty-set path, so a
  suppressed group's stale opportunities are cleared rather than left standing.

## Testing

Tests never hit a real venue.

| Test | Covers |
| --- | --- |
| `tests/shared/test_liveness.py` | window boundaries; `start_time=None`; unknown sport falls back to the default window |
| `tests/shared/test_quotebook_staleness.py` | extended: per-outcome hook overrides the global; hook returning `None` falls back; **no hook is byte-identical to today** |
| `tests/adapters/test_polymarket_us.py` | extended: live slugs polled at the live interval; non-live only on the base sweep; concurrent fetch issues one call per slug; one failing slug does not abort the sweep |
| `tests/test_engine_runtime.py` | extended (there is no `tests/ingest/`): in-play group with skewed ages publishes nothing; within tolerance publishes; **pre-game group with identical skew still publishes** |
| `tests/discovery/test_polymarket_us.py` | extended: MLB totals parsed and keyed OVER/UNDER |
| `tests/test_backend_e2e.py` | extended: `live_outcome_ids` memoisation — cache is reused within its TTL and invalidated when `event_groups` changes |

The pre-game control case matters most. The guard must not quietly suppress the
edges the scanner finds today, and only a test asserting the negative will
catch that.

Green-build bar unchanged: `pytest` (179 today), `ruff check .`, and
`npm run build` in `frontend/`. mypy remains outside the bar.

## Config summary

| Variable | Default | Purpose |
| --- | --- | --- |
| `ARBYS_POLYMARKET_US_LIVE_POLL_S` | 1.0 | live-tier poll interval, 0.5s floor |
| `ARBYS_LIVE_QUOTE_MAX_AGE_S` | 15 | staleness ceiling for in-play outcomes |
| `ARBYS_MAX_QUOTE_SKEW_S` | 3.0 | max leg age spread in play; `0` disables |

## Phase 3 — spreads

Unchanged from the Phase 1 spec and still worth doing, just not first. Both
venues have deep books (`KXMLBSPREAD`, `KXNFLSPREAD`,
`baseball_team_full_game_spread`); the work is sign normalisation between
Kalshi's named-team tickers and Polymarket US's slug-position anchoring. The
matcher's `anchor` field already exists to guard it.

The fee curve means spreads will mostly matter *in play*, which is another
reason to build this spec first: it is the infrastructure that makes spreads
worth having.

## Open questions

None. Decisions settled:

| Decision | Resolution |
| --- | --- |
| Spreads vs in-game first | In-game first; spreads become Phase 3 |
| Include coin-flip markets | Yes — they are the in-play-interesting ones |
| Liveness source | Derived from `start_time`, not the stored `live` flag |
| Polymarket US WebSocket | Deferred pending KYC; this spec mitigates its absence |
| UI changes | None; `quote_age_s` already exposed |
