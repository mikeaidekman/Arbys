# Capital Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop counting DraftKings paper cash, deposit $2,000 into each trading venue's paper balance on the hosted account, and refuse any ticket on a game that starts more than seven days from now.

**Architecture:** Three independent controls on the existing chokepoints. The paper broker map follows the DraftKings feature flag instead of being unconditional. A one-shot Alembic data migration delivers the deposit to the live Neon database through the deploy's `release_command`, and the seed constant moves to $4,000 so a reset agrees. A `starts_too_far_out` rule joins `cap_breach` in `ticket_service`, enforced in `submit_arb_ticket` and pre-checked silently by the auto-trader through a new injected callable, exactly as the position cap is.

**Tech Stack:** Python 3.11+ / FastAPI / SQLAlchemy 2 async / Alembic; pytest with `asyncio_mode = "auto"`; Vite + React 19 + TypeScript frontend (oxlint, `tsc -b`).

Spec: [docs/superpowers/specs/2026-09-03-capital-controls-design.md](../specs/2026-09-03-capital-controls-design.md)

## Global Constraints

- Run everything from the repo root with `venv\Scripts\python.exe`, never a bare `python`.
- `venv\Scripts\python.exe -m pytest -q` must stay green (483 tests today; the count rises). `venv\Scripts\python.exe -m ruff check .` must stay clean. mypy is **not** part of the bar; do not claim it clean and do not fix unrelated mypy errors.
- All money and prices are `Decimal`, never float. Days-to-start is a duration, not money, so `float` is correct there.
- `arbys/ingest/` must not import `arbys/backend/`. The auto-trader receives callables from `AppState`; it never imports `ticket_service`.
- `arbys/shared/` has no I/O and no framework imports. Nothing in this plan touches it.
- Migrations describe their own change in explicit `op.*` calls and never build DDL from `Base.metadata`. The new revision is data-only.
- `.env` is gitignored. Only `.env.example` is edited.
- No new hex colours, radii or type scales in the frontend. This plan only deletes a filter and changes one numeric constant there.
- Every commit message ends with the trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Use two `-m` flags; do not nest heredocs inside `$(...)` in Git Bash (it fails to parse).
- Never run `POST /paper/{id}/reset` or touch `arbys-local.db` as part of this work. Nothing is reset.

---

## File structure

| file | responsibility in this plan |
| --- | --- |
| `arbys/backend/state.py` | `DEFAULT_STARTING_BALANCE` → 4000; `max_days_to_start()` config helper; DraftKings fee model/broker gated on `draftkings_enabled()`; `_auto_would_start_too_late` wiring for the auto-trader |
| `arbys/backend/ticket_service.py` | `starts_too_far_out(state, opp)` rule; enforced in `submit_arb_ticket` |
| `arbys/ingest/auto_trade_service.py` | new `would_start_too_late` callable, pre-checked in `handle()` |
| `arbys/db/migrations/versions/0010_fund_trading_venues.py` | the deposit and the DraftKings balance removal |
| `frontend/src/pages/AccountPage.tsx` | drop the DraftKings filter; `START` → 4000 |
| `tests/test_config.py` | `max_days_to_start()` parsing |
| `tests/test_ticket_service.py` | refusal paths |
| `tests/test_auto_trade_service.py` | pre-check unit test |
| `tests/test_ingest_wiring.py` | broker set follows the flag; end-to-end pre-check |
| `tests/db/test_migration_0010_funds_the_account.py` | the migration does what the request asked |
| `.env.example`, `CLAUDE.md`, `docs/RUNBOOK.md` | documentation |

