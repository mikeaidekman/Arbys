# Dense opportunity table — design

**Date:** 2026-08-22
**Status:** approved, not yet implemented
**Replaces:** the card grid in `frontend/src/pages/TerminalPage.tsx`

## Why now

The terminal renders one blueprint card per event group in an auto-fill grid.
With ~250 registered groups, comparing events means reading the same field at a
different position in every card.

The gain here is **alignment and sortable order, not items on screen.** The card
grid is `repeat(auto-fill, minmax(216px, 1fr))`, so on a wide window it already
fits a comparable number of cards to a compact table's rows. What a table buys
is a fixed column position for every field, so a column can be scanned down
rather than hunted for, and the whole set can be reordered by a single value.
Do not later "optimise" this back to cards on a density argument — density was
never the claim.

Density chosen: **compact** — ~28px rows, 12px text. Comfortable (~38px) and a
mono blotter variant (~22px) were both mocked and rejected. The blotter also
required a full-name-to-nickname lookup the frontend does not have, which would
have missed on unmapped entries, tennis players especially.

## Findings established against the live instance

Both measured 2026-08-22 against 245 monitored groups quoting on all four legs.

### At most one combo is favorable — but not by construction

A group has two two-leg combos: `A = K-Yes ask + P-No ask` and
`B = P-Yes ask + K-No ask`. Their sum is
`(K_yes + K_no) + (P_yes + P_no)`, so if each venue's own YES+NO asks exceed 1,
the two combos cannot both come in under 1 and only one can ever be an arb.

Polymarket US satisfies that **structurally**: its short side is derived, so
`ask_long + ask_short = 1 + spread`. Measured 0 crossed out of 245.

Kalshi does **not**. Its YES and NO are separate order books and can cross —
measured **1 of 245**, at `KY 0.47 + KN 0.52 = 0.99`, an intra-venue arb in its
own right. So "only one combo can be favorable" is near-certain, not
guaranteed.

| | count of 245 |
| --- | --- |
| Combo A favorable | 11 |
| Combo B favorable | 3 |
| **Both favorable** | **0** |
| Kalshi internally crossed | 1 |
| Polymarket US internally crossed | 0 |

This is why the row collapses to a single best-pair column: the second combo
almost never carries news. It is also why `both` is carried explicitly rather
than assumed away — see `bestPair()` below.

### Size, not edge, decides whether a row is actionable

Depth at the best pair across the nine largest live edges: **15, 58, 3, 12,
150, 0, 13, 11, 5** contracts. A +2¢ edge on 15 contracts is 30¢ of profit.

One row — `nfl-PHI-WAS-2026-09-13-total-41.5` — showed a +1.0¢ edge against
depth **0**. Per the three-state size rule, 0 is *known empty* and
`paper_broker` refuses to fill it. That row is unfillable and must not offer a
green button.

Size therefore gets its own column rather than the card's quiet `×1.2k`
annotation.

## Design

### 1. Files

| | file | change |
| --- | --- | --- |
| new | `components/OpportunityTable.tsx` | header row, sort state, maps rows |
| new | `components/OpportunityRow.tsx` | one row; owns its execute mutation |
| mod | `pages/TerminalPage.tsx` | swap the card grid for the table |
| mod | `lib/combo.ts` | add `bestPair()`, `splitTitle()` |
| mod | `index.css` | row stripe, compact density; retire card-only classes |
| del | `components/OpportunityCard.tsx` | only TerminalPage imports it |

`CategoryRail`, `AccountPanel`, and the nav are untouched. **`BlueprintCard`
stays** — `AdminPage` uses it in seven places; only `OpportunityCard` goes.

Table and row are separate files because the row needs `useMutation` and the
table needs sort state, and neither needs the other's internals.

### 2. `bestPair(group)`

```ts
export interface BestPair {
  combo: Combo | null;   // lower-total combo; null when neither is fully quoted
  both: boolean;         // both favorable — the Kalshi-crossed case
  size: number | null;   // tradeable size across the pair
}
```

`combo` is whichever of the two has the lower total. `both` is set when both
totals are under 1, which the findings above show is rare but reachable.

`size` combines the two legs' `ask_size` under the project's three-state rule,
**and the order of these checks is the whole point**:

1. either leg is `0` → **`0`**. Known empty. The broker rejects; no Fill.
2. either leg is `null` → **`null`**. Unknown depth. The broker still fills.
3. otherwise → **`min(a, b)`**. The pair is limited by its thinner leg.

Checking `null` before `0` would report unknown for an empty book and offer a
Fill that cannot execute. Collapsing `null` into `0` would reject every
hand-pushed quote from `POST /quotes`, which omits sizes entirely — a change
that would look like a safety improvement while breaking every demo.

### 3. `splitTitle(title)`

Titles arrive long and carry two fields plus a redundant date:

```
"Philadelphia Eagles vs Washington Commanders — Over 41.5 (2026-09-13)"
```

Split on the first occurrence of space-em-dash-space (`" — "`, U+2014 flanked by
spaces) to get `matchup` and `market`; strip a trailing ` (YYYY-MM-DD)` because
the Start column already carries it. Matching the spaced form matters — a team
or player name containing a bare em-dash would otherwise be cut in half.
Moneyline titles have no separator, so `market` is `null` and the column
renders `—`.

