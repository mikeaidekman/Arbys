# A ledger you can trust — design

**Date:** 2026-08-25
**Status:** approved, not yet implemented
**Blocks:** any sizing decision based on recorded history, and
[the auto-trader](2026-08-23-auto-trader-design.md), whose whole output is
ledger rows

## Why now

One night of manual trading (2026-08-24, 23:33–00:05) produced nine tickets
and three symptoms that all turn out to share a cause:

- A ticket stuck at `pending` — the transient status that should never
  survive a submission.
- `paper_position.realized_pnl` summing to **−$29.17** while the in-memory
  broker reports **+$103.42** for the same account. The $132 gap is entirely
  on the Polymarket side; Kalshi agrees to the cent.
- **Zero `missed` tickets**, despite several clicks visibly failing on the
  Fill button.

The first two are the same bug. The third is a separate design mistake. Both
matter more than they look, because the conclusion being drawn from this data
is "the opportunity is bigger than expected, put resources into it" — and
right now nobody can say how much of that night is missing from the record.

## Findings

### The database drops writes, silently and by design

From the backend log across roughly one day:

```
sqlite3.OperationalError: database is locked                          18
sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) ...       17
sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10     6
```

Measured against the live database:

```
PRAGMA journal_mode  = delete     <- one writer, and it blocks readers too
PRAGMA busy_timeout  = 5000
PRAGMA synchronous   = 2          (FULL)
```

`create_async_engine` is called with no `connect_args` and no pragma hook
(`db/session.py:30`), so nothing has ever configured this.

Five things write concurrently:

| writer | shape |
| --- | --- |
| `DiscoveryService.run_once` | **one transaction per group**, 567 groups a pass, each a leg delete + N leg inserts + placeholder upserts |
| `PnlSnapshotService` | one row every 30s (17,597 rows to date) |
| `DbPaperPersistenceSink` | six separate write sites, each its own `session_scope`, per order/fill/balance/position/settlement |
| `AutoSettleService` | 481 settlement rows in two days |
| `POST /quotes` | rare (10 rows total — not a contributor) |

The discovery pass is the obvious offender: a burst of ~567 short
transactions, each taking the single write lock, while the snapshotter and the
sink try to interleave. `busy_timeout` is already 5s and still loses, which
means the lock is held longer than that — consistent with a burst plus
`synchronous=FULL` fsyncing every commit.

**Every one of those write paths swallows its exception.** That is deliberate
and correct in spirit — `paper_broker._emit` wraps sink calls in
`contextlib.suppress(Exception)` so persistence can never break a trade, and
`ticket_service`'s three writers catch and `log.exception`. The flaw is not
the swallowing; it is that a swallowed write is **indistinguishable from a
successful one**. Nothing counts them, nothing surfaces them, and the audit
page renders whatever survived as if it were complete.

That is the actual defect. A ledger that quietly loses rows is worse than one
that refuses to write, because it still looks authoritative.

### A failed click leaves no trace

`POST /paper/execute` resolves the opportunity itself and raises before ever
reaching the ticket service (`backend/app.py`):

```python
if opp is None:
    raise HTTPException(409, "edge no longer available at live quotes ...")
```

So the most common real failure — the edge dying in the second between the row
rendering and the click landing — writes nothing at all. The `missed` status
exists precisely for this and is unreachable from the manual path; it can only
fire in the narrower race where the endpoint finds an opportunity and
`submit_arb_ticket`'s own re-detect then does not.

The rule this came from is in the account-page spec: *"an attempt is logged
only once it reaches `submit_arb_ticket`; the detector finding nothing is not
an attempt."* That is right for a bot evaluating every tick — it would write
thousands of rows a night saying nothing happened. It is **wrong for a human
pressing a button**. A click is an attempt by definition, and how often an edge
evaporates between publication and submission is the single measurement that
decides whether latency work is worth anything.

## Part A — make SQLite behave

Configure the engine once, in `db/session.py`, applied per connection through a
`connect` event hook on the sync engine so pooled connections all get it:

```
PRAGMA journal_mode = WAL     -- readers stop blocking the writer, and vice versa
PRAGMA synchronous  = NORMAL  -- safe under WAL; stops fsync-per-commit holding the lock
PRAGMA busy_timeout = 15000   -- wait rather than fail when a burst is in flight
```

Raise the pool to match the writer count: `pool_size=10, max_overflow=20`. Six
`QueuePool` timeouts say the default 5+10 is genuinely exhausted, not merely
tight.

**Apply the pragmas only for SQLite URLs.** `ARBYS_DB_URL` may point at
Postgres, where `journal_mode` is meaningless and issuing it is an error. Gate
on the dialect, not on a string match against the URL.

WAL leaves `-wal` and `-shm` sidecar files next to `arbys-local.db`; both are
already covered by the gitignore pattern for the database, and should be
confirmed rather than assumed.

## Part B — a dropped write must be knowable

