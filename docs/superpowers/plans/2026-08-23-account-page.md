# Account Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an audit trail for paper arb tickets — a `/account` page showing what was filled, what was rejected, and whether the guaranteed profit materialised — and fix the six defects that make that trail impossible to record today.

**Architecture:** A new `paper_ticket` table gives an arb ticket a durable identity, with `paper_order.ticket_id` grouping its legs. A new `paper_settlement` table records resolution events so a ticket can be scored. All submissions funnel through one new `submit_arb_ticket()` in `arbys/backend/ticket_service.py`, which is where the position cap moves to and where filled / rejected / missed tickets get written. The frontend deletes the sidebar, replaces it with a full-width `AccountStrip`, and adds a `/account` page whose centrepiece is the ticket log.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2 async, Alembic, pytest (`asyncio_mode = "auto"`), Vite + React 19 + TypeScript, TanStack Query, react-router-dom.

**Spec:** [docs/superpowers/specs/2026-08-23-account-page-design.md](../specs/2026-08-23-account-page-design.md)

## Global Constraints

- **All money and all prices are `Decimal`. Never float.** Prices are probabilities in `[0, 1]`.
- Run everything from the repo root with `venv\Scripts\python.exe`, never a bare `python`.
- `venv\Scripts\python.exe -m pytest -q` must stay green — 235 tests before this plan.
- `venv\Scripts\python.exe -m ruff check .` must stay clean.
- mypy is **not** part of the green bar (47 pre-existing errors). Do not claim "mypy clean" and do not start a cleanup. Annotating code you already touch is welcome.
- **Migrations must never build DDL from `Base.metadata`.** Each revision describes its own change in explicit `op.*` calls.
- SQLite autoincrement PKs need `BigInteger().with_variant(Integer(), "sqlite")` or inserts fail on a NOT NULL constraint.
- `tests/db/test_migrations_match_models.py` diffs the replayed migration chain against `create_all`, so **migration and `models.py` must agree exactly on column type and nullability**.
- Tests never hit a real venue. `tests/conftest.py` forces the venue switches off session-wide.
- Domain types in `arbys/shared/` are `@dataclass(frozen=True)`; `shared/` may not import `httpx`, SQLAlchemy, or FastAPI.
- Frontend styling comes from `frontend/public/design/industry/styles.css` semantic classes and CSS custom properties. **No new hex colors, radii, or type scales.** No dark mode.
- Frontend typecheck is `npm run build` (`tsc -b && vite build`); lint is `npm run lint` (oxlint, not eslint).
- Git identity is repo-local on purpose. Do not touch it.

## File Structure

**Created:**

| File | Responsibility |
| --- | --- |
| `arbys/db/migrations/versions/0006_paper_ticket_and_settlement.py` | Adds `paper_ticket`, `paper_settlement`, `paper_order.ticket_id` |
| `arbys/shared/equity.py` | Pure `account_equity()` — the single mark-to-market computation |
| `arbys/backend/ticket_service.py` | `submit_arb_ticket()` — the only way a ticket is submitted |
| `frontend/src/components/AccountStrip.tsx` | Six-figure summary bar, used on both pages |
| `frontend/src/components/TicketHistory.tsx` | The ticket log table |
| `frontend/src/pages/AccountPage.tsx` | `/account` route composition |
| `tests/shared/test_equity.py` | `account_equity` maths |
| `tests/test_ticket_service.py` | Cap enforcement, filled / rejected / missed writes |
| `tests/db/test_ticket_repo.py` | Repo round-trips and ticket scoring |

**Modified:**

| File | Change |
| --- | --- |
| `arbys/db/models.py` | `PaperTicket`, `PaperSettlement`, `PaperOrder.ticket_id` |
| `arbys/db/repositories.py` | Ticket + settlement writes, `list_paper_tickets`, `list_paper_positions`, enriched `list_paper_orders` |
| `arbys/adapters/base.py` | `Order.ticket_id`, `ExecutionIntent.ticket_id` |
| `arbys/shared/paper_broker.py` | `apply_fill(ticket_id=…)`, sink `on_settlement`, settlement emits it |
| `arbys/shared/persistence.py` | `on_settlement` on both sinks; `ticket_id` passed through `on_order` |
| `arbys/shared/execution_router.py` | Structured `InsufficientLegsError.rejections`; threads `ticket_id` |
| `arbys/ingest/pnl_service.py` | Delegates to `account_equity` |
| `arbys/backend/app.py` | `/paper/execute` rewired; `/tickets`, `/positions` added; summary enriched |
| `arbys/backend/schemas.py` | Ticket, position, and summary schemas |
| `frontend/src/api/types.ts`, `client.ts` | New types and calls |
| `frontend/src/pages/TerminalPage.tsx` | Sidebar column removed, strip + `/account` link added |
| `frontend/src/main.tsx` | `/account` route |
| `CLAUDE.md`, `docs/RUNBOOK.md` | Document the ticket log and settlement events |

**Deleted:** `frontend/src/components/AccountPanel.tsx`

---

### Task 1: Ticket and settlement tables

**Files:**
- Modify: `arbys/db/models.py` (add after `PaperOrder`, around line 232)
- Create: `arbys/db/migrations/versions/0006_paper_ticket_and_settlement.py`
- Test: `tests/db/test_migrations_match_models.py` (existing, no edit — it must pass)

**Interfaces:**
- Consumes: nothing.
- Produces: `models.PaperTicket`, `models.PaperSettlement`, `models.PaperOrder.ticket_id`.

- [ ] **Step 1: Add the two models and the column**

In `arbys/db/models.py`, add `ticket_id` to `PaperOrder` immediately after `arb_opportunity_id`:

```python
    ticket_id: Mapped[str | None] = mapped_column(ForeignKey("paper_ticket.id"))
```

Then add both new classes after the `PaperOrder` class body:

```python
class PaperTicket(Base):
    """One submitted arb ticket: filled, rejected, or missed.

    `event_group_id` is deliberately **not** a ForeignKey. Discovery retires
    groups when they stop matching and `delete_event_group` takes the legs with
    it, so a live join would blank the name of every finished game — exactly
    the rows worth auditing. `title_snapshot` is frozen at submit time for the
    same reason and is the only naming the UI renders.

    The three economic columns are nullable because a `missed` ticket has no
    economics: a manual click passes only an event group and outcome ids, so if
    the re-detect comes up empty there is no stake or expected profit to write.
    Zero would read as a free ticket that made nothing.
    """

    __tablename__ = "paper_ticket"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("paper_account.id"), nullable=False)
    event_group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title_snapshot: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(String(256))
    total_stake: Mapped[Decimal | None] = mapped_column(NUM)
    expected_profit: Mapped[Decimal | None] = mapped_column(NUM)
    expected_edge_bps: Mapped[Decimal | None] = mapped_column(NUM)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_paper_ticket_account_ts", "account_id", "submitted_at"),
    )


class PaperSettlement(Base):
    """A resolution event for one outcome.

    Settlement previously left no trace: `settle_outcome_async` zeroed the
    position and credited cash, making a settled winner indistinguishable from
    a position sold out at market. Without this row a ticket cannot be scored.
    """

    __tablename__ = "paper_settlement"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    outcome_id: Mapped[str] = mapped_column(ForeignKey("outcome.id"), nullable=False)
    resolved_value: Mapped[Decimal] = mapped_column(NUM, nullable=False)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="heuristic")

    __table_args__ = (
        Index("ix_paper_settlement_outcome_ts", "outcome_id", "ts"),
    )
```

- [ ] **Step 2: Write the migration**

Create `arbys/db/migrations/versions/0006_paper_ticket_and_settlement.py`:

```python
"""add paper_ticket and paper_settlement

Revision ID: 0006_paper_ticket_and_settlement
Revises: 0005_polymarket_us_venue
Create Date: 2026-08-23

Two gaps this closes.

`paper_order` had no ticket identity: `arb_opportunity_id` existed but no
caller ever set it, and it could not be used — opportunities are persisted
deduped by fingerprint while execution re-detects and mints a fresh uuid, so
the executed object's id is routinely absent from the DB.

Settlement wrote no event row at all, so a settled winner looked exactly like
a position sold out at market and no ticket could be scored.

`paper_ticket.event_group_id` is intentionally not a foreign key: discovery
deletes groups, and trade history must outlive them.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_paper_ticket_and_settlement"
down_revision: str | None = "0005_polymarket_us_venue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NUM = sa.Numeric(28, 12)


def upgrade() -> None:
    op.create_table(
        "paper_ticket",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("event_group_id", sa.String(64), nullable=False),
        sa.Column("title_snapshot", sa.String(512), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("rejection_reason", sa.String(256), nullable=True),
        sa.Column("total_stake", NUM, nullable=True),
        sa.Column("expected_profit", NUM, nullable=True),
        sa.Column("expected_edge_bps", NUM, nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["account_id"], ["paper_account.id"],
                                name="fk_paper_ticket_account_id_paper_account"),
    )
    op.create_index(
        "ix_paper_ticket_account_ts", "paper_ticket", ["account_id", "submitted_at"]
    )

    op.create_table(
        "paper_settlement",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("outcome_id", sa.String(64), nullable=False),
        sa.Column("resolved_value", NUM, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("source", sa.String(16), nullable=False, server_default="heuristic"),
        sa.ForeignKeyConstraint(["outcome_id"], ["outcome.id"],
                                name="fk_paper_settlement_outcome_id_outcome"),
    )
    op.create_index(
        "ix_paper_settlement_outcome_ts", "paper_settlement", ["outcome_id", "ts"]
    )

    with op.batch_alter_table("paper_order") as batch:
        batch.add_column(sa.Column("ticket_id", sa.String(64), nullable=True))
        batch.create_foreign_key(
            "fk_paper_order_ticket_id_paper_ticket", "paper_ticket", ["ticket_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("paper_order") as batch:
        batch.drop_constraint("fk_paper_order_ticket_id_paper_ticket", type_="foreignkey")
        batch.drop_column("ticket_id")
    op.drop_index("ix_paper_settlement_outcome_ts", table_name="paper_settlement")
    op.drop_table("paper_settlement")
    op.drop_index("ix_paper_ticket_account_ts", table_name="paper_ticket")
    op.drop_table("paper_ticket")
```

- [ ] **Step 3: Run the schema-agreement test**

Run: `venv\Scripts\python.exe -m pytest tests/db/test_migrations_match_models.py -q`
Expected: PASS. A failure prints the differing table/column — the usual cause is a nullability or `Numeric` precision mismatch between the migration and `models.py`.

- [ ] **Step 4: Run the whole suite**

Run: `venv\Scripts\python.exe -m pytest -q`
Expected: 235 passed. Nothing reads the new tables yet.

- [ ] **Step 5: Commit**

```bash
git add arbys/db/models.py arbys/db/migrations/versions/0006_paper_ticket_and_settlement.py
git commit -m "feat(db): add paper_ticket and paper_settlement"
```

---

### Task 2: Ticket and settlement writes

**Files:**
- Modify: `arbys/db/repositories.py` (`insert_paper_order` at line 220; new functions after `insert_paper_fill`)
- Test: `tests/db/test_ticket_repo.py` (create)

**Interfaces:**
- Consumes: `models.PaperTicket`, `models.PaperSettlement` (Task 1).
- Produces:
  - `insert_paper_ticket(session, *, ticket_id, account_id, event_group_id, title_snapshot, source, status, rejection_reason=None, total_stake=None, expected_profit=None, expected_edge_bps=None) -> None`
  - `insert_paper_settlement(session, *, outcome_id, venue_id, resolved_value, source) -> None`
  - `insert_paper_order(..., ticket_id: str | None = None)`

- [ ] **Step 1: Write the failing test**

Create `tests/db/test_ticket_repo.py`:

