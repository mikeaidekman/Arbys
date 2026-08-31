"""Engine configuration and write reliability.

SQLite in the default `delete` journal mode allows one writer and blocks
readers while it writes. Five services in this app write concurrently — the
discovery pass alone bursts one transaction per changed group — which produced
18 "database is locked" errors and 6 QueuePool timeouts in a single day, each
one swallowed by a persistence path that must never break a trade.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

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


async def test_memory_sqlite_bare_url_spelling_still_configures():
    """`sqlite+aiosqlite://` (no database part at all) is a second legal
    in-memory spelling. `make_url(url).database` is `None` for it, not the
    literal string `":memory:"` -- so a `":memory:" not in resolved` substring
    check on the raw URL text misses it, and it still gets `StaticPool`
    (verified against SQLAlchemy 2.0.51), so it hits the exact
    `pool_size`/`max_overflow` crash the guard exists to prevent.
    """
    engine = db_session.configure_engine("sqlite+aiosqlite://")
    assert engine.dialect.name == "sqlite"


async def test_run_write_commits_and_reports_success():
    from arbys.db import repositories as repo
    from arbys.db.session import create_all, run_write

    await create_all()
    db_session.reset_dropped_writes()
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


async def test_run_write_retries_a_pool_timeout_then_succeeds():
    """`sqlalchemy.exc.TimeoutError` (the QueuePool timeout) is not a subclass
    of `OperationalError` (verified against SQLAlchemy 2.0.51), so a predicate
    that only checked `isinstance(exc, OperationalError)` abandoned this on
    the first attempt with no retry -- despite it being one of the two
    transient error classes the spec measured (6 occurrences in a day) and
    the more transient of the two: the pool drains as in-flight work
    finishes, with no lock to wait out.
    """
    from sqlalchemy.exc import TimeoutError as SATimeoutError

    from arbys.db.session import create_all, run_write

    await create_all()
    db_session.reset_dropped_writes()
    attempts = {"n": 0}

    async def work(_session):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise SATimeoutError("QueuePool limit exceeded")

    ok = await run_write("test.pool_timeout", work)
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


async def test_a_routed_sink_site_counts_its_dropped_write(monkeypatch):
    """Proves `DbPaperPersistenceSink.on_fill` goes through `run_write`, not a
    raw `session_scope`.

    `run_write` itself is well covered above, but nothing exercised the sink
    by name -- so a later "simplification" reverting this one call back to
    `async with session_scope(): ...` would pass every existing test (rows
    still land on the happy path) while silently restoring invisible data
    loss on failure. A raw `session_scope` write here would leave
    `dropped_writes` at 0 and let the exception vanish into
    `paper_broker._emit`'s `contextlib.suppress(Exception)`; only routing
    through `run_write` counts it.
    """
    from decimal import Decimal

    from sqlalchemy.exc import OperationalError

    from arbys.adapters.base import Fill, Order, OrderStatus
    from arbys.db import repositories as repo
    from arbys.db.session import create_all
    from arbys.shared.persistence import DbPaperPersistenceSink

    await create_all()
    db_session.reset_dropped_writes()

    async def boom(*_a, **_k):
        raise OperationalError("stmt", {}, Exception("database is locked"))

    monkeypatch.setattr(repo, "insert_paper_fill", boom)

    sink = DbPaperPersistenceSink()
    order = Order(
        id="ord-1",
        venue_id="kalshi",
        outcome_id="k-yes",
        is_buy=True,
        qty=Decimal("10"),
        limit_price=Decimal("0.50"),
        status=OrderStatus.FILLED,
    )
    fill = Fill(order_id="ord-1", qty=Decimal("10"), price=Decimal("0.50"), fee=Decimal("0"))

    # Must not raise: a broken trade is worse than an unrecorded one.
    await sink.on_fill(order, fill)

    stats = db_session.dropped_write_stats()
    assert stats["dropped_writes"] == 1
    assert "sink.on_fill" in str(stats["last_dropped_write"])


async def test_ticket_status_write_counts_its_dropped_write(monkeypatch):
    """Proves `ticket_service._set_status` goes through `run_write`.

    This is the exact path that left a real ticket stuck at `pending` on
    2026-08-25: the lock error was swallowed with no trace. Only a test that
    forces the underlying repository call to fail and checks the counter can
    tell this site apart from one still using a raw `session_scope`.
    """
    from sqlalchemy.exc import OperationalError

    from arbys.backend.ticket_service import _set_status
    from arbys.db import repositories as repo
    from arbys.db.session import create_all

    await create_all()
    db_session.reset_dropped_writes()

    async def boom(*_a, **_k):
        raise OperationalError("stmt", {}, Exception("database is locked"))

    monkeypatch.setattr(repo, "update_paper_ticket_status", boom)

    # Must not raise.
    await _set_status("ticket-1", status="filled", reason=None)

    stats = db_session.dropped_write_stats()
    assert stats["dropped_writes"] == 1
    assert "ticket.status" in str(stats["last_dropped_write"])


async def test_persist_opp_write_counts_its_dropped_write(monkeypatch):
    """Proves `AppState._persist_opp` goes through `run_write`.

    This used to be a bare `except Exception: pass` -- no log, no count, no
    retry -- fired fire-and-forget from the engine's hot path on every new
    opportunity fingerprint, making it the highest-frequency writer in the
    app and the last fully silent one.
    """
    from sqlalchemy.exc import OperationalError

    from arbys.backend import state as state_mod
    from arbys.db import repositories as repo
    from arbys.db.session import create_all
    from arbys.shared.arb_engine import ArbLeg, ArbOpportunity

    await create_all()
    db_session.reset_dropped_writes()

    async def boom(*_a, **_k):
        raise OperationalError("stmt", {}, Exception("database is locked"))

    monkeypatch.setattr(repo, "insert_opportunity", boom)

    state = state_mod.AppState()
    opp = ArbOpportunity(
        event_group_id="eg",
        legs=(
            ArbLeg(
                outcome_id="y", venue_id="poly", is_buy=True,
                price=Decimal("0.45"), qty=Decimal("1"), fee=Decimal("0"),
            ),
            ArbLeg(
                outcome_id="n", venue_id="kals", is_buy=True,
                price=Decimal("0.50"), qty=Decimal("1"), fee=Decimal("0"),
            ),
        ),
        total_stake=Decimal("0.95"),
        guaranteed_profit=Decimal("0.05"),
        guaranteed_profit_bps=Decimal("526.3"),
    )

    # Must not raise.
    await state._persist_opp(opp)

    stats = db_session.dropped_write_stats()
    assert stats["dropped_writes"] == 1
    assert "state.opportunity" in str(stats["last_dropped_write"])


async def test_pnl_snapshot_write_counts_its_dropped_write(monkeypatch):
    """Proves `PnlSnapshotService.snapshot_once` goes through `run_write`.

    Previously this logged the exception and moved on without counting or
    retrying -- a dropped snapshot is a hole in the equity curve that
    `/health` had no way to reflect.
    """
    from sqlalchemy.exc import OperationalError

    from arbys.db import repositories as repo
    from arbys.db.session import create_all
    from arbys.ingest.pnl_service import PnlSnapshotService
    from arbys.shared.quotebook import QuoteBook

    await create_all()
    db_session.reset_dropped_writes()

    async def boom(*_a, **_k):
        raise OperationalError("stmt", {}, Exception("database is locked"))

    monkeypatch.setattr(repo, "insert_paper_pnl_snapshot", boom)

    svc = PnlSnapshotService(brokers={}, quotebook=QuoteBook(), account_ids=["acct-1"])

    # Must not raise.
    await svc.snapshot_once()

    stats = db_session.dropped_write_stats()
    assert stats["dropped_writes"] == 1
    assert "pnl.snapshot" in str(stats["last_dropped_write"])


async def test_concurrent_write_burst_and_reads_drop_nothing(seed_reference_rows):
    """The test the spec called for and that was never written.

    Every other test in this module injects a synthetic `OperationalError`
    from a fake `work` -- proving `run_write`'s retry loop in isolation, but
    passing byte-identically under `journal_mode=delete` too, because nothing
    in it touches real SQLite contention. This one drives real concurrent
    writes -- a discovery-shaped burst of small group upserts racing a
    snapshot-shaped burst of pnl writes, all gated on an `asyncio.Event` so
    they genuinely overlap rather than merely interleaving -- against a real
    file-backed database, while a join-heavy read (`list_event_groups`'s
    3-table join) holds its transaction open across the whole burst.

    That held read is the one WAL exists to fix: under the pre-WAL `delete`
    journal mode a writer must wait for every open reader to finish before it
    can even begin, where under WAL a reader never blocks a writer and vice
    versa. `busy_timeout=15000` means neither mode ever raises `database is
    locked` at this scale -- the difference shows up as wall-clock time, not
    as a dropped write, which is why this asserts `dropped_writes == 0` for
    correctness and the discriminating check below is a manual timing
    comparison instead of an assertion (timing assertions in a shared-CI
    test are a flakiness trap).

    So this test is NOT the WAL regression guard, despite driving the
    contention WAL exists to fix -- it would pass unchanged after a revert to
    `journal_mode=delete`. The guard is
    `test_sqlite_connections_are_wal_with_a_busy_timeout`, which asserts the
    pragma value directly. This one's job is to prove real concurrent writes
    against a real file drop nothing, which no other test in the module does.

    Verified manually by forcing `_SQLITE_PRAGMAS` to `journal_mode=DELETE`
    and running this exact test 10x under each mode: mean 1.78s / median
    1.82s under WAL vs mean 2.22s / median 2.05s under DELETE -- individual
    runs overlap (this machine's own scheduling noise is real), but DELETE
    was slower in aggregate every time this was repeated, a ~20-25%
    slowdown from forcing every one of the 30 concurrent writers to queue up
    behind the held read instead of proceeding alongside it. Two
    smaller-scale designs (a handful of short-held sequential reads instead
    of one long-held read gated on all 30 writers at once) did NOT
    discriminate at all -- their effect was too small relative to this
    machine's timing noise -- before this one did; see the trustworthy-ledger
    fix report for the full raw numbers from all three designs.
    """
    import asyncio

    from arbys.db import repositories as repo
    from arbys.db.session import create_all, get_session_factory, run_write
    from arbys.shared.types import EventGroup, EventGroupLeg

    await create_all()
    await seed_reference_rows(account_id="acct-burst")
    db_session.reset_dropped_writes()
    factory = get_session_factory()

    def _group(i: int) -> EventGroup:
        return EventGroup(
            id=f"burst-eg-{i}",
            title=f"burst {i}",
            legs=(
                EventGroupLeg(outcome_id=f"burst-{i}-a", venue_id="kalshi", is_yes_side=True),
                EventGroupLeg(
                    outcome_id=f"burst-{i}-b", venue_id="polymarket_us", is_yes_side=False
                ),
            ),
        )

    reader_ready = asyncio.Event()

    async def held_read() -> None:
        async with factory() as session:
            await repo.list_event_groups(session)
            reader_ready.set()
            # Hold the read transaction open across the writer burst below --
            # long enough that a writer under `delete` journal mode has to
            # wait on it, which is exactly the blocking WAL removes.
            await asyncio.sleep(1.0)

    async def group_write(i: int) -> bool:
        await reader_ready.wait()
        group = _group(i)
        return await run_write(
            "discovery.groups", lambda s, group=group: repo.upsert_event_group(s, group)
        )

    async def snapshot_write(i: int) -> bool:
        await reader_ready.wait()
        return await run_write(
            "pnl.snapshot",
            lambda s, i=i: repo.insert_paper_pnl_snapshot(
                s,
                account_id="acct-burst",
                cash=Decimal(i),
                mtm_positions=Decimal("0"),
                total_equity=Decimal(i),
            ),
        )

    # 15 group upserts + 15 snapshot writes, all released the instant the
    # read has its snapshot -- genuine overlap, not just interleaving.
    await asyncio.gather(
        held_read(),
        *(group_write(i) for i in range(15)),
        *(snapshot_write(i) for i in range(15)),
    )

    stats = db_session.dropped_write_stats()
    assert stats["dropped_writes"] == 0, stats