Keep the swallow. Add two things around it.

**Retry the lock case.** `database is locked` is transient by nature. Wrap the
sink and ticket-service writes in a bounded retry — three attempts with a
short backoff — that retries only `OperationalError` whose message contains
`database is locked`, and re-raises anything else so a real bug is not masked
as contention.

**Count what still fails.** A module-level counter in `db/session.py`,
incremented when a write is finally abandoned, exposed on `GET /health`
alongside the existing status:

```json
{"status": "ok", "dropped_writes": 0, "last_dropped_write": null}
```

Non-zero means the ledger on screen is incomplete, and the account page can
say so rather than presenting a partial history as the whole. This is the
piece that turns "we lost some rows" from an invisible property into an
observable one.

**Do not** make a failed persistence write break execution. The existing
priority is correct: a broken trade is worse than an unrecorded one. The point
is only to stop the two being indistinguishable.

## Part C — stop the discovery burst

`run_once` opens one transaction per changed group. It already skips groups
that are byte-identical to what `AppState` holds, so a steady state writes
almost nothing — but the first pass after a restart rewrites all 567, and that
burst is what starves the other writers.

Batch it: one `session_scope` per chunk of groups (50 is a reasonable start)
rather than one per group. Fewer lock acquisitions, and each still short enough
not to hold the write lock for the whole pass. A single transaction for all 567
is the wrong end of that trade-off — under WAL it would not block readers, but
it would block every other writer for its whole duration and turn one failure
into 567 lost groups.

Keep the existing `existing == group` skip ahead of the batching, so the common
case stays near-zero writes.

## Part D — record every attempt

Move the resolution inside the service, so the manual path cannot fail without
leaving a row.

Add to `backend/ticket_service.py`:

```python
async def submit_arb_ticket_for_descriptor(
    state, *, event_group_id: str, outcome_ids: set[str] | None,
    source: str, account_id: str | None = None,
) -> TicketResult
```

It resolves the descriptor against live opportunities exactly as the endpoint
does today; when nothing matches it writes a `missed` ticket — title from
`state.event_groups[...]`, economics null, `rejection_reason` naming the
descriptor — and returns it. When something matches it delegates to the
existing `submit_arb_ticket`.

`POST /paper/execute` then stops resolving anything and stops raising its own
409. It calls this, and maps a non-`filled` result to 409 as it does now, so
the API shape and the UI's "failed" button are unchanged. The difference is
that a row now exists.

The opportunity-object form of `submit_arb_ticket` stays exactly as it is —
that is what the auto-trader will call, and its "not an attempt" rule remains
right for a detector evaluating every tick.

One consequence worth stating plainly: `missed` tickets will now appear in
volume, because a human clicking a moving market misses often. That is the
data, not noise. The account page already filters on status.

## Testing

- Pragmas are applied on a SQLite URL and **not** on a Postgres one — the
  dialect gate is the part worth pinning, since getting it wrong breaks
  Postgres startup entirely.
- `journal_mode` reads back as `wal` after `configure_engine()`.
- A write that raises `database is locked` twice and succeeds on the third
  attempt is not counted as dropped; one that fails all three is.
- `dropped_writes` appears on `/health` and increments exactly once per
  abandoned write.
- Discovery writes N groups in `ceil(N/50)` transactions, not N.
- A click on a group with no live opportunity writes a `missed` ticket and
  returns 409 — the regression test for the actual gap.
- The six existing `/paper/execute` contract tests still pass unchanged.
- Concurrency: a test that runs a discovery-shaped write burst against a
  snapshot write and asserts neither is dropped. This is the one that would
  have caught the original bug, and it needs WAL to pass.

## What deliberately does not change

Execution, sizing, the detectors, the fee models, and the mid-marking
convention. The account page's layout is untouched — it will simply have more
and more accurate rows.

## Non-goals

- Moving to Postgres. WAL plus a retry is the right amount of effort for a
  single-user paper simulator; Postgres is the answer if this ever runs
  multi-process, and the engine config here should not make that harder.
- Reconciling the existing $132 divergence. The current
  `paper_position.realized_pnl` rows are known-incomplete and there is no
  record of what was lost. The fix stops the bleeding; it cannot recover
  history. Worth considering a one-off resync from broker state to DB at
  startup, but that is a separate decision and it would overwrite rather than
  reconstruct.
- Batching the sink's six write sites into one transaction per fill. It would
  reduce contention further, but it couples the ledger's atomicity to the
  broker's event sequence, and Part A plus Part B should be enough. Revisit if
  `dropped_writes` stays non-zero.

## Open questions

None blocking. One judgement call to revisit after it runs: whether
`dropped_writes` should be surfaced on the account page as a visible warning
rather than only on `/health`. It should be, if it ever goes non-zero in
practice — a stale ledger the user cannot see is the problem this spec exists
to remove, and hiding the indicator on a health endpoint only half-solves it.