```python
"""Ticket and settlement persistence."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from arbys.db import repositories as repo
from arbys.db import session as db_session
from arbys.db.session import create_all, session_scope


@pytest.fixture(autouse=True)
def _sqlite_db(tmp_path: Path):
    os.environ["ARBYS_DB_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'tickets.db'}"
    db_session.reset_engine()
    yield
    db_session.reset_engine()
    os.environ.pop("ARBYS_DB_URL", None)


async def test_ticket_and_settlement_round_trip():
    await create_all()
    async with session_scope() as session:
        await repo.ensure_venue(session, "kalshi", name="Kalshi", kind="exchange")
        await repo.ensure_paper_account(session, "default")
        await repo.insert_paper_ticket(
            session,
            ticket_id="tkt-1",
            account_id="default",
            event_group_id="eg-1",
            title_snapshot="MLB: ATL @ LAD",
            source="manual",
            status="filled",
            total_stake=Decimal("90.00"),
            expected_profit=Decimal("10.00"),
            expected_edge_bps=Decimal("1111"),
        )
        await repo.insert_paper_order(
            session,
            order_id="ord-1",
            account_id="default",
            venue_id="kalshi",
            outcome_id="k-yes",
            is_buy=True,
            qty=Decimal("100"),
            limit_price=Decimal("0.40"),
            status="filled",
            ticket_id="tkt-1",
        )
        await repo.insert_paper_settlement(
            session,
            outcome_id="k-yes",
            venue_id="kalshi",
            resolved_value=Decimal("1"),
            source="heuristic",
        )

    async with session_scope() as session:
        orders = await repo.list_paper_orders(session, "default")
        assert orders[0]["ticket_id"] == "tkt-1"
        settlements = await repo.list_paper_settlements(session)
        assert settlements[0]["outcome_id"] == "k-yes"
        assert settlements[0]["resolved_value"] == Decimal("1")


async def test_missed_ticket_has_null_economics():
    """A missed ticket has no stake or expectation. Zero would read as a free
    ticket that made nothing."""
    await create_all()
    async with session_scope() as session:
        await repo.ensure_paper_account(session, "default")
        await repo.insert_paper_ticket(
            session,
            ticket_id="tkt-miss",
            account_id="default",
            event_group_id="eg-9",
            title_snapshot="NFL: ARI @ LAC",
            source="auto",
            status="missed",
            rejection_reason="edge_no_longer_available",
        )
    async with session_scope() as session:
        rows = await repo.list_paper_settlements(session)
        assert rows == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/db/test_ticket_repo.py -q`
Expected: FAIL — `AttributeError: module 'arbys.db.repositories' has no attribute 'insert_paper_ticket'`.

- [ ] **Step 3: Add the repo functions**

In `arbys/db/repositories.py`, add `ticket_id` to `insert_paper_order` — a new keyword-only parameter and a matching field on the model construction:

```python
async def insert_paper_order(
    session: AsyncSession, *, order_id: str, account_id: str, venue_id: str,
    outcome_id: str, is_buy: bool, qty: Decimal, limit_price: Decimal, status: str,
    arb_opportunity_id: str | None = None, rejection_reason: str | None = None,
    ticket_id: str | None = None,
) -> None:
    await ensure_outcome_placeholder(session, outcome_id, venue_id=venue_id)
    session.add(
        m.PaperOrder(
            id=order_id,
            account_id=account_id,
            venue_id=venue_id,
            outcome_id=outcome_id,
            is_buy=is_buy,
            qty=qty,
            limit_price=limit_price,
            status=status,
            arb_opportunity_id=arb_opportunity_id,
            rejection_reason=rejection_reason,
            ticket_id=ticket_id,
        )
    )
```

Add `ticket_id` and `rejection_reason` to the dict `list_paper_orders` returns, so the enriched shape lands in one place:

```python
        {
            "id": r.id,
            "ticket_id": r.ticket_id,
            "venue_id": r.venue_id,
            "outcome_id": r.outcome_id,
            "is_buy": r.is_buy,
            "qty": r.qty,
            "limit_price": r.limit_price,
            "status": r.status,
            "rejection_reason": r.rejection_reason,
            "submitted_at": r.submitted_at,
        }
```

Then append the new functions after `insert_paper_fill`:

```python
async def insert_paper_ticket(
    session: AsyncSession, *, ticket_id: str, account_id: str, event_group_id: str,
    title_snapshot: str, source: str, status: str,
    rejection_reason: str | None = None, total_stake: Decimal | None = None,
    expected_profit: Decimal | None = None, expected_edge_bps: Decimal | None = None,
) -> None:
    session.add(
        m.PaperTicket(
            id=ticket_id,
            account_id=account_id,
            event_group_id=event_group_id,
            title_snapshot=title_snapshot,
            source=source,
            status=status,
            rejection_reason=rejection_reason,
            total_stake=total_stake,
            expected_profit=expected_profit,
            expected_edge_bps=expected_edge_bps,
        )
    )


async def insert_paper_settlement(
    session: AsyncSession, *, outcome_id: str, venue_id: str,
    resolved_value: Decimal, source: str = "heuristic",
) -> None:
    await ensure_outcome_placeholder(session, outcome_id, venue_id=venue_id)
    session.add(
        m.PaperSettlement(
            outcome_id=outcome_id, resolved_value=resolved_value, source=source
        )
    )


async def list_paper_settlements(session: AsyncSession) -> list[dict]:
    """Latest settlement per outcome, newest first."""
    rows = (
        await session.execute(
            select(m.PaperSettlement).order_by(m.PaperSettlement.ts.desc())
        )
    ).scalars().all()
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        if r.outcome_id in seen:
            continue
        seen.add(r.outcome_id)
        out.append(
            {
                "outcome_id": r.outcome_id,
                "resolved_value": r.resolved_value,
                "ts": r.ts,
                "source": r.source,
            }
        )
    return out
```

Add `"insert_paper_ticket"`, `"insert_paper_settlement"`, and `"list_paper_settlements"` to the `__all__` list at the end of the module, keeping it alphabetically sorted as it already is.

- [ ] **Step 4: Run the test**

Run: `venv\Scripts\python.exe -m pytest tests/db/test_ticket_repo.py -q`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add arbys/db/repositories.py tests/db/test_ticket_repo.py
git commit -m "feat(db): ticket and settlement writes"
```

---

### Task 3: Ticket reads with fills and scoring

**Files:**
- Modify: `arbys/db/repositories.py`
- Test: `tests/db/test_ticket_repo.py` (append)

**Interfaces:**
- Consumes: `insert_paper_ticket`, `insert_paper_settlement`, `insert_paper_order` with `ticket_id` (Task 2).
- Produces: `list_paper_tickets(session, account_id, *, limit=200, status=None, source=None) -> list[dict]`. Each dict has keys `id, event_group_id, title_snapshot, source, status, rejection_reason, total_stake, expected_profit, expected_edge_bps, submitted_at, realized_profit, legs`; each leg has `venue_id, outcome_id, is_buy, qty, limit_price, fill_price, fee, status, rejection_reason`.

- [ ] **Step 1: Write the failing test**

Append to `tests/db/test_ticket_repo.py`:

```python
async def _two_leg_filled_ticket(session, *, ticket_id: str) -> None:
    """A 100-unit arb: buy YES at 0.40 and NO at 0.50, 1c fee each side."""
    await repo.ensure_venue(session, "kalshi", name="Kalshi", kind="exchange")
    await repo.ensure_venue(
        session, "polymarket_us", name="Polymarket US", kind="exchange"
    )
    await repo.ensure_paper_account(session, "default")
    await repo.insert_paper_ticket(
        session,
        ticket_id=ticket_id,
        account_id="default",
        event_group_id="eg-1",
        title_snapshot="MLB: ATL @ LAD",
        source="manual",
        status="filled",
        total_stake=Decimal("92.00"),
        expected_profit=Decimal("8.00"),
        expected_edge_bps=Decimal("869"),
    )
    for oid, venue, px, order_id in (
        ("k-yes", "kalshi", Decimal("0.40"), f"{ticket_id}-a"),
        ("p-no", "polymarket_us", Decimal("0.50"), f"{ticket_id}-b"),
    ):
        await repo.insert_paper_order(
            session,
            order_id=order_id,
            account_id="default",
            venue_id=venue,
            outcome_id=oid,
            is_buy=True,
            qty=Decimal("100"),
            limit_price=px,
            status="filled",
            ticket_id=ticket_id,
        )
        await repo.insert_paper_fill(
            session, order_id=order_id, qty=Decimal("100"), price=px, fee=Decimal("1.00")
        )


async def test_ticket_reports_fills_not_just_limits():
    await create_all()
    async with session_scope() as session:
        await _two_leg_filled_ticket(session, ticket_id="tkt-1")
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, "default")
    assert len(tickets) == 1
    legs = sorted(tickets[0]["legs"], key=lambda leg: leg["outcome_id"])
    assert [leg["fill_price"] for leg in legs] == [Decimal("0.40"), Decimal("0.50")]
    assert [leg["fee"] for leg in legs] == [Decimal("1.00"), Decimal("1.00")]


async def test_unsettled_ticket_scores_null():
    await create_all()
    async with session_scope() as session:
        await _two_leg_filled_ticket(session, ticket_id="tkt-1")
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, "default")
    assert tickets[0]["realized_profit"] is None


async def test_settled_ticket_scores_from_its_own_fills():
    """One leg resolves to 1, the other to 0.

    Winner: (1 - 0.40) * 100 - 1 = +59. Loser: (0 - 0.50) * 100 - 1 = -51.
    Net +8, which is the stake-implied edge: 100 payout on 92 spent.
    """
    await create_all()
    async with session_scope() as session:
        await _two_leg_filled_ticket(session, ticket_id="tkt-1")
        await repo.insert_paper_settlement(
            session, outcome_id="k-yes", venue_id="kalshi",
            resolved_value=Decimal("1"), source="heuristic",
        )
        await repo.insert_paper_settlement(
            session, outcome_id="p-no", venue_id="polymarket_us",
            resolved_value=Decimal("0"), source="heuristic",
        )
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, "default")
    assert tickets[0]["realized_profit"] == Decimal("8.00")


async def test_partially_settled_ticket_still_scores_null():
    await create_all()
    async with session_scope() as session:
        await _two_leg_filled_ticket(session, ticket_id="tkt-1")
        await repo.insert_paper_settlement(
            session, outcome_id="k-yes", venue_id="kalshi",
            resolved_value=Decimal("1"), source="heuristic",
        )
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, "default")
    assert tickets[0]["realized_profit"] is None


async def test_title_survives_event_group_deletion():
    """Discovery retires groups routinely. The snapshot is why history keeps
    its name for exactly the games that have finished."""
    from arbys.shared.types import EventGroup, EventGroupLeg

    await create_all()
    async with session_scope() as session:
        await repo.upsert_event_group(
            session,
            EventGroup(
                id="eg-1",
                title="MLB: ATL @ LAD",
                legs=(
                    EventGroupLeg(outcome_id="k-yes", venue_id="kalshi", is_yes_side=True),
                ),
            ),
        )
        await _two_leg_filled_ticket(session, ticket_id="tkt-1")
    async with session_scope() as session:
        await repo.delete_event_group(session, "eg-1")
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, "default")
    assert tickets[0]["title_snapshot"] == "MLB: ATL @ LAD"


