# Trustworthy Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the paper-trading ledger silently losing rows, and record every manual fill attempt — including the ones that fail because the edge died.

**Architecture:** Four contained changes. Configure SQLite for concurrent access (WAL) so the write lock stops being contended; funnel every swallowed persistence write through one retry-and-count helper so a dropped row becomes observable instead of invisible; batch the discovery pass's per-group transactions so it stops starving the other writers; and move opportunity resolution out of the HTTP handler into the ticket service so a failed click writes a `missed` ticket instead of nothing.

**Tech Stack:** Python 3.11, SQLAlchemy 2 async + aiosqlite, FastAPI, pytest (`asyncio_mode = "auto"`).

**Spec:** [docs/superpowers/specs/2026-08-25-trustworthy-ledger-design.md](../specs/2026-08-25-trustworthy-ledger-design.md)

## Global Constraints

- **All money and all prices are `Decimal`. Never float.**
- Run everything from the repo root with `venv\Scripts\python.exe`, never a bare `python`.
- `venv\Scripts\python.exe -m pytest -q` must stay green — **286 tests** before this plan.
- `venv\Scripts\python.exe -m ruff check .` must stay clean. Note the repo enables `SIM300`: never write a comparison with the literal on the right-hand side (`assert X == {"a"}` is flagged); assert membership and length instead.
- **This environment does not print pytest's trailing "N passed" line**, and `echo "EXIT=$?"` may print `EXIT=True`. Neither means failure. Verify by the exit indicator plus all-dots progress output. Use `-p no:warnings`.
- mypy is **not** part of the green bar (47 pre-existing errors across 17 files). Do not claim mypy clean; do not start a cleanup.
- `arbys/shared/` is pure domain: no SQLAlchemy, no FastAPI, no I/O. The retry helper therefore lives in `arbys/db/`, not `shared/`.
- **A failed persistence write must never break a trade.** Every write path in the paper broker and ticket service swallows its exception on purpose. This plan keeps that and makes the swallow *countable*; it does not make persistence failures fatal.
- **`ARBYS_DB_URL` may point at Postgres.** SQLite pragmas must be gated on the dialect — issuing `journal_mode` against Postgres is an error and would break startup entirely.
- Tests never contact a real venue. `tests/conftest.py` pins the venue switches off session-wide.
- Do not touch the developer's `arbys-local.db`. Tests use `tmp_path` databases; any live check uses a throwaway `ARBYS_DB_URL` under `.superpowers/`.

## File Structure

**Modified:**

| File | Change |
| --- | --- |
| `arbys/db/session.py` | SQLite pragmas via a `connect` hook, pool sizing, the `run_write` retry helper, and the dropped-write counter |
| `arbys/shared/persistence.py` | Six sink write sites routed through `run_write` |
| `arbys/backend/ticket_service.py` | Three writers routed through `run_write`; new `submit_arb_ticket_for_descriptor` |
| `arbys/discovery/service.py` | `run_once` batches group upserts |
| `arbys/backend/app.py` | `/health` reports dropped writes; `/paper/execute` delegates resolution |
| `CLAUDE.md` | Document the pragmas, the counter, and the attempt-recording rule |

**Created:** `tests/db/test_write_reliability.py`

---

### Task 1: Configure SQLite for concurrent access

