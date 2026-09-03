# Capital controls: DraftKings out, $4,000 a venue, seven days out — design

**Date:** 2026-09-03
**Status:** approved, not yet implemented
**Follows:** [the auto-trader](2026-08-23-auto-trader-design.md), whose fills
are what spent the cash this is about

## Why now

The hosted auto-trader placed no new trades on 2026-09-03 because both trading
venues ran out of buying power. Three parameter changes were asked for, and
each turns out to touch a different layer:

- **Stop counting DraftKings paper dollars.** `AppState.fees` registers
  DraftKings unconditionally, so a paper broker is built for it and seeded
  with `DEFAULT_STARTING_BALANCE` even though `ARBYS_ENABLE_DRAFTKINGS=0` and
  no group has ever carried a DraftKings leg. That is $2,000 of cash in the
  headline `cash` and `equity` figures, and in every P&L snapshot, that can
  never be traded. The `/account` buying-power bars already filter it out by
  hand; the equity headline above them does not.
- **Add $2,000 to each of Kalshi and Polymarket US.** Cash is one
  `paper_balance` row per venue. Bootstrap seeds a row only for a venue that
  has never been funded, so bumping the constant does nothing to a live
  account — the runbook says so. The hosted account lives in Neon Postgres,
  whose connection string exists only in Fly's secret store, and the only
  path into that database is a deploy, whose release step runs
  `alembic upgrade head`.
- **Trade nothing that starts more than seven days out.** A pre-game edge on
  a game two weeks away locks its stake for two weeks, and with a $500 cap
  per game and ~$2,000 per venue, four such fixtures are the bankroll. There
  is no time-to-start control anywhere: the engine sizes by depth and
  budget, the ticket service refuses on settlement, skew and the cap, and
  the auto-trader paces by cooldown.

## Part A — DraftKings leaves the paper book

The fee registry, and therefore the broker map, the execution router, the
`ensure_venue` loop and the two services that iterate brokers, is built from
Kalshi and Polymarket US only, with DraftKings added **when
`draftkings_enabled()`** — the same flag that already gates its adapter
factory. A venue whose data adapter cannot be built has no business holding a
paper balance.

Consequences, all intended:

- `cash` and `equity` on `GET /paper/{id}` fall by the DraftKings balance,
  and `PaperAccountSummary.balances` stops listing the venue. The equity
  curve on `/account` shows a step at the deploy (see Part B for the net).
- The `venue` row for DraftKings stays; it is reference data and nothing
  is gained by deleting it. Its `paper_balance` row is removed by the
  migration in Part B, so re-enabling the flag later seeds it fresh at the
  then-current default rather than resurrecting a 2026 figure.
- `/account`'s buying-power panel drops its hard-coded DraftKings filter.
  The page renders what the server reports — that is already its rule — and
  with the flag on, a DraftKings bar is the honest display.
- The Admin page's venue picker keeps DraftKings. It selects a venue for a
  hand-pushed quote or a manual group, and the adapter is still a real,
  flagged integration.

## Part B — $2,000 more per trading venue, delivered by a migration

Two mechanisms were considered for reaching the live account and one was
ruled out immediately:

| mechanism | verdict |
| --- | --- |
| **data migration `0010`** | chosen — runs exactly once per database, on the next deploy, reviewable in git, no new write surface |
| `POST /paper/{id}/deposit` + Admin form | reusable, but a permanent write surface behind Access, more UI, and it still needs a click after the deploy |
| bump the default and reset the account | wipes the ledger and every open position — not acceptable |

Revision `0010_fund_trading_venues`, `down_revision` `0009`:

```sql
UPDATE paper_balance SET amount = amount + 2000
 WHERE venue_id IN ('kalshi', 'polymarket_us');
DELETE FROM paper_balance WHERE venue_id = 'draftkings';
```

Plain SQL, valid on both dialects. On an empty database — the SQLite replay
test, the Postgres CI branch, a fresh deploy — both statements touch zero
rows and `bootstrap()` then seeds the new default below. On the hosted
database each venue ends at *whatever it holds now* plus $2,000, which is the
request as stated. `downgrade()` subtracts the $2,000 again; it does not
restore the DraftKings row, because there is no broker to hydrate it into and
nothing that reads it.

`DEFAULT_STARTING_BALANCE` moves from $2,000 to **$4,000**, so a future
`POST /paper/{id}/reset` seeds the same level rather than quietly undoing the
deposit. The frontend's matching `START` constant in the buying-power panel
follows. Net effect of the deploy on equity: −$2,000 (DraftKings) + $4,000 =
**+$2,000**, as a single step in the P&L curve at deploy time.

