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

It might be one — and that is precisely the difficulty. Either the resting
order on Polymarket US is still there at the old price, in which case filling
against it is real money; or it has already been pulled and we are looking at
a memory. The two are **indistinguishable from the quote alone**, because a
stale observation tells us only when we last asked, never what is on the book
now.

So widening coverage without addressing this would not merely add noise, it
would add *plausible* noise: opportunities that look identical to the real
thing and that the paper broker will happily fill at the remembered price.
Coverage, and the ability to tell those two cases apart, have to land together.

## Scope

In scope:

1. MLB totals (+36 groups, measured).
2. Liveness detection.
3. Two-tier adaptive polling, with concurrent fetch for the live tier.
4. Per-outcome staleness ceilings.
5. Age-skew labelling on in-play groups, plus execute-time verification of the
   stale leg, plus the instrumentation to measure how often such a leg is
   actually fillable.

Out of scope, deliberately:

- **Spreads** — deferred, not cancelled. See "Phase 3" below.
- **The Polymarket US WebSocket.** Needs completed KYC and an Ed25519 key pair
  from `polymarket.us/developer`. Items 3 and 5 are the mitigation for its
  absence; when it lands, skew collapses to near zero, almost nothing gets
  flagged, and verification becomes a rare path rather than a common one. The
  env placeholders already exist. These are the same credentials live order
  placement will require, so obtaining them is on the critical path regardless.
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

### 5. Age skew — label, then verify at execution

**Two different things must not be conflated:**

- *Our observation* is stale — we have not polled in 4.4s. This says nothing
  about the venue.
- *The venue's book* is stale — the resting order has not been pulled yet.

The second is an opportunity: filling against a resting order at a pre-news
price is real money. The first is only our ignorance. **From a stale quote
alone the two are indistinguishable**, so the design must not discard both.

An earlier draft of this spec suppressed all high-skew in-play groups. That was
wrong — it threw away the profitable case along with the ghost. Instead:

1. **Detection labels, it does not suppress.** `EngineRuntime.evaluate_now`
   computes leg age spread from `QuoteBook.get_with_age` (already exists). When
   a group is in play and the spread exceeds `ARBYS_MAX_QUOTE_SKEW_S`, the
   opportunity is still published, carrying `unconfirmed_stale_leg=True` and
   the offending `outcome_id`.
2. **Execution verifies.** Before filling a ticket flagged this way, the
   execution path force-refreshes the stale leg — one `/bbo` call for one slug,
   ~30ms — and re-evaluates against the refreshed quote.
3. **Fill or reject on what comes back.** Still there at that price: the
   resting order was real, fill it. Gone or moved: the edge evaporated, reject
   the ticket rather than fill against a memory.

#### Why verification is mandatory, not optional

`paper_broker.place_order` fills at `self._book.get(outcome_id)`
unconditionally (`arbys/shared/paper_broker.py:167`). A stale quote therefore
*always* fills, at the pre-news price. Publishing flagged opportunities without
execute-time verification would make paper P&L show consistent in-game profits
that live trading could not reproduce — and it would be most wrong exactly
where we are most interested. Verification is what keeps paper honest; it is
not a refinement.

#### What this is, and is not

This is **not** arbitrage in the sense the rest of the system means it. A
guaranteed-profit ticket assumes both legs fill at known prices simultaneously.
This is a race: the fast leg may fill while the slow one does not, leaving
naked directional exposure. That is legging risk, and it is the substantive
obstacle to trading any of this for real money. Nothing in Phase 2 solves it;
Phase 2 only stops us from lying to ourselves about whether the edge existed.

We are also not fast. Real latency arbitrage is measured in milliseconds; even
with a 1s live tier we act seconds after the news. Whether a mispriced resting
order survives that long is an empirical question, and the answer plausibly
differs by book depth — a sampled NFL total showed `bid_size 7 / ask_size 13`,
thin enough that nobody may bother; MLB moneyline showed 1155 at the touch and
161k one tick down, deep enough to be picked off instantly.

