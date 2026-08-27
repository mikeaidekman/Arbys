# Auto-Trader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fill every net-positive arbitrage opportunity the engine publishes, automatically, into the paper account — so the fill-versus-miss ratio the strategy hinges on becomes measurable instead of hypothetical.

**Architecture:** One new `asyncio` service in `arbys/ingest/`, consuming `AppState`'s existing opportunity broadcast rather than polling, and submitting through the existing `submit_arb_ticket`. It adds no sizing logic and no new safety rail — it fills what the engine already published, under the two caps that already exist. The only structural wrinkle is dependency direction: `arbys/ingest/` may not import `arbys/backend/`, so the service receives its submit and cap-check as injected callables (see **Global Constraints**).

**Tech Stack:** Python 3.11, asyncio, SQLAlchemy 2 async + aiosqlite, FastAPI, pytest (`asyncio_mode = "auto"`).

**Spec:** [docs/superpowers/specs/2026-08-23-auto-trader-design.md](../specs/2026-08-23-auto-trader-design.md)

## Global Constraints

- **All money and all prices are `Decimal`. Never float.** The one exception in this plan is the cooldown clock, which is `float` seconds on `time.monotonic()` — the same discipline `QuoteBook` uses.
- Run everything from the repo root with `venv\Scripts\python.exe`, never a bare `python`.
- `venv\Scripts\python.exe -m pytest -q` must stay green — **335 tests** before this plan. (CLAUDE.md says 304; that figure is stale, do not "restore" it.)
- `venv\Scripts\python.exe -m ruff check .` must stay clean. Re-run it after each task, including after writing tests.
- mypy is **not** part of the green bar (47 pre-existing errors across 17 files). Do not claim mypy clean; do not start a cleanup. Annotate what you touch.
- **`arbys/ingest/` must not import `arbys/backend/`.** Verified: nothing in `ingest/`, `shared/`, or `discovery/` imports `backend/`, while `backend/state.py` imports four `ingest` modules. `ticket_service.py` also imports `from .state import max_outcome_qty` at module level, so a module-level `state.py` → `ticket_service.py` import is a genuine cycle, not a stylistic worry. Hence: the service takes callables, and `AppState` does its `ticket_service` imports **inside the method bodies**.
- **The service adds no edge gate.** Both detectors already refuse to publish anything unprofitable — `net_edge_per_contract(...) <= 0` at `arbys/shared/arb_engine.py:137` and `:226`, plus `qty <= 0` and `profit <= 0` guards. Every opportunity that reaches the queue is already net-positive of fees. "Any opportunity received" *is* the spec's trigger. Do not add a threshold, a floor, or a gross-edge mode — those are explicit non-goals.
- **No new sizing logic.** `ARBYS_MAX_TICKET_STAKE` (detection time) and `ARBYS_MAX_OUTCOME_QTY` (execution time) are unchanged and both still bind.
- Tests never contact a real venue. `tests/conftest.py` pins the venue switches off session-wide and strips credentials.
- Do not touch the developer's `arbys-local.db`. Tests use `tmp_path` databases via the `_fresh_state` fixture pattern in `tests/test_ticket_service.py`.
- Paper fills are atomic (`_commit_atomically` fills every leg with the synchronous `apply_fill`, no `await` between legs), and `PaperExecutionAdapter` is the only concrete `ExecutionAdapter` in the repo. **There is no legging risk and no path to a real venue.** Do not add real-venue execution.

## File Structure

**Created:**

| File | Responsibility |
| --- | --- |
| `arbys/ingest/auto_trade_service.py` | `AutoTradeService` — consume the opportunity queue, apply cooldown + cap pre-check, submit. Pure of `backend` imports. |
| `tests/test_auto_trade_service.py` | Service behaviour in isolation, via injected callables. No `AppState`, no DB. |

**Modified:**

| File | Change |
| --- | --- |
| `arbys/backend/ticket_service.py` | Rename `_cap_breach` → `cap_breach` so the service's pre-check reuses the one implementation instead of duplicating a safety check. |
| `arbys/backend/state.py` | `_auto_trade_enabled()` / `_auto_trade_cooldown_s()` helpers; construct, start, stop, and reset-clear the service; two adapter methods for injection. |
| `tests/test_ingest_wiring.py` | Wiring: off by default, on when enabled, end-to-end fill, reset clears cooldowns. |
| `tests/conftest.py` | Pin `ARBYS_ENABLE_AUTO_TRADE=0` session-wide. |
| `.env.example` | `ARBYS_ENABLE_AUTO_TRADE` and `ARBYS_AUTO_TRADE_COOLDOWN_S`, with the reasoning. |
| `CLAUDE.md` | Document the service, the flags, and the injection rule. |
| `docs/RUNBOOK.md` | How to turn it on and how to read what it did. |
| `docs/superpowers/specs/2026-08-23-auto-trader-design.md` | Status → implemented; correct the "it will be quiet" prediction. |

---

### Task 1: Config surface for the auto-trader

Two env helpers next to the existing ones, off by default, plus `.env.example`. Nothing reads them yet — this task exists on its own because the defaults and the naming are the part worth rejecting independently, and a wrong default here means a bot that runs when nobody asked it to.

