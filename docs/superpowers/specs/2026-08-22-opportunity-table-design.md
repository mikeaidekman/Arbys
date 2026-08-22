# Truthful capacity and the dense opportunity table — design

**Date:** 2026-08-22
**Status:** approved, not yet implemented
**Replaces:** the card grid in `frontend/src/pages/TerminalPage.tsx`
**Fixes:** engine sizing and paper-broker fill-size checking

## Why now

The request was a dense one-row-per-event table in place of the card grid.
Specifying it surfaced two defects that make the table impossible to build
honestly, so they are fixed here rather than deferred.

**The engine ignores book depth.** `arb_engine` sets `qty = target_payoff` with
`DEFAULT_TARGET_PAYOFF = Decimal("100")` (`ingest/engine_runtime.py:31`). Every
published opportunity is sized at a flat 100 contracts whether the book holds 3
or 419,882.

**The broker never checks order size against resting size.**
`paper_broker._preview_fill` blocks only an explicit `0`:

```python
resting = quote.ask_size if is_buy else quote.bid_size
if resting is not None and resting <= 0:
    return "no_liquidity"
```

`qty` is never compared to `resting`, so an order for 100 fills completely
against a book with 3 available. Combined with the flat sizing above, every
reported `guaranteed_profit` assumes a fill the market usually cannot support —
7× to 33× the real depth on the rows measured below.

A Size column would have been the only honest statement of capacity in the
application, sitting next to a Fill button that contradicted it. That is worse
than not showing it.

## Findings established against the live instance

All measured 2026-08-22 against 245 monitored groups quoting on all four legs.

### There are currently no net-positive arbs

Twelve gross-positive pairs, **zero** net-positive once the documented fee
curves are applied (Kalshi `0.07·p·(1−p)`, Polymarket US `0.06·p·(1−p)`).
`GET /opportunities` returning 0 agrees.

| group | pair | gross | net | depth | gross $ | net $ |
| --- | --- | --- | --- | --- | --- | --- |
| `atp-FUCSOVICS-SAFIULLIN` | K-Yes+P-No | +2.00¢ | −0.87¢ | 164.7 | +$3.29 | −$1.43 |
| `mlb-CLE-LAA` | K-Yes+P-No | +1.50¢ | −1.65¢ | 57.9 | +$0.87 | −$0.96 |
| `atp-KOPRIVA-SONEGO` | K-Yes+P-No | +1.00¢ | −2.05¢ | 160 | +$1.60 | −$3.27 |
| `nfl-PHI-WAS-…-total-41.5` | K-Yes+P-No | +1.00¢ | −1.85¢ | **0.02** | +$0.00 | −$0.00 |

This is the fee drag CLAUDE.md documents — up to 3.25¢/contract at even money
against gross divergence that tops out near 2.75¢ — not a regression.

**It is also why dollars must be net.** A gross dollar column would have
printed **+$3.29** on the Fucsovics row, a position that loses **$1.43**. Gross
cents are a documented divergence signal; gross dollars read as money.

### At most one combo is favorable — but not by construction

`A = K-Yes ask + P-No ask`, `B = P-Yes ask + K-No ask`. Their sum is
`(K_yes + K_no) + (P_yes + P_no)`, so if each venue's own YES+NO exceeds 1,
only one combo can be under 1.

Polymarket US satisfies that **structurally** — its short side is derived, so
`ask_long + ask_short = 1 + spread`. Measured 0 crossed of 245. Kalshi does
not: separate order books, measured crossed **1 of 245** at
`KY 0.47 + KN 0.52 = 0.99`. So the property is near-certain, not guaranteed,
and `bestPair()` carries a `both` flag rather than assuming it away.

## Part A — truthful sizing (backend)

### A1. Sizing is depth-aware and stake-capped

#### The existing detection gate is dimensionally wrong and currently inert

`detect_cross_venue_two_leg` defaults `target_payoff` to `Decimal("1")`, but
`engine_runtime` passes `Decimal("100")`. Its gate compares a **per-contract**
cost against a **total** payoff:

```python
total_unit_cost = y_unit + n_unit      # per contract, ~1.03 on a near-arb
if total_unit_cost >= target_payoff:   # target_payoff is 100
    continue                            # never true; the gate never fires
```