Tasks 1 → 2 → 3 are sequential (each consumes the previous one's name). Task 4 and Task 5 are independent of each other and of 1–3. Task 6 and 7 come last.

---

### Task 1: `max_days_to_start()` config helper

**Files:**
- Modify: `arbys/backend/state.py` (insert after `min_contract_qty()`, which ends `return Decimal("0") if value <= 0 else value` and is followed by the comment `# venue_id -> factory(outcome_ids) -> MarketDataAdapter`)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `DEFAULT_MAX_DAYS_TO_START: float = 7.0` and `max_days_to_start() -> float | None` in `arbys.backend.state`. `None` means disabled. Task 2 imports both names.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_max_days_to_start_defaults_to_seven(monkeypatch):
    monkeypatch.delenv("ARBYS_MAX_DAYS_TO_START", raising=False)
    assert max_days_to_start() == 7.0
    assert DEFAULT_MAX_DAYS_TO_START == 7.0


def test_max_days_to_start_reads_the_env(monkeypatch):
    monkeypatch.setenv("ARBYS_MAX_DAYS_TO_START", "3.5")
    assert max_days_to_start() == 3.5


def test_max_days_to_start_zero_disables_the_rule(monkeypatch):
    monkeypatch.setenv("ARBYS_MAX_DAYS_TO_START", "0")
    assert max_days_to_start() is None
    monkeypatch.setenv("ARBYS_MAX_DAYS_TO_START", "-2")
    assert max_days_to_start() is None


@pytest.mark.parametrize("bad", ["", "abc", "1.2.3"])
def test_max_days_to_start_garbage_falls_back(monkeypatch, bad):
    monkeypatch.setenv("ARBYS_MAX_DAYS_TO_START", bad)
    assert max_days_to_start() == 7.0
```

Change the import at the top of `tests/test_config.py` from

```python
from arbys.backend.state import DEFAULT_MAX_TICKET_STAKE, max_ticket_stake
```

to

```python
from arbys.backend.state import (
    DEFAULT_MAX_DAYS_TO_START,
    DEFAULT_MAX_TICKET_STAKE,
    max_days_to_start,
    max_ticket_stake,
)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv\Scripts\python.exe -m pytest tests/test_config.py -q`
Expected: collection error, `ImportError: cannot import name 'DEFAULT_MAX_DAYS_TO_START'`.

- [ ] **Step 3: Implement the helper**

In `arbys/backend/state.py`, directly after the `min_contract_qty()` function body and before the `# venue_id -> factory(outcome_ids) -> MarketDataAdapter` comment, insert:

```python
DEFAULT_MAX_DAYS_TO_START = 7.0


def max_days_to_start() -> float | None:
    """Furthest-out game the paper account will trade, in days. ``None`` disables.

    A pre-game edge locks its stake until the game settles, so a fixture two
    weeks away holds capital for two weeks. With ``ARBYS_MAX_OUTCOME_STAKE`` at
    $500 a game and a few thousand dollars a venue, a handful of far-out
    fixtures is the bankroll -- and on 2026-09-03 that is exactly what
    happened: both venues out of buying power, no new trades all day.

    Enforced at the submission chokepoint (`ticket_service.starts_too_far_out`)
    and pre-checked by the auto-trader so a far-out edge, which republishes on
    every depth tick for as long as it exists, does not write a rejected ticket
    per tick. It is a rule about *tying up capital*, not about edge: the engine
    still detects, publishes and displays far-out edges. Set
    ARBYS_MAX_DAYS_TO_START=0 to turn it off.
    """
    raw = os.environ.get("ARBYS_MAX_DAYS_TO_START")
    if raw is None:
        return DEFAULT_MAX_DAYS_TO_START
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MAX_DAYS_TO_START
    return None if value <= 0 else value
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv\Scripts\python.exe -m pytest tests/test_config.py -q`
Expected: all pass, including the four new tests.

- [ ] **Step 5: Lint and commit**

```bash
venv/Scripts/python.exe -m ruff check arbys/backend/state.py tests/test_config.py
git add arbys/backend/state.py tests/test_config.py
git commit -m "feat(config): ARBYS_MAX_DAYS_TO_START, the furthest-out game worth tying capital up in" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `starts_too_far_out` refuses at the submission chokepoint

**Files:**
- Modify: `arbys/backend/ticket_service.py` (imports at lines 31–45; new function after `cap_breach`, before `_group_outcomes`; enforcement inside `submit_arb_ticket` after the `_is_settled` block and before `state.enter_ticket()`)
- Test: `tests/test_ticket_service.py`

**Interfaces:**
- Consumes: `max_days_to_start` from Task 1; existing `_starts_at(state, event_group_id) -> datetime | None`, which already strips a synthetic `<group>:<venue>` id.
- Produces: `starts_too_far_out(state: AppState, opp: ArbOpportunity) -> str | None` — a reason string beginning `starts_too_far_out:<group id>` when the game is too far ahead, else `None`. Task 3 imports it.

- [ ] **Step 1: Extend the test fixture and write the failing tests**

In `tests/test_ticket_service.py`, add to the imports:

```python
from datetime import UTC, datetime, timedelta
```

Change the `_arb_group` helper signature and the `EventGroup(...)` it builds so a start time can be supplied. The current helper begins:

```python
async def _arb_group(*, ask_size: Decimal | None = None):
    """An eg-1 group quoted 0.40 / 0.50 — a live 10c gross edge."""
    s = get_state()
    group = EventGroup(
        id="eg-1",
        title="MLB: ATL @ LAD",
        legs=(
```

Change it to:

```python
async def _arb_group(
    *, ask_size: Decimal | None = None, start_time: datetime | None = None
):
    """An eg-1 group quoted 0.40 / 0.50 — a live 10c gross edge."""
    s = get_state()
    group = EventGroup(
        id="eg-1",
        title="MLB: ATL @ LAD",
        start_time=start_time,
        legs=(
```

Then append these tests at the end of the file:

```python
# --- ARBYS_MAX_DAYS_TO_START: capital is not tied up in far-out games -------


async def test_a_game_more_than_the_window_away_is_refused(monkeypatch):
    """A pre-game edge locks its stake until the game settles. On 2026-09-03
    a slate one to two weeks out had both venues out of buying power."""
    monkeypatch.delenv("ARBYS_MAX_DAYS_TO_START", raising=False)  # default: 7
    s, _ = await _arb_group(start_time=datetime.now(UTC) + timedelta(days=8))
    opp = s.engine.evaluate_now("eg-1")[0]

    result = await submit_arb_ticket(s, opp, source="manual")

    assert result.status == "rejected"
    assert result.reason is not None
    assert result.reason.startswith("starts_too_far_out:eg-1")
    assert "limit 7" in result.reason
    assert result.order_ids == ()
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert [t["status"] for t in tickets] == ["rejected"]


async def test_a_game_inside_the_window_fills(monkeypatch):
    monkeypatch.delenv("ARBYS_MAX_DAYS_TO_START", raising=False)
    s, _ = await _arb_group(start_time=datetime.now(UTC) + timedelta(days=6))
    opp = s.engine.evaluate_now("eg-1")[0]

    result = await submit_arb_ticket(s, opp, source="manual")

    assert result.status == "filled", result.reason


async def test_a_game_already_under_way_is_not_refused(monkeypatch):
    """Negative distance to kickoff is in-play, which is the best case."""
    monkeypatch.delenv("ARBYS_MAX_DAYS_TO_START", raising=False)
    s, _ = await _arb_group(start_time=datetime.now(UTC) - timedelta(hours=1))
    opp = s.engine.evaluate_now("eg-1")[0]

    result = await submit_arb_ticket(s, opp, source="manual")

    assert result.status == "filled", result.reason


async def test_an_unknown_start_time_does_not_block(monkeypatch):
    """None means unknown, never "far away" -- a hand-registered group without
    a start time must stay tradeable, as it does for settlement."""
    monkeypatch.delenv("ARBYS_MAX_DAYS_TO_START", raising=False)
    s, _ = await _arb_group(start_time=None)
    opp = s.engine.evaluate_now("eg-1")[0]

    result = await submit_arb_ticket(s, opp, source="manual")

    assert result.status == "filled", result.reason


async def test_the_start_window_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ARBYS_MAX_DAYS_TO_START", "0")
    s, _ = await _arb_group(start_time=datetime.now(UTC) + timedelta(days=30))
    opp = s.engine.evaluate_now("eg-1")[0]

    result = await submit_arb_ticket(s, opp, source="manual")

    assert result.status == "filled", result.reason


async def test_a_naive_start_time_is_read_as_utc(monkeypatch):
    """Discovery writes aware datetimes, but a hand-registered group may not;
    `in_play_slugs` already reads naive as UTC and this must agree."""
    monkeypatch.delenv("ARBYS_MAX_DAYS_TO_START", raising=False)
    naive = (datetime.now(UTC) + timedelta(days=8)).replace(tzinfo=None)
    s, _ = await _arb_group(start_time=naive)
    opp = s.engine.evaluate_now("eg-1")[0]

    result = await submit_arb_ticket(s, opp, source="manual")

    assert result.status == "rejected"
    assert result.reason is not None
    assert result.reason.startswith("starts_too_far_out:")


async def test_the_far_out_refusal_honours_record_nonfill_false(monkeypatch):
    """The auto-trader's duplicate-row suppression applies here as it does to
    every other pre-execution refusal."""
    monkeypatch.delenv("ARBYS_MAX_DAYS_TO_START", raising=False)
    s, _ = await _arb_group(start_time=datetime.now(UTC) + timedelta(days=8))
    opp = s.engine.evaluate_now("eg-1")[0]

    result = await submit_arb_ticket(s, opp, source="auto", record_nonfill=False)

    assert result.status == "rejected"
    async with session_scope() as session:
        tickets = await repo.list_paper_tickets(session, s.default_account_id)
    assert tickets == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv\Scripts\python.exe -m pytest tests/test_ticket_service.py -q -k "far_out or window or under_way or unknown_start or naive_start"`
Expected: the four "refused" cases fail with `assert 'filled' == 'rejected'` (there is no rule yet, so they fill); the "fills" cases pass already. Confirm the pre-existing tests in the file still pass too (the `start_time` kwarg defaults to `None`).

- [ ] **Step 3: Implement the rule**

In `arbys/backend/ticket_service.py`, change the two import lines

```python
from datetime import datetime
```

```python
from .state import max_leg_age_skew_s, max_outcome_stake
```

to

```python
from datetime import UTC, datetime
```

```python
from .state import max_days_to_start, max_leg_age_skew_s, max_outcome_stake
```

Insert this function directly after `cap_breach` (which ends with `return None` after the `position_cap:` f-string) and before `def _group_outcomes`:

```python
def starts_too_far_out(state: AppState, opp: ArbOpportunity) -> str | None:
    """Why this ticket's game starts too far ahead to tie capital up in, or None.

    Public because the auto-trader pre-checks it, as it does `cap_breach`, and
    for the same reason: one implementation of the rule, with
    `submit_arb_ticket` the authoritative enforcement point.

    A pre-game edge locks its stake until the game settles. With a $500 cap
    per game and a few thousand dollars per venue, a handful of fixtures a
    fortnight out is the whole bankroll -- which is what stopped the hosted
    account on 2026-09-03: both venues out of buying power, no new trades.

    `None` for a group with no start time: unknown is not "far away", and a
    hand-registered group without one must stay tradeable, as it does for
    settlement. A naive datetime is read as UTC, as `in_play_slugs` reads it.
    A game already under way has a negative distance and is never refused
    here. The reason carries the distance because nothing else records it.
    """
    limit_days = max_days_to_start()
    if limit_days is None:
        return None
    start = _starts_at(state, opp.event_group_id)
    if start is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    days_ahead = (start - datetime.now(UTC)).total_seconds() / 86400
    if days_ahead <= limit_days:
        return None
    return (
        f"starts_too_far_out:{opp.event_group_id} starts in {days_ahead:.1f} days; "
        f"limit {limit_days:g} (ARBYS_MAX_DAYS_TO_START)"
    )[:256]
```

Then in `submit_arb_ticket`, after the `_is_settled` block (which ends `return TicketResult(ticket_id, "rejected", (), reason)`) and before `state.enter_ticket()`, insert:

```python
    # A game more than ARBYS_MAX_DAYS_TO_START away locks its stake until it
    # settles. A property of the game rather than of the edge, so it is judged
    # here beside settlement and not inside the in-flight section below.
    too_far = starts_too_far_out(state, opp)
    if too_far is not None:
        if record_nonfill:
            await _write_ticket(
                ticket_id=ticket_id, account_id=account_id, opp=opp, title=title,
                starts_at=starts_at, source=source, status="rejected",
                reason=too_far, economics=None,
            )
        return TicketResult(ticket_id, "rejected", (), too_far)
```

- [ ] **Step 4: Run the whole ticket-service file**

Run: `venv\Scripts\python.exe -m pytest tests/test_ticket_service.py -q`
Expected: all pass, including the seven new tests.

- [ ] **Step 5: Lint and commit**

```bash
venv/Scripts/python.exe -m ruff check arbys/backend/ticket_service.py tests/test_ticket_service.py
git add arbys/backend/ticket_service.py tests/test_ticket_service.py
git commit -m "feat(risk): refuse a ticket on a game more than seven days out" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: the auto-trader pre-checks the window and skips silently

**Files:**
- Modify: `arbys/ingest/auto_trade_service.py` (constructor signature at ~lines 93–121; `handle()` after the `_would_breach_cap` check)
- Modify: `arbys/backend/state.py` (`AutoTradeService(...)` construction in `AppState.__init__`; new method next to `_auto_would_breach_cap`)
- Test: `tests/test_auto_trade_service.py`, `tests/test_ingest_wiring.py`

**Interfaces:**
- Consumes: `starts_too_far_out` from Task 2.
- Produces: `AutoTradeService.__init__(..., would_start_too_late: Callable[[ArbOpportunity], bool] = lambda _opp: False, ...)`; `AppState._auto_would_start_too_late(opp) -> bool`.

- [ ] **Step 1: Write the failing unit test**

In `tests/test_auto_trade_service.py`, in `_Harness.__init__`, directly after `self.breach = False`, add:

```python
        self.too_late = False
```

In `_Harness.service()`, directly after the line `would_breach_cap=lambda _opp: self.breach,`, add:

```python
            would_start_too_late=lambda _opp: self.too_late,
```

Directly after `test_the_cap_precheck_skips_silently_without_submitting`, add:

```python
async def test_the_far_out_precheck_skips_silently_without_submitting():
    """An edge on a game a fortnight away persists for days and republishes on
    every depth tick; recording each refusal would fill the ledger with rows
    saying only "still too early". Same treatment as the cap, same reason."""
    h = _Harness()
    h.too_late = True
    assert await h.service().handle(_opp()) is None
    assert h.submitted == []


async def test_the_far_out_precheck_defaults_to_never():
    """A caller that does not wire the callable gets the old behaviour."""
    h = _Harness()
    service = AutoTradeService(
        subscribe=lambda: h.queue,
        unsubscribe=h.unsubscribed.append,
        submit=h.submit,
        would_breach_cap=lambda _opp: False,
        enabled=lambda: True,
        cooldown_s=0.0,
        nonfill_log_s=0.0,
        clock=lambda: h.now,
    )
    assert await service.handle(_opp()) == "filled"
    assert len(h.submitted) == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `venv\Scripts\python.exe -m pytest tests/test_auto_trade_service.py -q`
Expected: every test in the file errors with `TypeError: AutoTradeService.__init__() got an unexpected keyword argument 'would_start_too_late'` (the harness now passes it).

- [ ] **Step 3: Implement in the service**

In `arbys/ingest/auto_trade_service.py`, in `AutoTradeService.__init__`, after the parameter

```python
        would_breach_cap: Callable[[ArbOpportunity], bool],
```

add

```python
        would_start_too_late: Callable[[ArbOpportunity], bool] = lambda _opp: False,
```

and after `self._would_breach_cap = would_breach_cap` add

```python
        self._would_start_too_late = would_start_too_late
```

In `handle()`, directly after

```python
        if self._would_breach_cap(opp):
            return None
```

add

```python
        # Same treatment as the cap, for the same reason: an edge on a game a
        # fortnight away can persist for days and republishes on every depth
        # tick, so a recorded rejection per tick would say only "still too
        # early" all night. `submit_arb_ticket` remains authoritative.
        if self._would_start_too_late(opp):
            return None
```

- [ ] **Step 4: Run the unit tests**

Run: `venv\Scripts\python.exe -m pytest tests/test_auto_trade_service.py -q`
Expected: all pass.

- [ ] **Step 5: Write the failing wiring test**

In `tests/test_ingest_wiring.py`, add to the imports:

```python
from dataclasses import replace
from datetime import UTC, datetime, timedelta
```

Directly after `test_auto_trader_skips_silently_when_the_cap_would_be_breached`, add:

```python
async def test_auto_trader_skips_silently_when_the_game_is_too_far_out(monkeypatch):
    """AppState._auto_would_start_too_late has to actually stop a submission.

    The unit tests inject the callable as a plain boolean; this is the one
    place the real AppState wiring runs end to end. Zero ticket rows is the
    assertion: the pre-check exists so a far-out edge does not write a
    rejected row on every fingerprint change for a week.
    """
    monkeypatch.setenv("ARBYS_ENABLE_AUTO_TRADE", "1")
    monkeypatch.delenv("ARBYS_MAX_DAYS_TO_START", raising=False)  # default: 7
    s = await _live_edge()
    # `_starts_at` reads `state.event_groups`, so replacing the mapping entry
    # is enough; the engine's own registration is by id and unaffected.
    s.event_groups["eg-1"] = replace(
        s.event_groups["eg-1"], start_time=datetime.now(UTC) + timedelta(days=8)
    )
    await s.bootstrap()
    tickets: list[dict] = []
    try:
        opps = s.engine.evaluate_now("eg-1")
        assert opps, "fixture must produce a live net-positive opportunity"
        s._set_group_opportunities("eg-1", opps)
        for _ in range(200):
            async with session_scope() as session:
                tickets = await repo.list_paper_tickets(session, s.default_account_id)
            if tickets:
                break
            await asyncio.sleep(0.005)
    finally:
        await s.shutdown()
    assert tickets == [], "the far-out pre-check must skip the submission, not write a ticket"
```

- [ ] **Step 6: Run it to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/test_ingest_wiring.py -q -k too_far_out`
Expected: FAIL — the auto-trader submits, `submit_arb_ticket` refuses with `starts_too_far_out:` and *records* it, so `tickets` holds one `rejected` row.

- [ ] **Step 7: Wire it in `AppState`**

In `arbys/backend/state.py`, in `AppState.__init__`, change

```python
            would_breach_cap=self._auto_would_breach_cap,
```

to

```python
            would_breach_cap=self._auto_would_breach_cap,
            would_start_too_late=self._auto_would_start_too_late,
```

Directly after the `_auto_would_breach_cap` method (which ends `return cap_breach(self, opp, self.default_account_id) is not None`), add:

```python
    def _auto_would_start_too_late(self, opp: ArbOpportunity) -> bool:
        """Whether the time-to-start rule would reject this ticket.

        Same shape as `_auto_would_breach_cap`: reuses the ticket service's
        own rule so there is one implementation, and decides only whether the
        submission is worth an audit row. The authoritative check still runs
        inside `submit_arb_ticket`.
        """
        from .ticket_service import starts_too_far_out

        return starts_too_far_out(self, opp) is not None
```

- [ ] **Step 8: Run the wiring tests**

Run: `venv\Scripts\python.exe -m pytest tests/test_ingest_wiring.py tests/test_auto_trade_service.py -q`
Expected: all pass.

- [ ] **Step 9: Lint and commit**

```bash
venv/Scripts/python.exe -m ruff check arbys tests
git add arbys/ingest/auto_trade_service.py arbys/backend/state.py tests/test_auto_trade_service.py tests/test_ingest_wiring.py
git commit -m "feat(auto-trade): pre-check the start window so a far-out edge is not logged every tick" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: DraftKings leaves the paper book

**Files:**
- Modify: `arbys/backend/state.py` (`AppState.__init__`, the `self.fees` dict)
- Modify: `frontend/src/pages/AccountPage.tsx` (the `NetOfCosts` component, ~lines 1200–1206)
- Test: `tests/test_ingest_wiring.py`

**Interfaces:**
- Consumes: existing `draftkings_enabled()` from `arbys.adapters.draftkings` (already imported in `state.py`).
- Produces: nothing new. `AppState.fees` and `AppState.paper_brokers` carry exactly `{"polymarket_us", "kalshi"}` unless `ARBYS_ENABLE_DRAFTKINGS=1`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ingest_wiring.py`:

```python
# --- the paper book follows the venue flags ---------------------------------


def test_draftkings_has_no_paper_broker_unless_enabled(monkeypatch):
    """A venue whose data adapter is not built has no business holding a paper
    balance. DraftKings used to be registered unconditionally, so a broker was
    seeded with DEFAULT_STARTING_BALANCE every time: $2,000 of headline cash
    and equity that could never be traded, on a venue that never carried a leg."""
    monkeypatch.setenv("ARBYS_ENABLE_DRAFTKINGS", "0")
    state_module.reset_state()
    s = get_state()
    assert set(s.paper_brokers) == {"kalshi", "polymarket_us"}
    assert set(s.fees) == {"kalshi", "polymarket_us"}


def test_draftkings_gets_a_paper_broker_when_enabled(monkeypatch):
    monkeypatch.setenv("ARBYS_ENABLE_DRAFTKINGS", "1")
    state_module.reset_state()
    s = get_state()
    assert "draftkings" in s.paper_brokers
    assert "draftkings" in s.fees
```

- [ ] **Step 2: Run to verify failure**

Run: `venv\Scripts\python.exe -m pytest tests/test_ingest_wiring.py -q -k draftkings`
Expected: the first fails (`draftkings` is present); the second passes.

- [ ] **Step 3: Gate the fee model and broker**

In `arbys/backend/state.py`, `AppState.__init__`, replace

```python
        self.fees: FeeModelRegistry = {
            "polymarket_us": PolymarketUsFeeModel(),
            "kalshi": KalshiFeeModel(),
            "draftkings": SportsbookFeeModel("draftkings"),
        }
```

with

```python
        self.fees: FeeModelRegistry = {
            "polymarket_us": PolymarketUsFeeModel(),
            "kalshi": KalshiFeeModel(),
        }
        # Everything downstream -- the broker map, the router, the venue rows
        # bootstrap ensures, the balance bootstrap seeds -- is built from this
        # registry, so a venue listed here holds paper cash. DraftKings was
        # listed unconditionally while its adapter was behind a flag, which
        # put $2,000 of untradeable cash into the headline equity of every
        # account. The same flag now governs both.
        if draftkings_enabled():
            self.fees["draftkings"] = SportsbookFeeModel("draftkings")
```

- [ ] **Step 4: Run the tests**

Run: `venv\Scripts\python.exe -m pytest tests/test_ingest_wiring.py tests/test_backend_e2e.py -q`
Expected: all pass. (`test_open_positions_hydrate_once_per_venue` asserts `len(paper_brokers) > 1`, which two brokers satisfy.)

- [ ] **Step 5: Drop the frontend's hand-rolled filter**

In `frontend/src/pages/AccountPage.tsx`, inside `NetOfCosts`, replace

```tsx
  const balances = Object.entries(summary?.balances ?? {})
    // draftkings is seeded but never traded; showing it as 100% headroom
    // implies capacity that does not exist.
    .filter(([v]) => v === "kalshi" || v === "polymarket_us")
    .map(([venue, amt]) => ({ venue, cash: Number(amt), pct: (Number(amt) / START) * 100 }))
```

with

```tsx
  // The server reports only venues that hold a paper balance, so every entry
  // here is real buying power; the page renders what it is given.
  const balances = Object.entries(summary?.balances ?? {})
    .map(([venue, amt]) => ({ venue, cash: Number(amt), pct: (Number(amt) / START) * 100 }))
```

- [ ] **Step 6: Build the frontend**

```bash
cd frontend && npm run lint && npm run build
```

Expected: lint clean, `tsc -b && vite build` succeeds. Return to the repo root afterwards.

- [ ] **Step 7: Commit**

```bash
venv/Scripts/python.exe -m ruff check arbys tests
git add arbys/backend/state.py tests/test_ingest_wiring.py frontend/src/pages/AccountPage.tsx
git commit -m "fix(paper): a venue without an adapter holds no paper balance" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: migration 0010 funds the trading venues; $4,000 becomes the baseline

**Files:**
- Create: `arbys/db/migrations/versions/0010_fund_trading_venues.py`
- Create: `tests/db/test_migration_0010_funds_the_account.py`
- Modify: `arbys/backend/state.py` (`DEFAULT_STARTING_BALANCE` and its comment, ~lines 63–68)
- Modify: `tests/test_ingest_wiring.py` (the `_live_edge` docstring, which says "$2000 as of this writing")
- Modify: `frontend/src/pages/AccountPage.tsx` (`const START = 2000;` in `NetOfCosts`)

**Interfaces:**
- Produces: revision id `0010_fund_trading_venues` with `down_revision = "0009_paper_position_open_fees"`. `DEFAULT_STARTING_BALANCE == Decimal("4000")`.

- [ ] **Step 1: Write the failing migration test**

Create `tests/db/test_migration_0010_funds_the_account.py`:

```python
"""Revision 0010 is the delivery mechanism for a real deposit, so it is tested
as one.

The hosted account is in Neon Postgres, whose connection string exists only in
Fly's secret store; the deploy's `release_command` runs `alembic upgrade head`,
and that is the only path into the database. So "add $2,000 to each trading
venue" *is* this migration, and a migration that ran but did the wrong
arithmetic would look exactly like a successful deploy.

SQLite here, like the replay test beside it; the Postgres CI branch replays
the same chain and both statements are plain SQL on either dialect. The
starting amounts are deliberately not the seed value -- a live account holds
whatever its fills left, and the request was "add", not "set".
"""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]
PREVIOUS = "0009_paper_position_open_fees"


def _alembic(url: str, target: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=REPO_ROOT,
        env=dict(os.environ, ARBYS_DB_URL=url),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"alembic upgrade {target} failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
        )


def _fund_at_previous_revision(url: str) -> None:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            for venue in ("kalshi", "polymarket_us", "draftkings"):
                conn.execute(
                    sa.text(
                        "INSERT INTO venue (id, name, kind) VALUES (:id, :name, 'exchange')"
                    ),
                    {"id": venue, "name": venue.title()},
                )
            conn.execute(
                sa.text(
                    "INSERT INTO paper_account (id, name, base_currency) "
                    "VALUES ('default', 'default', 'USD')"
                )
            )
            for venue, amount in (
                ("kalshi", "1177.50"),
                ("polymarket_us", "0.25"),
                ("draftkings", "2000"),
            ):
                conn.execute(
                    sa.text(
                        "INSERT INTO paper_balance (account_id, venue_id, currency, amount) "
                        "VALUES ('default', :venue, 'USD', :amount)"
                    ),
                    {"venue": venue, "amount": amount},
                )
    finally:
        engine.dispose()


def _balances(url: str) -> dict[str, Decimal]:
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT venue_id, amount FROM paper_balance ORDER BY venue_id")
            ).all()
    finally:
        engine.dispose()
    return {venue: Decimal(str(amount)) for venue, amount in rows}


def test_0010_adds_2000_to_each_trading_venue_and_drops_draftkings(tmp_path):
    url = f"sqlite:///{tmp_path / 'fund.db'}"
    _alembic(url, PREVIOUS)
    _fund_at_previous_revision(url)

    _alembic(url, "head")

    assert _balances(url) == {
        "kalshi": Decimal("3177.50"),
        "polymarket_us": Decimal("2000.25"),
    }


def test_0010_is_a_no_op_on_an_unfunded_database(tmp_path):
    """A fresh deploy, the CI replay and the SQLite replay test all run this
    against empty tables; bootstrap() then seeds the new default."""
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    _alembic(url, "head")
    assert _balances(url) == {}
```

- [ ] **Step 2: Run to verify failure**

Run: `venv\Scripts\python.exe -m pytest tests/db/test_migration_0010_funds_the_account.py -q`
Expected: the first test fails — with no 0010, `head` is 0009, so the balances are unchanged and `draftkings` is still present.

- [ ] **Step 3: Write the migration**

Create `arbys/db/migrations/versions/0010_fund_trading_venues.py`:

```python
"""Fund the two trading venues by $2,000 each and drop the DraftKings balance.

A data migration, not a schema change. The hosted paper account lives in Neon
Postgres, whose connection string exists only in Fly's secret store, and the
deploy's `release_command` (`alembic upgrade head`) is the only path into it.
Bootstrap seeds a balance only for a venue that has never been funded, so
raising `DEFAULT_STARTING_BALANCE` alone would do nothing to a live account.

Why: on 2026-09-03 both venues ran out of buying power and the auto-trader
placed nothing all day. The account started at $2,000 a venue and each venue
now gets $2,000 more, on top of whatever its fills have left -- "add", not
"set". `DEFAULT_STARTING_BALANCE` moves to $4,000 in the same change so a
future reset seeds the same level rather than quietly undoing this.

DraftKings: its paper broker is now built only when `ARBYS_ENABLE_DRAFTKINGS=1`,
the flag that has always gated its adapter. With the flag off nothing hydrates
or reads its balance row, and the $2,000 in it was never tradeable -- it sat
in the headline cash and equity of every account as capacity that did not
exist. Removing the row means re-enabling the flag later seeds the venue fresh
at the then-current default rather than resurrecting a 2026 figure.

On an empty database (a fresh deploy, the CI replay) both statements touch
zero rows and bootstrap seeds the new default afterwards. `downgrade()`
subtracts the deposit again; it does not restore the DraftKings row, because
there is no broker to hydrate it into and nothing that reads it.
"""

from __future__ import annotations

from alembic import op

revision: str = "0010_fund_trading_venues"
down_revision: str | None = "0009_paper_position_open_fees"
branch_labels = None
depends_on = None

_TRADING_VENUES = "('kalshi', 'polymarket_us')"
_DEPOSIT = "2000"


def upgrade() -> None:
    op.execute(
        f"UPDATE paper_balance SET amount = amount + {_DEPOSIT} "
        f"WHERE venue_id IN {_TRADING_VENUES}"
    )
    op.execute("DELETE FROM paper_balance WHERE venue_id = 'draftkings'")


def downgrade() -> None:
    op.execute(
        f"UPDATE paper_balance SET amount = amount - {_DEPOSIT} "
        f"WHERE venue_id IN {_TRADING_VENUES}"
    )
```

- [ ] **Step 4: Run the migration tests, then the whole db suite**

Run: `venv\Scripts\python.exe -m pytest tests/db -q`
Expected: all pass. `test_migration_chain_replays_from_empty_and_matches_models` and the foreign-key comparison still pass because 0010 changes no schema.

- [ ] **Step 5: Move the baseline to $4,000**

In `arbys/backend/state.py`, replace

```python
# Seeded per venue on `bootstrap` for a new account and on every
# `reset_paper_account`. Raised from 1000 on 2026-08-26 so a fresh paper
# account has room to hold several concurrent tickets: ARBYS_MAX_TICKET_STAKE
# is 200, so 1000 bound the account at five open tickets on one venue before
# cash ran out - and a two-leg arb draws on both venues at once.
DEFAULT_STARTING_BALANCE = Decimal("2000")
```

with

```python
# Seeded per venue on `bootstrap` for a new account and on every
# `reset_paper_account`. Raised from 1000 on 2026-08-26 so a fresh paper
# account has room to hold several concurrent tickets: ARBYS_MAX_TICKET_STAKE
# is 200, so 1000 bound the account at five open tickets on one venue before
# cash ran out - and a two-leg arb draws on both venues at once. Raised again
# to 4000 on 2026-09-03, when both venues ran dry at 2000: migration 0010
# deposited the difference into the live account, and this constant follows so
# a reset seeds the same level rather than quietly undoing the deposit. Only
# venues that have never been funded are seeded from it -- hydrated balances
# always win -- so changing it does nothing to an existing account; fund one
# with a data migration, as 0010 does.
DEFAULT_STARTING_BALANCE = Decimal("4000")
```

In `tests/test_ingest_wiring.py`, in the `_live_edge` docstring, change

```
    $2000 as of this writing): it runs after this helper and ``hydrate_balance``
```

to

```
    $4000 as of this writing): it runs after this helper and ``hydrate_balance``
```

In `frontend/src/pages/AccountPage.tsx`, inside `NetOfCosts`, change

```tsx
  const START = 2000; // seeded per venue by bootstrap and by every reset
```

to

```tsx
  // Seeded per venue by bootstrap and by every reset: DEFAULT_STARTING_BALANCE
  // in arbys/backend/state.py. The two must move together.
  const START = 4000;
```

- [ ] **Step 6: Run the suite and build the frontend**

```bash
venv/Scripts/python.exe -m pytest -q
venv/Scripts/python.exe -m ruff check .
cd frontend && npm run lint && npm run build
```

Expected: pytest green, ruff clean, frontend builds. Return to the repo root.

- [ ] **Step 7: Commit**

```bash
git add arbys/db/migrations/versions/0010_fund_trading_venues.py tests/db/test_migration_0010_funds_the_account.py arbys/backend/state.py tests/test_ingest_wiring.py frontend/src/pages/AccountPage.tsx
git commit -m "feat(paper): deposit 2,000 into each trading venue and make 4,000 the baseline" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: documentation

**Files:**
- Modify: `.env.example` (after the `ARBYS_MIN_CONTRACT_QTY=5` block; and the DraftKings comment near the top)
- Modify: `CLAUDE.md` (Config bullets; the `ARBYS_ENABLE_DRAFTKINGS` paragraph)
- Modify: `docs/RUNBOOK.md` (bootstrap steps 3 and 6; section 4 "Starting balances"; section 6 "Data reset")

**Interfaces:** none.

- [ ] **Step 1: `.env.example`**

Replace

```
# Only the DraftKings flag is actually read (adapters/draftkings.py); the
# Polymarket and Kalshi adapters are always built when ingest is on.
ARBYS_ENABLE_DRAFTKINGS=0
```

with

```
# Only the DraftKings flag is actually read (adapters/draftkings.py); the
# Polymarket and Kalshi adapters are always built when ingest is on. The same
# flag decides whether DraftKings gets a paper broker and a seeded balance at
# all: with it off, the venue holds no paper cash and does not appear in the
# account's balances or equity.
ARBYS_ENABLE_DRAFTKINGS=0
```

Directly after the `ARBYS_MIN_CONTRACT_QTY=5` line, add:

```

# Furthest-out game the paper account will trade, in days from now to the
# scheduled start. A pre-game edge locks its stake until the game settles, and
# on 2026-09-03 a slate of fixtures one to two weeks out had both venues out of
# buying power with no new trades all day. Refused at submission -- a manual
# Fill click is recorded as rejected, with the days to kickoff in the reason --
# and pre-checked by the auto-trader, which skips silently as it does for the
# position cap. The engine still detects and displays far-out edges; only
# filling them stops. A game with no known start time is not blocked. 0
# disables.
ARBYS_MAX_DAYS_TO_START=7
```

- [ ] **Step 2: `CLAUDE.md`**

In the **Config** section, directly after the `ARBYS_MIN_CONTRACT_QTY` bullet (which ends `...leaves a live arb's Fill button disabled.`), add:

```markdown
- `ARBYS_MAX_DAYS_TO_START` — furthest-out game worth tying capital up in,
  in days to scheduled start, default 7, `0` disables. A pre-game edge locks
  its stake until the game settles, and on 2026-09-03 a slate one to two
  weeks out had both venues out of buying power with no new trades all day.
  A rule about **capital**, not edge: the engine still detects, publishes and
  displays far-out edges, `/monitored` still ranks them, and only filling
  stops. Enforced at the submission chokepoint
  (`ticket_service.starts_too_far_out`, beside `cap_breach`) so a manual
  Fill click on a far-out row is recorded as `rejected` with the days to
  kickoff in the reason; the auto-trader pre-checks it and skips **silently**
  for the same reason it pre-checks the cap — a far-out edge persists for
  days and republishes on every depth tick. `start_time=None` does not
  block: unknown is not "far away", matching the settlement convention. Note
  the Fill button therefore stays live on a far-out row; greying it needs a
  flag on `/monitored` and is deferred until a click actually hits the
  refusal.
```

Replace this exact three-line paragraph in the **Config** section:

```markdown
`.env.example` also lists `ARBYS_ENABLE_DRAFTKINGS`, which *is* read. Note
that `ARBYS_ENABLE_POLYMARKET` / `ARBYS_ENABLE_KALSHI` / `POLYMARKET_API_KEY`
were **dead config** — nothing ever read them — and have been removed.
```

with:

```markdown
`.env.example` also lists `ARBYS_ENABLE_DRAFTKINGS`, which *is* read — and as
of 2026-09-03 it gates the **paper broker** as well as the adapter. DraftKings
used to be in `AppState.fees` unconditionally, so a broker was built and
seeded with `DEFAULT_STARTING_BALANCE` for a venue that never carried a leg:
$2,000 of headline cash and equity that could never trade. Migration `0010`
removed that balance row and deposited $2,000 into each trading venue;
`DEFAULT_STARTING_BALANCE` is **$4,000** so a reset seeds the same level.
Hydrated balances always win over the constant, so funding an existing
account is a data migration, not a constant change. Note that
`ARBYS_ENABLE_POLYMARKET` / `ARBYS_ENABLE_KALSHI` / `POLYMARKET_API_KEY`
were **dead config** — nothing ever read them — and have been removed.
```

The paragraph that follows ("Run the backend **from the repo root**…") is unchanged.

- [ ] **Step 3: `docs/RUNBOOK.md`**

Bootstrap step 3, replace

```
3. Ensure the three seed venues exist (`polymarket_us`, `kalshi`, `draftkings`).
```

with

```
3. Ensure the seed venues exist (`polymarket_us`, `kalshi`, plus `draftkings`
   only when `ARBYS_ENABLE_DRAFTKINGS=1` — the same flag gates its paper broker).
```

Bootstrap step 6, replace `` Seed `DEFAULT_STARTING_BALANCE = $1000` `` with `` Seed `DEFAULT_STARTING_BALANCE = $4000` ``.

In section 4, replace

```
- **Starting balances** live in `DEFAULT_STARTING_BALANCE` in
  `arbys/backend/state.py`. They only apply to venues never previously funded
  — hydrated balances always win, so bumping the constant won't retroactively
  top up an existing account.
```

with

```
- **Starting balances** live in `DEFAULT_STARTING_BALANCE` in
  `arbys/backend/state.py` ($4,000 a venue since 2026-09-03). They only apply
  to venues never previously funded — hydrated balances always win, so bumping
  the constant won't retroactively top up an existing account. To fund a live
  account, write a data migration: `0010_fund_trading_venues` is the
  precedent, and the deploy's `release_command` is what carries it to the
  hosted database.
```

In section 6, replace `` Restart the backend and the seed data + `$1000` per venue `` with `` Restart the backend and the seed data + `$4000` per venue ``.

- [ ] **Step 4: Commit**

```bash
git add .env.example CLAUDE.md docs/RUNBOOK.md
git commit -m "docs: ARBYS_MAX_DAYS_TO_START, the flag-gated DraftKings book, and the 4,000 baseline" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: final verification

**Files:**
- Modify: `CLAUDE.md` (the test count in **Commands**, currently `# 483 tests, must stay green`)

- [ ] **Step 1: Run everything**

```bash
venv/Scripts/python.exe -m pytest -q
venv/Scripts/python.exe -m ruff check .
cd frontend && npm run lint && npm run build && cd ..
```

Expected: pytest reports all passed with no failures (the count will be 483 plus the new tests: 4 config, 7 ticket-service, 2 auto-trade unit, 1 wiring pre-check, 2 broker set, 2 migration = **501**), ruff clean, frontend lint clean and build successful.

- [ ] **Step 2: Update the test count in CLAUDE.md**

Change `# 483 tests, must stay green` to the number pytest actually reported.

- [ ] **Step 3: Review the diff against the spec**

```bash
git diff main~6 --stat
```

Check each spec section has landed: Part A (Task 4), Part B (Task 5), Part C (Tasks 1–3), docs (Task 6). Confirm nothing under `arbys/shared/` changed and `arbys/ingest/auto_trade_service.py` still imports nothing from `arbys/backend/`.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: test count" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

Do **not** push. Pushing to `main` triggers the Fly deploy, whose release step runs migration 0010 against the live account; that is the intended delivery, and it is the user's call when to trigger it.