#### Instrumentation — the number that decides whether this is real

Every verification attempt logs its outcome: the group, the stale leg, its age
at detection, whether the refreshed quote still supported the edge, and the
resting size at the touch. Aggregated, that answers the only question that
matters — **how often is a flagged leg still fillable, and does it depend on
depth?**

Neither the author of this spec nor its reviewer knows that number today. It
determines whether in-game divergence is a genuine edge or a mirage, and it
should be measured before this path is trusted with real money.

Threshold behaviour: `ARBYS_MAX_QUOTE_SKEW_S` (default 3.0) now controls
*labelling*, not suppression. Guessing it wrong costs an extra HTTP call rather
than a missed trade, which makes its precise value far less consequential than
it would have been under the suppress design. `0` disables labelling entirely.

The guard applies **only in play**. Pre-game, legs sit unticked for minutes and
skew carries no information; flagging there would send every quiet market
through needless verification.

Items 3 and 5 remain complementary: 3 narrows the gap so fewer opportunities
need verifying at all, 5 makes the ones that remain trustworthy.

## Error handling

- A failed live-tier fetch yields no quote for that slug that tick; the next
  tick retries. Concurrent fetch uses `return_exceptions=True` so one failure
  cannot abort the whole sweep.
- Existing invariants are preserved and must not regress: discovery marks a
  pass incomplete when any sub-pass raises and **skips retirement**; retiring a
  group must still call `clear_group_opportunities`.
- A failed verification fetch at execution time **rejects the ticket**. It does
  not fall through to the remembered quote, which is the whole point of
  verifying; an unreachable venue is not evidence that a resting order is
  still there.

## Testing

Tests never hit a real venue.

| Test | Covers |
| --- | --- |
| `tests/shared/test_liveness.py` | window boundaries; `start_time=None`; unknown sport falls back to the default window |
| `tests/shared/test_quotebook_staleness.py` | extended: per-outcome hook overrides the global; hook returning `None` falls back; **no hook is byte-identical to today** |
| `tests/adapters/test_polymarket_us.py` | extended: live slugs polled at the live interval; non-live only on the base sweep; concurrent fetch issues one call per slug; one failing slug does not abort the sweep |
| `tests/test_engine_runtime.py` | extended (there is no `tests/ingest/`): in-play group with skewed ages publishes **flagged**; within tolerance publishes unflagged; **pre-game group with identical skew is never flagged** |
| `tests/test_backend_e2e.py` | execution of a flagged ticket force-refreshes the stale leg; a refreshed quote that still supports the edge fills; one that does not **rejects rather than filling at the remembered price** |
| `tests/discovery/test_polymarket_us.py` | extended: MLB totals parsed and keyed OVER/UNDER |
| `tests/test_backend_e2e.py` | extended: `live_outcome_ids` memoisation — cache is reused within its TTL and invalidated when `event_groups` changes |

The two negative cases matter most. Pre-game groups must never be flagged —
otherwise every quiet market pays for a needless verification round trip. And a
failed verification must **reject**, never fall back to the remembered quote;
a test that only checks the happy path would let the paper broker keep filling
against memory, which is the exact failure this section exists to prevent.

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
| High-skew in-play groups | **Label and verify at execution, never suppress** — a stale quote may still be fillable, and that is the opportunity |
| Polymarket US WebSocket | Deferred pending KYC; this spec mitigates its absence |
| UI changes | None; `quote_age_s` already exposed |

One thing is deliberately left **unknown rather than assumed**: how often a
flagged leg is still fillable on re-fetch, and whether that rate depends on
book depth. The instrumentation in §5 exists to answer it. Until it has, this
path should be treated as unproven — it is the difference between a genuine
in-game edge and a mirage that paper trading would happily confirm.