**Files:**
- Modify: `arbys/backend/state.py` (helpers beside `_discovery_interval_s`, around lines 81-86)
- Modify: `.env.example`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_auto_trade_enabled() -> bool`, `_auto_trade_cooldown_s() -> float`, `DEFAULT_AUTO_TRADE_COOLDOWN_S: float`. Task 3 calls both helpers.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`. It currently imports only `DEFAULT_MAX_TICKET_STAKE, max_ticket_stake` by name, so add `from arbys.backend import state as state_module` to its imports.

```python
def test_auto_trade_is_off_by_default(monkeypatch):
    monkeypatch.delenv("ARBYS_ENABLE_AUTO_TRADE", raising=False)
    assert state_module._auto_trade_enabled() is False


def test_auto_trade_enabled_only_by_exactly_one(monkeypatch):
    monkeypatch.setenv("ARBYS_ENABLE_AUTO_TRADE", "1")
    assert state_module._auto_trade_enabled() is True
    for value in ("0", "true", "yes", "", "2"):
        monkeypatch.setenv("ARBYS_ENABLE_AUTO_TRADE", value)
        assert state_module._auto_trade_enabled() is False


def test_auto_trade_cooldown_defaults_to_60(monkeypatch):
    monkeypatch.delenv("ARBYS_AUTO_TRADE_COOLDOWN_S", raising=False)
    assert state_module._auto_trade_cooldown_s() == 60.0


def test_auto_trade_cooldown_reads_the_env(monkeypatch):
    monkeypatch.setenv("ARBYS_AUTO_TRADE_COOLDOWN_S", "5.5")
    assert state_module._auto_trade_cooldown_s() == 5.5


def test_auto_trade_cooldown_survives_garbage_and_refuses_negatives(monkeypatch):
    monkeypatch.setenv("ARBYS_AUTO_TRADE_COOLDOWN_S", "not-a-number")
    assert state_module._auto_trade_cooldown_s() == 60.0
    # 0 is a legitimate "no cooldown"; negative is nonsense and clamps to it.
    monkeypatch.setenv("ARBYS_AUTO_TRADE_COOLDOWN_S", "0")
    assert state_module._auto_trade_cooldown_s() == 0.0
    monkeypatch.setenv("ARBYS_AUTO_TRADE_COOLDOWN_S", "-30")
    assert state_module._auto_trade_cooldown_s() == 0.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv\Scripts\python.exe -m pytest tests/test_config.py -q -p no:warnings -k auto_trade`
Expected: FAIL — `AttributeError: module 'arbys.backend.state' has no attribute '_auto_trade_enabled'`

- [ ] **Step 3: Add the helpers**

In `arbys/backend/state.py`, immediately after `_discovery_interval_s()`:

```python
def _auto_trade_enabled() -> bool:
    """Auto-trader master switch. Off by default, like ingest and discovery.

    There is deliberately no runtime UI toggle: the env flag plus the cooldown
    plus the two existing caps are the whole agreed control set.
    """
    return os.environ.get("ARBYS_ENABLE_AUTO_TRADE", "0") == "1"


DEFAULT_AUTO_TRADE_COOLDOWN_S = 60.0


def _auto_trade_cooldown_s() -> float:
    """Seconds a group is ignored after the auto-trader fills it.

    An edge stays published for as long as it exists, so without this one edge
    becomes a burst of tickets on consecutive ticks until the position cap
    stops it. 0 disables the cooldown.
    """
    raw = os.environ.get("ARBYS_AUTO_TRADE_COOLDOWN_S")
    if raw is None:
        return DEFAULT_AUTO_TRADE_COOLDOWN_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_AUTO_TRADE_COOLDOWN_S
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv\Scripts\python.exe -m pytest tests/test_config.py -q -p no:warnings -k auto_trade`
Expected: PASS (5 tests)

- [ ] **Step 5: Document the flags in `.env.example`**

Append after the `ARBYS_MAX_TICKET_STAKE` block:

```
# Auto-trader: submit a paper ticket for every net-positive opportunity the
# engine publishes, without a human clicking Fill. 0 by default — it trades
# (on paper) the moment it is on.
#
# The trigger is the honest net-of-fees gate, identical to what the Fill button
# accepts. There is no gross-edge mode and no configurable edge floor. Measured
# 2026-08-27, 5 of 496 groups had a net-positive pair worth ~18c in total, three
# of them sized at 0.01-0.03 contracts against off-market dust orders. A near-
# empty ticket log is the bot working correctly, and is not a reason to loosen
# the gate.
#
# No real venue can be reached: PaperExecutionAdapter is the only
# ExecutionAdapter in the repo and paper fills are atomic, so the bot cannot
# end up holding one naked leg.
ARBYS_ENABLE_AUTO_TRADE=0

# Seconds a group is ignored after the auto-trader *fills* it. An edge stays
# published while it exists, so without this one edge becomes a burst of
# tickets until the position cap stops it. Rejects and misses do not start a
# cooldown — a miss means the edge was gone, which is no reason to stop
# watching the group. 0 disables.
ARBYS_AUTO_TRADE_COOLDOWN_S=60
```

- [ ] **Step 6: Verify the suite and lints are clean**