**Files:**
- Modify: `arbys/db/session.py:27-32` (`configure_engine`)
- Test: `tests/db/test_write_reliability.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: no new public names. `configure_engine(url=None) -> AsyncEngine` keeps its signature; connections made through it now carry the pragmas.

- [ ] **Step 1: Write the failing tests**

Create `tests/db/test_write_reliability.py`:

```python
"""Engine configuration and write reliability.

SQLite in the default `delete` journal mode allows one writer and blocks
readers while it writes. Five services in this app write concurrently — the
discovery pass alone bursts one transaction per changed group — which produced
18 "database is locked" errors and 6 QueuePool timeouts in a single day, each
one swallowed by a persistence path that must never break a trade.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa

from arbys.db import session as db_session


@pytest.fixture(autouse=True)
def _isolated_engine(tmp_path: Path):
    os.environ["ARBYS_DB_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'wal.db'}"
    db_session.reset_engine()
    yield
    db_session.reset_engine()
    os.environ.pop("ARBYS_DB_URL", None)


async def test_sqlite_connections_are_wal_with_a_busy_timeout():
    """WAL is the whole point: it lets readers and the writer proceed together.
    `synchronous=NORMAL` is safe under WAL and stops an fsync-per-commit from
    holding the write lock through the discovery burst."""
    engine = db_session.configure_engine()
    async with engine.connect() as conn:
        journal = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()
        synchronous = (await conn.exec_driver_sql("PRAGMA synchronous")).scalar()
        busy = (await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar()
    assert str(journal).lower() == "wal"
    # NORMAL is 1; FULL (the default this replaces) is 2.
    assert int(synchronous) == 1
    assert int(busy) == 15000


async def test_postgres_url_gets_no_sqlite_pragmas():
    """ARBYS_DB_URL may point at Postgres, where PRAGMA is a syntax error.
    The gate is on the dialect, and getting it wrong breaks startup entirely.

    No server is contacted: the engine is lazy, so building it is enough to
    prove the pragma hook was not registered for this dialect.
    """
    engine = db_session.configure_engine(
        "postgresql+asyncpg://user:pw@localhost:5432/nowhere"
    )
    assert engine.dialect.name == "postgresql"
    # The connect hook is only attached for sqlite; nothing to fire here.
    assert not db_session.sqlite_pragmas_registered(engine)


async def test_sqlite_pool_is_sized_for_the_concurrent_writers():
    """Six QueuePool timeouts say the default 5 + 10 is genuinely exhausted.

    A file-backed aiosqlite URL gets `AsyncAdaptedQueuePool`, so both figures
    are real. `_max_overflow` is private, deliberately — SQLAlchemy exposes no
    public accessor for it, and asserting the configured value is worth more
    than skipping the check.
    """
    engine = db_session.configure_engine()
    assert engine.pool.size() == 10
    assert engine.pool._max_overflow == 20


async def test_memory_sqlite_still_configures():
    """An in-memory URL must not be handed QueuePool sizing.

    Verified 2026-08-25: SQLAlchemy 2.0.51 gives `:memory:` a `StaticPool` and
    raises `TypeError: Invalid argument(s) 'pool_size','max_overflow'` if they
    are passed, so this is a real crash the guard prevents, not a hypothetical.
    """
    engine = db_session.configure_engine("sqlite+aiosqlite:///:memory:")
    assert engine.dialect.name == "sqlite"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv\Scripts\python.exe -m pytest tests/db/test_write_reliability.py -q -p no:warnings`
Expected: FAIL — `journal_mode` reads `delete`, and `sqlite_pragmas_registered` does not exist.

- [ ] **Step 3: Implement**

In `arbys/db/session.py`, add imports and the pragma machinery above `configure_engine`:

```python
from sqlalchemy import event
from sqlalchemy.engine import make_url

# journal_mode is persisted in the database file; the other two are
# per-connection and so must be re-issued on every checkout.
_SQLITE_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("busy_timeout", "15000"),
)

_PRAGMA_FLAG = "_arbys_sqlite_pragmas"


def sqlite_pragmas_registered(engine: AsyncEngine) -> bool:
    """Whether this engine carries the SQLite pragma hook. For tests."""
    return getattr(engine, _PRAGMA_FLAG, False)


def _register_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Apply WAL and friends to every new SQLite connection.

    Gated on the dialect: `ARBYS_DB_URL` may point at Postgres, where PRAGMA
    is a syntax error, so issuing these unconditionally would break startup.
    """
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # noqa: ANN001 - DBAPI types
        cursor = dbapi_connection.cursor()
        try:
            for name, value in _SQLITE_PRAGMAS:
                cursor.execute(f"PRAGMA {name}={value}")
        finally:
            cursor.close()

    setattr(engine, _PRAGMA_FLAG, True)
```

Then rewrite `configure_engine`:

```python
def configure_engine(url: str | None = None) -> AsyncEngine:
    """(Re)configure the global engine. Call this from tests to swap DB URL."""
    global _engine, _session_factory
    resolved = url or _get_db_url()
    kwargs: dict[str, object] = {"pool_pre_ping": True, "future": True}
    backend = make_url(resolved).get_backend_name()
    # An in-memory SQLite database uses a pool class that rejects these, and
    # sizing it would be meaningless anyway.
    if ":memory:" not in resolved:
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
    _engine = create_async_engine(resolved, **kwargs)
    if backend == "sqlite":
        _register_sqlite_pragmas(_engine)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine
```

- [ ] **Step 4: Run the tests**

Run: `venv\Scripts\python.exe -m pytest tests/db/test_write_reliability.py -q -p no:warnings`
Expected: 4 passed.

- [ ] **Step 5: Run the full suite**

Run: `venv\Scripts\python.exe -m pytest -q -p no:warnings` then `venv\Scripts\python.exe -m ruff check .`
Expected: all green. WAL creates `-wal` and `-shm` sidecar files beside each test database; confirm `git status` stays clean (the gitignore already covers `arbys-local.db*`-shaped artifacts — if it does not, extend it in this task).

- [ ] **Step 6: Commit**

```bash
git add arbys/db/session.py tests/db/test_write_reliability.py
git commit -m "fix(db): WAL journal mode and a pool sized for the concurrent writers"
```

---

### Task 2: One retry-and-count path for every swallowed write

**Files:**
- Modify: `arbys/db/session.py` (add `run_write`, the counter)
- Modify: `arbys/shared/persistence.py` (six write sites)
- Modify: `arbys/backend/ticket_service.py` (`_write_ticket`, `_set_status`, `_write_rejected_legs`)
- Modify: `arbys/backend/app.py:104-106` (`/health`)
- Test: `tests/db/test_write_reliability.py` (append)

**Interfaces:**
- Consumes: `configure_engine` from Task 1.
- Produces:
  - `async def run_write(context: str, work: Callable[[AsyncSession], Awaitable[None]]) -> bool` — opens a session, runs `work`, commits; retries `database is locked` up to 3 attempts with backoff; returns `True` on success, `False` when abandoned. Never raises.
  - `def dropped_write_stats() -> dict[str, object]` — `{"dropped_writes": int, "last_dropped_write": str | None}`.
  - `def reset_dropped_writes() -> None` — for tests.

- [ ] **Step 1: Write the failing tests**

Append to `tests/db/test_write_reliability.py`:

```python
async def test_run_write_commits_and_reports_success():
    from arbys.db import repositories as repo
    from arbys.db.session import create_all, run_write

    await create_all()
    ok = await run_write(
        "test.account", lambda s: repo.ensure_paper_account(s, "acct-ok")
    )
    assert ok is True
    assert db_session.dropped_write_stats()["dropped_writes"] == 0


async def test_run_write_retries_a_locked_database_then_succeeds():
    """`database is locked` is transient by nature: the point of retrying is
    that the burst which caused it has usually passed a few milliseconds
    later. A retried write must not be counted as dropped."""
    from sqlalchemy.exc import OperationalError

    from arbys.db.session import create_all, run_write

    await create_all()
    db_session.reset_dropped_writes()
    attempts = {"n": 0}

    async def work(_session):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OperationalError("stmt", {}, Exception("database is locked"))

    ok = await run_write("test.retry", work)
    assert ok is True
    assert attempts["n"] == 3
    assert db_session.dropped_write_stats()["dropped_writes"] == 0


async def test_run_write_counts_a_write_it_finally_abandons():
    """The defect this whole plan exists for: a swallowed write used to be
    indistinguishable from a successful one."""
    from sqlalchemy.exc import OperationalError

    from arbys.db.session import create_all, run_write

    await create_all()
    db_session.reset_dropped_writes()

    async def always_locked(_session):
        raise OperationalError("stmt", {}, Exception("database is locked"))

    ok = await run_write("test.always", always_locked)
    assert ok is False
    stats = db_session.dropped_write_stats()
    assert stats["dropped_writes"] == 1
    assert "test.always" in str(stats["last_dropped_write"])


async def test_run_write_does_not_retry_a_non_lock_error():
    """A genuine bug must not be retried three times and filed as
    contention — it is counted once and logged as itself."""
    from arbys.db.session import create_all, run_write

    await create_all()
    db_session.reset_dropped_writes()
    attempts = {"n": 0}

    async def boom(_session):
        attempts["n"] += 1
        raise ValueError("not a lock")

    ok = await run_write("test.bug", boom)
    assert ok is False
    assert attempts["n"] == 1
    assert db_session.dropped_write_stats()["dropped_writes"] == 1


async def test_run_write_never_raises():
    """Callers rely on this: a broken trade is worse than an unrecorded one."""
    from arbys.db.session import create_all, run_write

    await create_all()

    async def boom(_session):
        raise RuntimeError("anything")

    assert await run_write("test.noraise", boom) is False
```

And a health-endpoint test in `tests/test_backend_e2e.py`:

```python
def test_health_reports_dropped_writes():
    """A non-zero count means the ledger on screen is incomplete. Surfacing it
    is what turns silent data loss into something observable."""
    with TestClient(create_app()) as client:
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["dropped_writes"] == 0
        assert body["last_dropped_write"] is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv\Scripts\python.exe -m pytest tests/db/test_write_reliability.py -q -p no:warnings`
Expected: FAIL — `run_write` does not exist.

- [ ] **Step 3: Implement the helper**

In `arbys/db/session.py`:

```python
import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.exc import OperationalError