async def test_tickets_filter_by_status_and_source():
    await create_all()
    async with session_scope() as session:
        await _two_leg_filled_ticket(session, ticket_id="tkt-filled")
        await repo.insert_paper_ticket(
            session,
            ticket_id="tkt-missed",
            account_id="default",
            event_group_id="eg-2",
            title_snapshot="NFL: ARI @ LAC",
            source="auto",
            status="missed",
            rejection_reason="edge_no_longer_available",
        )
    async with session_scope() as session:
        assert len(await repo.list_paper_tickets(session, "default")) == 2
        only_missed = await repo.list_paper_tickets(session, "default", status="missed")
        assert [t["id"] for t in only_missed] == ["tkt-missed"]
        only_auto = await repo.list_paper_tickets(session, "default", source="auto")
        assert [t["id"] for t in only_auto] == ["tkt-missed"]
        assert only_missed[0]["legs"] == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/db/test_ticket_repo.py -q`
Expected: FAIL — no attribute `list_paper_tickets`.

- [ ] **Step 3: Implement the read**

Append to `arbys/db/repositories.py`:

```python
async def list_paper_tickets(
    session: AsyncSession, account_id: str, *, limit: int = 200,
    status: str | None = None, source: str | None = None,
) -> list[dict]:
    """Ticket-level history, newest first, with fills joined and scoring.

    `realized_profit` is computed here rather than stored, from the ticket's
    **own** fills. Broker state cannot answer this: settlement uses an
    `avg_price` blended across every ticket on that outcome, and
    ARBYS_MAX_OUTCOME_QTY permits roughly 2.5 tickets on one.
    """
    stmt = select(m.PaperTicket).where(m.PaperTicket.account_id == account_id)
    if status is not None:
        stmt = stmt.where(m.PaperTicket.status == status)
    if source is not None:
        stmt = stmt.where(m.PaperTicket.source == source)
    tickets = (
        await session.execute(
            stmt.order_by(m.PaperTicket.submitted_at.desc()).limit(limit)
        )
    ).scalars().all()
    if not tickets:
        return []

    ticket_ids = [t.id for t in tickets]
    orders = (
        await session.execute(
            select(m.PaperOrder).where(m.PaperOrder.ticket_id.in_(ticket_ids))
        )
    ).scalars().all()

    fills_by_order: dict[str, tuple[Decimal, Decimal]] = {}
    if orders:
        fill_rows = (
            await session.execute(
                select(m.PaperFill).where(
                    m.PaperFill.order_id.in_([o.id for o in orders])
                )
            )
        ).scalars().all()
        for f in fill_rows:
            price, fee = fills_by_order.get(f.order_id, (Decimal("0"), Decimal("0")))
            fills_by_order[f.order_id] = (f.price, fee + f.fee)

    settled = {
        row["outcome_id"]: row["resolved_value"]
        for row in await list_paper_settlements(session)
    }

    legs_by_ticket: dict[str, list[dict]] = {tid: [] for tid in ticket_ids}
    for o in orders:
        fill_price, fee = fills_by_order.get(o.id, (None, Decimal("0")))
        legs_by_ticket[o.ticket_id].append(
            {
                "venue_id": o.venue_id,
                "outcome_id": o.outcome_id,
                "is_buy": o.is_buy,
                "qty": o.qty,
                "limit_price": o.limit_price,
                "fill_price": fill_price,
                "fee": fee,
                "status": o.status,
                "rejection_reason": o.rejection_reason,
            }
        )

    out: list[dict] = []
    for t in tickets:
        legs = legs_by_ticket[t.id]
        out.append(
            {
                "id": t.id,
                "event_group_id": t.event_group_id,
                "title_snapshot": t.title_snapshot,
                "source": t.source,
                "status": t.status,
                "rejection_reason": t.rejection_reason,
                "total_stake": t.total_stake,
                "expected_profit": t.expected_profit,
                "expected_edge_bps": t.expected_edge_bps,
                "submitted_at": t.submitted_at,
                "realized_profit": _score_ticket(legs, settled),
                "legs": legs,
            }
        )
    return out


def _score_ticket(
    legs: list[dict], settled: dict[str, Decimal]
) -> Decimal | None:
    """Realized profit, or None while any leg is unsettled.

    A sold leg inverts: the direction factor keeps the sign right even though
    every detector currently emits buys only.
    """
    if not legs:
        return None
    total = Decimal("0")
    for leg in legs:
        if leg["fill_price"] is None:
            return None
        resolved = settled.get(leg["outcome_id"])
        if resolved is None:
            return None
        direction = Decimal("1") if leg["is_buy"] else Decimal("-1")
        total += direction * (resolved - leg["fill_price"]) * leg["qty"] - leg["fee"]
    return total
```

Add `"list_paper_tickets"` to `__all__`.

- [ ] **Step 4: Run the tests**

Run: `venv\Scripts\python.exe -m pytest tests/db/test_ticket_repo.py -q`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add arbys/db/repositories.py tests/db/test_ticket_repo.py
git commit -m "feat(db): ticket history with fills and settlement scoring"
```

---

### Task 4: Thread ticket_id from intent to persisted order

**Files:**
- Modify: `arbys/adapters/base.py:33-58`, `arbys/shared/paper_broker.py`, `arbys/shared/persistence.py`, `arbys/shared/execution_router.py`
- Test: `tests/shared/test_paper_broker.py` (append)

**Interfaces:**
- Consumes: `insert_paper_order(ticket_id=…)` (Task 2).
- Produces: `Order.ticket_id: str | None`, `ExecutionIntent.ticket_id: str | None`, `PaperExecutionAdapter.apply_fill(..., ticket_id: str | None = None)`.

Threading it on `Order` rather than widening the sink protocol keeps
`on_order(order)` unchanged — the sink simply reads `order.ticket_id`.

- [ ] **Step 1: Write the failing test**

Append to `tests/shared/test_paper_broker.py`:

```python
def test_apply_fill_stamps_ticket_id_on_the_order():
    """The sink reads ticket_id off the Order, so it must survive apply_fill."""
    book = QuoteBook()
    book.upsert(
        Quote(
            outcome_id="k-yes",
            bid=Decimal("0.39"),
            ask=Decimal("0.40"),
        )
    )
    broker = PaperExecutionAdapter(
        venue_id="kalshi", quotebook=book, fee_model=KalshiFeeModel()
    )
    broker.deposit("acct", Decimal("100"))
    order, fill, reason = broker.apply_fill(
        account_id="acct",
        outcome_id="k-yes",
        is_buy=True,
        qty=Decimal("10"),
        limit_price=Decimal("0.40"),
        ticket_id="tkt-42",
    )
    assert reason is None
    assert fill is not None
    assert order.ticket_id == "tkt-42"


def test_rejected_order_also_carries_the_ticket_id():
    """A rejected leg must be attributable to its ticket, or the audit log
    cannot show why a ticket failed."""
    book = QuoteBook()
    broker = PaperExecutionAdapter(
        venue_id="kalshi", quotebook=book, fee_model=KalshiFeeModel()
    )
    order, fill, reason = broker.apply_fill(
        account_id="acct",
        outcome_id="missing",
        is_buy=True,
        qty=Decimal("10"),
        limit_price=Decimal("0.40"),
        ticket_id="tkt-43",
    )
    assert reason == "no_quote"
    assert fill is None
    assert order.ticket_id == "tkt-43"
```

Check the imports already at the top of that file and add any of `QuoteBook`, `Quote`, `KalshiFeeModel`, `PaperExecutionAdapter`, `Decimal` that are missing.

- [ ] **Step 2: Run it to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/shared/test_paper_broker.py -q`
Expected: FAIL — `apply_fill() got an unexpected keyword argument 'ticket_id'`.

- [ ] **Step 3: Add the field and thread it**

In `arbys/adapters/base.py`, add a trailing field to `Order` and to `ExecutionIntent`:

```python
@dataclass(frozen=True)
class Order:
    id: str
    venue_id: str
    outcome_id: str
    is_buy: bool
    qty: Decimal
    limit_price: Decimal
    status: OrderStatus
    # Groups the legs of one arb ticket. None for orders placed outside a
    # ticket, and for every row written before migration 0006.
    ticket_id: str | None = None
```

```python
@dataclass(frozen=True)
class ExecutionIntent:
    """A multi-leg trade the router should submit atomically ("arb ticket")."""

    event_group_id: str
    account_id: str
    legs: tuple[IntentLeg, ...]
    ticket_id: str | None = None
```

In `arbys/shared/paper_broker.py`, add `ticket_id: str | None = None` to the
`apply_fill` signature and pass it into **both** `Order(...)` constructions —
the one inside `_rejected` and the filled one.

In `place_order`, forward it too: add `ticket_id: str | None = None` to that
signature and pass it to `apply_fill`.

In `arbys/shared/execution_router.py`, `_commit_atomically` must pass the
intent's id:

```python
            order, fill, reason = adapter.apply_fill(
                account_id=account_id,
                outcome_id=leg.outcome_id,
                is_buy=leg.is_buy,
                qty=leg.qty,
                limit_price=leg.limit_price,
                ticket_id=intent.ticket_id,
            )
```

and `_commit_sequentially` likewise, adding `ticket_id=intent.ticket_id` to its
`place_order` call.

In `arbys/shared/persistence.py`, both `DbPaperPersistenceSink.on_order` and
`AccountScopedSink.on_order` pass it through:

```python
                ticket_id=order.ticket_id,
```

- [ ] **Step 4: Run the tests**

Run: `venv\Scripts\python.exe -m pytest tests/shared/test_paper_broker.py tests/test_backend_e2e.py -q`
Expected: all pass. `Order` gained a defaulted field, so existing constructions are unaffected.

- [ ] **Step 5: Commit**

```bash
git add arbys/adapters/base.py arbys/shared/paper_broker.py arbys/shared/persistence.py arbys/shared/execution_router.py tests/shared/test_paper_broker.py
git commit -m "feat(exec): carry ticket_id from intent through to the persisted order"
```

---

### Task 5: Structured router rejections

**Files:**
- Modify: `arbys/shared/execution_router.py:21-56`
- Test: `tests/shared/test_paper_broker.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `LegRejection(venue_id: str, outcome_id: str, reason: str)` and `InsufficientLegsError.rejections: tuple[LegRejection, ...]`. `str(error)` keeps the existing joined format, so `/paper/execute`'s 409 detail text does not change.

Without per-leg structure the ticket log cannot say which leg failed — only a
joined string.

- [ ] **Step 1: Write the failing test**

```python
async def test_router_rejection_names_the_failing_leg():
    """The audit log needs the leg, not just a joined message string."""
    book = QuoteBook()
    book.upsert(
        Quote(
            outcome_id="k-yes",
            bid=Decimal("0.39"),
            ask=Decimal("0.40"),
            ask_size=Decimal("5"),
        )
    )
    broker = PaperExecutionAdapter(
        venue_id="kalshi", quotebook=book, fee_model=KalshiFeeModel()
    )
    broker.deposit("acct", Decimal("1000"))
    router = ExecutionRouter({"kalshi": broker})
    intent = ExecutionIntent(
        event_group_id="eg-1",
        account_id="acct",
        legs=(
            IntentLeg(
                venue_id="kalshi",
                outcome_id="k-yes",
                is_buy=True,
                qty=Decimal("100"),
                limit_price=Decimal("0.40"),
            ),
        ),
        ticket_id="tkt-1",
    )
    with pytest.raises(InsufficientLegsError) as excinfo:
        await router.submit(intent)
    rejections = excinfo.value.rejections
    assert len(rejections) == 1
    assert rejections[0].outcome_id == "k-yes"
    assert rejections[0].reason == "insufficient_liquidity"
    assert "kalshi:insufficient_liquidity" in str(excinfo.value)
```

Add `pytest`, `ExecutionRouter`, `ExecutionIntent`, `IntentLeg`, and
`InsufficientLegsError` to that file's imports if absent.

- [ ] **Step 2: Run it to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/shared/test_paper_broker.py -q`
Expected: FAIL — `AttributeError: 'InsufficientLegsError' object has no attribute 'rejections'`.

- [ ] **Step 3: Implement**

In `arbys/shared/execution_router.py`, replace the exception class and rework
the preview loop:

```python
@dataclass(frozen=True)
class LegRejection:
    venue_id: str
    outcome_id: str
    reason: str


class InsufficientLegsError(RuntimeError):
    """A ticket that could not be submitted, with the failing legs named.

    `str(...)` keeps the joined `venue:reason` form the API already returns as
    a 409 detail; `rejections` is what the ticket log records per leg.
    """

    def __init__(self, rejections: tuple[LegRejection, ...] | str) -> None:
        if isinstance(rejections, str):
            self.rejections: tuple[LegRejection, ...] = ()
            super().__init__(rejections)
            return
        self.rejections = rejections
        super().__init__(
            ", ".join(f"{r.venue_id}:{r.reason}" for r in rejections)
        )
```

`from dataclasses import dataclass` goes at the top of the module.

In `submit`, build `LegRejection` values instead of strings:

```python
        rejections: list[LegRejection] = []
        for leg in intent.legs:
            adapter = self._adapters.get(leg.venue_id)
            if adapter is None:
                rejections.append(
                    LegRejection(leg.venue_id, leg.outcome_id, "no_adapter")
                )
                continue
            if isinstance(adapter, PaperExecutionAdapter):
                preview = adapter._preview_fill(
                    outcome_id=leg.outcome_id,
                    is_buy=leg.is_buy,
                    qty=leg.qty,
                    limit_price=leg.limit_price,
                )
                if isinstance(preview, str):
                    rejections.append(
                        LegRejection(leg.venue_id, leg.outcome_id, preview)
                    )
                    continue
                if leg.is_buy:
                    _px, cost = preview
                    balances = await adapter.get_balances(intent.account_id)
                    if cost > balances.get(leg.venue_id, Decimal("0")):
                        rejections.append(
                            LegRejection(
                                leg.venue_id, leg.outcome_id, "insufficient_funds"
                            )
                        )
        if rejections:
            raise InsufficientLegsError(tuple(rejections))
