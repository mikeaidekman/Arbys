# The auto-trader — design

**Date:** 2026-08-23
**Status:** implemented 2026-08-27 (live venue verification pending)
**Depends on:** [the account page design](2026-08-23-account-page-design.md)

## Why now

Tradeable edges are expected to exist for short moments — better captured by
software than by a person watching a table. That is the whole rationale, and it
drives three decisions below: the trigger is event-driven rather than polled,
the trigger gate is the honest net-of-fees one, and a vanished edge is
*recorded* rather than discarded, because how often an edge evaporates between
detection and submission is the central open question about the strategy.

This spec is small because the account page spec does the load-bearing work:
ticket identity, `submit_arb_ticket()`, and attempt logging all land there.

## What already makes this safe

`PaperExecutionAdapter` is the only `ExecutionAdapter` in the repository, and
`ExecutionRouter` is constructed from paper brokers alone
(`backend/state.py:232`). No code path exists that could send an order to a
real venue. The `_commit_sequentially` fallback in the router is dead code
today — nothing constructs a non-paper adapter.

Paper fills are atomic: `_commit_atomically` fills every leg with no `await`
between them, so **there is no legging risk**. The bot cannot end up holding
one naked side. Its realistic failure modes are therefore narrow: stacking too
much into one outcome, hammering the same edge repeatedly, and inheriting wrong
answers from the auto-settler's heuristic.

## Trigger

**Any net-positive engine opportunity** — exactly what the Fill button already
accepts, net of fees. No separate threshold, no gross-edge mode.

This is the honest baseline, and it will be quiet. Measured 2026-08-22, 12 of
245 groups had a gross-positive pair and **zero** had a net-positive one. An
empty ticket log is the bot working correctly, not a bug, and is not a reason
to loosen the gate.

**Update 2026-08-27.** The prediction above — that the honest gate would be
near-silent — held on 2026-08-22 and does not hold now. Measured on 496 live
groups: 8 gross-positive and **5 net-positive**, together worth about 18c.
Three of the five were sized at 0.01-0.03 contracts, carrying implausible
25-36c "edges" that come from dust orders resting at off-market prices on thin
pre-season books.

The gate stays as specified. But the non-goals below anticipated the wrong
failure mode: the risk is not that the bot is too quiet to learn from, it is
that a log of penny fills pollutes the fill-versus-miss ratio this spec exists
to measure, and that a dust fill spends a full cooldown window blinding the bot
to a real edge on that group. Revisit an edge floor only with a week of data in
hand.

## Service

`arbys/ingest/auto_trade_service.py`, modelled on `AutoSettleService`: an
`asyncio.Task` with `start()` / `stop()`, owned by `AppState` and started only
when enabled.

It consumes `AppState.subscribe_opportunities()` rather than polling. That is
the same seam the WebSocket endpoint uses, and it means the bot reacts on the
tick that created the edge instead of up to an interval later — which for
short-lived edges is the difference between filling and missing.

Per opportunity received:

1. Enabled? (checked per iteration, not captured at boot.)
2. Group in cooldown? → skip silently.
3. Would any buy leg exceed `max_outcome_qty()` against current broker
   positions? → skip silently. See **Cap pre-check** below.
4. `submit_arb_ticket(opportunity, source="auto")`.
5. On `filled`, stamp the group's cooldown.

Rejections and misses need no handling: `submit_arb_ticket` has already
recorded them as tickets, which is exactly the audit trail wanted.

## Cap pre-check, and why it is not redundant

`submit_arb_ticket` enforces `max_outcome_qty()` authoritatively — that is the
fix for the cap-in-the-endpoint defect, and it stays the backstop.

The bot *additionally* pre-checks it against in-memory broker positions and
skips silently when it would bind. Without that, a capped-out group writes a
`rejected` ticket on every publish for the rest of the night. Opportunities
publish on fingerprint *change*, and a live moving market changes fingerprint
often, so the position cap — which permits roughly 2.5 tickets on one outcome
at ~$1.00 all-in per contract pair against a $200 ticket budget — is reached
early and then generates rejection rows indefinitely. The audit log would fill
with noise that says only "still capped".

