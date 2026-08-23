# The account page — an audit trail for paper tickets — design

**Date:** 2026-08-23
**Status:** approved, not yet implemented
**Replaces:** `frontend/src/components/AccountPanel.tsx`, deleted
**Depended on by:** [the auto-trader design](2026-08-23-auto-trader-design.md)

## Why now

The request was a separate page tracking trade history and current portfolio.
Its stated primary job is **audit** — what did I fill, and why — with
performance and risk secondary. That framing matters, because it means the
page's centrepiece is a ticket log, not an equity curve.

Specifying it surfaced eight findings, six of them outright defects. None break
anything today, because a human clicking a Fill button sees the result in a
toast and remembers what they did. They become serious the moment a bot is
clicking instead, which is the next piece of work — so they are fixed here
rather than deferred.

## Findings established against the code, 2026-08-23

**Nothing links the two legs of an arb ticket.** `paper_order.arb_opportunity_id`
exists (`db/models.py:226`) and `insert_paper_order` accepts the kwarg
(`db/repositories.py:223`), but no caller anywhere passes it. Every leg is a
standalone row; a filled arb is two rows with adjacent timestamps and no
relation between them.

**That column cannot be reused, either.** Opportunities are persisted
fire-and-forget and deduped by fingerprint (`backend/state.py:455-469`): a
re-detection byte-identical to what is already held is deliberately not
re-persisted. But execution re-detects fresh and mints a **new uuid** each
time, so the object being executed routinely carries an id that was never
written, while the DB holds an earlier row with the same fingerprint under a
different id. An FK from `paper_order` would fail on insert.

**Human-readable names are reachable, but through a table that gets deleted.**
`outcome.id` *is* the venue-native outcome id — `ensure_outcome_placeholder`
(`db/repositories.py:38-63`) uses it as the PK — but the market row it hangs
off is a stub titled `auto-placeholder for <outcome_id>`. The real title is
`event_group.title`, reachable via `event_group_leg` joined on `outcome_id`,
plus `is_yes_side` for the side. Discovery **retires groups routinely**, and
`delete_event_group` takes the legs with it. Joining live means trade history
reverts to nameless slugs for precisely the games that have finished — every
row worth auditing.

**`GET /paper/{id}/orders` drops the interesting columns.**
`list_paper_orders` (`db/repositories.py:347-367`) returns `limit_price` but
never the actual fill price or fee — those live in `paper_fill`, unjoined — and
omits `rejection_reason` entirely. The endpoint cannot answer "what did I pay?"
or "why did that fail?".

**Rejected tickets leave no trace whatsoever.** When `ExecutionRouter.submit`
rejects at preview — `insufficient_liquidity`, `no_liquidity`,
`insufficient_funds`, `no_adapter` — it raises `InsufficientLegsError` before
any `Order` object is constructed, so `on_order` never fires and nothing
reaches the database. `rejection_reason` only ever catches a *post-preview*
rejection, which the atomic commit path makes rare. A bot attempting 400
tickets and filling 3 would leave a database indistinguishable from one that
attempted 3.

**Settlement writes no event row.** `settle_outcome_async`
(`shared/paper_broker.py:393-420`) zeroes the position, credits cash, and
accumulates `realized_by_outcome`. In the database a settled winner is
indistinguishable from a position sold out at market: same zeroed row, same
bumped realized figure, no timestamp, no record of the value it settled at, no
note that this was the heuristic auto-settler rather than a real resolution.
Per-ticket profit is unrecoverable from broker state regardless, because
settlement uses `avg_price` blended across every ticket on that outcome — and
`ARBYS_MAX_OUTCOME_QTY` permits roughly 2.5 tickets on one.

**The position cap lives in the HTTP endpoint, not the router.** The
`max_outcome_qty()` check is inside `paper_execute` (`backend/app.py:397-440`).
Anything calling `s.router.submit()` directly bypasses it silently and stacks
without bound.