```

The two post-preview raises in `_commit_atomically` and `_commit_sequentially`
already pass a string; the `str` branch of the constructor keeps them working
unchanged.

- [ ] **Step 4: Run the tests**

Run: `venv\Scripts\python.exe -m pytest tests/shared tests/test_backend_e2e.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add arbys/shared/execution_router.py tests/shared/test_paper_broker.py
git commit -m "feat(exec): structured per-leg rejections"
```

---

### Task 6: Settlement events

**Files:**
- Modify: `arbys/shared/paper_broker.py:44-72` (protocol) and `:393-420` (`settle_outcome_async`), `arbys/shared/persistence.py`
- Test: `tests/shared/test_paper_broker.py` (append)

**Interfaces:**
- Consumes: `insert_paper_settlement` (Task 2).
- Produces: `PaperPersistenceSink.on_settlement(outcome_id: str, resolved_value: Decimal, *, venue_id: str, source: str) -> None`.

- [ ] **Step 1: Write the failing test**

```python
async def test_settlement_notifies_the_sink():
    """Settlement used to leave no trace, making a settled winner
    indistinguishable from a position sold out at market."""
    recorded: list[tuple[str, Decimal, str, str]] = []

    class _Sink:
        async def on_order(self, order, *, rejection_reason=None): ...
        async def on_fill(self, order, fill): ...
        async def on_balance(self, account_id, venue_id, amount): ...
        async def on_position(
            self, account_id, outcome_id, qty, avg_price, realized_pnl, *, venue_id
        ): ...
        async def on_settlement(
            self, outcome_id, resolved_value, *, venue_id, source
        ):
            recorded.append((outcome_id, resolved_value, venue_id, source))

    book = QuoteBook()
    book.upsert(
        Quote(
            outcome_id="k-yes",
            bid=Decimal("0.39"),
            ask=Decimal("0.40"),
        )
    )
    broker = PaperExecutionAdapter(
        venue_id="kalshi", quotebook=book, fee_model=KalshiFeeModel(), sink=_Sink()
    )
    broker.deposit("acct", Decimal("100"))
    await broker.place_order(
        account_id="acct",
        outcome_id="k-yes",
        is_buy=True,
        qty=Decimal("10"),
        limit_price=Decimal("0.40"),
    )
    await broker.settle_outcome_async("k-yes", Decimal("1"))
    assert recorded == [("k-yes", Decimal("1"), "kalshi", "heuristic")]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/shared/test_paper_broker.py -q`
Expected: FAIL — `recorded` is empty; nothing calls `on_settlement`.

- [ ] **Step 3: Implement**

Add to the `PaperPersistenceSink` Protocol in `arbys/shared/paper_broker.py`:

```python
    async def on_settlement(
        self, outcome_id: str, resolved_value: Decimal, *, venue_id: str, source: str
    ) -> None: ...
```

Give `settle_outcome_async` a `source` parameter and emit the event once per
outcome, after the per-account loop — settlement is a property of the outcome,
not of an account:

```python
    async def settle_outcome_async(
        self, outcome_id: str, resolved_value: Decimal, *, source: str = "heuristic"
    ) -> None:
```

At the end of the method body, outside the `for account_id, st in ...` loop:

```python
        if self._sink is not None:
            await self._emit(
                self._sink.on_settlement(
                    outcome_id, resolved_value, venue_id=self.venue_id, source=source
                )
            )
```

Note the existing loop `continue`s for accounts holding nothing, so the emit
must not live inside it.

In `arbys/shared/persistence.py`, implement it on `DbPaperPersistenceSink`:

```python
    async def on_settlement(
        self, outcome_id: str, resolved_value: Decimal, *, venue_id: str, source: str
    ) -> None:
        async with session_scope() as session:
            await repo.insert_paper_settlement(
                session,
                outcome_id=outcome_id,
                venue_id=venue_id,
                resolved_value=resolved_value,
                source=source,
            )
```

and delegate from `AccountScopedSink`:

```python
    async def on_settlement(
        self, outcome_id: str, resolved_value: Decimal, *, venue_id: str, source: str
    ) -> None:
        await self._inner.on_settlement(
            outcome_id, resolved_value, venue_id=venue_id, source=source
        )
```

- [ ] **Step 4: Run the tests**

Run: `venv\Scripts\python.exe -m pytest tests/shared tests/test_ingest_wiring.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add arbys/shared/paper_broker.py arbys/shared/persistence.py tests/shared/test_paper_broker.py
git commit -m "feat(paper): record settlement events"
```

---

### Task 7: One equity computation

**Files:**
- Create: `arbys/shared/equity.py`
- Modify: `arbys/ingest/pnl_service.py:55-75`
- Test: `tests/shared/test_equity.py` (create)

**Interfaces:**
- Consumes: `PaperExecutionAdapter.account_snapshot` (existing).
- Produces:

```python
@dataclass(frozen=True)
class AccountEquity:
    cash: Decimal
    position_value: Decimal
    equity: Decimal
    unrealized: Decimal
    realized: Decimal

def account_equity(brokers, quotebook, account_id) -> AccountEquity
```

- [ ] **Step 1: Write the failing test**

Create `tests/shared/test_equity.py`:

```python
"""Equity is computed in exactly one place.

The summary endpoint and the PnL snapshotter both call this. If they computed
it differently the account strip and the curve below it would disagree on the
same page.
"""

from __future__ import annotations

from decimal import Decimal

from arbys.shared.equity import account_equity
from arbys.shared.fees import KalshiFeeModel
from arbys.shared.paper_broker import PaperExecutionAdapter
from arbys.shared.quotebook import QuoteBook
from arbys.shared.types import Quote


def _broker(book: QuoteBook) -> PaperExecutionAdapter:
    return PaperExecutionAdapter(
        venue_id="kalshi", quotebook=book, fee_model=KalshiFeeModel()
    )


def test_marks_positions_at_the_mid():
    book = QuoteBook()
    book.upsert(
        Quote(
            outcome_id="k-yes",
            bid=Decimal("0.60"),
            ask=Decimal("0.70"),
        )
    )
    broker = _broker(book)
    broker.deposit("acct", Decimal("100"))
    broker.hydrate_position(
        "acct", "k-yes", qty=Decimal("10"), avg_price=Decimal("0.50"),
        realized_pnl=Decimal("0"),
    )
    eq = account_equity({"kalshi": broker}, book, "acct")
    assert eq.cash == Decimal("100")
    assert eq.position_value == Decimal("6.5")
    assert eq.equity == Decimal("106.5")
    assert eq.unrealized == Decimal("1.5")


def test_falls_back_to_avg_price_without_a_quote():
    """Flat MTM, not zero — a missing quote is unknown, not worthless."""
    book = QuoteBook()
    broker = _broker(book)
    broker.deposit("acct", Decimal("50"))
    broker.hydrate_position(
        "acct", "no-quote", qty=Decimal("4"), avg_price=Decimal("0.25"),
        realized_pnl=Decimal("0"),
    )
    eq = account_equity({"kalshi": broker}, book, "acct")
    assert eq.position_value == Decimal("1.00")
    assert eq.unrealized == Decimal("0")


def test_realized_is_summed_across_venues():
    book = QuoteBook()
    a = PaperExecutionAdapter(
        venue_id="kalshi", quotebook=book, fee_model=KalshiFeeModel()
    )
    b = PaperExecutionAdapter(
        venue_id="polymarket_us", quotebook=book, fee_model=KalshiFeeModel()
    )
    a.hydrate_position(
        "acct", "x", qty=Decimal("0"), avg_price=Decimal("0"),
        realized_pnl=Decimal("3"),
    )
    b.hydrate_position(
        "acct", "y", qty=Decimal("0"), avg_price=Decimal("0"),
        realized_pnl=Decimal("4"),
    )
    eq = account_equity({"kalshi": a, "polymarket_us": b}, book, "acct")
    assert eq.realized == Decimal("7")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/shared/test_equity.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'arbys.shared.equity'`.

- [ ] **Step 3: Implement**

Create `arbys/shared/equity.py`:

```python
"""Mark-to-market for a paper account.

Pure: takes the brokers and the quote book as arguments and performs no I/O,
so it is legal in `shared/`. Both `PnlSnapshotService` and the account summary
endpoint call this — the strip and the equity curve must not disagree.

A position with no live quote marks at its own average price (flat MTM) rather
than zero: a missing quote means unknown, not worthless.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .paper_broker import PaperExecutionAdapter
from .quotebook import QuoteBook
from .types import Quote


@dataclass(frozen=True)
class AccountEquity:
    cash: Decimal
    position_value: Decimal
    equity: Decimal
    unrealized: Decimal
    realized: Decimal


def _mid(q: Quote) -> Decimal:
    return (q.bid + q.ask) / Decimal(2)


def account_equity(
    brokers: dict[str, PaperExecutionAdapter],
    quotebook: QuoteBook,
    account_id: str,
) -> AccountEquity:
    cash = Decimal("0")
    position_value = Decimal("0")
    unrealized = Decimal("0")
    realized = Decimal("0")
    for broker in brokers.values():
        broker_cash, positions = broker.account_snapshot(account_id)
        cash += broker_cash
        realized += broker.realized_pnl(account_id)
        for outcome_id, (qty, avg_price, _realized) in positions.items():
            quote = quotebook.get(outcome_id)
            mark = _mid(quote) if quote is not None else avg_price
            position_value += mark * qty
            unrealized += (mark - avg_price) * qty
    return AccountEquity(
        cash=cash,
        position_value=position_value,
        equity=cash + position_value,
        unrealized=unrealized,
        realized=realized,
    )
```

`realized_pnl(account_id)` reads `self._accounts[account_id]` directly, and
`_accounts` is a `defaultdict`, so an unknown account id returns 0 rather than
raising.

- [ ] **Step 4: Point the snapshotter at it**

In `arbys/ingest/pnl_service.py`, replace the body of `snapshot_once`'s
per-account computation with a call, and delete the module-level `_mid`:

```python
    async def snapshot_once(self) -> None:
        for account_id in self._account_ids:
            eq = account_equity(self._brokers, self._book, account_id)
            try:
                async with session_scope() as session:
                    await repo.insert_paper_pnl_snapshot(
                        session,
                        account_id=account_id,
                        cash=eq.cash,
                        mtm_positions=eq.position_value,
                        total_equity=eq.equity,
                    )
            except Exception:
                log.exception("pnl snapshot write failed for %s", account_id)
```

Add `from ..shared.equity import account_equity` to its imports and drop the
now-unused `Decimal` import if ruff flags it.

- [ ] **Step 5: Run the tests**

Run: `venv\Scripts\python.exe -m pytest tests/shared/test_equity.py tests/test_ingest_wiring.py -q` then `venv\Scripts\python.exe -m ruff check .`
Expected: tests pass, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add arbys/shared/equity.py arbys/ingest/pnl_service.py tests/shared/test_equity.py
git commit -m "refactor(pnl): one mark-to-market computation"
```

---

### Task 8: submit_arb_ticket

**Files:**
- Create: `arbys/backend/ticket_service.py`
- Test: `tests/test_ticket_service.py` (create)

**Interfaces:**
- Consumes: `insert_paper_ticket`, `insert_paper_order` (Task 2); `ExecutionIntent.ticket_id` (Task 4); `InsufficientLegsError.rejections` (Task 5).
- Produces:

```python
@dataclass(frozen=True)
class TicketResult:
    ticket_id: str
    status: str                  # "filled" | "rejected" | "missed"
    order_ids: tuple[str, ...]
    reason: str | None

async def submit_arb_ticket(
    state, opp: ArbOpportunity, *, source: str, account_id: str | None = None
) -> TicketResult
```

This is the module the auto-trader plan depends on. Do not let the position cap
stay in `app.py` — that is the defect this task exists to fix.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ticket_service.py`:

```python
"""submit_arb_ticket is the only way a ticket is submitted.

