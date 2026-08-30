# Live execution — design

**Date:** 2026-08-29
**Status:** proposed — Part G measured 2026-08-29; Parts A, C, D, F planned 2026-08-30
**Depends on:** [hosting without a server](2026-08-25-hosting-without-a-server-design.md),
which names this as its explicit non-goal and prepares Parts A and B for it
**Changes:** `shared/execution_router.py`, `adapters/base.py`, both venue adapters

## Why now

Paper has answered the question it can answer. Over 33 hours and 878 settled
tickets: **+$169.57 realized net on $20,018.86 deployed, every single ticket
profitable, and 97.2% landing within one cent of what the engine predicted at
detection.** That is the signature of real arbitrage rather than directional
bets that happened to win — the legs hedge, so each ticket returns its
prediction deterministically.

What paper cannot answer is whether that survives a real exchange, and the
reason is structural rather than incidental. `_commit_atomically` fills every
leg **with no `await` between them**, so a paper ticket physically cannot leg.
The 100% win rate is partly a property of the simulator. Across two real
venues no such guarantee exists or can exist.

Two numbers set the tolerance for everything below:

- **Fees are 62% of gross profit** ($281.44 of $451.01). There is no margin to
  give away to execution.
- **The median ticket earns 2.1 cents** while a single unhedged leg carries a
  $6.94 average standard deviation, $99.94 at the maximum. One bad legging
  event costs more than hundreds of good tickets earn.

So the object of this spec is not "place orders." It is **never to hold an
unintended naked leg for longer than it takes to close it.**

## Findings

### There is no live execution adapter, and the path that would use one is dead

`PaperExecutionAdapter` is the only implementation of `ExecutionAdapter` in the
repo. `ExecutionRouter.submit` routes to `_commit_atomically` when *all*
adapters are paper, which today is always — so **`_commit_sequentially` has
never run in production**. It is scaffolding, and its docstring is honest about
what it does not do:

```
A real venue fill is not reversible, so this path can still leave a
partial ticket — the error names the leg that failed.
```

The error text says `N leg(s) already filled and NOT reversed`. That is the
behaviour this spec has to change.

### Signing is done; the order endpoints are not

Both venues' request-signing machinery already exists and is tested against
live hosts for market data.

| venue | primitive | ready for orders? |
| --- | --- | --- |
| Polymarket US | `polymarket_us_auth.auth_headers(creds, method, path)` | **yes** — already general over method and path |
| Kalshi | `kalshi_ws._sign_pss` + `_auth_headers` | **no** — `_auth_headers` hardcodes `"GET"` and defaults `path` to the WS signing path |

So Part A is a widening, not a build. The RSA-PSS and Ed25519 work — the part
that is fiddly and fails opaquely — is done and proven.

### The paper/live boundary is an `isinstance` check

`submit` decides its whole strategy on `isinstance(adapter, PaperExecutionAdapter)`,
in three separate places. Once a live adapter exists, a group with one paper
adapter and one live adapter silently takes the *sequential* path — the
unsafe one — with no announcement. The discrimination has to become explicit
and a mixed ticket has to be refused outright, because a half-paper ticket is
not a hedge at all.

### Live, the venue becomes the source of truth

`PaperBroker` currently *is* the ledger: positions and cash are computed from
fills it applied itself. A real venue can reject, partially fill, or fill an
order we never learn about because the process died. After live execution the
venue's position is authoritative and ours is a cache, which is a different
relationship and needs reconciliation rather than trust.

### A crash between legs is indistinguishable from success

`_commit_sequentially` places leg 1, awaits, places leg 2. If the process dies
in that await — a deploy, an OOM, a Fly Machine restart — leg 1 is filled at
the venue and **nothing anywhere records that we intended a leg 2**. On
restart the position looks like a deliberate one-sided bet. The hosting spec
made restarts routine, which makes this reachable rather than theoretical.

## Part A — generalise Kalshi signing

`_auth_headers(key_id, private_key, *, path)` becomes
`(key_id, private_key, *, method, path)`, with the existing WS call site
passing `method="GET"` explicitly. Move it out of `kalshi_ws.py` into a
`kalshi_auth.py` beside `polymarket_us_auth.py`, so a REST order path does not
import the WebSocket module to sign a POST.

No behaviour change, and the existing WS handshake test pins it.

## Part B — one live adapter per venue

`KalshiExecutionAdapter` and `PolymarketUsExecutionAdapter`, both implementing
the existing `ExecutionAdapter` ABC unchanged. Each needs `place_order`,
`cancel_order`, `get_balances`, `get_positions`, `get_fills`.

Three rules that are not obvious:

- **`place_order` returns only when the venue has acknowledged a terminal
  state for that order** — filled, rejected, or cancelled. A "resting" order is
  not a filled leg, and returning one as though it were is how the router comes
  to believe it is hedged when it is not. Poll `get_fills` against the order id
  until terminal or `ORDER_ACK_TIMEOUT_S`, then cancel and treat as failed.
- **Every arb leg is IOC or its venue equivalent.** A resting limit order is a
  free option written to the market: the price moves, the other leg fills, and
  yours sits there getting picked off. We are taking a spread, not making one.
- **Partial fills are a legging event, not a success.** If leg 1 fills 60 of
  100 contracts, the ticket is 40 contracts naked. It takes the Part C path,
  sized to the shortfall.

## Part C — unwind immediately

**Agreed policy, 2026-08-29.** When a leg fails after another has filled,
close the filled legs at market rather than holding them to settlement.