Run: `venv\Scripts\python.exe -m pytest -q -p no:warnings`
Expected: all dots, 340 tests, no failures.
Run: `venv\Scripts\python.exe -m ruff check .`
Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add arbys/backend/state.py .env.example tests/test_config.py
git commit -m "feat(auto-trade): config flags, off by default"
```

---

### Task 2: The AutoTradeService

The service itself, with no `AppState` in sight. Everything it needs arrives as a callable, which is what keeps `arbys/ingest/` free of `arbys/backend/` and what makes every branch testable without a database.

**Files:**
- Create: `arbys/ingest/auto_trade_service.py`
- Modify: `arbys/backend/ticket_service.py:170` (rename `_cap_breach` → `cap_breach`) and its one call site at line 222
- Test: `tests/test_auto_trade_service.py`

**Interfaces:**
- Consumes: `ArbOpportunity` / `ArbLeg` from `arbys/shared/arb_engine.py`.
- Produces:
  - `AutoTradeService(*, subscribe, unsubscribe, submit, would_breach_cap, enabled, cooldown_s, clock=time.monotonic)`
  - `async start() -> None`, `async stop() -> None`, `clear_cooldowns() -> None`
  - `async handle(opp: ArbOpportunity) -> str | None` — the whole decision, exposed so tests drive one opportunity without a running task. Returns the ticket status (`"filled"` / `"rejected"` / `"missed"`) or `None` when deliberately skipped.
  - `BACKPRESSURE_WARN_QSIZE: int = 50`
  - `cap_breach(state, live, account_id) -> str | None` in `ticket_service` (renamed, now public)

Callable types, exactly as Task 3 must supply them:

```python
subscribe:        Callable[[], asyncio.Queue[ArbOpportunity]]
unsubscribe:      Callable[[asyncio.Queue[ArbOpportunity]], None]
submit:           Callable[[ArbOpportunity], Awaitable[str]]   # returns ticket status
would_breach_cap: Callable[[ArbOpportunity], bool]
enabled:          Callable[[], bool]
cooldown_s:       float
clock:            Callable[[], float]
```

- [ ] **Step 1: Rename the cap check so it can be reused**

In `arbys/backend/ticket_service.py`, rename the function at line 170 and its single call site at line 222. Do not change its body or its returned string — `test_position_cap_is_enforced_here_not_in_the_endpoint` matches the `position_cap:` prefix, and the HTTP endpoint surfaces the message verbatim as a 409 detail.

```python
def cap_breach(state: AppState, live: ArbOpportunity, account_id: str) -> str | None:
    """The outcome that would exceed ARBYS_MAX_OUTCOME_QTY, or None.

    Public because the auto-trader pre-checks the same condition before
    submitting. Duplicating this logic there would mean two implementations of
    one safety rule, free to drift apart; this stays the single source and
    `submit_arb_ticket` stays the authoritative enforcement point.
    """
```

Call site inside `submit_arb_ticket`:

```python
    breach = cap_breach(state, live, account_id)
