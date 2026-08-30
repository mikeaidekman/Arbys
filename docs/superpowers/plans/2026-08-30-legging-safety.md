# Legging Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make it impossible for this system to hold an unintended naked leg silently. Build and prove the entire safety apparatus *before* anything exists that can place a real order.

**Scope, and the reason for it:** the [live-execution spec](../specs/2026-08-29-live-execution-design.md) has seven parts. This plan implements **A, C, D and F plus the health surfacing** — every one of which is fully testable against stub adapters with zero venue contact. It deliberately stops short of **Part B (the live venue adapters)** and **Part E (reconciliation)**, which are the only parts that can move real money and the only parts that cannot be tested here. Building the net before the thing that falls into it is the whole sequencing argument; a plan that did both at once would land untested order placement alongside its own untested safety rail.

**Architecture:** No new services and no new concepts. An `ExecutionMode` on the adapter replaces three `isinstance` checks, `_commit_sequentially` gains an unwind branch, and the ticket row records intent before the first leg leaves. The paper path is untouched throughout — if a paper test needs changing, that is a bug in this work.

**Tech Stack:** Python 3.11, asyncio, SQLAlchemy 2 async + aiosqlite, FastAPI, pytest (`asyncio_mode = "auto"`).

**Spec:** [docs/superpowers/specs/2026-08-29-live-execution-design.md](../specs/2026-08-29-live-execution-design.md)

## Why this is worth building now

Part G was measured on 2026-08-29/30 and cleared the spec's blocking question:

- Kalshi authenticated round trip **50ms median** (genuine `200`, p90 63ms).
- An **active** leg's ask changes every **13.8s** (Kalshi) / **15.5s** (Polymarket). Only 8% of legs moved at all in a 240s sample.
- Sequential legging window ≈ **81ms** → **P(legged) ≈ 0.29% per ticket on an active leg**, an order of magnitude lower on a quiet one.
- At that rate the 1-sigma P&L swing is **$17 against $169** of measured profit — about 10%.

So legging is a rarely-exercised path, not a hot one. That is exactly why it must be built carefully and tested synthetically: **it will almost never run, so it will almost never be exercised by accident, so it must be right the first time it matters.**

Caveat carried from the measurement: the sample was taken in a quiet period. 804 of the ledger's 2,306 tickets landed between 00:00–03:00 UTC with games in play, where both the active fraction and the tick rate will be higher. Treat 0.29% as a floor.

## Global Constraints

- **All money and all prices are `Decimal`. Never float.**
- Run everything from the repo root with `venv\Scripts\python.exe`, never a bare `python`.
- `venv\Scripts\python.exe -m pytest -q` must stay green — **386 tests** before this plan.
- `venv\Scripts\python.exe -m ruff check .` must stay clean. Re-run after each task, including after writing tests.
- mypy is **not** part of the green bar (71 errors across 24 files). Do not claim mypy clean; do not start a cleanup.
- **The paper path must not change behaviour.** `_commit_atomically` and every existing paper test are load-bearing regression cover. If one needs editing, stop — something is wrong.
- **Nothing in this plan may contact a venue.** No new HTTP client, no new credentials read, no live adapter class. `tests/conftest.py` already pins the venue switches off session-wide.
- **`ARBYS_ENABLE_LIVE_EXECUTION` stays `0` and nothing reads it to build an adapter.** It exists in this plan only so the flag's name and default are reviewable on their own, the same way `ARBYS_ENABLE_AUTO_TRADE` was.
- Do not touch the developer's `arbys-local.db`. Tests use `tmp_path` databases via the `_fresh_state` fixture pattern in `tests/test_ticket_service.py`.

## File Structure

**Created:**

| File | Responsibility |
| --- | --- |
| `arbys/adapters/kalshi_auth.py` | RSA-PSS signing generalised over `(method, path)`, moved out of the WS module. |
| `tests/shared/test_unwind.py` | The unwind branch, driven entirely by stub adapters in `LIVE` mode. |
| `tests/adapters/test_kalshi_auth.py` | Signing over an arbitrary method and path. |

**Modified:**