The reasoning is quantified in Why now: held to settlement, a naked binary is
mean-zero but carries a standard deviation many times the ticket's edge, and
it is adversely selected — you get legged precisely when the price moved,
which means the leg you *did* fill is now stale on the wrong side. Unwound
immediately, the cost collapses to the spread plus fees: at the observed 1c
median spread that is a small, bounded, budgetable number, which is the
"you win some, you lose some" the policy assumes. **The policy is only true
with the unwind; without it, it is false.**

In `_commit_sequentially`'s failure branch, for each already-filled leg, place
an opposing order for the filled quantity.

**Price.** The router has no quote book and `place_order` requires a
`limit_price`. For a binary a **sell at `limit_price=0`** accepts any price at
or above zero and is therefore a market sell — correct, and needing no
interface change. It is too implicit to leave as a bare `0` at the call site:
name it `MARKET_SELL_LIMIT = Decimal("0")` with the reasoning attached, so the
next reader does not "fix" it into looking like a real limit.

**When the unwind itself fails, shout.** This is the one state no code can
repair: an open, unintended, one-sided position on a live venue. It must
- raise a distinct `UnwindFailed`, never the generic `InsufficientLegsError`,
- record every unwind attempt as its own `paper_order` row so the audit trail
  shows what was tried,
- surface on `/health` as a non-zero counter beside `dropped_writes`, because
  the same rule applies: **a silent failure here is worse than a loud one.**

Unwind orders carry the same `ticket_id` as the ticket that spawned them.
`_score_ticket` must not read them as fills, or a ticket that legged and
unwound will report a phantom profit.

## Part D — record intent before placing

Before the first leg goes to a venue, write the ticket row with
`status="executing"` and its full intended leg set. On boot, any ticket left
at `executing` is a possible naked position from a process that died
mid-flight: log it loudly, reconcile against the venue (Part E), and never
auto-resume it. Resuming a ticket whose age you cannot establish is how you
buy the second leg of an arb whose first leg settled hours ago.

This costs one write on a path that already writes one and closes the crash
gap in Findings.

## Part E — reconcile against the venue on boot

On startup, and on a schedule thereafter, compare `get_positions()` per venue
against `paper_position`. Any disagreement is real and ours is wrong.

Report the delta on `/health`; do not auto-correct. A silent correction erases
the evidence of whatever caused the drift, and the 2026-08-25 incident — a
$132 broker-vs-DB divergence that no counter recorded — is the argument for
counting rather than fixing.

## Part F — an explicit execution mode, and no mixed tickets

Replace the three `isinstance(adapter, PaperExecutionAdapter)` checks with an
explicit `mode` on the adapter (`PAPER` / `LIVE`).

- All-paper ticket → `_commit_atomically`, unchanged.
- All-live ticket → `_commit_sequentially` with Parts B and C.
- **Mixed → refuse before placing anything.** A ticket half-simulated is not a
  hedge, and it is the single most dangerous state the router can reach: it
  looks filled and is naked by construction.

`ARBYS_ENABLE_LIVE_EXECUTION` (**0 by default**) gates whether live adapters
are constructed at all, matching how ingest, discovery and auto-trade are
gated. With it off the live adapter classes exist and are never instantiated.

## Part G — measure before trusting

Two numbers decide whether any of this is viable, and neither is knowable from
paper:

- **Time-to-fill per leg**, from `place_order` to terminal acknowledgement.
  This is the legging window; everything in Part C is bounded by it.
- **Which venue is slower.** Place the slower leg **first**. Filling the fast,
  deep venue first and then discovering the slow one will not fill maximises
  exactly the exposure Part C then has to clean up.

Both are cheap to instrument and should run against the real venues at the
smallest tradeable size before any meaningful capital moves.

## Testing

- A stub `ExecutionAdapter` in `LIVE` mode whose second leg rejects: assert the
  first leg is sold back, at the filled quantity, and that the ticket records
  both the fill and the unwind.
- The same, where the *unwind* rejects: assert `UnwindFailed`, the `/health`
  counter increments, and the attempt is recorded.
- A partial fill on leg 1: assert the unwind is sized to the shortfall, not
  the intent.
- A mixed paper/live ticket: assert refusal before any `place_order` call —
  the strongest available assertion is that the stub was never called.
- Existing paper tests must be untouched. If any needs changing, the paper path
  has changed and that is a bug in this work.

## Non-goals

- **Deciding to go live.** This spec makes it *possible* and *safe to attempt*.
  Turning the flag on is a separate decision with real money behind it.
- **A resting-order or market-making strategy.** Every leg here is IOC. Making
  markets is a different product with different risk.
- **Removing the paper path.** It stays the default and stays the test bed.
- **Cross-venue netting or margin.** Each venue is funded and settled
  independently, as now.

## Open questions

**Part G is measured (2026-08-29/30) and the answer is favourable.** Kalshi's
authenticated round trip is **50ms median** on a genuine `200` (p90 63ms). An
*active* leg's ask changes every **13.8s** on Kalshi and **15.5s** on
Polymarket US, and only 8% of 240 sampled legs moved at all in four minutes.
A sequential legging window of ~**81ms** therefore gives **P(legged) ≈ 0.29%
per ticket on an active leg** — a 1-sigma P&L swing of $17 against $169 of
measured profit. Part C is a rarely-exercised safety net, not a hot path.

Two caveats on that number. The sample came from a quiet period, while 804 of
the ledger's 2,306 tickets landed between 00:00–03:00 UTC with games in play,
so treat 0.29% as a floor and repeat the measurement in that window. And a
signed `GET` is not a `POST` that has to match against a book, so 50ms
understates real order latency by an unknown amount.

Still open, non-blocking: whether either venue offers a true IOC order type,
or whether it must be emulated as place-then-cancel. Emulation widens the
window above and changes these numbers, so confirm it against the order APIs
before building Part B.