```

- [ ] **Step 2: Run the existing ticket tests to prove the rename broke nothing**

Run: `venv\Scripts\python.exe -m pytest tests/test_ticket_service.py -q -p no:warnings`
Expected: PASS (10 tests). A failure here means a call site was missed — `grep -rn "_cap_breach" arbys/ tests/` should return nothing.

- [ ] **Step 3: Write the failing service tests**

Create `tests/test_auto_trade_service.py`:

```python
"""AutoTradeService decides; it does not resolve, size, or price.

Everything the service needs is injected, so these tests need no AppState, no
database and no venue. That is the same boundary that keeps `arbys/ingest/`
from importing `arbys/backend/`.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from arbys.ingest.auto_trade_service import BACKPRESSURE_WARN_QSIZE, AutoTradeService
from arbys.shared.arb_engine import ArbLeg, ArbOpportunity


def _opp(group_id: str = "eg-1", qty: str = "10") -> ArbOpportunity:
    return ArbOpportunity(
        event_group_id=group_id,
        legs=(
            ArbLeg(
                outcome_id="p-yes",
                venue_id="polymarket_us",
                is_buy=True,
                price=Decimal("0.40"),
                qty=Decimal(qty),
                fee=Decimal("0"),
            ),
            ArbLeg(
                outcome_id="k-no",
                venue_id="kalshi",
                is_buy=True,
                price=Decimal("0.50"),
                qty=Decimal(qty),
                fee=Decimal("0"),
            ),
        ),
        total_stake=Decimal("9"),
        guaranteed_profit=Decimal("1"),
        guaranteed_profit_bps=Decimal("1111"),
    )


class _Harness:
    """Records what the service asked for and lets a test dictate the answers.

    Reassign `submit` or flip `enabled`/`breach` *before* calling `service()`
    where the value is read at construction; `enabled` and `breach` are read
    through lambdas, so those two can also be changed afterwards.
    """

    def __init__(self, *, status: str = "filled", enabled: bool = True) -> None:
        self.status = status
        self.enabled = enabled
        self.breach = False
        self.submitted: list[ArbOpportunity] = []
        self.now = 1000.0
        self.queue: asyncio.Queue[ArbOpportunity] = asyncio.Queue(maxsize=100)
        self.unsubscribed: list[asyncio.Queue[ArbOpportunity]] = []

    async def submit(self, opp: ArbOpportunity) -> str:
        self.submitted.append(opp)
        return self.status

    def service(self, *, cooldown_s: float = 60.0) -> AutoTradeService:
        return AutoTradeService(
            subscribe=lambda: self.queue,
            unsubscribe=self.unsubscribed.append,
            submit=lambda opp: self.submit(opp),
            would_breach_cap=lambda _opp: self.breach,
            enabled=lambda: self.enabled,
            cooldown_s=cooldown_s,
            clock=lambda: self.now,
        )


async def test_fires_on_an_opportunity_and_reports_the_status():
    h = _Harness()
    assert await h.service().handle(_opp()) == "filled"
    assert len(h.submitted) == 1


async def test_does_nothing_when_disabled():
    h = _Harness(enabled=False)
    assert await h.service().handle(_opp()) is None
    assert h.submitted == []


async def test_enabled_is_read_per_opportunity_not_captured_at_construction():
    h = _Harness(enabled=False)
    svc = h.service()
    assert await svc.handle(_opp()) is None
    h.enabled = True
    assert await svc.handle(_opp()) == "filled"


async def test_a_fill_cools_the_group_down():
    h = _Harness()
    svc = h.service(cooldown_s=60.0)
    assert await svc.handle(_opp()) == "filled"
    h.now += 30.0
    assert await svc.handle(_opp()) is None
    assert len(h.submitted) == 1


async def test_the_cooldown_expires():
    h = _Harness()
    svc = h.service(cooldown_s=60.0)
    await svc.handle(_opp())
    h.now += 61.0
    assert await svc.handle(_opp()) == "filled"
    assert len(h.submitted) == 2


async def test_the_cooldown_is_per_group():
    h = _Harness()
    svc = h.service()
    await svc.handle(_opp("eg-1"))
    assert await svc.handle(_opp("eg-2")) == "filled"
    assert len(h.submitted) == 2


async def test_a_miss_does_not_start_a_cooldown():
    """A miss means the edge was gone, which is no reason to stop watching."""
    h = _Harness(status="missed")
    svc = h.service()
    assert await svc.handle(_opp()) == "missed"
    assert await svc.handle(_opp()) == "missed"
    assert len(h.submitted) == 2


async def test_a_rejection_does_not_start_a_cooldown():
    h = _Harness(status="rejected")
    svc = h.service()
    assert await svc.handle(_opp()) == "rejected"
    assert await svc.handle(_opp()) == "rejected"
    assert len(h.submitted) == 2


async def test_the_cap_precheck_skips_silently_without_submitting():
    """Without this, a capped-out group writes a rejected ticket on every
    publish for the rest of the night."""
    h = _Harness()
    h.breach = True
    assert await h.service().handle(_opp()) is None
    assert h.submitted == []


async def test_clear_cooldowns_forgets_everything():
    h = _Harness()
    svc = h.service()
    await svc.handle(_opp())
    svc.clear_cooldowns()
    assert await svc.handle(_opp()) == "filled"
    assert len(h.submitted) == 2


async def test_a_zero_cooldown_never_suppresses():
    h = _Harness()
    svc = h.service(cooldown_s=0.0)
    await svc.handle(_opp())
    assert await svc.handle(_opp()) == "filled"


async def test_the_run_loop_drains_the_queue_and_survives_a_failure():
    h = _Harness()
    calls = {"n": 0}

    async def flaky(opp: ArbOpportunity) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("submit blew up")
        h.submitted.append(opp)
        return "filled"

    h.submit = flaky  # type: ignore[method-assign]
    svc = h.service(cooldown_s=0.0)
    await svc.start()
    h.queue.put_nowait(_opp())
    h.queue.put_nowait(_opp())
    for _ in range(400):
        if len(h.submitted) == 1:
            break
        await asyncio.sleep(0.005)
    await svc.stop()
    assert calls["n"] == 2, "a raising submit must not kill the consumer"
    assert len(h.submitted) == 1


async def test_stop_unsubscribes_the_queue():
    h = _Harness()
    svc = h.service()
    await svc.start()
    await svc.stop()
    assert h.unsubscribed == [h.queue]


async def test_a_deep_queue_warns_about_silent_drops(caplog):
    """The subscriber queue drops with put_nowait under suppress(QueueFull), so
    backpressure is invisible by construction unless something says so."""
    h = _Harness()
    svc = h.service(cooldown_s=0.0)
    for _ in range(BACKPRESSURE_WARN_QSIZE + 2):
        h.queue.put_nowait(_opp())
    with caplog.at_level(logging.WARNING, logger="arbys.ingest.auto_trade_service"):
        await svc.start()
        for _ in range(400):
            if h.queue.qsize() == 0:
                break
            await asyncio.sleep(0.005)
        await svc.stop()
    assert any("auto-trade backpressure" in r.message for r in caplog.records)
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `venv\Scripts\python.exe -m pytest tests/test_auto_trade_service.py -q -p no:warnings`
Expected: FAIL — `ModuleNotFoundError: No module named 'arbys.ingest.auto_trade_service'`

- [ ] **Step 5: Write the service**

Create `arbys/ingest/auto_trade_service.py`:

```python
"""Automatic execution of published arbitrage opportunities.

Consumes `AppState`'s opportunity broadcast and submits a paper ticket for
every edge it receives. Event-driven rather than polled: tradeable edges are
expected to exist for short moments, so reacting on the tick that created one
is the difference between filling and missing.

**Every published opportunity is already net-positive of fees** — both
detectors gate on `net_edge_per_contract(...) <= 0` before publishing — so
there is no edge test here. "Any opportunity received" is the whole trigger.