log = logging.getLogger(__name__)

_WRITE_ATTEMPTS = 3
_WRITE_BACKOFF_S = 0.05

_dropped_writes = 0
_last_dropped_write: str | None = None


def dropped_write_stats() -> dict[str, object]:
    """Counts of persistence writes this process gave up on.

    Non-zero means the ledger is incomplete. Every write path in the paper
    broker and ticket service swallows its exception on purpose — persistence
    must never break a trade — so without this a lost row is invisible and the
    audit page presents a partial history as the whole.
    """
    return {"dropped_writes": _dropped_writes, "last_dropped_write": _last_dropped_write}


def reset_dropped_writes() -> None:
    global _dropped_writes, _last_dropped_write
    _dropped_writes = 0
    _last_dropped_write = None


def _record_dropped_write(context: str, exc: BaseException) -> None:
    global _dropped_writes, _last_dropped_write
    _dropped_writes += 1
    _last_dropped_write = f"{context}: {type(exc).__name__}: {exc}"


def _is_locked(exc: BaseException) -> bool:
    return isinstance(exc, OperationalError) and "database is locked" in str(exc).lower()


async def run_write(
    context: str, work: Callable[[AsyncSession], Awaitable[None]]
) -> bool:
    """Run one write transaction, retrying lock contention. Never raises.

    Returns True if it committed. Returns False — and counts a dropped write —
    if it gave up. `context` names the write for the counter, e.g.
    "ticket.status" or "sink.on_fill".

    Only `database is locked` is retried. Anything else is a real bug and is
    counted once rather than masked as contention.
    """
    for attempt in range(1, _WRITE_ATTEMPTS + 1):
        try:
            async with session_scope() as session:
                await work(session)
            return True
        except Exception as exc:  # noqa: BLE001 - callers must never see this
            if _is_locked(exc) and attempt < _WRITE_ATTEMPTS:
                await asyncio.sleep(_WRITE_BACKOFF_S * attempt)
                continue
            log.exception("persistence write abandoned (%s)", context)
            _record_dropped_write(context, exc)
            return False
    return False
```

- [ ] **Step 4: Route the sink through it**

In `arbys/shared/persistence.py`, replace each of the six
`async with session_scope() as session: await repo.…` bodies with a `run_write`
call. `DbPaperPersistenceSink.on_fill` becomes:

```python
    async def on_fill(self, order: Order, fill: Fill) -> None:
        await run_write(
            "sink.on_fill",
            lambda s: repo.insert_paper_fill(
                s, order_id=order.id, qty=fill.qty, price=fill.price, fee=fill.fee
            ),
        )
```

Apply the same shape to `on_order` (context `"sink.on_order"`), `on_balance`
(`"sink.on_balance"`), `on_position` (`"sink.on_position"`), `on_settlement`
(`"sink.on_settlement"`), and `AccountScopedSink.on_order`
(`"sink.on_order.scoped"`). Import `run_write` from `..db.session`.

`paper_broker._emit` already suppresses exceptions around these; leave it —
`run_write` not raising makes that belt-and-braces rather than load-bearing.

- [ ] **Step 5: Route the ticket writers through it**

In `arbys/backend/ticket_service.py`, `_set_status` becomes:

```python
async def _set_status(ticket_id: str, *, status: str, reason: str | None) -> None:
    """Move a pending ticket to its final status.

    Never raises. A write abandoned here is counted in
    `dropped_write_stats()` — this is the path that left a ticket stuck at
    `pending` on 2026-08-25, because the lock error was swallowed with no
    trace.
    """
    await run_write(
        "ticket.status",
        lambda s: repo.update_paper_ticket_status(
            s, ticket_id, status=status, rejection_reason=reason
        ),
    )
```

Do the same for `_write_ticket` (context `"ticket.insert"`) and
`_write_rejected_legs` (context `"ticket.rejected_legs"`), dropping their
`try`/`except`/`log.exception` blocks since `run_write` owns that now. Note
`_write_rejected_legs` writes several rows in one transaction — keep them in a
single `work` callable so the batch stays atomic.

- [ ] **Step 6: Surface it on /health**

In `arbys/backend/app.py`:

```python
    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", **dropped_write_stats()}
```

Import `dropped_write_stats` from `..db.session` with the existing
`# noqa: E402` convention, and widen the return annotation as shown.

- [ ] **Step 7: Run the tests**