Every pair therefore passes it, because prices are in `[0, 1]` and fees are
cents. The filtering is actually done downstream by
`profit = target_payoff − total_stake; if profit <= 0: continue`, which — since
`total_stake = total_unit_cost × target_payoff` for linear fee models — reduces
to exactly `total_unit_cost >= 1`.

So the right test is reached by accident. No false arbs escape today, and this
is **not** a live defect. But it is masked rather than correct, and it breaks
the moment `qty` stops equalling `target_payoff` — which is precisely what this
spec does. The gate becomes explicit and size-independent:

```python
if total_unit_cost >= 1:      # per contract; nothing to do with sizing
    continue
```

`target_payoff` then leaves detection entirely. Payoff is `qty` (each contract
settles at $1), so `profit = qty − total_stake`, matching what
`size_to_bankroll` already computes via `min(leg.qty for leg in new_legs)`.

#### Sizing

Detection and sizing separate cleanly. The arb *test* is per-contract and
size-independent, as above. Sizing is a second step:

```
qty = min(depth, budget_qty)          floored to venue tick
  depth      = min over legs of ask_size, treating None as unbounded
  budget_qty = max_ticket_stake / total_unit_cost   (all-in, incl. fees)
```

`ARBYS_MAX_TICKET_STAKE` is new, **default `200`**, `0` disables — matching
`ARBYS_MAX_OUTCOME_QTY`'s convention. At ~$1.00 all-in per contract pair, $200
is ~198 contracts.

Depth follows the project's three-state rule:

| `ask_size` | meaning | effect on sizing |
| --- | --- | --- |
| `0` on either leg | known empty | **no opportunity emitted at all** |
| `None` on either leg | unknown depth | that leg imposes no ceiling |
| `> 0` | real quantity | caps `qty` |

Treating `None` as a ceiling of zero would silence every opportunity built from
`POST /quotes`, which omits sizes entirely. Treating `0` as unknown would emit
opportunities against empty books. The order of these checks is the whole
point.

`size_to_max_stake` already caps total capital and `size_to_bankroll` already
handles per-venue ticks and proportional fee rescaling — both stay. What is new
is the depth ceiling, applied at detection where the quotes are already in
hand rather than as a post-hoc rescale.

### A2. `ARBYS_MAX_TICKET_STAKE` does not replace `ARBYS_MAX_OUTCOME_QTY`

They cap different things and both remain:

| | scope | default | binds at ~$1/contract |
| --- | --- | --- | --- |
| `ARBYS_MAX_TICKET_STAKE` | one ticket | 200 | ~198 contracts per execution |
| `ARBYS_MAX_OUTCOME_QTY` | cumulative, per outcome per account | 500 | ~2.5 tickets on one outcome |

An edge stays published while it exists, so without the cumulative cap repeat
executions stack without bound. The per-ticket cap does not change that.

### A3. The broker rejects orders larger than the resting size

`_preview_fill` gains the comparison it is missing:

```python
resting = quote.ask_size if is_buy else quote.bid_size
if resting is not None:
    if resting <= 0:
        return "no_liquidity"
    if qty > resting:
        return "insufficient_liquidity"
```

**Reject, not partial-fill.** A partial fill on one leg of a two-leg arb leaves
an unhedged position, which is the one outcome the whole design exists to
avoid. Rejecting lets `execution_router` roll the ticket back through the
existing `_forget` path.

With A1 in place the engine should never ask for more than is resting, so this
is a backstop against the book moving between detection and execution — which
is exactly when it matters.

`None` still fills, unchanged. Hand-pushed quotes must keep working.

### A4. `/monitored` carries the net figures

Four fields per group, computed with the real fee registry — which `AppState`
already holds — for the best pair:

| field | meaning |
| --- | --- |
| `net_edge` | net profit per contract, after both legs' fees |
| `max_tradeable_qty` | the depth ceiling; `null` when unknown |
| `net_max_profit` | `net_edge × qty` after both caps |
| `capital_required` | total stake for that `qty` |

These belong on `/monitored`, not `/opportunities`: the table must state the
net position for *every* row, including the ones that are not opportunities.
`/opportunities` holds only net-positive entries, which is currently none.

Pure arithmetic per group, so cost across 250 groups is negligible.

## Part B — the dense table (frontend)

### B1. Files