Nothing in this module may import `arbys.backend`: `backend.state` imports this
package, and `backend.ticket_service` imports `backend.state`, so an import in
the other direction is a cycle. Submission and the position-cap pre-check
therefore arrive as injected callables, which also means every branch below is
testable without a database or an `AppState`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from ..shared.arb_engine import ArbOpportunity

log = logging.getLogger(__name__)

# `subscribe_opportunities` hands out a queue of maxsize=100 and publishers use
# `put_nowait` under `suppress(QueueFull)`, so a slow consumer loses
# opportunities with no error anywhere. Raising the maxsize or adding a
# per-subscriber drop counter is out of scope; a warning is enough to tell us
# whether either is ever needed.
BACKPRESSURE_WARN_QSIZE = 50


class AutoTradeService:
    def __init__(
        self,
        *,
        subscribe: Callable[[], asyncio.Queue[ArbOpportunity]],
        unsubscribe: Callable[[asyncio.Queue[ArbOpportunity]], None],
        submit: Callable[[ArbOpportunity], Awaitable[str]],
        would_breach_cap: Callable[[ArbOpportunity], bool],
        enabled: Callable[[], bool],
        cooldown_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._subscribe = subscribe
        self._unsubscribe = unsubscribe
        self._submit = submit
        self._would_breach_cap = would_breach_cap
        self._enabled = enabled
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue[ArbOpportunity] | None = None
        # group id -> monotonic deadline before which that group is ignored.
        self._cooldown_until: dict[str, float] = {}

    async def start(self) -> None:
        if self._task is not None:
            return
        self._queue = self._subscribe()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None
        if self._queue is not None:
            self._unsubscribe(self._queue)
            self._queue = None

    def clear_cooldowns(self) -> None:
        """Forget every group's cooldown; used after a portfolio reset."""
        self._cooldown_until.clear()

    async def handle(self, opp: ArbOpportunity) -> str | None:
        """Submit one opportunity, or return None having deliberately skipped it.

        The enabled check is re-read here rather than captured at construction,
        so the flag governs behaviour and not merely whether a task exists.
        """
        if not self._enabled():
            return None

        group_id = opp.event_group_id
        until = self._cooldown_until.get(group_id)
        if until is not None:
            if self._clock() < until:
                return None
            # Expired: drop the entry so this dict stays the size of the
            # currently-cooling set rather than of every group ever filled.
            self._cooldown_until.pop(group_id, None)

        # Pre-check, not enforcement: `submit_arb_ticket` remains authoritative.
        # The point is to skip *silently* — opportunities republish on
        # fingerprint change, so a capped-out group would otherwise write a
        # rejected ticket on every tick for the rest of the night, filling the
        # audit log with rows that say only "still capped".
        if self._would_breach_cap(opp):
            return None

        status = await self._submit(opp)
        if status == "filled" and self._cooldown_s > 0:
            self._cooldown_until[group_id] = self._clock() + self._cooldown_s
        return status

    async def _run(self) -> None:
        queue = self._queue
        assert queue is not None  # set by start() before the task is created
        while True:
            opp = await queue.get()
            depth = queue.qsize()
            if depth > BACKPRESSURE_WARN_QSIZE:
                log.warning(
                    "auto-trade backpressure: %d opportunities queued "
                    "(max 100, excess is dropped silently)",
                    depth,
                )
            # Serial on purpose. Concurrent tickets would race each other on
            # both the cash balance and the position cap, and a lost race there
            # is a real oversized position rather than a missed trade.
            try:
                await self.handle(opp)
            except Exception:
                log.exception("auto-trade failed for group=%s", opp.event_group_id)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `venv\Scripts\python.exe -m pytest tests/test_auto_trade_service.py -q -p no:warnings`
Expected: PASS (14 tests)

- [ ] **Step 7: Verify the whole suite and lints**

Run: `venv\Scripts\python.exe -m pytest -q -p no:warnings`
Expected: 354 tests, no failures.
Run: `venv\Scripts\python.exe -m ruff check .`
Expected: `All checks passed!`

- [ ] **Step 8: Commit**

```bash
git add arbys/ingest/auto_trade_service.py tests/test_auto_trade_service.py arbys/backend/ticket_service.py
git commit -m "feat(auto-trade): the service, with cooldown and cap pre-check"
```

---

### Task 3: Wire it into AppState

Construct the service, start it only when the flag is on, stop it on shutdown, and clear its cooldowns on a portfolio reset. This is where the injected callables get their real implementations.

**Files:**
- Modify: `arbys/backend/state.py` (import beside the other `ingest` imports at line 28-31; construction after `auto_settle_service` at lines 317-321; two adapter methods near `live_opportunities_for` at line 700; `bootstrap` after line 408; `shutdown` before line 619; `reset_paper_account` beside line 643)
- Modify: `tests/conftest.py`
- Test: `tests/test_ingest_wiring.py`

**Interfaces:**
- Consumes: `AutoTradeService` from Task 2; `cap_breach` (renamed in Task 2); `_auto_trade_enabled()`, `_auto_trade_cooldown_s()` from Task 1.
- Produces: `AppState.auto_trade_service: AutoTradeService`, `AppState._auto_submit_ticket(opp) -> str`, `AppState._auto_would_breach_cap(opp) -> bool`.

- [ ] **Step 1: Write the failing wiring tests**

Append to `tests/test_ingest_wiring.py`. If the file has no per-test state fixture, copy the `_fresh_state` fixture from `tests/test_ticket_service.py` verbatim (it points `ARBYS_DB_URL` at `tmp_path` and resets the engine and state around each test). Ensure these imports are present:

```python
import asyncio
from decimal import Decimal

from arbys.backend.state import get_state
from arbys.db import repositories as repo
from arbys.db.session import session_scope
from arbys.shared.types import EventGroup, EventGroupLeg, Quote
```

```python
async def _live_edge():
    """An eg-1 group quoted 0.40 / 0.50 — a live net-positive edge, funded."""
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
        s.quotebook.upsert(Quote(outcome_id=oid, bid=px, ask=px))
    for broker in s.paper_brokers.values():
        broker.deposit(s.default_account_id, Decimal("10000"))
    return s


async def test_auto_trader_is_not_started_by_default(monkeypatch):
    monkeypatch.delenv("ARBYS_ENABLE_AUTO_TRADE", raising=False)
    s = get_state()
    await s.bootstrap()
    try:
        assert s.auto_trade_service._task is None
    finally:
        await s.shutdown()


async def test_auto_trader_starts_when_enabled_and_stops_on_shutdown(monkeypatch):
    monkeypatch.setenv("ARBYS_ENABLE_AUTO_TRADE", "1")
    s = get_state()
    await s.bootstrap()
    assert s.auto_trade_service._task is not None
    await s.shutdown()
    assert s.auto_trade_service._task is None


async def test_auto_trader_fills_a_published_edge_end_to_end(monkeypatch):
    """The broadcast path AppState already uses, straight into a ticket."""
    monkeypatch.setenv("ARBYS_ENABLE_AUTO_TRADE", "1")
    s = await _live_edge()
    await s.bootstrap()
    tickets: list[dict] = []
    try:
        opps = s.engine.evaluate_now("eg-1")
        assert opps, "fixture must produce a live net-positive opportunity"
        # _set_group_opportunities is what broadcasts to subscribers.
        s._set_group_opportunities("eg-1", opps)
        for _ in range(400):
            async with session_scope() as session:
                tickets = await repo.list_paper_tickets(session, s.default_account_id)
            if tickets:
                break
            await asyncio.sleep(0.005)
    finally:
        await s.shutdown()
    assert len(tickets) == 1
    assert tickets[0]["source"] == "auto"
    assert tickets[0]["status"] == "filled"


async def test_reset_clears_the_auto_trade_cooldowns():
    s = get_state()
    s.auto_trade_service._cooldown_until["eg-1"] = 1e9
    await s.reset_paper_account(s.default_account_id)
    assert s.auto_trade_service._cooldown_until == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv\Scripts\python.exe -m pytest tests/test_ingest_wiring.py -q -p no:warnings -k auto_trad`
Expected: FAIL — `AttributeError: 'AppState' object has no attribute 'auto_trade_service'`

- [ ] **Step 3: Wire the service into AppState**

Import, beside the other `ingest` imports:

```python
from ..ingest.auto_trade_service import AutoTradeService
```

Construct it in `__init__`, immediately after `self.auto_settle_service = AutoSettleService(...)`:

```python
        # Callables rather than `self`: this service lives in `arbys/ingest/`,
        # which must not import `arbys/backend/`. See its module docstring.
        self.auto_trade_service = AutoTradeService(
            subscribe=self.subscribe_opportunities,
            unsubscribe=self.unsubscribe_opportunities,
            submit=self._auto_submit_ticket,
            would_breach_cap=self._auto_would_breach_cap,
            enabled=_auto_trade_enabled,
            cooldown_s=_auto_trade_cooldown_s(),
        )
```

Add the two adapter methods to `AppState`, next to `live_opportunities_for`:

```python
    async def _auto_submit_ticket(self, opp: ArbOpportunity) -> str:
        """Submit an auto-trader ticket, returning just its status.

        Imported inside the method: `ticket_service` imports `max_outcome_qty`
        from this module at module level, so a module-level import here is a
        genuine cycle.
        """
        from .ticket_service import submit_arb_ticket

        result = await submit_arb_ticket(self, opp, source="auto")
        return result.status

    def _auto_would_breach_cap(self, opp: ArbOpportunity) -> bool:
        """Whether the position cap would reject this ticket, checked cheaply.

        Reuses `cap_breach` so there is one implementation of the rule, not
        two. This reads the *published* opportunity while `submit_arb_ticket`
        re-checks the re-detected live one, so the two can disagree at the
        margin — which is fine and intended: this one only decides whether
        submitting is worth an audit row, and the authoritative check still
        runs inside the ticket service.
        """
        from .ticket_service import cap_breach

        return cap_breach(self, opp, self.default_account_id) is not None
```

In `bootstrap`, immediately after `await self.auto_settle_service.start()`:

```python
        if _auto_trade_enabled():
            await self.auto_trade_service.start()
            log.info(
                "ARBYS_ENABLE_AUTO_TRADE=1; the auto-trader will fill "
                "net-positive opportunities into paper account %s",
                self.default_account_id,
            )
```

In `shutdown`, before `await self.auto_settle_service.stop()`:

```python
        await self.auto_trade_service.stop()
```

In `reset_paper_account`, beside the existing `clear_settled()` call:

```python
        self.auto_trade_service.clear_cooldowns()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv\Scripts\python.exe -m pytest tests/test_ingest_wiring.py -q -p no:warnings -k auto_trad`
Expected: PASS (4 tests)

- [ ] **Step 5: Pin the flag off for the whole suite**

The autouse session fixture in `tests/conftest.py` does not pin `ARBYS_ENABLE_AUTO_TRADE`, and `app.py`'s `load_dotenv()` pulls in the developer's `.env` at import. Add it to `_OFFLINE_ENV` so no test ever trades by accident:

```python
_OFFLINE_ENV: dict[str, str] = {
    "ARBYS_ENABLE_INGEST": "0",
    "ARBYS_ENABLE_DISCOVERY": "0",
    "ARBYS_ENABLE_DRAFTKINGS": "0",
    "ARBYS_ENABLE_AUTO_TRADE": "0",
}
```

Extend that module's docstring with one sentence: the auto-trader submits tickets the moment it is on, so a suite run on a live-configured machine would otherwise write real paper history.

- [ ] **Step 6: Run the whole suite and lints**

Run: `venv\Scripts\python.exe -m pytest -q -p no:warnings`
Expected: 358 tests, no failures.
Run: `venv\Scripts\python.exe -m ruff check .`
Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add arbys/backend/state.py tests/test_ingest_wiring.py tests/conftest.py
git commit -m "feat(auto-trade): own the service from AppState, off unless enabled"
```

---

### Task 4: Verify against the running app, then document

The suite proves the logic; it does not prove the bot fills anything against a real book. This task runs it for real, then writes down what was learned.

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/RUNBOOK.md`
- Modify: `docs/superpowers/specs/2026-08-23-auto-trader-design.md`

- [ ] **Step 1: Run the backend with the auto-trader on, against live venues**

Use a throwaway database so the developer's `arbys-local.db` is untouched, and a spare port so it can run alongside the normal backend:

```bash
mkdir -p .superpowers
ARBYS_DB_URL="sqlite+aiosqlite:///./.superpowers/auto-trade-check.db" \
ARBYS_ENABLE_AUTO_TRADE=1 \
ARBYS_AUTO_TRADE_COOLDOWN_S=60 \
venv/Scripts/python.exe -m uvicorn arbys.backend.app:app --host 127.0.0.1 --port 8001
```

Confirm the startup log carries `ARBYS_ENABLE_AUTO_TRADE=1; the auto-trader will fill ...`. Leave it up for at least one discovery pass (~10 minutes) so groups exist and quotes stream.

Note that app-level `INFO` does not reach stdout under a plain `uvicorn` invocation — uvicorn configures only its own loggers — so add `--log-level info` plus a root handler, or read the confirmation from a test instead, rather than concluding the line is missing.

- [ ] **Step 2: Read the ticket log it produced**

```bash
venv/Scripts/python.exe -c "
import sqlite3
c = sqlite3.connect('.superpowers/auto-trade-check.db')
for row in c.execute('''select source, status, count(*), round(sum(expected_profit), 4)
                        from paper_ticket group by source, status order by 3 desc'''):
    print(row)
"
```

Expected: rows with `source='auto'`. Record the `filled` / `missed` / `rejected` split — that ratio is the entire reason this feature exists. `missed` means the edge died between publication and submission.

If there are **no** rows at all, that is a legitimate outcome, not a failure: check `GET /monitored` for any group with `net_edge > 0`. Zero net-positive groups means zero tickets, correctly.

- [ ] **Step 3: Confirm no dropped writes and no backpressure**

Check `GET http://127.0.0.1:8001/health` for `dropped_writes: 0` — non-zero means the ticket log on screen is incomplete and every figure from it is a lower bound. Also grep the log for `auto-trade backpressure`: any occurrence means the consumer is falling behind and opportunities are being dropped silently, which is worth reporting even though widening the queue is out of scope.

- [ ] **Step 4: Document it in `CLAUDE.md`**

Under **Architecture**, add `auto_trade_service` to the `arbys/ingest/` bullet's list of async services. Then add a section after **Trade history is ticket-level**:

```markdown
## The auto-trader fills what the engine published

`ARBYS_ENABLE_AUTO_TRADE` (**0 by default**) starts `AutoTradeService`, which
consumes `AppState.subscribe_opportunities()` and calls `submit_arb_ticket`
with `source="auto"` for every opportunity it receives. There is no separate
threshold: both detectors already refuse to publish anything with
`net_edge_per_contract(...) <= 0`, so everything on that queue is net-positive
of fees. **Do not add an edge floor or a gross-edge mode** — they are explicit
non-goals, and the honest gate being quiet is the finding, not a bug.

It adds no sizing logic. `ARBYS_MAX_TICKET_STAKE` (detection) and
`ARBYS_MAX_OUTCOME_QTY` (execution) both still bind, and `submit_arb_ticket`
stays the authoritative enforcement point for the latter. The service
*additionally* pre-checks the cap via the shared `cap_breach` and skips
**silently** when it would bind: opportunities republish on fingerprint change,
so a capped-out group would otherwise write a `rejected` ticket on every tick
all night, filling the audit log with rows saying only "still capped".

`ARBYS_AUTO_TRADE_COOLDOWN_S` (default 60) ignores a group for a window after a
**fill**. Rejects and misses deliberately do not start one — a miss means the
edge was gone, which is no reason to stop watching. Beware the interaction with
dust: a fill of 0.01 contracts starts the same 60s cooldown as a real one, so a
half-cent fill can mask a genuine edge on that group. Measured 2026-08-27, 5 of
496 groups were net-positive worth ~18c in total, three of them sized 0.01-0.03
contracts against off-market orders — so this is not hypothetical.

**The service must not import `arbys/backend/`.** `backend/state.py` imports
`ingest`, and `backend/ticket_service.py` imports `backend/state.py`, so the
reverse is a cycle. Submission and the cap pre-check are injected as callables
by `AppState`, whose own `ticket_service` imports sit inside the method bodies
for the same reason. That boundary is also why the service's tests need no
database.

Backpressure is a known, bounded limitation: `subscribe_opportunities` returns a
queue of `maxsize=100` and publishers drop with `put_nowait` under
`suppress(QueueFull)`, so a slow consumer loses opportunities with no error.
The service logs `auto-trade backpressure` above 50 queued. Processing is
serial on purpose — concurrent tickets would race on both the cash balance and
the position cap, and a lost race there is a real oversized position rather
than a missed trade.
```

- [ ] **Step 5: Add a RUNBOOK entry**

Add to `docs/RUNBOOK.md`, near the paper-trading operations:

```markdown
### Turning the auto-trader on

Set `ARBYS_ENABLE_AUTO_TRADE=1` in `.env` and restart the backend. There is no
UI toggle by design. It trades paper only — `PaperExecutionAdapter` is the only
`ExecutionAdapter` in the repo, and paper fills are atomic, so it cannot end up
holding one naked leg.

To see what it did:

    select source, status, count(*) from paper_ticket group by source, status;

`source='auto'` rows are the bot's. The `filled` / `missed` / `rejected` split
is the point: `missed` counts edges that died between publication and
submission, which is what decides whether latency work is worth anything.
Cross-check `GET /health` for `dropped_writes` first — non-zero makes every
count above a lower bound.

To stop it, set the flag back to 0 and restart. To clear its cooldowns without
a restart, reset the paper account (`POST /paper/{account_id}/reset`), which
calls `clear_cooldowns()`.
```

- [ ] **Step 6: Update the spec's status and its stale prediction**

In `docs/superpowers/specs/2026-08-23-auto-trader-design.md`, change `**Status:** approved, not yet implemented` to `**Status:** implemented 2026-08-27`. Then append to the **Trigger** section:

```markdown
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
```

- [ ] **Step 7: Final verification**

Run: `venv\Scripts\python.exe -m pytest -q -p no:warnings`
Expected: 358 tests, no failures.
Run: `venv\Scripts\python.exe -m ruff check .`
Expected: `All checks passed!`

Then delete the throwaway database and its WAL sidecars: `rm -f .superpowers/auto-trade-check.db*`

- [ ] **Step 8: Commit**

```bash
git add CLAUDE.md docs/RUNBOOK.md docs/superpowers/specs/2026-08-23-auto-trader-design.md
git commit -m "docs(auto-trade): document the service and correct the quiet-gate prediction"
```

---

## Self-review notes

**Spec coverage.** Every section of the spec maps to a task: trigger and "no
separate threshold" → Global Constraints plus Task 2's module docstring; service
shape modelled on `AutoSettleService` → Task 2; the five-step per-opportunity
sequence → `handle()`; cap pre-check and why it is not redundant → Task 2 Steps
1 and 5; cooldown semantics including "not started by a reject or a miss" →
Task 2 tests; both config flags with defaults → Task 1; backpressure warning
above qsize 50 → Task 2; all seven of the spec's listed test cases → Tasks 2 and
3; non-goals → Global Constraints. The spec's "Open questions: none blocking" is
answered by Task 4 Step 2, which is where the measurement actually starts.

**One deviation in mechanism, none in behaviour.** The spec says the service is
"owned by `AppState`" and "calls `submit_arb_ticket`". Taken literally that is a
circular import, because `arbys/ingest/` sits inward of `arbys/backend/` and
`ticket_service` imports `state` at module level. `AppState` still owns the
service and `submit_arb_ticket` is still the only submission path; the call
simply arrives as an injected callable. Flagged here because it is the one place
an implementer will find the spec and the codebase disagreeing.

**Two additions the spec did not ask for**, both small and both justified
inline: `clear_cooldowns()` on portfolio reset (mirrors
`AutoSettleService.clear_settled()`, which is already called there — without it
a reset leaves stale cooldowns suppressing a fresh account), and
`ARBYS_ENABLE_AUTO_TRADE=0` in `tests/conftest.py`'s `_OFFLINE_ENV` (the suite
loads the developer's `.env`, so without it a machine configured for live
trading would have its test runs writing auto tickets).

**Renaming `_cap_breach` to `cap_breach`** is preferred over a second
implementation inside the service. Two copies of one safety rule are free to
drift, and this is the rule that stops the bot stacking without bound.

**Test-count arithmetic:** 335 before, +5 (Task 1), +14 (Task 2), +4 (Task 3) =
358. If the actual number differs, recount rather than assuming a break — a
parametrised test may collect differently.