Run: `venv\Scripts\python.exe -m pytest tests/db/test_write_reliability.py tests/test_backend_e2e.py tests/test_ticket_service.py -q -p no:warnings`
Expected: all pass.

- [ ] **Step 8: Run the full suite and ruff, then commit**

```bash
git add arbys/db/session.py arbys/shared/persistence.py arbys/backend/ticket_service.py arbys/backend/app.py tests/db/test_write_reliability.py tests/test_backend_e2e.py
git commit -m "fix(db): retry locked writes and count the ones we abandon"
```

---

### Task 3: Stop the discovery write burst

**Files:**
- Modify: `arbys/discovery/service.py` (`run_once`)
- Test: `tests/discovery/test_service.py` (append)

**Interfaces:**
- Consumes: `run_write` from Task 2.
- Produces: no new public names. `DiscoveryService.run_once() -> int` keeps its signature and return meaning.

- [ ] **Step 1: Write the failing test**

Append to `tests/discovery/test_service.py`:

```python
async def test_run_once_batches_group_writes():
    """One transaction per group starved the other writers.

    The first pass after a restart rewrites every group — 567 of them live —
    and each took the single SQLite write lock in turn while the PnL
    snapshotter and the broker's sink tried to interleave. Batching cuts the
    lock acquisitions by the batch size.

    A single transaction for all of them would be the wrong end of the
    trade-off: it holds the write lock for the whole pass and turns one
    failure into every group lost, so the batch size is asserted here too.
    """
    from arbys.discovery import service as svc

    calls: list[int] = []

    async def fake_run_write(context, work):
        calls.append(1)
        return True

    groups = [_stub_group(f"eg-{i}") for i in range(120)]
    # 120 groups in batches of 50 -> 3 transactions, not 120.
    written = svc._batch(groups, svc.GROUP_WRITE_BATCH)
    assert [len(b) for b in written] == [50, 50, 20]
```

Add a `_stub_group` helper alongside the file's existing helpers if one is not
already present:

```python
def _stub_group(group_id: str):
    from arbys.shared.types import EventGroup, EventGroupLeg

    return EventGroup(
        id=group_id,
        title=f"stub {group_id}",
        legs=(
            EventGroupLeg(outcome_id=f"{group_id}-a", venue_id="kalshi", is_yes_side=True),
            EventGroupLeg(
                outcome_id=f"{group_id}-b", venue_id="polymarket_us", is_yes_side=False
            ),
        ),
    )
```

- [ ] **Step 2: Run it to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/discovery/test_service.py -q -p no:warnings`
Expected: FAIL — `_batch` and `GROUP_WRITE_BATCH` do not exist.

- [ ] **Step 3: Implement**

In `arbys/discovery/service.py`, add near the other module constants:

```python
# Groups per upsert transaction. One transaction per group made the first pass
# after a restart a burst of ~567 lock acquisitions; one transaction for the
# whole pass would instead hold the write lock for its entire duration and lose
# every group on a single failure.
GROUP_WRITE_BATCH = 50


def _batch(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]
```

Then rewrite the write loop in `run_once`. The ordering matters: only apply
in-memory state and engine registration for a batch whose write actually
committed, so a dropped batch does not leave `AppState` claiming groups the
database has never seen.

```python
        pending = [
            (group, self._state.event_groups.get(group.id))
            for group in groups
            if self._state.event_groups.get(group.id) != group
        ]

        changed = False
        for batch in _batch(pending, GROUP_WRITE_BATCH):
            async def _write(session, batch=batch):
                for group, _existing in batch:
                    await repo.upsert_event_group(session, group)

            if not await run_write("discovery.groups", _write):
                log.warning(
                    "discovery: a batch of %d group upserts was dropped; "
                    "leaving AppState untouched for them so it cannot claim "
                    "groups the DB has never seen",
                    len(batch),
                )
                continue
            for group, existing in batch:
                self._state.event_groups[group.id] = group
                if existing is None:
                    self._state.engine.register_group(group)
                else:
                    self._state.engine.unregister_group(group.id)
                    self._state.engine.register_group(group)
                changed = True