| File | Change |
| --- | --- |
| `arbys/adapters/base.py` | `ExecutionMode` enum; `mode` on `ExecutionAdapter`; `UnwindFailed`. |
| `arbys/shared/paper_broker.py` | `PaperExecutionAdapter.mode = PAPER`. |
| `arbys/shared/execution_router.py` | Replace three `isinstance` checks; refuse mixed tickets; unwind branch. |
| `arbys/adapters/kalshi_ws.py` | Import signing from `kalshi_auth`; keep the WS call site passing `method="GET"`. |
| `arbys/backend/state.py` | `live_execution_enabled()`, `order_ack_timeout_s()`; unwind-failure counter. |
| `arbys/backend/ticket_service.py` | Write `status="executing"` before the router runs; boot-time scan. |
| `arbys/backend/app.py` | `/health` reports `unwind_failures` and `stuck_executing`. |
| `.env.example`, `CLAUDE.md`, `docs/RUNBOOK.md` | The flags and what they mean. |

---

### Task 1: Config surface

Two env helpers beside the existing ones. Nothing reads them to build anything — this task exists separately because the naming and the defaults are the part worth rejecting on their own.

**Files:**
- Modify: `arbys/backend/state.py` (beside `_auto_trade_enabled`)
- Modify: `.env.example`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `live_execution_enabled() -> bool`, `order_ack_timeout_s() -> float`, `DEFAULT_ORDER_ACK_TIMEOUT_S`.

- [ ] **Step 1: Write the failing tests**

```python
def test_live_execution_is_off_by_default(monkeypatch):
    monkeypatch.delenv("ARBYS_ENABLE_LIVE_EXECUTION", raising=False)
    assert state_module.live_execution_enabled() is False


def test_live_execution_enabled_only_by_exactly_one(monkeypatch):
    monkeypatch.setenv("ARBYS_ENABLE_LIVE_EXECUTION", "1")
    assert state_module.live_execution_enabled() is True
    for value in ("0", "true", "yes", "", "2"):
        monkeypatch.setenv("ARBYS_ENABLE_LIVE_EXECUTION", value)
        assert state_module.live_execution_enabled() is False


def test_order_ack_timeout_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv("ARBYS_ORDER_ACK_TIMEOUT_S", raising=False)
    assert state_module.order_ack_timeout_s() == 2.0
    monkeypatch.setenv("ARBYS_ORDER_ACK_TIMEOUT_S", "garbage")
    assert state_module.order_ack_timeout_s() == 2.0
    # 0 would mean "give up before asking", which is never what anyone wants.
    monkeypatch.setenv("ARBYS_ORDER_ACK_TIMEOUT_S", "0")
    assert state_module.order_ack_timeout_s() == 2.0
```

- [ ] **Step 2: Run and watch them fail** — `AttributeError`.

- [ ] **Step 3: Add the helpers.** `DEFAULT_ORDER_ACK_TIMEOUT_S = 2.0` — 25x the measured 81ms window, generous enough that a slow venue is not mistaken for a rejection, tight enough that a wedged order does not hold a naked leg open. Document that number's provenance in the docstring; it is the only place the Part G measurement enters the code.

- [ ] **Step 4: Green, ruff clean, commit.**

---

### Task 2: An explicit execution mode, and no mixed tickets

The router currently decides everything on `isinstance(adapter, PaperExecutionAdapter)` at `execution_router.py:77`, `:104`, `:122` and `:135`. The moment one live adapter exists, a group with one paper and one live adapter silently takes the **sequential** path — the unsafe one — with no announcement.

**Files:**
- Modify: `arbys/adapters/base.py`, `arbys/shared/paper_broker.py`, `arbys/shared/execution_router.py`
- Test: `tests/shared/test_execution_router.py`

**Interfaces:**
- Produces: `ExecutionMode` (`PAPER` / `LIVE`), `ExecutionAdapter.mode`. Task 4 branches on it.

- [ ] **Step 1: Write the failing tests**