### 4. Columns

| col | source | rendering |
| --- | --- | --- |
| ● | `eventClock().phase` | dot; pulses once started |
| Cat | `categoryOf()` | `.tag` |
| Matchup | `splitTitle().matchup` | ellipsis, full text in `title` |
| Market | `splitTitle().market` | `O 41.5`, or `—` for moneyline |
| Start | `eventClock().text` | mono, abbreviated (`2d 6h`, `live 2h`) |
| Best pair | `bestPair().combo` | mono, `K-Yes 32 + P-No 66` |
| Size | `bestPair().size` | mono, right; `0` struck, `null` as `?` |
| Edge | `combo.edge` | mono, right, green when positive |
| — | row state | Fill / badge / ✓ filled |

The price-move flash (`usePriceMoves`, the `▲`/`▼` glyph plus
`.vt-move`) ports from the card onto the two prices in Best pair. Direction
stays carried by the glyph rather than a red/green pair, so no palette entries
are added.

### 5. Ordering

Default: start-time ascending. Nothing reorders on its own.

Start, Size, Edge, Matchup and Cat headers are clickable. Sorting by Edge or
Size is opt-in precisely because those values change on every 3s poll — the
user chooses the instability rather than inheriting it.

**Every comparator must tiebreak on the existing `compareGroups`.** Without
that, equal keys fall back to `/monitored`'s dict-insertion order, which shifts
as discovery registers games. That is the exact bug the comment at
`TerminalPage.tsx:51-53` was added to fix, and a dense table makes it worse:
rows are adjacent, so a row moving under the cursor between poll and click can
mean filling the wrong event. Paper makes that recoverable; live would not.

### 6. Row states

| state | Edge | Action | Detection |
| --- | --- | --- | --- |
| ready | `+1.5¢` green | green **Fill** | engine opportunity found |
| waiting | `+1.5¢` green | disabled *waiting* | favorable, no opportunity yet |
| no edge | `no edge` muted | none | `total >= 1` |
| no quotes | `—` | none | `combo == null` |
| no size | edge shown | disabled *no size* | `size === 0` |
| stale | `stale` | none | either leg `is_stale` |
| filled | — | `✓ filled` | existing `filledMap` |
| both favorable | edge + marker | Fill for the better | `both === true` |

`both favorable` shows the better pair and marks the cell, naming the second
pair in the tooltip. It is rare enough not to deserve a column and real enough
not to be silently dropped.

A stale leg reads "stale" rather than `—`: the price is *known* but no longer
tradeable, and `—` would misreport a quiet feed as missing data. This mirrors
the card's existing treatment (`.vt-stale`, struck through).

### 7. CSS

Added to `index.css`: a compact-density block for the table and
`.vt-row-arb`, a green left-edge stripe plus a faint row tint replacing the
card's green outline. Both reuse the existing `--vt-green` locals.

Retire `.vt-card`, `.vt-card.vt-arb`, `.vt-combo*` and `.vt-filled` once
`OpportunityCard` is gone. Everything else — colors, spacing, type, `.table`,
`.tag`, `.btn` — comes from the industry design system. **No new hex values,
radii, or type scales.** The `.table` primitive at
`public/design/industry/styles.css:249-259` is the base; the compact block only
overrides padding and font-size.

## What deliberately does not change

- **Gross vs net.** The stripe is gross of fees (`yes_ask + no_ask < 1`); an
  enabled Fill requires a live engine opportunity, which is net. A striped row
  with a disabled button is correct and expected, and clusters on ~50/50
  markets where fee drag is largest. Do not "fix" it.
- Quote staleness, the arb-only toggle, the category rail, the 3s poll plus
  websocket-invalidate pattern, and `ARBYS_MAX_OUTCOME_QTY` enforcement.
- The execute call: `api.executeArb(event_group_id, buyOutcomeIds(opp))`,
  unchanged.

## Error handling

Execution failure keeps the card's behaviour: the action cell shows *failed*
with the error in its tooltip, and the row stays put. A disabled button always
states why — a bare disabled control reads as broken.

## Testing

The frontend has **no test runner** — no vitest or jest in `package.json`. The
bar for this change is therefore:

```powershell
cd frontend
npm run lint      # oxlint
npm run build     # tsc -b && vite build — the real typecheck
```

plus manual verification against the running instance, specifically: a striped
row with a disabled button, a `size 0` row offering no Fill, a stale row, and
clicking each sortable header without rows jumping on the following poll.

Adding vitest for the two pure helpers was considered and deliberately
deferred — introducing a runner is its own decision, and it would not change
the backend's 203 tests either way.

## Non-goals

- Adding a test runner (above).
- Row expansion, inline detail, or a card view behind a toggle. The cards are
  deleted, not hidden; they are one revert away if the table disappoints.
- Days-to-settlement or annualised-return columns. Discussed separately and
  not part of this change.
- Detecting or surfacing the intra-venue Kalshi arb the crossed-book finding
  exposed. Worth its own spec — the engine is scoped to cross-venue event
  groups today.
- Dark mode. The design system is a light-ground brief.

## Open questions

None. The one open item at design time — whether to add vitest — was resolved
as no.