```

Import `run_write` from `..db.session`. Everything after this — the
`complete` retirement branch, `restart_ingest`, the log line and the return —
stays exactly as it is.

- [ ] **Step 4: Run the tests**

Run: `venv\Scripts\python.exe -m pytest tests/discovery -q -p no:warnings`
Expected: all pass.

- [ ] **Step 5: Run the full suite and commit**

```bash
git add arbys/discovery/service.py tests/discovery/test_service.py
git commit -m "perf(discovery): batch group upserts so the pass stops starving other writers"
```

---

### Task 4: Record every manual attempt

**Files:**
- Modify: `arbys/backend/ticket_service.py` (add `submit_arb_ticket_for_descriptor`)
- Modify: `arbys/backend/app.py` (`paper_execute`)
- Test: `tests/test_ticket_service.py` and `tests/test_backend_e2e.py` (append)

**Interfaces:**
- Consumes: `submit_arb_ticket`, `TicketResult`, `_write_ticket`, `_title` (all existing in `ticket_service.py`); `run_write` from Task 2.
- Produces:

```python
async def submit_arb_ticket_for_descriptor(
    state, *, event_group_id: str, outcome_ids: set[str] | None,
    source: str, account_id: str | None = None,
) -> TicketResult
```

`TicketResult.status` is `"filled" | "rejected" | "missed"` as today.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ticket_service.py`:

```python
async def test_descriptor_with_no_live_edge_writes_a_missed_ticket():
    """The gap this closes.

    /paper/execute used to resolve the opportunity itself and raise 409 before
    ever reaching the ticket service, so the most common real failure — the
    edge dying between the row rendering and the click landing — wrote nothing
    at all. Six manual fills and several visible failures on 2026-08-24
    produced zero `missed` tickets.
    """
    from arbys.backend.ticket_service import submit_arb_ticket_for_descriptor
    from arbys.db import repositories as repo

    s, _ = await _arb_group()
    # Reprice both sides so no edge exists, then describe the group anyway —
    # exactly what a click on a stale row does.
    for oid in ("p-yes", "k-no"):
        s.quotebook.upsert(
            Quote(outcome_id=oid, bid=Decimal("0.60"), ask=Decimal("0.60"))
        )

    result = await submit_arb_ticket_for_descriptor(
        s, event_group_id="eg-1", outcome_ids={"p-yes", "k-no"}, source="manual"
    )
    assert result.status == "missed"
    assert result.order_ids == ()
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert len(tickets) == 1
    assert tickets[0]["status"] == "missed"
    assert tickets[0]["title_snapshot"] == "MLB: ATL @ LAD"
    assert tickets[0]["total_stake"] is None
    assert tickets[0]["legs"] == []


async def test_descriptor_with_a_live_edge_still_fills():
    from arbys.backend.ticket_service import submit_arb_ticket_for_descriptor
    from arbys.db import repositories as repo

    s, _ = await _arb_group()
    result = await submit_arb_ticket_for_descriptor(
        s, event_group_id="eg-1", outcome_ids={"p-yes", "k-no"}, source="manual"
    )
    assert result.status == "filled"
    assert len(result.order_ids) == 2
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert tickets[0]["status"] == "filled"


async def test_descriptor_missed_ticket_names_an_unknown_group():
    """A descriptor for a group AppState has never heard of still gets a row,
    titled with the id rather than crashing on the lookup."""
    from arbys.backend.ticket_service import submit_arb_ticket_for_descriptor
    from arbys.db import repositories as repo

    s, _ = await _arb_group()
    result = await submit_arb_ticket_for_descriptor(
        s, event_group_id="eg-nonexistent", outcome_ids=None, source="manual"
    )
    assert result.status == "missed"
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert tickets[0]["title_snapshot"] == "eg-nonexistent"
```

And in `tests/test_backend_e2e.py`:

```python
def test_execute_records_a_missed_ticket_when_the_edge_is_gone():
    """The endpoint keeps returning 409 — the UI's "failed" button is
    unchanged — but a row now exists for the attempt."""
    with TestClient(create_app()) as client:
        _register(client, "eg-miss", "p-yes-ms", "k-no-ms")
        client.post("/quotes", json={"outcome_id": "p-yes-ms", "bid": "0.60", "ask": "0.60"})
        client.post("/quotes", json={"outcome_id": "k-no-ms", "bid": "0.60", "ask": "0.60"})

        r = client.post(
            "/paper/execute",
            json={"event_group_id": "eg-miss", "outcome_ids": ["p-yes-ms", "k-no-ms"]},
        )
        assert r.status_code == 409

        tickets = client.get("/paper/default/tickets").json()
        assert len(tickets) == 1
        assert tickets[0]["status"] == "missed"
        assert tickets[0]["legs"] == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv\Scripts\python.exe -m pytest tests/test_ticket_service.py tests/test_backend_e2e.py -q -p no:warnings -k "descriptor or missed_ticket"`
Expected: FAIL — `submit_arb_ticket_for_descriptor` does not exist, and the e2e case finds zero tickets.