The local SQLite database is built by `create_all()` and has no
`alembic_version`, so it receives the deposit only if migrated by hand. It is
not what trades; a reset seeds $4,000 there either way.

## Part C — nothing trades more than seven days out

New setting `ARBYS_MAX_DAYS_TO_START`, **default 7**, `0` disables, read by
`max_days_to_start()` in `state.py` in the same shape as the other caps
(garbage falls back to the default, non-positive means off).

**Enforcement lives at the submission chokepoint.** `ticket_service` gains a
public `starts_too_far_out(state, opp) -> str | None` beside `cap_breach`,
and `submit_arb_ticket` applies it after the settled-group refusal and before
the ticket enters flight — it is a property of the *game*, like settlement,
not of the live economics. The rule: refuse when the group's `start_time`
minus now exceeds the limit. A naive `start_time` is read as UTC, as
`in_play_slugs` already does. **`None` does not block**, matching the
settlement convention: unknown is not "far away", and a hand-registered
group without a start time must stay tradeable.

The rejection reason carries the evidence, as the skew reason does, because
nothing else records it:

```
starts_too_far_out:<group> starts in 12.3 days; limit 7 (ARBYS_MAX_DAYS_TO_START)
```

**The auto-trader pre-checks it and skips silently**, through a new injected
callable beside `would_breach_cap`, for the same reason the cap is
pre-checked: an edge on a game a fortnight away can persist for days and
republishes on every depth tick, so without the pre-check the ledger fills
with one `rejected` row per group per non-fill window, all night, saying only
"still too early". The callable defaults to "never", so the service's
existing tests and any caller that does not wire it are unchanged.

**A manual Fill click is still an attempt** and is recorded as `rejected`
with the reason above. The engine keeps publishing far-out edges and the
terminal keeps showing them: seeing where the venues disagree is worth
having, and the only-tradeable invariants are about what may be *filled*.
The Fill button is therefore live on a far-out row and the click produces a
recorded rejection rather than a fill. That is a known rough edge, not an
oversight — see Open questions.

## Testing

- **Ticket service:** a group starting in eight days is refused, recorded as
  `rejected` with the `starts_too_far_out:` prefix and no orders; one
  starting in six days fills; one already under way fills; `start_time=None`
  fills; `ARBYS_MAX_DAYS_TO_START=0` lets a 30-day game through; the
  refusal honours `record_nonfill=False`.
- **Auto-trader:** the new pre-check skips without calling `submit`,
  mirroring `test_the_cap_precheck_skips_silently_without_submitting`.
- **Config:** `max_days_to_start()` — default 7, `0` → `None`, garbage →
  default.
- **State:** `AppState()` builds brokers for exactly Kalshi and Polymarket
  US by default, and adds DraftKings when the flag is set.
- **Migration:** the existing replay-and-diff test proves the chain still
  builds; a new `tests/db/` test migrates to `0009`, inserts funded rows for
  all three venues, upgrades to `head`, and asserts the two trading venues
  gained exactly $2,000 and the DraftKings row is gone. The migration *is*
  the delivery mechanism for the request, so it is tested as such.
- **Frontend:** `npm run lint` and `npm run build`.
- The suite's existing `len(paper_brokers) > 1` guard in the hydration
  regression still holds with two brokers.

## What deliberately does not change

- The engine and `/monitored`. Far-out edges are still detected, ranked,
  published and displayed; only submission refuses them.
- `ARBYS_MAX_TICKET_STAKE`, `ARBYS_MAX_OUTCOME_STAKE`,
  `ARBYS_MIN_CONTRACT_QTY`, the cooldown and the non-fill window. The new
  rule is a fourth, independent control.
- The DraftKings adapter, its flag and its tests.
- Open positions and the ledger. Nothing is reset.

## Non-goals

- **No edge floor and no gross-edge mode.** Still explicit non-goals.
- **No deposit endpoint.** If funding becomes routine, build it then, with
  the migration as the record of the first deposit.
- **No count of silently skipped far-out edges.** As with the cap pre-check,
  every published opportunity is already on the `arb_opportunity` tape, so
  the volume stays recoverable.

## Open questions

- Whether `/monitored` should carry a `starts_too_far_out` flag so the
  terminal can grey the Fill button on a far-out row. Deferred until a
  manual click actually hits the refusal; the recorded rejection is the
  measurement of whether it is worth doing.