```python
def _stub(mode, venue="live_a"):
    """Minimal ExecutionAdapter that records calls and never touches a venue."""
    ...


async def test_a_mixed_paper_and_live_ticket_is_refused_before_anything_is_placed():
    """A half-simulated ticket is not a hedge. It is the single most dangerous
    state the router can reach: it looks filled and is naked by construction.

    The assertion that matters is that the stub was NEVER CALLED — refusing
    after placing one leg would be the bug wearing a different hat."""
    paper, live = _stub(ExecutionMode.PAPER, "paper_v"), _stub(ExecutionMode.LIVE, "live_v")
    router = ExecutionRouter({"paper_v": paper, "live_v": live})
    with pytest.raises(InsufficientLegsError) as e:
        await router.submit(_intent_across("paper_v", "live_v"))
    assert "mixed" in str(e.value).lower()
    assert live.placed == [] and paper.placed == []


async def test_all_paper_still_takes_the_atomic_path():
    """Regression: the existing paper behaviour is unchanged."""
```

- [ ] **Step 2: Run and watch them fail.**

- [ ] **Step 3: Implement.** `ExecutionMode` in `base.py`; `mode: ExecutionMode` as a class attribute on the ABC with no default (so a future adapter cannot forget it); `PaperExecutionAdapter.mode = ExecutionMode.PAPER`. Replace all four `isinstance` sites. The preview phase at `:77` keeps its `isinstance` — it reaches into `_preview_fill`, which is genuinely paper-only API, not a mode question. Say so in a comment, or the next reader will "finish" the refactor and break it.

- [ ] **Step 4: Green (all 386 + new), ruff clean, commit.**

---

### Task 3: Generalise Kalshi signing

`kalshi_ws._auth_headers` hardcodes `"GET"` and defaults `path` to the WS signing path. A REST order is a POST, and it should not have to import the WebSocket module to sign one.

**Files:**
- Create: `arbys/adapters/kalshi_auth.py`, `tests/adapters/test_kalshi_auth.py`
- Modify: `arbys/adapters/kalshi_ws.py`

- [ ] **Step 1: Write the failing tests** — signature verifies against the public key for `("POST", "/trade-api/v2/portfolio/orders")`, and the signed message is `f"{ts}{METHOD}{path}"` with the method uppercased. Mirror `tests/adapters/test_polymarket_us_auth.py`, which already pins the same shape for Ed25519.

- [ ] **Step 2: Run and watch them fail** — module does not exist.

- [ ] **Step 3: Move `_sign_pss`, `_auth_headers`, `load_kalshi_private_key` into `kalshi_auth.py`**, with `_auth_headers(key_id, private_key, *, method, path)`. Re-export from `kalshi_ws` so no other import site changes; the WS call site passes `method="GET"` explicitly.

- [ ] **Step 4: The existing WS handshake test must pass untouched.** It signs `GET /trade-api/ws/v2`; if it needs editing, the move changed behaviour. Green, ruff, commit.

---

### Task 4: Unwind immediately

The heart of the plan. `_commit_sequentially` currently raises `N leg(s) already filled and NOT reversed` and leaves the position open.

**Files:**
- Modify: `arbys/shared/execution_router.py`, `arbys/adapters/base.py`
- Test: `tests/shared/test_unwind.py`

**Interfaces:**
- Produces: `UnwindFailed(Exception)` carrying the legs it could not close. Task 5 counts it.

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_failed_second_leg_sells_the_first_one_back():
    """Held to settlement a naked binary is mean-zero but carries a standard
    deviation many times the ticket's edge. Unwound at once the cost is the
    spread plus fees."""
    # leg 1 fills, leg 2 rejects -> expect a SELL of leg 1 at MARKET_SELL_LIMIT
    assert stub.placed[-1].is_buy is False
    assert stub.placed[-1].qty == Decimal("100")
    assert stub.placed[-1].limit_price == MARKET_SELL_LIMIT


async def test_a_partial_fill_is_a_legging_event_and_unwinds_the_shortfall():
    """60 of 100 filled means 40 naked, not a success. The unwind is sized to
    what actually filled, read from get_fills — Order carries the REQUESTED
    qty, so trusting order.qty would sell back size we never owned."""