- [ ] **Step 3: Implement the descriptor entry point**

Add to `arbys/backend/ticket_service.py`:

```python
async def submit_arb_ticket_for_descriptor(
    state: AppState,
    *,
    event_group_id: str,
    outcome_ids: set[str] | None,
    source: str,
    account_id: str | None = None,
) -> TicketResult:
    """Submit the ticket a caller *described*, recording it even if it cannot.

    `POST /paper/execute` used to resolve the opportunity itself and raise 409
    before reaching this module, so a click on a row whose edge had just died
    left no record — the single most common real failure, and the one
    measurement that tells you whether latency work is worth anything.

    The "a detector finding nothing is not an attempt" rule still holds for
    `submit_arb_ticket` itself, which is what the auto-trader calls: a bot
    evaluating every tick would otherwise write thousands of rows a night
    saying nothing happened. A human pressing a button is unambiguously an
    attempt.
    """
    account_id = account_id or state.default_account_id
    for candidate in state.live_opportunities_for(event_group_id):
        if candidate.event_group_id != event_group_id:
            continue
        if outcome_ids is not None:
            buy_legs = {leg.outcome_id for leg in candidate.legs if leg.is_buy}
            if buy_legs != outcome_ids:
                continue
        return await submit_arb_ticket(
            state, candidate, source=source, account_id=account_id
        )

    ticket_id = uuid.uuid4().hex
    reason = f"edge_no_longer_available:{event_group_id}"
    await _write_missed_descriptor(
        ticket_id=ticket_id,
        account_id=account_id,
        event_group_id=event_group_id,
        title=_title(state, event_group_id),
        source=source,
        reason=reason,
    )
    return TicketResult(ticket_id, "missed", (), reason)


async def _write_missed_descriptor(
    *, ticket_id: str, account_id: str, event_group_id: str, title: str,
    source: str, reason: str,
) -> None:
    """A missed ticket with no opportunity object behind it, so no economics.

    Null rather than zero: a `missed` ticket has nothing to record, and zero
    would read as a free ticket that made nothing.
    """
    async def _work(session):
        await repo.ensure_paper_account(session, account_id)
        await repo.insert_paper_ticket(
            session,
            ticket_id=ticket_id,
            account_id=account_id,
            event_group_id=event_group_id,
            title_snapshot=title,
            source=source,
            status="missed",
            rejection_reason=reason,
        )

    await run_write("ticket.missed", _work)
```

`_title` already falls back to the raw `event_group_id` when `AppState` has no
such group, which is what the unknown-group test pins.

- [ ] **Step 4: Rewire the endpoint**

Replace `paper_execute`'s `event_group_id` branch so it resolves nothing:

```python
    @app.post("/paper/execute", response_model=list[str])
    async def paper_execute(body: ExecuteArbIn) -> list[str]:
        s = get_state()
        if body.event_group_id is not None:
            result = await submit_arb_ticket_for_descriptor(
                s,
                event_group_id=body.event_group_id,
                outcome_ids=set(body.outcome_ids) if body.outcome_ids else None,
                source="manual",
                account_id=body.account_id,
            )
        else:
            opportunities = list(s.opportunities)
            if body.opportunity_index < 0 or body.opportunity_index >= len(opportunities):
                raise HTTPException(status_code=404, detail="opportunity_index out of range")
            result = await submit_arb_ticket(
                s, opportunities[body.opportunity_index], source="manual",
                account_id=body.account_id,
            )
        if result.status != "filled":
            raise HTTPException(status_code=409, detail=result.reason or result.status)
        return list(result.order_ids)
```

The out-of-range `opportunity_index` stays a 404 and writes nothing: that is a
malformed request, not an attempt on a market. Import
`submit_arb_ticket_for_descriptor` alongside the existing
`submit_arb_ticket` import.

- [ ] **Step 5: Run the contract tests**

Run: `venv\Scripts\python.exe -m pytest tests/test_backend_e2e.py tests/test_ticket_service.py -q -p no:warnings`
Expected: all pass, including the six pre-existing `/paper/execute` contract
tests. `test_execute_by_event_group_rejects_unknown_descriptor` still expects
409 — it now also leaves a `missed` row, which is the intended change.

- [ ] **Step 6: Run the full suite and commit**

```bash
git add arbys/backend/ticket_service.py arbys/backend/app.py tests/test_ticket_service.py tests/test_backend_e2e.py
git commit -m "feat(paper): record a missed ticket when a click finds no live edge"
```

---

### Task 5: Verify against the running app, then document