The position cap used to live in the HTTP endpoint, so any non-HTTP caller
bypassed it silently and stacked without bound. These tests pin it to the
shared path instead.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from arbys.backend import state as state_module
from arbys.backend.state import get_state
from arbys.backend.ticket_service import submit_arb_ticket
from arbys.db import session as db_session
from arbys.db.session import create_all, session_scope
from arbys.db import repositories as repo
from arbys.shared.types import EventGroup, EventGroupLeg, Quote


@pytest.fixture(autouse=True)
async def _fresh_state(tmp_path: Path):
    os.environ["ARBYS_DB_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'tickets.db'}"
    db_session.reset_engine()
    state_module.reset_state()
    await create_all()
    yield
    db_session.reset_engine()
    state_module.reset_state()
    os.environ.pop("ARBYS_DB_URL", None)


async def _arb_group(*, ask_size: Decimal | None = None):
    """An eg-1 group quoted 0.40 / 0.50 — a live 10c gross edge."""
    s = get_state()
    group = EventGroup(
        id="eg-1",
        title="MLB: ATL @ LAD",
        legs=(
            EventGroupLeg(outcome_id="p-yes", venue_id="polymarket_us", is_yes_side=True),
            EventGroupLeg(outcome_id="k-no", venue_id="kalshi", is_yes_side=False),
        ),
    )
    s.event_groups[group.id] = group
    s.engine.register_group(group)
    async with session_scope() as session:
        await repo.ensure_paper_account(session, s.default_account_id)
    for oid, px in (("p-yes", Decimal("0.40")), ("k-no", Decimal("0.50"))):
        s.quotebook.upsert(
            Quote(
                outcome_id=oid,
                bid=px,
                ask=px,
                bid_size=ask_size,
                ask_size=ask_size,
            )
        )
    for broker in s.paper_brokers.values():
        broker.deposit(s.default_account_id, Decimal("10000"))
    return s, group


async def test_filled_ticket_groups_its_legs():
    s, _ = await _arb_group()
    opp = s.engine.evaluate_now("eg-1")[0]
    result = await submit_arb_ticket(s, opp, source="manual")
    assert result.status == "filled"
    assert len(result.order_ids) == 2
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert len(tickets) == 1
    assert tickets[0]["status"] == "filled"
    assert tickets[0]["title_snapshot"] == "MLB: ATL @ LAD"
    assert tickets[0]["source"] == "manual"
    assert len(tickets[0]["legs"]) == 2


async def test_position_cap_is_enforced_here_not_in_the_endpoint(monkeypatch):
    monkeypatch.setenv("ARBYS_MAX_OUTCOME_QTY", "1")
    s, _ = await _arb_group()
    opp = s.engine.evaluate_now("eg-1")[0]
    result = await submit_arb_ticket(s, opp, source="auto")
    assert result.status == "rejected"
    assert result.reason is not None
    assert result.reason.startswith("position_cap:")
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert tickets[0]["status"] == "rejected"


async def test_vanished_edge_writes_a_missed_ticket_and_no_orders():
    s, _ = await _arb_group()
    opp = s.engine.evaluate_now("eg-1")[0]
    # The edge disappears before submission: both sides reprice to 0.60.
    for oid in ("p-yes", "k-no"):
        s.quotebook.upsert(
            Quote(outcome_id=oid, bid=Decimal("0.60"), ask=Decimal("0.60"))
        )
    result = await submit_arb_ticket(s, opp, source="auto")
    assert result.status == "missed"
    assert result.order_ids == ()
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert tickets[0]["status"] == "missed"
    assert tickets[0]["legs"] == []
    assert tickets[0]["total_stake"] is None


async def test_router_rejection_writes_per_leg_order_rows(monkeypatch):
    """A preview rejection never builds an Order, so before this nothing about
    it reached the database at all.

    The router is stubbed rather than provoked with a thin book: sizing is
    depth-aware, so re-quoting one side with `ask_size=1` makes the detector
    publish a qty-1 opportunity that fills perfectly well. No quote reliably
    produces a preview rejection *and* still detects.
    """
    from arbys.shared.execution_router import InsufficientLegsError, LegRejection

    s, _ = await _arb_group()
    opp = s.engine.evaluate_now("eg-1")[0]

    async def _refuse(_intent):
        raise InsufficientLegsError(
            (LegRejection("kalshi", "k-no", "insufficient_liquidity"),)
        )

    monkeypatch.setattr(s.router, "submit", _refuse)

    result = await submit_arb_ticket(s, opp, source="auto")
    assert result.status == "rejected"
    assert result.order_ids == ()
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert tickets[0]["status"] == "rejected"
    reasons = {leg["outcome_id"]: leg["rejection_reason"] for leg in tickets[0]["legs"]}
    assert reasons["k-no"] == "insufficient_liquidity"
    # A leg that previewed fine still gets a row: the ticket failed as a whole
    # and no leg was submitted.
    assert reasons["p-yes"] == "ticket_rejected"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/test_ticket_service.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'arbys.backend.ticket_service'`.

- [ ] **Step 3: Implement**

Create `arbys/backend/ticket_service.py`:

```python
"""Submitting an arb ticket — the one path that writes trade history.

Everything that submits goes through here: the HTTP endpoint and the
auto-trader alike. Three reasons it is a single function rather than logic in
the endpoint:

* The ARBYS_MAX_OUTCOME_QTY check used to live in `app.py`, so any non-HTTP
  caller bypassed it silently and stacked positions without bound.
* A ticket's identity has to be minted before the intent is built, so the
  legs can be grouped.
* Rejected and missed tickets are the most valuable rows in the audit log and
  they have to be written somewhere both callers share.

An attempt is logged only once it reaches this function. "The detector found
nothing" is not an attempt and is never written — otherwise a bot writes
thousands of rows a night saying nothing happened.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from ..adapters.base import ExecutionIntent, IntentLeg
from ..db import repositories as repo
from ..db.session import session_scope
from ..shared.arb_engine import ArbOpportunity
from ..shared.execution_router import InsufficientLegsError
from .state import max_outcome_qty

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .state import AppState

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TicketResult:
    ticket_id: str
    status: str
    order_ids: tuple[str, ...]
    reason: str | None


def _buy_legs(opp: ArbOpportunity) -> frozenset[str]:
    return frozenset(leg.outcome_id for leg in opp.legs if leg.is_buy)


def _match_live(state: AppState, opp: ArbOpportunity) -> ArbOpportunity | None:
    """Re-detect and return the same edge at live prices, or None.

    Replaying a recorded opportunity is what produced `limit_exceeded` once the
    market moved, so the ticket is always priced against the current book.
    """
    wanted = _buy_legs(opp)
    for candidate in state.live_opportunities_for(opp.event_group_id):
        if candidate.event_group_id != opp.event_group_id:
            continue
        if _buy_legs(candidate) == wanted:
            return candidate
    return None


def _title(state: AppState, event_group_id: str) -> str:
    """Freeze the group's title. Discovery deletes groups; history outlives
    them, so the name is snapshotted rather than joined at read time."""
    base_id = event_group_id.split(":", 1)[0]
    group = state.event_groups.get(base_id)
    return group.title if group is not None else event_group_id


async def _write_ticket(
    *, ticket_id: str, account_id: str, opp: ArbOpportunity, title: str,
    source: str, status: str, reason: str | None,
    economics: ArbOpportunity | None,
) -> None:
    """Persist the ticket. Never raises: an unrecorded ticket is acceptable,
    a broken trade is not."""
    try:
        async with session_scope() as session:
            await repo.insert_paper_ticket(
                session,
                ticket_id=ticket_id,
                account_id=account_id,
                event_group_id=opp.event_group_id,
                title_snapshot=title,
                source=source,
                status=status,
                rejection_reason=reason,
                total_stake=None if economics is None else economics.total_stake,
                expected_profit=(
                    None if economics is None else economics.guaranteed_profit
                ),
                expected_edge_bps=(
                    None if economics is None else economics.guaranteed_profit_bps
                ),
            )
    except Exception:
        log.exception("ticket write failed for %s", ticket_id)


async def _write_rejected_legs(
    *, ticket_id: str, account_id: str, live: ArbOpportunity,
    reasons: dict[str, str],
) -> None:
    """One row per attempted leg, so the attempted prices are recorded.

    A leg that previewed fine still gets a row: the ticket failed as a whole
    and no leg was submitted.
    """
    try:
        async with session_scope() as session:
            for leg in live.legs:
                await repo.insert_paper_order(
                    session,
                    order_id=uuid.uuid4().hex,
                    account_id=account_id,
                    venue_id=leg.venue_id,
                    outcome_id=leg.outcome_id,
                    is_buy=leg.is_buy,
                    qty=leg.qty,
                    limit_price=leg.price,
                    status="rejected",
                    rejection_reason=reasons.get(leg.outcome_id, "ticket_rejected"),
                    ticket_id=ticket_id,
                )
    except Exception:
        log.exception("rejected-leg write failed for %s", ticket_id)


def _cap_breach(state: AppState, live: ArbOpportunity, account_id: str) -> str | None:
    """The outcome that would exceed ARBYS_MAX_OUTCOME_QTY, or None."""
    cap = max_outcome_qty()
    if cap is None:
        return None
    for leg in live.legs:
        if not leg.is_buy:
            continue
        broker = state.paper_brokers.get(leg.venue_id)
        if broker is None:
            continue
        _cash, positions = broker.account_snapshot(account_id)
        held = positions.get(leg.outcome_id, (Decimal("0"),))[0]
        if held + leg.qty > cap:
            return (
                f"position_cap:{leg.outcome_id} holding {held} "
                f"adds {leg.qty} cap {cap}"
            )
    return None


async def submit_arb_ticket(
    state: AppState,
    opp: ArbOpportunity,
    *,
    source: str,
    account_id: str | None = None,
) -> TicketResult:
    account_id = account_id or state.default_account_id
    ticket_id = uuid.uuid4().hex
    title = _title(state, opp.event_group_id)

    live = _match_live(state, opp)
    if live is None:
        reason = "edge_no_longer_available"
        await _write_ticket(
            ticket_id=ticket_id, account_id=account_id, opp=opp, title=title,
            source=source, status="missed", reason=reason, economics=None,
        )
        return TicketResult(ticket_id, "missed", (), reason)

    breach = _cap_breach(state, live, account_id)
    if breach is not None:
        await _write_ticket(
            ticket_id=ticket_id, account_id=account_id, opp=live, title=title,
            source=source, status="rejected", reason=breach, economics=live,
        )
        return TicketResult(ticket_id, "rejected", (), breach)

    intent = ExecutionIntent(
        event_group_id=live.event_group_id,
        account_id=account_id,
        ticket_id=ticket_id,
        legs=tuple(
            IntentLeg(
                venue_id=leg.venue_id,
                outcome_id=leg.outcome_id,
                is_buy=leg.is_buy,
                qty=leg.qty,
                limit_price=leg.price,
            )
            for leg in live.legs
        ),
    )
    # The ticket row must exist before the router runs: paper_order.ticket_id
    # is an FK to it, and the sink writes order rows from inside submit().
    await _write_ticket(
        ticket_id=ticket_id, account_id=account_id, opp=live, title=title,
        source=source, status="pending", reason=None, economics=live,
    )

    try:
        orders = await state.router.submit(intent)
    except InsufficientLegsError as e:
        reason = str(e)
        await _set_status(ticket_id, status="rejected", reason=reason)
        await _write_rejected_legs(
            ticket_id=ticket_id,
            account_id=account_id,
            live=live,
            reasons={r.outcome_id: r.reason for r in e.rejections},
        )
        return TicketResult(ticket_id, "rejected", (), reason)

    await _set_status(ticket_id, status="filled", reason=None)
    return TicketResult(
        ticket_id, "filled", tuple(o.id for o in orders), None
    )
```

**Why the ticket row is inserted before the router runs.**
`paper_order.ticket_id` is a foreign key to `paper_ticket.id`, and the
persistence sink writes order rows from inside `router.submit` (during
`emit_order_events`). The ticket must already exist, so it goes in as
`status="pending"` and is updated to `filled` or `rejected` afterwards. SQLite
does not enforce foreign keys by default and this would appear to work either
way — which is exactly why it is worth pinning down now rather than
discovering it on Postgres. A row left at `pending` means the process died
mid-ticket, which is worth being able to see.

The `missed` and `position_cap` paths never reach the router, so they insert
their final status directly and never pass through `pending`.

- [ ] **Step 4: Add the status helpers**

In `arbys/db/repositories.py`, and add the name to `__all__`:

```python
async def update_paper_ticket_status(
    session: AsyncSession, ticket_id: str, *, status: str,
    rejection_reason: str | None = None,
) -> None:
    row = await session.get(m.PaperTicket, ticket_id)
    if row is None:
        return
    row.status = status
    row.rejection_reason = rejection_reason
```

and in `ticket_service.py`, alongside `_write_ticket`:

```python
async def _set_status(ticket_id: str, *, status: str, reason: str | None) -> None:
    """Move a pending ticket to its final status. Never raises: an unrecorded
    ticket is acceptable, a broken trade is not."""
    try:
        async with session_scope() as session:
            await repo.update_paper_ticket_status(
                session, ticket_id, status=status, rejection_reason=reason
            )
    except Exception:
        log.exception("ticket status update failed for %s", ticket_id)
```

- [ ] **Step 5: Run the tests**

Run: `venv\Scripts\python.exe -m pytest tests/test_ticket_service.py -q`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add arbys/backend/ticket_service.py arbys/db/repositories.py tests/test_ticket_service.py
git commit -m "feat(paper): submit_arb_ticket owns the cap and the audit trail"
```

---

### Task 9: Rewire POST /paper/execute

**Files:**
- Modify: `arbys/backend/app.py:397-473`
- Test: `tests/test_backend_e2e.py` (existing tests must pass unchanged)

**Interfaces:**
- Consumes: `submit_arb_ticket`, `TicketResult` (Task 8).
- Produces: no API shape change. Still `list[str]` of order ids on success, still 409 on a lost edge or a cap breach, still 404 for an out-of-range `opportunity_index`.

The existing tests `test_repeat_fills_stop_at_the_position_cap`,
`test_position_cap_can_be_disabled`, `test_execute_by_event_group_rejects_unknown_descriptor`,
and `test_execute_prices_against_live_quotes_not_the_recorded_opportunity`
are the contract. Do not edit them.

- [ ] **Step 1: Replace the handler body**

```python
    @app.post("/paper/execute", response_model=list[str])
    async def paper_execute(body: ExecuteArbIn) -> list[str]:
        s = get_state()
        if body.event_group_id is not None:
            # Pick the published opportunity the caller is describing. The
            # re-detect against live quotes happens inside submit_arb_ticket.
            wanted = set(body.outcome_ids) if body.outcome_ids else None
            opp = None
            for candidate in s.live_opportunities_for(body.event_group_id):
                if candidate.event_group_id != body.event_group_id:
                    continue
                if wanted is not None:
                    buy_legs = {leg.outcome_id for leg in candidate.legs if leg.is_buy}
                    if buy_legs != wanted:
                        continue
                opp = candidate
                break
            if opp is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "edge no longer available at live quotes for "
                        f"event_group_id={body.event_group_id!r}"
                    ),
                )
        else:
            opportunities = list(s.opportunities)
            if body.opportunity_index < 0 or body.opportunity_index >= len(opportunities):
                raise HTTPException(status_code=404, detail="opportunity_index out of range")
            opp = opportunities[body.opportunity_index]

        result = await submit_arb_ticket(
            s, opp, source="manual", account_id=body.account_id
        )
        if result.status != "filled":
            raise HTTPException(status_code=409, detail=result.reason or result.status)
        return list(result.order_ids)
```

Replace the `state` import line to add the service, and drop the imports the
handler no longer uses:

```python
from .ticket_service import submit_arb_ticket  # noqa: E402
```

`ExecutionIntent`, `IntentLeg`, `InsufficientLegsError`, and `max_outcome_qty`
are now unused in `app.py` if nothing else references them — run ruff and
remove whatever it flags.

- [ ] **Step 2: Run the endpoint tests**

Run: `venv\Scripts\python.exe -m pytest tests/test_backend_e2e.py -q`
Expected: all pass. If the cap test fails with a 200, the cap check is not
running — confirm `submit_arb_ticket` calls `_cap_breach` before building the
intent.

- [ ] **Step 3: Run the full suite and ruff**

Run: `venv\Scripts\python.exe -m pytest -q` then `venv\Scripts\python.exe -m ruff check .`
Expected: all green, ruff clean.

- [ ] **Step 4: Commit**

```bash
git add arbys/backend/app.py
git commit -m "refactor(api): /paper/execute goes through submit_arb_ticket"
```

---

### Task 10: Enriched summary, tickets, positions, orders

**Files:**
- Modify: `arbys/backend/schemas.py`, `arbys/backend/app.py:361-395`, `arbys/db/repositories.py`
- Test: `tests/test_backend_e2e.py` (append)

**Interfaces:**
- Consumes: `account_equity` (Task 7), `list_paper_tickets` (Task 3).
- Produces: `PaperAccountSummary` with `cash`, `position_value`, `equity`, `unrealized_pnl`, `open_ticket_count`; `GET /paper/{id}/tickets`; `GET /paper/{id}/positions`; `list_paper_positions(session, account_id)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_backend_e2e.py`:

```python
def test_summary_reports_live_equity_without_waiting_for_a_snapshot():
    """PnlSnapshotService writes every 30s; the strip cannot wait for it, and
    after a restart there is no snapshot at all."""
    with TestClient(create_app()) as client:
        _register(client, "eg-eq", "p-yes", "k-no")
        client.post("/quotes", json={"outcome_id": "p-yes", "bid": "0.40", "ask": "0.40"})
        client.post("/quotes", json={"outcome_id": "k-no", "bid": "0.50", "ask": "0.50"})
        assert client.post(
            "/paper/execute",
            json={"event_group_id": "eg-eq", "outcome_ids": ["p-yes", "k-no"]},
        ).status_code == 200

        body = client.get("/paper/default").json()
        assert Decimal(body["position_value"]) > 0
        assert Decimal(body["equity"]) == Decimal(body["cash"]) + Decimal(
            body["position_value"]
        )
        assert "unrealized_pnl" in body
        assert body["open_ticket_count"] == 0


def test_tickets_endpoint_groups_legs_and_names_the_event():
    with TestClient(create_app()) as client:
        _register(client, "eg-tk", "p-yes", "k-no")
        client.post("/quotes", json={"outcome_id": "p-yes", "bid": "0.40", "ask": "0.40"})
        client.post("/quotes", json={"outcome_id": "k-no", "bid": "0.50", "ask": "0.50"})
        client.post(
            "/paper/execute",
            json={"event_group_id": "eg-tk", "outcome_ids": ["p-yes", "k-no"]},
        )
        tickets = client.get("/paper/default/tickets").json()
        assert len(tickets) == 1
        assert tickets[0]["status"] == "filled"
        assert tickets[0]["source"] == "manual"
        assert len(tickets[0]["legs"]) == 2
        assert tickets[0]["title_snapshot"]
        assert tickets[0]["legs"][0]["fill_price"] is not None


def test_positions_endpoint_returns_readable_titles():
    with TestClient(create_app()) as client:
        _register(client, "eg-pos", "p-yes", "k-no")
        client.post("/quotes", json={"outcome_id": "p-yes", "bid": "0.40", "ask": "0.40"})
        client.post("/quotes", json={"outcome_id": "k-no", "bid": "0.50", "ask": "0.50"})
        client.post(
            "/paper/execute",
            json={"event_group_id": "eg-pos", "outcome_ids": ["p-yes", "k-no"]},
        )
        positions = client.get("/paper/default/positions").json()
        assert len(positions) == 2
        assert all(p["title"] for p in positions)
        assert all(p["mark"] is not None for p in positions)
```

`_register` already exists at `tests/test_backend_e2e.py:138`; check its
signature before use and match it.

- [ ] **Step 2: Run it to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/test_backend_e2e.py -q -k "summary_reports or tickets_endpoint or positions_endpoint"`
Expected: FAIL — `KeyError: 'position_value'` and 404s for the new routes.

- [ ] **Step 3: Add the schemas**

In `arbys/backend/schemas.py`:

```python
class PaperAccountSummary(BaseModel):
    account_id: str
    balances: dict[str, Decimal]
    positions: dict[str, Decimal]
    realized_pnl: dict[str, Decimal]
    # Live mark-to-market, computed on request rather than read from the
    # 30-second PnL snapshot — which does not exist at all after a restart.
    cash: Decimal
    position_value: Decimal
    equity: Decimal
    unrealized_pnl: Decimal
    open_ticket_count: int


class TicketLegOut(BaseModel):
    venue_id: str
    outcome_id: str
    is_buy: bool
    qty: Decimal
    limit_price: Decimal
    fill_price: Decimal | None
    fee: Decimal
    status: str
    rejection_reason: str | None


class TicketOut(BaseModel):
    id: str
    event_group_id: str
    title_snapshot: str
    source: str
    status: str
    rejection_reason: str | None
    total_stake: Decimal | None
    expected_profit: Decimal | None
    expected_edge_bps: Decimal | None
    submitted_at: datetime
    realized_profit: Decimal | None
    legs: list[TicketLegOut]


class PositionOut(BaseModel):
    venue_id: str
    outcome_id: str
    title: str
    qty: Decimal
    avg_price: Decimal
    mark: Decimal | None
    unrealized: Decimal
```

- [ ] **Step 4: Add the position read**

In `arbys/db/repositories.py`:

```python
async def paper_position_titles(
    session: AsyncSession, account_id: str
) -> dict[str, str]:
    """outcome_id -> best available human title.

    Prefers the most recent ticket that traded the outcome, because its
    `title_snapshot` survives group retirement. Falls back to the live
    event_group join for outcomes only ever quoted, never traded.
    """
    rows = (
        await session.execute(
            select(m.PaperOrder.outcome_id, m.PaperTicket.title_snapshot)
            .join(m.PaperTicket, m.PaperTicket.id == m.PaperOrder.ticket_id)
            .where(m.PaperOrder.account_id == account_id)
            .order_by(m.PaperTicket.submitted_at.desc())
        )
    ).all()
    titles: dict[str, str] = {}
    for outcome_id, title in rows:
        titles.setdefault(outcome_id, title)

    live = (
        await session.execute(
            select(m.EventGroupLeg.outcome_id, m.EventGroup.title).join(
                m.EventGroup, m.EventGroup.id == m.EventGroupLeg.event_group_id
            )
        )
    ).all()
    for outcome_id, title in live:
        titles.setdefault(outcome_id, title)
    return titles
```

Add `"paper_position_titles"` and `"update_paper_ticket_status"` to `__all__`.

- [ ] **Step 5: Wire the endpoints**

In `arbys/backend/app.py`, rewrite `paper_summary` and add the two routes:

```python
    @app.get("/paper/{account_id}", response_model=PaperAccountSummary)
    async def paper_summary(account_id: str) -> PaperAccountSummary:
        s = get_state()
        balances: dict[str, Decimal] = {}
        positions: dict[str, Decimal] = {}
        realized: dict[str, Decimal] = {}
        for venue_id, broker in s.paper_brokers.items():
            bals = await broker.get_balances(account_id)
            balances[venue_id] = bals.get(venue_id, Decimal("0"))
            for oid, qty in (await broker.get_positions(account_id)).items():
                if qty != 0:
                    positions[oid] = positions.get(oid, Decimal("0")) + qty
            realized[venue_id] = broker.realized_pnl(account_id)
        eq = account_equity(s.paper_brokers, s.quotebook, account_id)
        async with session_scope() as session:
            open_tickets = [
                t
                for t in await repo.list_paper_tickets(session, account_id)
                if t["status"] == "filled" and t["realized_profit"] is None
            ]
        return PaperAccountSummary(
            account_id=account_id,
            balances=balances,
            positions=positions,
            realized_pnl=realized,
            cash=eq.cash,
            position_value=eq.position_value,
            equity=eq.equity,
            unrealized_pnl=eq.unrealized,
            open_ticket_count=len(open_tickets),
        )

    @app.get("/paper/{account_id}/tickets", response_model=list[TicketOut])
    async def paper_tickets(
        account_id: str,
        limit: int = 200,
        status: str | None = None,
        source: str | None = None,
    ) -> list[dict]:
        async with session_scope() as session:
            return await repo.list_paper_tickets(
                session, account_id, limit=limit, status=status, source=source
            )

    @app.get("/paper/{account_id}/positions", response_model=list[PositionOut])
    async def paper_positions(account_id: str) -> list[PositionOut]:
        s = get_state()
        async with session_scope() as session:
            titles = await repo.paper_position_titles(session, account_id)
        out: list[PositionOut] = []
        for venue_id, broker in s.paper_brokers.items():
            _cash, held = broker.account_snapshot(account_id)
            for outcome_id, (qty, avg_price, _realized) in held.items():
                quote = s.quotebook.get(outcome_id)
                mark = (quote.bid + quote.ask) / Decimal(2) if quote is not None else None
                effective = avg_price if mark is None else mark
                out.append(
                    PositionOut(
                        venue_id=venue_id,
                        outcome_id=outcome_id,
                        title=titles.get(outcome_id, outcome_id),
                        qty=qty,
                        avg_price=avg_price,
                        mark=mark,
                        unrealized=(effective - avg_price) * qty,
                    )
                )
        return out
```

Add `account_equity` and the new schema names to the imports at the top of
`app.py`.

- [ ] **Step 6: Run the tests**

Run: `venv\Scripts\python.exe -m pytest tests/test_backend_e2e.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add arbys/backend/app.py arbys/backend/schemas.py arbys/db/repositories.py tests/test_backend_e2e.py
git commit -m "feat(api): ticket history, positions, and live equity in the summary"
```

---

### Task 11: Frontend types and client

**Files:**
- Modify: `frontend/src/api/types.ts`, `frontend/src/api/client.ts`

**Interfaces:**
- Consumes: the endpoints from Task 10.
- Produces: `Ticket`, `TicketLeg`, `PaperPosition` types; `PaperAccountSummary` extended; `api.paperTickets`, `api.paperPositions`.

Money crosses the wire as a **string** — every existing money field in
`types.ts` is `string`, because these are `Decimal` server-side. Keep that.

- [ ] **Step 1: Extend the types**

In `frontend/src/api/types.ts`, extend the summary and add the new shapes:

```typescript
export interface PaperAccountSummary {
  account_id: string;
  balances: Record<string, string>;
  positions: Record<string, string>;
  realized_pnl: Record<string, string>;
  /** Live mark-to-market, not the 30s PnL snapshot. */
  cash: string;
  position_value: string;
  equity: string;
  unrealized_pnl: string;
  /** Filled tickets with at least one leg still unsettled. */
  open_ticket_count: number;
}

export interface TicketLeg {
  venue_id: string;
  outcome_id: string;
  is_buy: boolean;
  qty: string;
  limit_price: string;
  /** Null for a leg that never filled. */
  fill_price: string | null;
  fee: string;
  status: string;
  rejection_reason: string | null;
}

export interface Ticket {
  id: string;
  event_group_id: string;
  /** Frozen at submit time — event groups get retired and deleted. */
  title_snapshot: string;
  source: "manual" | "auto";
  status: "filled" | "rejected" | "missed" | "pending";
  rejection_reason: string | null;
  /** Null on a missed ticket: there were no economics to record. */
  total_stake: string | null;
  expected_profit: string | null;
  expected_edge_bps: string | null;
  submitted_at: string;
  /** Null while any leg is unsettled. */
  realized_profit: string | null;
  legs: TicketLeg[];
}

export interface PaperPosition {
  venue_id: string;
  outcome_id: string;
  title: string;
  qty: string;
  avg_price: string;
  mark: string | null;
  unrealized: string;
}
```

- [ ] **Step 2: Add the client calls**

In `frontend/src/api/client.ts`, extend the import list with `Ticket` and
`PaperPosition`, and add:

```typescript
  paperTickets: (
    account_id: string,
    opts: { limit?: number; status?: string; source?: string } = {},
  ) => {
    const q = new URLSearchParams();
    q.set("limit", String(opts.limit ?? 200));
    if (opts.status) q.set("status", opts.status);
    if (opts.source) q.set("source", opts.source);
    return req<Ticket[]>(`/paper/${account_id}/tickets?${q.toString()}`);
  },
  paperPositions: (account_id: string) =>
    req<PaperPosition[]>(`/paper/${account_id}/positions`),
```

- [ ] **Step 3: Typecheck**

Run: `cd frontend; npm run build`
Expected: build succeeds.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat(frontend): ticket and position API types"
```

---

### Task 12: AccountStrip replaces the sidebar

**Files:**
- Create: `frontend/src/components/AccountStrip.tsx`
- Delete: `frontend/src/components/AccountPanel.tsx`
- Modify: `frontend/src/pages/TerminalPage.tsx:8` (import), `:130-176` (grid + nav)

**Interfaces:**
- Consumes: `api.paperSummary`, `PaperAccountSummary` (Task 11).
- Produces: `<AccountStrip />`, default export absent — named export, matching every other component in the tree.

The strip sits **below** the nav, not inside it: the nav already carries the
brand, the live-count pulse, an Admin tag, and two venue tags, and six more
figures would crowd it.

- [ ] **Step 1: Write the component**

Create `frontend/src/components/AccountStrip.tsx`:

```tsx
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { PaperAccountSummary } from "../api/types";

const ACCOUNT = "default";

function money(n: number, opts: { sign?: boolean } = {}): string {
  const s = Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const sign = opts.sign ? (n >= 0 ? "+" : "-") : n < 0 ? "-" : "";
  return `${sign}$${s}`;
}

function Cell({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "pos" | "neg";
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <span
        style={{
          fontSize: 10,
          letterSpacing: "0.08em",
          textTransform: "uppercase",
          opacity: 0.55,
        }}
      >
        {label}
      </span>
      <span
        className="vt-mono"
        style={{
          fontSize: 15,
          fontWeight: 600,
          color:
            tone === "pos"
              ? "var(--vt-green-dark)"
              : tone === "neg"
                ? "#a1263c"
                : "var(--color-text)",
        }}
      >
        {value}
      </span>
    </div>
  );
}

/**
 * The account summary that replaces the old right-hand sidebar. Reused as the
 * header of /account so the two views cannot drift apart.
 *
 * Figures come from the live summary endpoint, not from pnl_snapshots: those
 * are written every 30s and do not exist at all until the first one lands
 * after a restart.
 */
export function AccountStrip() {
  const summary = useQuery<PaperAccountSummary>({
    queryKey: ["paper", "summary", ACCOUNT],
    queryFn: () => api.paperSummary(ACCOUNT),
    refetchInterval: 5_000,
  });

  const cash = Number(summary.data?.cash ?? 0);
  const positionValue = Number(summary.data?.position_value ?? 0);
  const equity = Number(summary.data?.equity ?? 0);
  const unrealized = Number(summary.data?.unrealized_pnl ?? 0);
  const realized = Object.values(summary.data?.realized_pnl ?? {}).reduce(
    (s, v) => s + Number(v),
    0,
  );
  const openTickets = summary.data?.open_ticket_count ?? 0;

  const tone = (n: number) => (n > 0 ? "pos" : n < 0 ? "neg" : undefined);

  return (
    <div
      style={{
        display: "flex",
        gap: "var(--space-5)",
        alignItems: "center",
        padding: "var(--space-2) var(--space-4)",
        borderBottom: "1px solid var(--color-divider)",
        flex: "none",
        flexWrap: "wrap",
      }}
    >
      <Cell label="Equity" value={money(equity)} />
      <Cell label="Cash" value={money(cash)} />
      <Cell label="Position value" value={money(positionValue)} />
      <Cell
        label="Unrealized"
        value={money(unrealized, { sign: true })}
        tone={tone(unrealized)}
      />
      <Cell
        label="Realized"
        value={money(realized, { sign: true })}
        tone={tone(realized)}
      />
      <Cell label="Open tickets" value={String(openTickets)} />
    </div>
  );
}
```

- [ ] **Step 2: Rewire TerminalPage**

- Replace the `AccountPanel` import with `AccountStrip`.
- Insert `<AccountStrip />` between the closing `</nav>` and the grid `<div>`.
- Change the grid to two columns:

```tsx
          gridTemplateColumns: "minmax(160px, 190px) minmax(0, 1fr)",
```

- Delete the `<AccountPanel />` element at the end of the grid.
- Add an Account link next to the existing Admin one:

```tsx
        <a href="/account" className="tag tag-outline" style={{ textDecoration: "none" }}>
          Account
        </a>
```

- [ ] **Step 3: Delete the old panel**

```bash
git rm frontend/src/components/AccountPanel.tsx
```

- [ ] **Step 4: Typecheck and lint**

Run: `cd frontend; npm run build; npm run lint`
Expected: both clean. A build error naming `AccountPanel` means a stale import
remains.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/AccountStrip.tsx frontend/src/pages/TerminalPage.tsx
git commit -m "feat(frontend): full-width account strip replaces the sidebar"
```

---

### Task 13: The ticket history table

**Files:**
- Create: `frontend/src/components/TicketHistory.tsx`

**Interfaces:**
- Consumes: `api.paperTickets`, `Ticket`, `TicketLeg` (Task 11).
- Produces: `<TicketHistory />`.

- [ ] **Step 1: Write the component**

Create `frontend/src/components/TicketHistory.tsx`:

```tsx
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Ticket, TicketLeg } from "../api/types";

const ACCOUNT = "default";

const STATUSES = ["all", "filled", "rejected", "missed"] as const;
const SOURCES = ["all", "manual", "auto"] as const;

function money(v: string | null, opts: { sign?: boolean } = {}): string {
  if (v === null) return "—";
  const n = Number(v);
  const s = Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const sign = opts.sign ? (n >= 0 ? "+" : "-") : n < 0 ? "-" : "";
  return `${sign}$${s}`;
}

function cents(v: string | null): string {
  return v === null ? "—" : `${(Number(v) * 100).toFixed(1)}¢`;
}

function LegLine({ leg }: { leg: TicketLeg }) {
  return (
    <div style={{ fontSize: 11, opacity: 0.85, whiteSpace: "nowrap" }}>
      <span style={{ textTransform: "capitalize" }}>
        {leg.venue_id.replace(/_/g, " ")}
      </span>{" "}
      {leg.is_buy ? "BUY" : "SELL"} {Number(leg.qty).toFixed(2)} @{" "}
      {cents(leg.fill_price ?? leg.limit_price)}
      {leg.fill_price === null ? " (unfilled)" : ""}
      {Number(leg.fee) > 0 ? ` · fee ${money(leg.fee)}` : ""}
    </div>
  );
}

/**
 * The audit log. One row per ticket, both legs together.
 *
 * Rejected and missed rows are shown, not hidden: "the bot attempted 400
 * tickets and filled 3" is the most useful thing this table can say, and a
 * missed ticket is how often an edge vanished between detection and
 * submission.
 */
export function TicketHistory() {
  const [status, setStatus] = useState<(typeof STATUSES)[number]>("all");
  const [source, setSource] = useState<(typeof SOURCES)[number]>("all");

  const tickets = useQuery<Ticket[]>({
    queryKey: ["paper", "tickets", ACCOUNT, status, source],
    queryFn: () =>
      api.paperTickets(ACCOUNT, {
        status: status === "all" ? undefined : status,
        source: source === "all" ? undefined : source,
      }),
    refetchInterval: 10_000,
  });

  const rows = tickets.data ?? [];

  return (
    <section style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
      <div style={{ display: "flex", gap: "var(--space-3)", alignItems: "center" }}>
        <h2 style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: 16 }}>
          Ticket history
        </h2>
        <span style={{ flex: 1 }} />
        {STATUSES.map((s) => (
          <button
            key={s}
            className={`btn ${status === s ? "btn-primary" : ""}`}
            style={{ fontSize: 11, padding: "2px 8px", textTransform: "capitalize" }}
            onClick={() => setStatus(s)}
          >
            {s}
          </button>
        ))}
        {SOURCES.map((s) => (
          <button
            key={s}
            className={`btn ${source === s ? "btn-primary" : ""}`}
            style={{ fontSize: 11, padding: "2px 8px", textTransform: "capitalize" }}
            onClick={() => setSource(s)}
          >
            {s}
          </button>
        ))}
      </div>

      <table className="table" style={{ fontSize: 12 }}>
        <thead>
          <tr>
            <th>Time</th>
            <th>Event</th>
            <th>Src</th>
            <th>Legs</th>
            <th>Stake</th>
            <th>Expected</th>
            <th>Realized</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={8} style={{ opacity: 0.5 }}>
                No tickets yet. An empty log is the expected state — measured
                2026-08-22, 0 of 245 groups had a net-positive pair.
              </td>
            </tr>
          ) : null}
          {rows.map((t) => {
            const dim = t.status !== "filled";
            return (
              <tr key={t.id} style={{ opacity: dim ? 0.55 : 1 }}>
                <td className="vt-mono" style={{ whiteSpace: "nowrap" }}>
                  {new Date(t.submitted_at).toLocaleTimeString("en-US", {
                    hour12: false,
                  })}
                </td>
                <td title={t.event_group_id}>{t.title_snapshot}</td>
                <td>
                  <span className={`tag ${t.source === "auto" ? "tag-accent" : "tag-outline"}`}>
                    {t.source}
                  </span>
                </td>
                <td>
                  {t.legs.length === 0 ? (
                    <span style={{ opacity: 0.6, fontSize: 11 }}>none submitted</span>
                  ) : (
                    t.legs.map((leg) => (
                      <LegLine key={`${leg.venue_id}:${leg.outcome_id}`} leg={leg} />
                    ))
                  )}
                </td>
                <td className="vt-mono">{money(t.total_stake)}</td>
                <td className="vt-mono">{money(t.expected_profit, { sign: true })}</td>
                <td
                  className="vt-mono"
                  style={{
                    color:
                      t.realized_profit === null
                        ? undefined
                        : Number(t.realized_profit) >= 0
                          ? "var(--vt-green-dark)"
                          : "#a1263c",
                  }}
                >
                  {t.realized_profit === null ? "open" : money(t.realized_profit, { sign: true })}
                </td>
                <td>
                  <span className="tag" title={t.rejection_reason ?? undefined}>
                    {t.status}
                  </span>
                  {t.rejection_reason ? (
                    <div style={{ fontSize: 10, opacity: 0.7, maxWidth: 220 }}>
                      {t.rejection_reason}
                    </div>
                  ) : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
```

- [ ] **Step 2: Typecheck and lint**

Run: `cd frontend; npm run build; npm run lint`
Expected: both clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/TicketHistory.tsx
git commit -m "feat(frontend): ticket history table"
```

---

### Task 14: The /account page

**Files:**
- Create: `frontend/src/pages/AccountPage.tsx`
- Modify: `frontend/src/main.tsx`

**Interfaces:**
- Consumes: `AccountStrip` (Task 12), `TicketHistory` (Task 13), `api.paperPositions`, `api.paperPnl`.
- Produces: the `/account` route.

Section order follows the audit priority: strip, tickets, positions, equity
curve last and smallest.

- [ ] **Step 1: Write the page**

Create `frontend/src/pages/AccountPage.tsx`:

```tsx
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { PaperPosition, PnlSnapshot } from "../api/types";
import { AccountStrip } from "../components/AccountStrip";
import { TicketHistory } from "../components/TicketHistory";

const ACCOUNT = "default";

function money(v: string, opts: { sign?: boolean } = {}): string {
  const n = Number(v);
  const s = Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const sign = opts.sign ? (n >= 0 ? "+" : "-") : n < 0 ? "-" : "";
  return `${sign}$${s}`;
}

function EquityCurve({ points }: { points: PnlSnapshot[] }) {
  if (points.length < 2) {
    return (
      <div style={{ opacity: 0.5, fontSize: 12 }}>
        Not enough snapshots yet — one is written every 30 seconds.
      </div>
    );
  }
  const values = points.map((p) => Number(p.total_equity));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const path = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * 100;
      const y = 100 - ((v - min) / span) * 100;
      return `${i === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
  return (
    <svg
      viewBox="0 0 100 100"
      preserveAspectRatio="none"
      style={{ width: "100%", height: 120, display: "block" }}
      role="img"
      aria-label="Equity over time"
    >
      <path
        d={path}
        fill="none"
        stroke="var(--color-accent)"
        strokeWidth={1}
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

export function AccountPage() {
  const positions = useQuery<PaperPosition[]>({
    queryKey: ["paper", "positions", ACCOUNT],
    queryFn: () => api.paperPositions(ACCOUNT),
    refetchInterval: 10_000,
  });
  const pnl = useQuery<PnlSnapshot[]>({
    queryKey: ["paper", "pnl", ACCOUNT, "account-page"],
    queryFn: () => api.paperPnl(ACCOUNT, 200),
    refetchInterval: 30_000,
  });

  const open = (positions.data ?? []).filter((p) => Number(p.qty) !== 0);

  return (
    <div style={{ minHeight: "100vh", display: "flex", flexDirection: "column" }}>
      <nav className="nav" style={{ borderBottom: "1px solid var(--color-divider)" }}>
        <span className="nav-brand">Vantage</span>
        <span style={{ flex: 1 }} />
        <a href="/" className="tag tag-outline" style={{ textDecoration: "none" }}>
          Terminal
        </a>
        <a href="/admin" className="tag tag-outline" style={{ textDecoration: "none" }}>
          Admin
        </a>
      </nav>

      <AccountStrip />

      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-5)",
          padding: "var(--space-4)",
        }}
      >
        <TicketHistory />

        <section style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
          <h2 style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: 16 }}>
            Open positions
          </h2>
          <table className="table" style={{ fontSize: 12 }}>
            <thead>
              <tr>
                <th>Event</th>
                <th>Venue</th>
                <th>Qty</th>
                <th>Avg</th>
                <th>Mark</th>
                <th>Unrealized</th>
              </tr>
            </thead>
            <tbody>
              {open.length === 0 ? (
                <tr>
                  <td colSpan={6} style={{ opacity: 0.5 }}>
                    No open positions.
                  </td>
                </tr>
              ) : null}
              {open.map((p) => (
                <tr key={`${p.venue_id}:${p.outcome_id}`}>
                  <td title={p.outcome_id}>{p.title}</td>
                  <td style={{ textTransform: "capitalize" }}>
                    {p.venue_id.replace(/_/g, " ")}
                  </td>
                  <td className="vt-mono">{Number(p.qty).toFixed(2)}</td>
                  <td className="vt-mono">{(Number(p.avg_price) * 100).toFixed(1)}¢</td>
                  <td className="vt-mono">
                    {p.mark === null ? "—" : `${(Number(p.mark) * 100).toFixed(1)}¢`}
                  </td>
                  <td
                    className="vt-mono"
                    style={{
                      color:
                        Number(p.unrealized) >= 0 ? "var(--vt-green-dark)" : "#a1263c",
                    }}
                  >
                    {money(p.unrealized, { sign: true })}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
          <h2 style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: 16 }}>
            Equity
          </h2>
          <EquityCurve points={[...(pnl.data ?? [])].reverse()} />
        </section>
      </div>
    </div>
  );
}
```

`paperPnl` returns newest-first (`ts DESC`), hence the `reverse()`.

- [ ] **Step 2: Register the route**

In `frontend/src/main.tsx`, add the import and the route:

```tsx
import { AccountPage } from "./pages/AccountPage";
```

```tsx
          <Route path="/account" element={<AccountPage />} />
```

- [ ] **Step 3: Typecheck and lint**

Run: `cd frontend; npm run build; npm run lint`
Expected: both clean.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/AccountPage.tsx frontend/src/main.tsx
git commit -m "feat(frontend): /account page"
```

---

### Task 15: Verify against the running app, then document

**Files:**
- Modify: `CLAUDE.md`, `docs/RUNBOOK.md`

**Interfaces:**
- Consumes: everything above.
- Produces: no code.

- [ ] **Step 1: Run the full backend bar**

Run, from the repo root:

```
venv\Scripts\python.exe -m pytest -q
venv\Scripts\python.exe -m ruff check .
```

Expected: all green, ruff clean. Report the actual test count — it should
exceed the 235 baseline by the tests this plan added.

- [ ] **Step 2: Replay the migration chain from empty**

Run: `venv\Scripts\python.exe -m pytest tests/db -q`
Expected: PASS. This is the only thing that proves `0006` is right; dev builds
its schema with `create_all()` and never runs a migration.

- [ ] **Step 3: Exercise the real app**

Start the backend from the **repo root** (`ARBYS_DB_URL` defaults to a relative
`./arbys-local.db`, so starting elsewhere silently creates a second empty
database):

```
venv\Scripts\python.exe -m uvicorn arbys.backend.app:app --reload
```

In another shell, `cd frontend; npm run dev`, then open http://127.0.0.1:5173.
With `ARBYS_ENABLE_INGEST=0` no venue is contacted, so register a group and
push quotes through `/admin` or `POST /quotes` to produce a fillable edge.
Confirm: the opportunity table is full-width with no sidebar, the strip shows
six figures, `/account` lists the ticket with both legs and real fill prices,
and a deliberately-capped fill (`ARBYS_MAX_OUTCOME_QTY=1`) shows as a rejected
row with its reason.

- [ ] **Step 4: Update CLAUDE.md**

Add to the **Architecture** section, under `arbys/backend/`:

```markdown
  `ticket_service.py` is the **only** way an arb ticket is submitted — both
  `POST /paper/execute` and (later) the auto-trader call `submit_arb_ticket`.
  It mints the ticket id, enforces `ARBYS_MAX_OUTCOME_QTY`, and writes the
  `paper_ticket` row. **The cap used to live in the endpoint**, so any
  non-HTTP caller bypassed it silently and stacked without bound; keep it in
  the service.
```

Add a new section after **Only-tradeable invariants**:

```markdown
## Trade history is ticket-level

`paper_ticket` gives an arb ticket a durable identity and `paper_order.ticket_id`
groups its legs. Three things about it are deliberate:

- **`event_group_id` is not a foreign key, and `title_snapshot` is frozen at
  submit time.** Discovery retires groups routinely and `delete_event_group`
  takes the legs with it, so a live join to `event_group.title` blanks the name
  of every finished game — exactly the rows worth auditing.
- **Rejected and missed tickets are recorded, not just fills.** A preview
  rejection never builds an `Order`, so before this nothing reached the DB and
  a bot attempting 400 tickets looked identical to one attempting 3. `missed`
  means the edge vanished between detection and submission, which is the
  measurement that decides whether latency work is worth anything.
- **An attempt is logged only once it reaches `submit_arb_ticket`.** "The
  detector found nothing" is not an attempt and is never written.

`paper_settlement` records resolution events, which `settle_outcome_async`
previously did not — a settled winner was indistinguishable from a position
sold out at market. A ticket's realized profit is computed at read time from
its **own** fills, because settlement uses an `avg_price` blended across every
ticket on that outcome.

Equity is computed by `shared/equity.py:account_equity` and by nothing else.
`PnlSnapshotService` and `GET /paper/{id}` both call it; if they diverged, the
account strip and the equity curve would disagree on the same page.
```

Update the **Frontend** section: the single-page terminal now has `/account`
as well as `/admin`, and `AccountPanel` no longer exists.

- [ ] **Step 5: Update the runbook**

Add a short section to `docs/RUNBOOK.md` covering: where trade history lives
(`GET /paper/default/tickets`), what the three ticket statuses mean, and that
`paper_settlement` rows come from the heuristic auto-settler so a wrong call
shows up as a ticket whose realized profit is nothing like its expectation.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md docs/RUNBOOK.md
git commit -m "docs: ticket-level trade history and settlement events"
```

---

## Self-review notes

**Spec coverage.** Part A → Task 1. Part B → Tasks 4, 5, 8, 9. Part C →
Tasks 1, 2, 3, 6. Part D → Task 7. Part E → Tasks 2, 3, 10. Part F → Tasks
11–14. Testing section → distributed, plus Task 15 for the full bar. Error
handling → the swallow-and-log in `_write_ticket` (Task 8) and the empty-list
behaviour in Task 10.

**Two deviations from the spec, both deliberate:**

1. The spec described writing the ticket row after the router call. Task 8
   inserts it as `status="pending"` *first*, because `paper_order.ticket_id` is
   an FK to `paper_ticket.id` and the sink writes order rows during
   `emit_order_events`. `"pending"` therefore appears in the `Ticket.status`
   union in Task 11 — a transient state, and a stuck `pending` row means the
   process died mid-ticket, which is worth being able to see.
2. `insert_paper_settlement` takes `venue_id` so it can call
   `ensure_outcome_placeholder`, which every other outcome-keyed write does.
   The spec's table has no `venue_id` column and neither does the model — it is
   a parameter, not a field.