The pre-check is a cheap in-memory read (`broker.account_snapshot`), so it
costs nothing on the latency path.

## Cooldown

`ARBYS_AUTO_TRADE_COOLDOWN_S`, default 60. After a **fill** on a group, that
group is ignored for the cooldown window. Keyed by `event_group_id` on a
monotonic clock, the same clock discipline `QuoteBook` uses.

An edge stays published while it exists, so without this one edge becomes a
burst of tickets on consecutive ticks until the position cap stops it. Rejects
and misses do **not** start a cooldown — a miss means the edge was gone, which
is no reason to stop watching a group.

## Config

- `ARBYS_ENABLE_AUTO_TRADE` — **0 by default**, matching the
  `ARBYS_ENABLE_INGEST` / `ARBYS_ENABLE_DISCOVERY` convention. Off unless
  explicitly turned on in `.env`. There is no runtime UI toggle.
- `ARBYS_AUTO_TRADE_COOLDOWN_S` — default 60.

Both go in `.env.example`. Account is `default_account_id`.

`ARBYS_MAX_TICKET_STAKE` (at detection) and `ARBYS_MAX_OUTCOME_QTY` (at
execution) are unchanged and still bind. The bot adds no new sizing logic
whatsoever — it fills what the engine published.

## Backpressure is a known, bounded limitation

`subscribe_opportunities` returns a queue of `maxsize=100`, and publishers use
`put_nowait` under `contextlib.suppress(asyncio.QueueFull)`
(`backend/state.py:502-505`). **A slow consumer drops opportunities silently.**

The bot processes serially — one ticket at a time, no concurrency — because
concurrent tickets would race each other on both the cash balance and the
position cap, and a lost race there is a real oversized position rather than a
missed trade.

Serial processing is expected to be far faster than the fill rate: publishes
are deduped by fingerprint, and only net-positive opportunities do any work at
all, which today is approximately none. But because a drop is invisible by
construction, the service logs a warning whenever `qsize()` exceeds 50 on
receipt. That makes backpressure visible before it becomes silent data loss.

Raising `maxsize` or adding a per-subscriber drop counter is deliberately out of
scope; the warning is enough to tell us whether either is ever needed.

## Testing

- Fires on a net-positive opportunity and produces a `filled` ticket with
  `source="auto"`.
- Does nothing when disabled — the default.
- Respects the cooldown: a second opportunity on the same group inside the
  window produces no ticket.
- Cooldown is not started by a rejection or a miss.
- **Cannot exceed `max_outcome_qty`** — the one that matters most.
- The cap pre-check skips silently rather than writing a rejected ticket.
- A vanished edge produces a `missed` ticket, exercising the shared path.

Tests never contact a venue; quotes are pushed directly into the `QuoteBook`,
and `asyncio_mode = "auto"` means no decorators.

## Non-goals

- Real-venue execution. Out of reach until an authenticated `ExecutionAdapter`
  exists, and a separate decision besides.
- Any loosening of the trigger: no gross-edge mode, no shadow mode, no
  configurable edge floor. Add them only if the honest gate proves too quiet
  to learn from.
- Daily ticket or capital budgets, and a runtime UI kill switch. The env flag
  plus the cooldown plus the two existing caps are the agreed control set.
- Selling out of positions, hedging, or any exit logic. The bot enters; the
  auto-settler closes.
- Alerting or notifications on fills.

## Open questions

None blocking. The measurement this spec exists to produce: over a week with
the flag on, how many opportunities were seen, how many filled, and how many
were `missed` because the edge vanished before submission. That ratio decides
whether latency work is worth anything — and it is unanswerable today, which
is why the `missed` status was added to the account page spec.