| | file | change |
| --- | --- | --- |
| new | `components/OpportunityTable.tsx` | header row, sort state, maps rows |
| new | `components/OpportunityRow.tsx` | one row; owns its execute mutation |
| mod | `pages/TerminalPage.tsx` | swap the card grid for the table |
| mod | `lib/combo.ts` | add `bestPair()`, `splitTitle()` |
| mod | `api/types.ts` | the four new `MonitoredGroup` fields |
| mod | `index.css` | row stripe, compact density; retire card-only classes |
| del | `components/OpportunityCard.tsx` | only TerminalPage imports it |

`CategoryRail`, `AccountPanel` and the nav are untouched. **`BlueprintCard`
stays** — `AdminPage` uses it in seven places; only `OpportunityCard` goes.

Table and row are separate files because the row needs `useMutation` and the
table needs sort state, and neither needs the other's internals.

### B2. Density

Compact: ~28px rows, 12px text. Comfortable (~38px) and a mono blotter (~22px)
were mocked and rejected; the blotter also needed a full-name-to-nickname
lookup the frontend lacks, which would have missed on unmapped entries, tennis
players especially.

The gain is **alignment and sortable order, not items on screen.** The card
grid is `repeat(auto-fill, minmax(216px, 1fr))` and on a wide window already
fits a comparable count. What a table buys is a fixed column position per
field. Do not later revert to cards on a density argument — density was never
the claim.

### B3. Columns

| col | source | rendering |
| --- | --- | --- |
| ● | `eventClock().phase` | dot; pulses once started |
| Cat | `categoryOf()` | `.tag` |
| Matchup | `splitTitle().matchup` | ellipsis, full text in `title` |
| Market | `splitTitle().market` | `O 41.5`, or `—` for moneyline |
| Start | `eventClock().text` | mono (`2d 6h`, `live 2h`) |
| Best pair | `bestPair().combo` | mono, `K-Yes 32 + P-No 66` |
| Size | `max_tradeable_qty` | mono, right; `0` struck, `null` as `?` |
| Edge | `net_edge` | mono, right; green positive, muted negative |
| Net $ | `net_max_profit` | mono, right; `capital_required` in tooltip |
| — | row state | Fill / badge / ✓ filled |

**The Edge column is net**, now that the backend supplies it. Gross moves to
the cell tooltip. This does not overturn the documented gross/net split — the
green stripe stays **gross**, preserving the divergence signal that split
exists to protect. The effect is that a striped row now reads `−0.87¢` instead
of an unexplained disabled button, which is strictly clearer.

The price-move flash (`usePriceMoves`, `▲`/`▼` with `.vt-move`) ports from the
card onto the two prices in Best pair. Direction stays carried by the glyph
rather than a red/green pair, so no palette entries are added.

### B4. Two pure helpers in `lib/combo.ts`

`bestPair(group)` → `{ combo, both, size }`. `combo` is the lower-total of the
two; `both` flags the Kalshi-crossed case; `size` mirrors the backend's depth
ceiling under the same three-state rule.

`splitTitle(title)` splits on the first space-em-dash-space (`" — "`, U+2014
flanked by spaces) and strips a trailing ` (YYYY-MM-DD)` since Start carries
it. Matching the *spaced* form matters — a name containing a bare em-dash would
otherwise be cut in half. Moneyline titles have no separator, so `market` is
`null`.

### B5. Ordering

Default start-time ascending; nothing reorders on its own. Start, Size, Edge,
Net $, Matchup and Cat headers are clickable, so sorting by a live-changing
value is opted into rather than inherited.

**Every comparator must tiebreak on the existing `compareGroups`.** Without it,
equal keys fall back to `/monitored`'s dict-insertion order, which shifts as
discovery registers games — the exact bug the comment at
`TerminalPage.tsx:51-53` was added to fix. A dense table makes it worse: rows
are adjacent, so a row moving between poll and click means filling the wrong
event. Paper makes that recoverable; live would not.

### B6. Row states

| state | Edge | Action | Detection |
| --- | --- | --- | --- |
| ready | net, green | green **Fill** | engine opportunity found |
| waiting | net, green | disabled *waiting* | net-positive, not yet published |
| no edge | net, muted | none | `net_edge <= 0` |
| no quotes | `—` | none | `combo == null` |
| no size | net shown | disabled *no size* | `max_tradeable_qty === 0` |
| stale | `stale` | none | either leg `is_stale` |
| filled | — | `✓ filled` | existing `filledMap` |
| both favorable | net + marker | Fill for the better | `both === true` |