**Equity is up to 30 seconds stale, and absent from the summary schema.**
`PnlSnapshotService` marks to market every 30s and writes a row
(`ingest/pnl_service.py`); `AccountPanel` reads the latest snapshot. After a
restart there is no snapshot, so equity reads as zero until the first one
lands. `PaperAccountSummary` (`backend/schemas.py:121-125`) carries balances,
raw positions, and realized only — no MTM, no equity, no unrealized.

## Part A — ticket identity

Migration `0006` adds one table and one column.

```
paper_ticket(
  id                String(64) PK
  account_id        FK paper_account
  event_group_id    String(64)          -- not an FK; groups get deleted
  title_snapshot    String(512)         -- frozen at submit time
  source            String(16)          -- manual | auto
  status            String(16)          -- filled | rejected | missed
  rejection_reason  String(256) NULL
  total_stake       NUM NULL
  expected_profit   NUM NULL
  expected_edge_bps NUM NULL
  submitted_at      DateTime(tz), indexed
)

paper_order.ticket_id  String(64) NULL, indexed, FK paper_ticket
```

`event_group_id` is deliberately **not** a foreign key. Discovery deletes
groups, and history must outlive them. `title_snapshot` exists for the same
reason and is the only naming the page ever renders — the live join is not used
at read time at all.

**The three economic columns are nullable, because a `missed` ticket has no
economics to record.** The bot always holds the opportunity it acted on and so
always supplies them; a manual click passes only `event_group_id` and
`outcome_ids` (`ExecuteArbIn`), so if the re-detect comes up empty there is
genuinely no stake or expected profit to write. Nullable is the honest
representation; zero would read as a free ticket that made nothing.

**There is no `market_type` column.** `EventGroup` (`shared/types.py:97-109`)
has no such field — market type is encoded only in the group *id* string for
discovered groups (`nfl-ARI-LAC-2026-09-13-total-44.5`), and manually
registered groups carry whatever id the POST body gave them, so parsing it
would be unreliable for exactly the rows a human created. `title_snapshot`
carries the market in prose, which is what the page renders anyway.

`source` lands now, with every row written as `manual`, even though nothing
sets `auto` until the auto-trader ships. Adding it later means a migration plus
backfilling every existing row.

`ticket_id` is nullable because rows written before this migration have no
ticket. The page renders those as ungrouped legacy legs rather than hiding
them.

Per the repo rule, `0006` describes its own change in explicit `op.*` calls and
never touches `Base.metadata`.
`tests/db/test_migrations_match_models.py` covers the replay.

## Part B — one way to submit a ticket

Extract `submit_arb_ticket()` into a new module
`arbys/backend/ticket_service.py`. It needs `AppState`, the DB, and the router,
so it cannot live in `shared/` under the no-I/O rule.

It is the **only** way a ticket is submitted. Both `POST /paper/execute` and
the auto-trader call it. Sequence:

1. Re-detect against live quotes via `EngineRuntime.evaluate_now`, matching the
   requested buy legs. This is the existing endpoint behaviour and the reason
   stale prices are never replayed.
2. **No live opportunity** → write `paper_ticket` with `status=missed`, no child
   orders, `expected_profit` carried from the caller's opportunity. Return the
   miss.
3. Enforce `max_outcome_qty()` per buy leg. Exceeded → `status=rejected`,
   `rejection_reason` of `position_cap:<outcome_id>`.
4. Mint the ticket id, build the `ExecutionIntent`, submit to the router.
5. `InsufficientLegsError` → `status=rejected` with the router's reason string,
   plus one `paper_order` row per attempted leg at `status=rejected`, so the
   attempted prices and sizes are recorded.
6. Success → `status=filled`, `ticket_id` stamped on each `paper_order`.