async def test_when_the_unwind_itself_fails_it_raises_UnwindFailed():
    """The one state no code can repair: an open, unintended, one-sided
    position on a live venue. It must not hide inside InsufficientLegsError,
    which callers already treat as 'the ticket did not happen'."""


async def test_the_unwind_is_recorded_even_when_it_fails():
    """A failed unwind is the most important row in the audit log."""


async def test_paper_tickets_never_reach_the_unwind_path():
    """Regression guard on the mode split from Task 2."""
```

- [ ] **Step 2: Run and watch them fail.**

- [ ] **Step 3: Implement.** In the failure branch, for each already-filled leg, `place_order(is_buy=not leg.is_buy, qty=<filled>, limit_price=MARKET_SELL_LIMIT)`.

  `MARKET_SELL_LIMIT = Decimal("0")` lives in `base.py` with its reasoning attached: for a binary, a sell accepting any price at or above zero **is** a market sell, which needs no interface change — but a bare `0` at the call site reads as a bug and someone will "fix" it.

  Filled quantity comes from `get_fills(order.id)`, never `order.qty`: `Order` carries the *requested* size and `OrderStatus.PARTIAL` exists, so `order.qty` on a partial would sell back size never owned.

- [ ] **Step 4: Green, ruff clean, commit.**

---

### Task 5: A failed unwind has to shout

**Files:**
- Modify: `arbys/backend/state.py`, `arbys/backend/app.py`, `arbys/backend/ticket_service.py`
- Test: `tests/test_backend_e2e.py`

- [ ] **Step 1: Write the failing test** — `/health` exposes `unwind_failures` and `last_unwind_failure`, both quiet at zero, and a simulated `UnwindFailed` increments them.

- [ ] **Step 2: Run and watch it fail.**

- [ ] **Step 3: Implement**, mirroring `dropped_write_stats()` exactly — same shape, same reasoning, same place on `/health`. The precedent is deliberate: the 2026-08-25 incident established that **a swallowed failure indistinguishable from success is the thing that costs data**, and this is the same failure mode with money attached.

- [ ] **Step 4: Green, ruff, commit.**

---

### Task 6: Record intent before placing

If the process dies between leg 1 and leg 2, leg 1 is filled at the venue and nothing records that a leg 2 was ever intended. On restart it looks like a deliberate one-sided bet. The hosting spec makes restarts routine, so this is reachable rather than theoretical.

**Files:**
- Modify: `arbys/backend/ticket_service.py`, `arbys/backend/state.py`, `arbys/backend/app.py`
- Test: `tests/test_ticket_service.py`

- [ ] **Step 1: Write the failing tests** — a ticket reaching the router is written `status="executing"` with its full leg set before any `place_order`; `bootstrap()` logs and counts tickets left at `executing`; **it never auto-resumes one.**

- [ ] **Step 2: Run and watch them fail.**

- [ ] **Step 3: Implement.** `executing` slots between `pending` and the terminal states. On boot, count them onto `/health` as `stuck_executing` and log each loudly. Resuming is explicitly not done: a ticket whose age you cannot establish might have had its first leg settle hours ago, and buying the second leg of *that* is worse than the naked position you started with.

- [ ] **Step 4: Green, ruff, commit.**

---

### Task 7: Documentation

- [ ] Update `CLAUDE.md` with the mode split, the unwind, `MARKET_SELL_LIMIT`, and the `executing` status.
- [ ] Update `docs/RUNBOOK.md` with what `unwind_failures` and `stuck_executing` mean and what to do about each — both are "stop the bot and look", not "wait and see".
- [ ] Mark the spec's Parts A, C, D, F implemented; leave B and E proposed.
- [ ] Correct the test count in `CLAUDE.md`.

---

## What this plan deliberately leaves undone

**Part B — the live venue adapters.** Nothing here can place a real order, and that is the point. When they are built they will need their own plan, their own venue-contact testing, and a separate decision to fund the account.

**Part E — reconciliation against the venue.** Only meaningful once a venue holds a position we did not simulate.

**Turning any of it on.** `ARBYS_ENABLE_LIVE_EXECUTION` stays `0`. After this plan the safety apparatus exists, is tested, and is unreachable — which is the correct state for it to be in until there is something to protect.