A stale leg reads "stale" rather than `—`: the price is *known* but no longer
tradeable, and `—` would misreport a quiet feed as missing data. Mirrors the
card's `.vt-stale` treatment.

### B7. CSS

A compact-density block plus `.vt-row-arb` — green left stripe and faint tint —
replacing the card's outline, both reusing the existing `--vt-green` locals.
Retire `.vt-card`, `.vt-card.vt-arb`, `.vt-combo*` and `.vt-filled` with
`OpportunityCard`.

Everything else comes from the industry design system. **No new hex values,
radii, or type scales.** The `.table` primitive at
`public/design/industry/styles.css:249-259` is the base; the compact block
overrides only padding and font-size.

## What deliberately does not change

- **The gross stripe.** Gross-of-fees divergence stays visible as the row
  stripe. Only the numeric column becomes net.
- Quote staleness and expiry, the arb-only toggle, the category rail, the
  3s-poll-plus-websocket-invalidate pattern.
- The execute call shape: `api.executeArb(event_group_id, buyOutcomeIds(opp))`.
- `None` sizes still fill in the broker. Hand-pushed quotes keep working.

## Error handling

Execution failure keeps the card's behaviour: the action cell shows *failed*
with the error in its tooltip and the row stays put. A disabled button always
states why — a bare disabled control reads as broken.

`insufficient_liquidity` is a new rejection reason and must surface distinctly
from `no_liquidity`; they mean different things and conflating them would hide
the book-moved race that A3 exists to catch.

## Testing

Fee-model behaviour is the gate on whether something is called an arbitrage, so
per the house rule **the fee test comes first**.

| area | file | covers |
| --- | --- | --- |
| fees | `tests/shared/test_fees.py` | net edge per contract on both venues |
| sizing | `tests/shared/test_sizing.py` | depth ceiling, $200 cap, tick floor, the three-state rule, both caps interacting |
| broker | `tests/shared/test_paper_broker.py` | `qty > resting` rejects; `None` still fills; `0` still `no_liquidity` |
| engine | `tests/shared/test_arb_engine.py` | per-unit detection independent of size; no opportunity when either leg is `0` |
| API | `tests/test_backend_e2e.py` | the four new `/monitored` fields |

Backend bar: `venv\Scripts\python.exe -m pytest -q` (203 tests today, must stay
green) and `ruff check .` clean.

The frontend has **no test runner** — no vitest or jest in `package.json` — so
its bar is `npm run lint` (oxlint) and `npm run build` (`tsc -b`, the real
typecheck), plus manual verification against the running instance: a striped
row showing negative net edge, a `size 0` row offering no Fill, a stale row,
and clicking each sortable header without rows jumping on the next poll.

Adding vitest for the two pure frontend helpers was considered and deferred —
introducing a runner is its own decision.

## Non-goals

- Row expansion, inline detail, or a card view behind a toggle. The cards are
  deleted, not hidden; one revert away if the table disappoints.
- Days-to-settlement and annualised-return columns. Discussed separately.
- ~~Detecting the intra-venue Kalshi arb.~~ **Struck — this was wrong.**
  `detect_complementary_set` already runs once per venue on every group, and
  every group carries 2 legs per venue (`:YES`/`:NO`, `:LONG`/`:SHORT`), so a
  candidate set always exists. Intra-venue arbs **are** detected today. The
  crossed Kalshi book at `KY 0.47 + KN 0.52 = 0.99` produced no opportunity
  only because fees put its all-in cost at `1.0249`. That detector therefore
  needs the same depth-aware sizing as the cross-venue one, and it is **in
  scope** — see the plan's Task 5.
- Walking deeper into the book for more size. Top-of-book is the correct
  ceiling for an arb; taking depth at worse prices destroys the edge being
  traded.
- Modelling Kalshi's per-contract fee rounding or Polymarket's maker rebate.
  Both already documented as known, and the broker always takes.
- Dark mode. The design system is a light-ground brief.

## Open questions

**Should there be a minimum ticket size or profit floor?** Depth-aware sizing
will publish genuinely tiny opportunities — the `nfl-PHI-WAS` row above has
depth `0.02`. Below some threshold an order is not worth placing, but any
floor is a trading-policy decision rather than a correctness one, so nothing is
specified here. If one is wanted, `ARBYS_MIN_TICKET_PROFIT` would follow the
existing config conventions.