**An attempt is only logged once it reaches this function.** "The detector
found nothing" is not an attempt and is never written — otherwise the bot
writes thousands of rows a night saying nothing happened. The `missed` status
is bounded for the same reason: it is written once per opportunity the caller
actually acted on, and opportunities reach subscribers only on fingerprint
*change*, not per tick.

Stamping `ticket_id` on `paper_order` requires threading it through
`ExecutionIntent` → `PaperExecutionAdapter.apply_fill` → the persistence sink.
`ExecutionIntent` gains a `ticket_id: str | None` field; the sink's `on_order`
gains the same.

`status=missed` is written for manual clicks too, not just the bot. One path,
one behaviour — and "I clicked and it vanished" is worth seeing.

## Part C — settlement events and ticket scoring

Migration `0006` also adds:

```
paper_settlement(
  id             BigInteger PK (Integer variant on sqlite)
  outcome_id     FK outcome
  resolved_value NUM              -- 0 or 1 in practice
  ts             DateTime(tz)
  source         String(16)       -- heuristic | manual
)
```

Note the PK variant rule: autoincrement PKs need
`BigInteger().with_variant(Integer(), "sqlite")` or inserts fail on a NOT NULL
constraint.

`PaperPersistenceSink` gains
`on_settlement(outcome_id, resolved_value, source)`. `settle_outcome_async`
emits it through the existing `_emit` wrapper, so a DB failure still cannot
break the in-memory simulator. `settle_outcome` — the synchronous
no-persistence variant — is unchanged.

**Scoring is computed at read time, never stored.** A ticket scores once every
leg's outcome has a settlement row:

```
realized = Σ over legs of (resolved_value − fill_price) × qty − fee
```

taken from that ticket's own `paper_fill` rows, so the blended `avg_price`
problem does not arise. Tickets with any unsettled leg report `realized: null`
and show as open. The page displays expected beside realized, which is the
first time the auto-settler's heuristic becomes auditable — a wrong call shows
up as a ticket whose realized profit is nothing like its expectation.

## Part D — live equity, computed one way

Extract the mark logic — mid price, falling back to the position's `avg_price`
when no live quote exists — out of `PnlSnapshotService.snapshot_once` into
`arbys/shared/equity.py`:

```python
def account_equity(brokers, quotebook, account_id) -> AccountEquity
# cash, position_value, equity, unrealized, realized
```

`shared/` is legal here: it takes the brokers and quote book as arguments and
performs no I/O.

`PnlSnapshotService` and the enriched summary endpoint both call it. If those
two computed equity differently, the strip and the curve below it would
disagree on the same page.

`unrealized = Σ (mark − avg_price) × qty`.

## Part E — API surface

`GET /paper/{account_id}` — `PaperAccountSummary` gains `cash`,
`position_value`, `equity`, `unrealized_pnl`, and `open_ticket_count`, computed
live. `balances`, `positions`, and `realized_pnl` are unchanged, so nothing
existing breaks.

`GET /paper/{account_id}/tickets?limit=&status=&source=` — new. One row per
ticket, newest first, each carrying `title_snapshot`, `source`, `status`,
`rejection_reason`, `total_stake`, `expected_profit`, `expected_edge_bps`,
`submitted_at`, `realized_profit` (null when unsettled),
and its legs: `venue_id`, `outcome_id`, `is_buy`, `qty`, `limit_price`,
`fill_price`, `fee`, `status`. Fills joined from `paper_fill`.

`GET /paper/{account_id}/orders` — keeps its shape, regains `rejection_reason`
and `ticket_id`, and joins `paper_fill` for `fill_price` and `fee`. Kept for
the raw leg-level view; `/tickets` is what the page uses.

`GET /paper/{account_id}/positions` — new, and the reason the page can show
readable names: each open position with `venue_id`, `outcome_id`, `qty`,
`avg_price`, live `mark`, `unrealized`, and a `title` resolved from the most
recent ticket that traded that outcome, falling back to the live
`event_group_leg` join, falling back to the raw id.

## Part F — the page, and the strip that replaces the sidebar