**Files:**
- Modify: `CLAUDE.md`
- No new code.

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

- [ ] **Step 1: Run the full bar**

```
venv\Scripts\python.exe -m pytest -q -p no:warnings
venv\Scripts\python.exe -m ruff check .
venv\Scripts\python.exe -m pytest tests/db -q -p no:warnings
```

Report the real counts. The migration-replay test in `tests/db` matters here
because Task 1 changed engine configuration, and that is the only test that
replays the chain from empty.

- [ ] **Step 2: Prove WAL and the counter on a live instance**

Use a **throwaway** database — never `arbys-local.db`:

```
ARBYS_DB_URL=sqlite+aiosqlite:///./.superpowers/ledger-smoke.db
ARBYS_ENABLE_INGEST=0
ARBYS_ENABLE_DISCOVERY=0
```

Start the backend from the repo root, then confirm:

- `GET /health` returns `dropped_writes: 0` and `last_dropped_write: null`.
- `PRAGMA journal_mode` on the throwaway file reads `wal`, and `-wal`/`-shm`
  sidecars exist beside it.
- Register a group, push quotes that make an edge, `POST /paper/execute` →
  200, then reprice both legs to kill the edge and execute again → **409**,
  and `GET /paper/default/tickets` shows two rows: one `filled`, one `missed`.

Delete the throwaway database and its sidecars afterwards.

- [ ] **Step 3: Update CLAUDE.md**

Add to the **Config** section:

```markdown
`ARBYS_DB_URL` SQLite databases are opened in **WAL** journal mode with
`synchronous=NORMAL` and `busy_timeout=15000`, applied per connection by a
`connect` hook in `db/session.py` and gated on the dialect — issuing `PRAGMA`
against Postgres is an error. The default `delete` journal mode allows one
writer *and blocks readers*, which with five concurrent writers produced 18
`database is locked` errors and 6 QueuePool timeouts in a day. WAL leaves
`-wal` and `-shm` sidecar files beside the database.
```

Add a new section after **Trade history is ticket-level**:

```markdown
## A dropped write is counted, not silent

Every persistence path in the paper broker and ticket service swallows its
exception on purpose: a broken trade is worse than an unrecorded one. The flaw
that cost real data was that a swallowed write was **indistinguishable from a
successful one**. On 2026-08-25 that left a ticket stuck at `pending` and
`paper_position.realized_pnl` $132 adrift from the broker's own figure, with
nothing anywhere saying rows had been lost.

All of those writes now go through `db/session.py:run_write`, which retries
`database is locked` three times and, if it still fails, counts it.
`GET /health` reports `dropped_writes` and `last_dropped_write`. **Non-zero
means the ledger on screen is incomplete** — treat any figure derived from it
as a lower bound until the count is back to zero.

Only `database is locked` is retried. Any other exception is counted once and
logged as itself, so a real bug is never filed as contention.
```

And to the **Trade history is ticket-level** section, replace the
"an attempt is logged only once it reaches `submit_arb_ticket`" bullet with:

```markdown
- **A manual click is always an attempt.**
  `submit_arb_ticket_for_descriptor` resolves the descriptor itself and writes
  a `missed` ticket when no live edge matches, so a click on a row whose edge
  just died leaves a record. The endpoint no longer resolves anything. The
  narrower rule still holds for `submit_arb_ticket`, which the auto-trader
  calls: a detector finding nothing is not an attempt, or a bot would write
  thousands of rows a night saying nothing happened.
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: WAL, the dropped-write counter, and attempt recording"
```

---

## Self-review notes

**Spec coverage.** Part A → Task 1. Part B → Task 2. Part C → Task 3. Part D →
Task 4. Testing section → distributed, with the full bar and the live check in
Task 5.

**Two deviations from the spec, both deliberate:**

1. The spec says the retry should "re-raise anything else so a real bug is not
   masked as contention." `run_write` cannot re-raise — its callers'
   contract is that persistence never breaks a trade, and re-raising would
   push the exception straight back into the broker's `_emit` suppression
   where it would be lost anyway. It instead *does not retry* a non-lock
   error, counts it once, and logs it with its own type. Same intent,
   achievable contract.
2. The spec's Part C batches "the 567 per-group transactions"; the plan also
   makes in-memory state and engine registration conditional on the batch
   having committed. Without that, a dropped batch would leave `AppState`
   holding groups the database has never seen — the exact class of divergence
   this plan exists to remove.

**Not covered, by design.** The existing $132 divergence is a spec non-goal
and no task addresses it; the fix stops further loss and cannot reconstruct
history.