**New route** `/account`, `frontend/src/pages/AccountPage.tsx`, registered in
`main.tsx` alongside `/` and `/admin`.

**`AccountPanel.tsx` is deleted** and the third grid column removed from
`TerminalPage` — `gridTemplateColumns` drops to
`minmax(160px, 190px) minmax(0, 1fr)`, giving the opportunity table the full
remaining width.

**A full-width account strip replaces it**, a slim horizontal bar between the
nav and the grid: cash, position value, equity, unrealized, realized, open
tickets. This is the requested account summary on the opportunity page. It sits
below the nav rather than inside it because the nav already carries the brand,
the live-count pulse, an Admin tag, and two venue tags; six more figures would
crowd it. The strip is its own component, `AccountStrip.tsx`, reused as the
header of `/account` so the two views cannot drift. An `/account` link joins
the nav, styled like the existing `/admin` tag.

**`/account` sections, in audit-first order:**

1. `AccountStrip` — the same six figures.
2. **Ticket history** — the centrepiece. One row per ticket: time, event title,
   source, legs (venue · side · qty · fill price · fee), stake, expected
   profit, status, realized. Rejected and missed rows are dimmed and show their
   reason. Filters: status (all / filled / rejected / missed) and source
   (all / manual / auto).
3. **Open positions** — readable name, venue, qty, avg price, mark, unrealized.
4. **Equity curve** — from `pnl_snapshots`, small and last. It is the least
   important thing here and gets the least space.

Styling comes from the existing design system's semantic classes and tokens —
`.table`, `.card`, `.tag`, `--color-*`, `--space-*`. No new hex colors, radii,
or type scales. Positive and negative figures reuse the colours the current
panel already uses for P&L.

## Error handling

A ticket write failing must never break execution: the persistence sink's
existing swallow-and-log behaviour covers it, and the in-memory broker stays
the source of truth. The consequence is an unrecorded ticket, which is
acceptable; a failed trade is not.

Endpoints return an empty list rather than an error when an account has no
history. `/tickets` on an unknown account id returns `[]`, matching how
`/paper/{id}` already treats unknown accounts.

Missing settlement rows are the normal state for open tickets and render as
"open", not as an error or a zero.

## Testing

Backend:

- Repo round-trips for `paper_ticket` and `paper_settlement`.
- `submit_arb_ticket` enforces `max_outcome_qty` — the test that would have
  caught the cap-in-the-endpoint defect.
- A preview rejection writes a `rejected` ticket plus per-leg orders.
- A vanished edge writes a `missed` ticket and no orders.
- A filled ticket stamps `ticket_id` on every leg.
- Scoring: a two-leg ticket with both outcomes settled reports realized profit
  from its own fills; one leg unsettled reports null.
- `title_snapshot` survives `delete_event_group` — the regression test for the
  retirement trap.
- `account_equity` agrees with what `PnlSnapshotService` writes.
- Migration replay via the existing `test_migrations_match_models`.

Frontend: `npm run build` is the typecheck; `npm run lint` clean.

## What deliberately does not change

The opportunity table, the detectors, sizing, the fee models, and the
gross-vs-net distinction in the UI. Settlement remains the existing heuristic —
this spec makes it *auditable*, not correct.

## Non-goals

- Multiple accounts. Everything stays on `default_account_id`.
- Real-venue execution. `PaperExecutionAdapter` is still the only
  `ExecutionAdapter` in the repo.
- Per-sport or per-venue performance attribution. Performance ranked below
  audit; the ticket log makes attribution a later query, not a rewrite.
- Backfilling ticket identity onto existing `paper_order` rows.
- CSV export.

## Open questions

None blocking. One judgement call to revisit after use: whether `missed`
tickets belong in the same table as real attempts or deserve their own filtered
view. They are in the same table because the fleeting-edge question — how often
an edge vanishes between detection and submission — is only answerable by
seeing them next to the fills.
