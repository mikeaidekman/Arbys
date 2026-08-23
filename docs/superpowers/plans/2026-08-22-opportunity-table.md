# Truthful Capacity and Dense Opportunity Table — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the engine size opportunities to what is actually tradeable — capped at $200 per ticket — make the paper broker refuse orders larger than the resting book, expose net-of-fee figures on `/monitored`, and replace the terminal's card grid with a dense one-row-per-event table.

**Architecture:** Detection stays a pure per-contract test (`unit_cost < 1`), independent of size. Sizing becomes a separate pure step in `shared/sizing.py` that takes the per-leg depths and a stake budget and returns a contract count. The broker gains a backstop rejection for when the book moves between detection and execution. `/monitored` gains four computed fields so the frontend can render net truth for every row without a fee model of its own; the frontend becomes a thin renderer.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, SQLAlchemy 2 async, pytest (`asyncio_mode = "auto"`); Vite + React 19 + TypeScript, TanStack Query, oxlint.

**Spec:** [docs/superpowers/specs/2026-08-22-opportunity-table-design.md](../specs/2026-08-22-opportunity-table-design.md)

## Global Constraints

- **All money and all prices are `Decimal`. Never float.** Prices are probabilities in `[0, 1]`.
- Run everything from the repo root with `venv\Scripts\python.exe`, never a bare `python`.
- Backend bar: `venv\Scripts\python.exe -m pytest -q` (203 tests today, must stay green) and `venv\Scripts\python.exe -m ruff check .` clean. **mypy is not part of the bar** — it has 47 pre-existing errors; do not attempt a cleanup.
- Frontend bar: `npm run lint` (oxlint, not eslint) and `npm run build` (`tsc -b && vite build` — the real typecheck). There is **no frontend test runner**; do not add one.
- `arbys/shared/` is pure domain: **no `httpx`, no SQLAlchemy, no FastAPI imports.**
- Domain types are `@dataclass(frozen=True)`; enums are `StrEnum`.
- **Size has three states, everywhere:** `None` = unknown (fills), `0` = known empty (rejects), `> 0` = real quantity. Never conflate `None` and `0`.
- Frontend styling comes from the industry design system's semantic classes and CSS custom properties. **No new hex colors, radii, or type scales.** No dark mode.
- Tests never hit a real venue.
- `ARBYS_MAX_TICKET_STAKE` default is **`200`**; `0` disables the budget cap.
- Commit after every task.

---

## File Structure

**Backend**

| file | responsibility |
| --- | --- |
| `arbys/shared/arb_engine.py` | detection only; per-contract gate, delegates sizing |
| `arbys/shared/qty.py` | **new leaf module** — `tradeable_qty`, tick rounding; imports nothing from the package |
| `arbys/shared/sizing.py` | bankroll/stake scaling; re-exports from `qty` |
| `arbys/shared/paper_broker.py` | `_preview_fill` gains the qty-vs-resting backstop |
| `arbys/backend/state.py` | `max_ticket_stake()` config reader |
| `arbys/backend/schemas.py` | four new `MonitoredGroupOut` fields |
| `arbys/backend/app.py` | `/monitored` computes them |
| `arbys/ingest/engine_runtime.py` | passes the stake cap instead of `target_payoff` |
| `arbys/backtest/__init__.py` | caller update only |

**Frontend**

| file | responsibility |
| --- | --- |
| `frontend/src/api/types.ts` | the four new fields |
| `frontend/src/lib/combo.ts` | `bestPair()`, `splitTitle()` |
| `frontend/src/components/OpportunityRow.tsx` | one row + its execute mutation |
| `frontend/src/components/OpportunityTable.tsx` | header, sort state, row mapping |
| `frontend/src/pages/TerminalPage.tsx` | renders the table |
| `frontend/src/index.css` | compact density + row stripe |
| `frontend/src/components/OpportunityCard.tsx` | **deleted** |

---

# Part A — Truthful sizing (backend)

Part A must be complete and green before Part B starts; Part B consumes A's API fields.

---

### Task 1: Public per-contract cost helpers

`_leg_unit_cost` is private but both the engine and `/monitored` need it. Promote it and add the net-edge helper on top.

**Files:**
- Modify: `arbys/shared/arb_engine.py:55-68`
- Test: `tests/shared/test_fees.py`

**Interfaces:**
- Consumes: `FeeModel` protocol from `arbys/shared/fees.py` — `fee(*, price, qty, is_buy) -> Decimal`
- Produces:
  - `leg_unit_cost(ask: Decimal, fee_model: FeeModel, *, is_buy: bool = True) -> Decimal`
  - `net_edge_per_contract(unit_costs: Iterable[Decimal]) -> Decimal`

- [ ] **Step 1: Write the failing test**

Append to `tests/shared/test_fees.py`:

```python
from decimal import Decimal

from arbys.shared.arb_engine import leg_unit_cost, net_edge_per_contract
from arbys.shared.fees import KalshiFeeModel, PolymarketUsFeeModel


def test_leg_unit_cost_includes_per_unit_fee():
    # Kalshi taker fee is 0.07 * p * (1-p): 0.07 * 0.47 * 0.53 = 0.017437
    cost = leg_unit_cost(Decimal("0.47"), KalshiFeeModel(), is_buy=True)
    assert cost > Decimal("0.47")
    assert cost == Decimal("0.47") + KalshiFeeModel().fee(
        price=Decimal("0.47"), qty=Decimal("1"), is_buy=True
    )


def test_net_edge_per_contract_is_one_minus_total_cost():
    k = leg_unit_cost(Decimal("0.47"), KalshiFeeModel())
    p = leg_unit_cost(Decimal("0.525"), PolymarketUsFeeModel())
    edge = net_edge_per_contract([k, p])
    assert edge == Decimal("1") - (k + p)
    # Measured 2026-08-22 on nfl-GB-MIN: gross +0.5c, net negative.
    assert edge < Decimal("0")


def test_net_edge_positive_when_legs_are_cheap():
    edge = net_edge_per_contract([Decimal("0.40"), Decimal("0.50")])
    assert edge == Decimal("0.10")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/shared/test_fees.py -k "unit_cost or net_edge" -v`
Expected: FAIL — `ImportError: cannot import name 'leg_unit_cost'`

- [ ] **Step 3: Write minimal implementation**

In `arbys/shared/arb_engine.py`, add `Iterable` to the imports and replace the private helper:

```python
from collections.abc import Iterable


def leg_unit_cost(
    ask: Decimal,
    fee_model: FeeModel,
    *,
    is_buy: bool = True,
) -> Decimal:
    """Cost per 1 contract, including per-unit fees.

    Uses qty=1 to get an average fee-per-unit at this price. Because our fee
    models are linear in qty, this is equivalent to (fees(N) / N) for any N > 0.
    """
    fee = fee_model.fee(price=ask, qty=Decimal("1"), is_buy=is_buy)
    return ask + fee


def net_edge_per_contract(unit_costs: Iterable[Decimal]) -> Decimal:
    """Guaranteed profit per contract after fees. Positive means an arb.

    Size-independent: this is the whole arb test. Exactly one leg of a
    complete ticket settles at 1, so the edge is 1 minus what the ticket costs.
    """
    return Decimal("1") - sum(unit_costs, Decimal("0"))
```

Then update the two existing internal call sites in `detect_cross_venue_two_leg` from `_leg_unit_cost(...)` to `leg_unit_cost(...)` — there are exactly two, both assigning `y_unit` and `n_unit`. Do **not** leave a `_leg_unit_cost = leg_unit_cost` alias behind; nothing else references it and it would be dead code by Task 4.

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv\Scripts\python.exe -m pytest tests/shared/ -q`
Expected: PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add arbys/shared/arb_engine.py tests/shared/test_fees.py
git commit -m "feat(shared): public per-contract cost and net-edge helpers"
```

---

### Task 2: `tradeable_qty` — depth ceiling and stake budget

**Files:**
- Create: `arbys/shared/qty.py`
- Modify: `arbys/shared/sizing.py` (re-export only)
- Test: `tests/shared/test_qty.py`

**Interfaces:**
- Produces, in the **new leaf module** `arbys/shared/qty.py`: `tradeable_qty(*, unit_cost: Decimal, depths: Sequence[Decimal | None], max_stake: Decimal | None, tick: Decimal = Decimal("0")) -> Decimal` and `LEGACY_UNBOUNDED_QTY: Decimal` (value `Decimal("100")`)
- **Why a new module, not `sizing.py`:** `sizing.py` imports `ArbLeg`/`ArbOpportunity` from `arb_engine`, and Task 4 needs `arb_engine` to import `tradeable_qty`. Putting it in `sizing.py` would make that circular. `qty.py` depends on nothing in the package, so both can import it.
- `_round_down_tick` currently lives in `sizing.py`. Move it to `qty.py` and have `sizing.py` import it back, so tick rounding has one definition.

- [ ] **Step 1: Write the failing test**

Create `tests/shared/test_qty.py`:

```python
from decimal import Decimal

from arbys.shared.qty import LEGACY_UNBOUNDED_QTY, tradeable_qty


def test_tradeable_qty_capped_by_thinnest_leg():
    # $200 budget would allow ~200 contracts, but one leg has only 3 resting.
    qty = tradeable_qty(
        unit_cost=Decimal("0.99"),
        depths=[Decimal("412"), Decimal("3")],
        max_stake=Decimal("200"),
    )
    assert qty == Decimal("3")


def test_tradeable_qty_capped_by_stake_budget():
    qty = tradeable_qty(
        unit_cost=Decimal("1.00"),
        depths=[Decimal("5000"), Decimal("5000")],
        max_stake=Decimal("200"),
    )
    assert qty == Decimal("200")


def test_known_empty_leg_blocks_entirely():
    # 0 is *known empty*, not unknown. Nothing is tradeable at any budget.
    qty = tradeable_qty(
        unit_cost=Decimal("0.98"),
        depths=[Decimal("1000"), Decimal("0")],
        max_stake=Decimal("200"),
    )
    assert qty == Decimal("0")


def test_unknown_depth_imposes_no_ceiling():
    # None = the venue did not report depth. POST /quotes omits sizes entirely,
    # so treating None as 0 would silence every hand-pushed quote.
    qty = tradeable_qty(
        unit_cost=Decimal("1.00"),
        depths=[None, None],
        max_stake=Decimal("200"),
    )
    assert qty == Decimal("200")


def test_mixed_known_and_unknown_uses_the_known_one():
    qty = tradeable_qty(
        unit_cost=Decimal("0.50"),
        depths=[None, Decimal("17")],
        max_stake=Decimal("200"),
    )
    assert qty == Decimal("17")


def test_disabled_budget_and_unknown_depth_falls_back_to_legacy():
    # ARBYS_MAX_TICKET_STAKE=0 disables the budget cap. With no depth known
    # either, there is no ceiling at all, so reproduce today's flat sizing
    # rather than emitting an unbounded ticket.
    qty = tradeable_qty(
        unit_cost=Decimal("0.98"),
        depths=[None, None],
        max_stake=None,
    )
    assert qty == LEGACY_UNBOUNDED_QTY == Decimal("100")


def test_tick_floors_the_result():
    qty = tradeable_qty(
        unit_cost=Decimal("1.00"),
        depths=[Decimal("7.6"), None],
        max_stake=Decimal("200"),
        tick=Decimal("1"),
    )
    assert qty == Decimal("7")


def test_zero_unit_cost_does_not_divide_by_zero():
    qty = tradeable_qty(
        unit_cost=Decimal("0"),
        depths=[Decimal("42")],
        max_stake=Decimal("200"),
    )
    assert qty == Decimal("42")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/shared/test_sizing.py -k tradeable -v`
Expected: FAIL — `ImportError: cannot import name 'tradeable_qty'`

- [ ] **Step 3: Write minimal implementation**

Create `arbys/shared/qty.py`. It must import nothing from the rest of the package — that is what keeps it importable from both `arb_engine` and `sizing`:

```python
"""Contract-count arithmetic: how much of an arb is actually tradeable.

A leaf module on purpose. `sizing.py` imports ArbOpportunity from
`arb_engine`, and `arb_engine` needs `tradeable_qty`, so this cannot live in
`sizing.py` without a cycle.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_DOWN, Decimal


def _round_down_tick(value: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return value
    return (value / tick).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick

# Retained only for the case where the stake budget is disabled *and* no leg
# reports depth. Without it there would be no ceiling at all. This is the old
# DEFAULT_TARGET_PAYOFF, so disabling the budget reproduces prior behaviour.
LEGACY_UNBOUNDED_QTY = Decimal("100")


def tradeable_qty(
    *,
    unit_cost: Decimal,
    depths: Sequence[Decimal | None],
    max_stake: Decimal | None,
    tick: Decimal = Decimal("0"),
) -> Decimal:
    """Contracts actually tradeable at the quoted prices.

    `unit_cost` is the all-in cost of one contract across every leg (asks plus
    per-unit fees). `depths` is each leg's resting size at its quoted price,
    under the project's three-state rule. `max_stake` caps total capital
    deployed; None disables that cap.

    The order of the depth checks is the whole point:

      * an explicit 0 on any leg means *known empty* -> nothing is tradeable,
      * None means *unknown* -> that leg imposes no ceiling.

    Treating None as 0 would silence every opportunity built from POST /quotes,
    which omits sizes entirely. Treating 0 as unknown would size tickets
    against an empty book.
    """
    if any(d is not None and d <= 0 for d in depths):
        return Decimal("0")

    known = [d for d in depths if d is not None]
    qty: Decimal | None = min(known) if known else None

    if max_stake is not None and max_stake > 0 and unit_cost > 0:
        budget_qty = max_stake / unit_cost
        qty = budget_qty if qty is None else min(qty, budget_qty)

    if qty is None:
        qty = LEGACY_UNBOUNDED_QTY

    if tick > 0:
        qty = _round_down_tick(qty, tick)
    return qty if qty > 0 else Decimal("0")
```

- [ ] **Step 4: Point `sizing.py` at the moved helper**

In `arbys/shared/sizing.py`, delete its local `_round_down_tick` definition and import from the new module instead, re-exporting the new names so existing importers of `sizing` keep working:

```python
from .qty import LEGACY_UNBOUNDED_QTY, _round_down_tick, tradeable_qty  # noqa: F401
```

`size_to_bankroll` already calls `_round_down_tick`, so it now uses the shared one. Nothing else in `sizing.py` changes.

- [ ] **Step 5: Run tests to verify they pass**

Run: `venv\Scripts\python.exe -m pytest tests/shared/test_qty.py tests/shared/test_sizing.py -q`
Expected: PASS — the new `tradeable_qty` tests plus the pre-existing `size_to_bankroll` / `size_to_max_stake` tests, which must still pass unchanged.

- [ ] **Step 6: Commit**

```bash
git add arbys/shared/qty.py arbys/shared/sizing.py tests/shared/test_qty.py
git commit -m "feat(shared): tradeable_qty caps sizing by depth and stake budget"
```

---

### Task 3: `ARBYS_MAX_TICKET_STAKE` config

**Files:**
- Modify: `arbys/backend/state.py` (after `max_outcome_qty`, ~line 141)
- Modify: `.env.example`
- Test: `tests/test_config.py` (create if absent)

**Interfaces:**
- Produces: `max_ticket_stake() -> Decimal | None` in `arbys.backend.state`; `DEFAULT_MAX_TICKET_STAKE: Decimal` (value `Decimal("200")`)

- [ ] **Step 1: Write the failing test**

Create or append `tests/test_config.py`:

```python
from decimal import Decimal

import pytest

from arbys.backend.state import DEFAULT_MAX_TICKET_STAKE, max_ticket_stake


def test_default_is_200(monkeypatch):
    monkeypatch.delenv("ARBYS_MAX_TICKET_STAKE", raising=False)
    assert max_ticket_stake() == Decimal("200")
    assert DEFAULT_MAX_TICKET_STAKE == Decimal("200")


def test_explicit_value_is_honoured(monkeypatch):
    monkeypatch.setenv("ARBYS_MAX_TICKET_STAKE", "50")
    assert max_ticket_stake() == Decimal("50")


def test_zero_disables_the_cap(monkeypatch):
    monkeypatch.setenv("ARBYS_MAX_TICKET_STAKE", "0")
    assert max_ticket_stake() is None


@pytest.mark.parametrize("bad", ["", "abc", "1.2.3"])
def test_garbage_falls_back_to_default(monkeypatch, bad):
    monkeypatch.setenv("ARBYS_MAX_TICKET_STAKE", bad)
    assert max_ticket_stake() == Decimal("200")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/test_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'max_ticket_stake'`

- [ ] **Step 3: Write minimal implementation**

In `arbys/backend/state.py`, immediately after `max_outcome_qty`:

```python
DEFAULT_MAX_TICKET_STAKE = Decimal("200")


def max_ticket_stake() -> Decimal | None:
    """Cap on total capital in one arb ticket. ``None`` disables it.

    Sizing is depth-driven, and some books are enormous — a single Polymarket
    US level has shown 419,882 contracts resting. Without a budget cap one
    ticket would consume the whole book. At ~$1.00 all-in per contract pair,
    the $200 default is roughly 198 contracts.

    This does **not** replace ``ARBYS_MAX_OUTCOME_QTY``: that caps cumulative
    open units per outcome per account and is enforced at execute time in
    ``app.py``, whereas this caps one ticket at detection time. Both apply.

    Set ARBYS_MAX_TICKET_STAKE=0 to turn it off.
    """
    raw = os.environ.get("ARBYS_MAX_TICKET_STAKE")
    if raw is None:
        return DEFAULT_MAX_TICKET_STAKE
    try:
        value = Decimal(raw)
    except (ArithmeticError, ValueError):
        return DEFAULT_MAX_TICKET_STAKE
    return None if value <= 0 else value
```

Add to `.env.example`, near `ARBYS_MAX_OUTCOME_QTY`:

```
# Max total capital in a single arb ticket, in dollars. Sizing is depth-driven,
# so without this one ticket could consume an entire book. 0 disables.
ARBYS_MAX_TICKET_STAKE=200
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv\Scripts\python.exe -m pytest tests/test_config.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add arbys/backend/state.py .env.example tests/test_config.py
git commit -m "feat(config): ARBYS_MAX_TICKET_STAKE, default 200"
```

---

### Task 4: Depth-aware `detect_cross_venue_two_leg`, explicit gate

The existing gate compares a per-contract cost against a total payoff (`total_unit_cost >= target_payoff` with `target_payoff=100`), so it never fires. The downstream `profit <= 0` check happens to reduce to the correct test. Make the gate explicit and size from depth.

**Files:**
- Modify: `arbys/shared/arb_engine.py:70-152`
- Modify: `arbys/ingest/engine_runtime.py:31,42,48,85`
- Modify: `arbys/backtest/__init__.py:40,65`
- Test: `tests/shared/test_arb_engine.py`

**Interfaces:**
- Consumes: `tradeable_qty`, `LEGACY_UNBOUNDED_QTY` (Task 2); `leg_unit_cost`, `net_edge_per_contract` (Task 1)
- Produces: `detect_cross_venue_two_leg(event_group, quotes, fees, *, max_ticket_stake: Decimal | None = None, tick_by_venue: dict[str, Decimal] | None = None) -> ArbOpportunity | None`. **The `target_payoff` parameter is removed.**

- [ ] **Step 1: Write the failing test**

Append to `tests/shared/test_arb_engine.py`:

```python
from decimal import Decimal

from arbys.shared.arb_engine import detect_cross_venue_two_leg
from arbys.shared.fees import FeeModelRegistry, ZeroFeeModel
from arbys.shared.types import EventGroup, EventGroupLeg, Quote


def _group() -> EventGroup:
    return EventGroup(
        id="eg",
        title="A vs B",
        legs=(
            EventGroupLeg(outcome_id="y", venue_id="v1", is_yes_side=True),
            EventGroupLeg(outcome_id="n", venue_id="v2", is_yes_side=False),
        ),
    )


def _fees() -> FeeModelRegistry:
    # FeeModelRegistry is just dict[str, FeeModel], and every fee model is a
    # frozen dataclass whose first field is venue_id. There is no register().
    return {"v1": ZeroFeeModel("v1"), "v2": ZeroFeeModel("v2")}


def test_sizes_to_the_thinnest_leg():
    quotes = {
        "y": Quote(outcome_id="y", bid=Decimal("0.40"), ask=Decimal("0.45"),
                   ask_size=Decimal("1000")),
        "n": Quote(outcome_id="n", bid=Decimal("0.45"), ask=Decimal("0.50"),
                   ask_size=Decimal("7")),
    }
    opp = detect_cross_venue_two_leg(
        _group(), quotes, _fees(), max_ticket_stake=Decimal("200")
    )
    assert opp is not None
    assert all(leg.qty == Decimal("7") for leg in opp.legs)
    # 7 contracts * 0.05 edge
    assert opp.guaranteed_profit == Decimal("0.35")


def test_stake_budget_caps_a_deep_book():
    quotes = {
        "y": Quote(outcome_id="y", bid=Decimal("0.40"), ask=Decimal("0.45"),
                   ask_size=Decimal("100000")),
        "n": Quote(outcome_id="n", bid=Decimal("0.45"), ask=Decimal("0.50"),
                   ask_size=Decimal("100000")),
    }
    opp = detect_cross_venue_two_leg(
        _group(), quotes, _fees(), max_ticket_stake=Decimal("95")
    )
    assert opp is not None
    # unit cost 0.95, budget 95 -> 100 contracts
    assert all(leg.qty == Decimal("100") for leg in opp.legs)


def test_known_empty_leg_yields_no_opportunity():
    quotes = {
        "y": Quote(outcome_id="y", bid=Decimal("0.40"), ask=Decimal("0.45"),
                   ask_size=Decimal("1000")),
        "n": Quote(outcome_id="n", bid=Decimal("0.45"), ask=Decimal("0.50"),
                   ask_size=Decimal("0")),
    }
    assert detect_cross_venue_two_leg(
        _group(), quotes, _fees(), max_ticket_stake=Decimal("200")
    ) is None


def test_unknown_depth_still_produces_an_opportunity():
    # Hand-pushed quotes via POST /quotes carry no sizes at all.
    quotes = {
        "y": Quote(outcome_id="y", bid=Decimal("0.40"), ask=Decimal("0.45")),
        "n": Quote(outcome_id="n", bid=Decimal("0.45"), ask=Decimal("0.50")),
    }
    opp = detect_cross_venue_two_leg(
        _group(), quotes, _fees(), max_ticket_stake=Decimal("200")
    )
    assert opp is not None
    assert all(leg.qty > 0 for leg in opp.legs)


def test_no_edge_is_rejected_regardless_of_size():
    # The gate is per-contract and size-independent: 0.55 + 0.50 > 1.
    quotes = {
        "y": Quote(outcome_id="y", bid=Decimal("0.50"), ask=Decimal("0.55"),
                   ask_size=Decimal("1000")),
        "n": Quote(outcome_id="n", bid=Decimal("0.45"), ask=Decimal("0.50"),
                   ask_size=Decimal("1000")),
    }
    assert detect_cross_venue_two_leg(
        _group(), quotes, _fees(), max_ticket_stake=Decimal("200")
    ) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/shared/test_arb_engine.py -k "thinnest or budget or empty_leg or unknown_depth or regardless" -v`
Expected: FAIL — `TypeError: detect_cross_venue_two_leg() got an unexpected keyword argument 'max_ticket_stake'`

- [ ] **Step 3: Write minimal implementation**

Replace the body of `detect_cross_venue_two_leg` in `arbys/shared/arb_engine.py`:

```python
def detect_cross_venue_two_leg(
    event_group: EventGroup,
    quotes: dict[str, Quote],
    fees: FeeModelRegistry,
    *,
    max_ticket_stake: Decimal | None = None,
    tick_by_venue: dict[str, Decimal] | None = None,
) -> ArbOpportunity | None:
    """Detect the most profitable YES-leg + NO-leg cross-venue arb for a group.

    The arb test is per-contract and size-independent: if the all-in cost of
    one contract on each side is under 1, the pair is an arb. Sizing is then a
    separate step bounded by book depth and `max_ticket_stake`.
    """
    tick_by_venue = tick_by_venue or {}
    yes_legs = [leg for leg in event_group.legs if leg.is_yes_side]
    no_legs = [leg for leg in event_group.legs if not leg.is_yes_side]
    if not yes_legs or not no_legs:
        return None

    best: ArbOpportunity | None = None
    for y in yes_legs:
        yq = quotes.get(y.outcome_id)
        if yq is None:
            continue
        y_fee_model = fees.get(y.venue_id)
        if y_fee_model is None:
            continue
        y_unit = leg_unit_cost(yq.ask, y_fee_model, is_buy=True)

        for n in no_legs:
            nq = quotes.get(n.outcome_id)
            if nq is None:
                continue
            n_fee_model = fees.get(n.venue_id)
            if n_fee_model is None:
                continue
            n_unit = leg_unit_cost(nq.ask, n_fee_model, is_buy=True)

            unit_cost = y_unit + n_unit
            # Per contract, and nothing to do with sizing.
            if net_edge_per_contract([y_unit, n_unit]) <= 0:
                continue

            tick = max(
                tick_by_venue.get(y.venue_id, Decimal("0")),
                tick_by_venue.get(n.venue_id, Decimal("0")),
            )
            qty = tradeable_qty(
                unit_cost=unit_cost,
                depths=[yq.ask_size, nq.ask_size],
                max_stake=max_ticket_stake,
                tick=tick,
            )
            if qty <= 0:
                continue

            y_fee = y_fee_model.fee(price=yq.ask, qty=qty, is_buy=True)
            n_fee = n_fee_model.fee(price=nq.ask, qty=qty, is_buy=True)
            total_stake = yq.ask * qty + nq.ask * qty + y_fee + n_fee
            # Exactly one side settles at 1 per contract, so payoff is qty.
            profit = qty - total_stake
            if profit <= 0:
                continue

            opp = ArbOpportunity(
                event_group_id=event_group.id,
                legs=(
                    ArbLeg(
                        outcome_id=y.outcome_id,
                        venue_id=y.venue_id,
                        is_buy=True,
                        price=yq.ask,
                        qty=qty,
                        fee=y_fee,
                    ),
                    ArbLeg(
                        outcome_id=n.outcome_id,
                        venue_id=n.venue_id,
                        is_buy=True,
                        price=nq.ask,
                        qty=qty,
                        fee=n_fee,
                    ),
                ),
                total_stake=total_stake,
                guaranteed_profit=profit,
                guaranteed_profit_bps=(profit / total_stake) * Decimal(10_000),
            )
            if best is None or opp.guaranteed_profit > best.guaranteed_profit:
                best = opp

    return best
```

Add the import at the top of `arb_engine.py` — from `qty`, **not** from `sizing`, which would be circular since `sizing` imports `ArbOpportunity` from here:

```python
from .qty import tradeable_qty
```

Update `arbys/ingest/engine_runtime.py`:

```python
# Replace DEFAULT_TARGET_PAYOFF with the stake cap.
DEFAULT_MAX_TICKET_STAKE = Decimal("200")


class EngineRuntime:
    def __init__(
        self,
        *,
        quotebook: QuoteBook,
        fees: FeeModelRegistry,
        on_opportunity: OpportunityHandler | None = None,
        on_opportunities: OpportunitySetHandler | None = None,
        max_ticket_stake: Decimal | None = DEFAULT_MAX_TICKET_STAKE,
    ) -> None:
        ...
        self._max_ticket_stake = max_ticket_stake
```

and in `evaluate_now`:

```python
        cross = detect_cross_venue_two_leg(
            group, quotes, self._fees, max_ticket_stake=self._max_ticket_stake
        )
```

In `arbys/backend/state.py`, where `EngineRuntime` is constructed, pass the config value:

```python
        max_ticket_stake=max_ticket_stake(),
```

Update `arbys/backtest/__init__.py`: replace its `target_payoff: Decimal = Decimal("1")` parameter with `max_ticket_stake: Decimal | None = Decimal("200")` and pass it through as `max_ticket_stake=max_ticket_stake`.

- [ ] **Step 4: Run the full backend suite**

Run: `venv\Scripts\python.exe -m pytest -q`
Expected: PASS. Pre-existing tests that passed `target_payoff=` must be updated to `max_ticket_stake=`; expected `qty` values change from a flat 100 to depth- or budget-derived numbers. Update assertions to the real values rather than loosening them.

- [ ] **Step 5: Commit**

```bash
git add arbys/shared/arb_engine.py arbys/shared/qty.py arbys/shared/sizing.py \
        arbys/ingest/engine_runtime.py arbys/backend/state.py \
        arbys/backtest/__init__.py tests/
git commit -m "fix(engine): size cross-venue arbs to real depth and a stake cap"
```

---

### Task 5: Depth-aware `detect_complementary_set`

This detector runs for **every** group, once per venue — each group carries 2 legs per venue (`:YES`/`:NO`, `:LONG`/`:SHORT`), so `by_venue` always yields a candidate set. It must not keep reporting flat-100 sizing after Task 4.

**Files:**
- Modify: `arbys/shared/arb_engine.py:154-206`
- Modify: `arbys/ingest/engine_runtime.py:97-103`
- Test: `tests/shared/test_arb_engine.py`

**Interfaces:**
- Produces: `detect_complementary_set(event_group_id, legs, quotes, fees, *, max_ticket_stake: Decimal | None = None, tick_by_venue: dict[str, Decimal] | None = None) -> ArbOpportunity | None`. **`target_payoff` removed.**

- [ ] **Step 1: Write the failing test**

Append to `tests/shared/test_arb_engine.py`:

```python
from arbys.shared.arb_engine import detect_complementary_set


def test_complementary_set_sizes_to_thinnest_leg():
    legs = [
        EventGroupLeg(outcome_id="a", venue_id="v1", is_yes_side=True),
        EventGroupLeg(outcome_id="b", venue_id="v1", is_yes_side=False),
    ]
    quotes = {
        "a": Quote(outcome_id="a", bid=Decimal("0.40"), ask=Decimal("0.45"),
                   ask_size=Decimal("900")),
        "b": Quote(outcome_id="b", bid=Decimal("0.45"), ask=Decimal("0.50"),
                   ask_size=Decimal("11")),
    }
    opp = detect_complementary_set(
        "eg:v1", legs, quotes, _fees(), max_ticket_stake=Decimal("200")
    )
    assert opp is not None
    assert all(leg.qty == Decimal("11") for leg in opp.legs)


def test_complementary_set_blocked_by_known_empty_leg():
    legs = [
        EventGroupLeg(outcome_id="a", venue_id="v1", is_yes_side=True),
        EventGroupLeg(outcome_id="b", venue_id="v1", is_yes_side=False),
    ]
    quotes = {
        "a": Quote(outcome_id="a", bid=Decimal("0.40"), ask=Decimal("0.45"),
                   ask_size=Decimal("900")),
        "b": Quote(outcome_id="b", bid=Decimal("0.45"), ask=Decimal("0.50"),
                   ask_size=Decimal("0")),
    }
    assert detect_complementary_set(
        "eg:v1", legs, quotes, _fees(), max_ticket_stake=Decimal("200")
    ) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/shared/test_arb_engine.py -k complementary -v`
Expected: FAIL — unexpected keyword argument `max_ticket_stake`

- [ ] **Step 3: Write minimal implementation**

Replace `detect_complementary_set` in `arbys/shared/arb_engine.py`:

```python
def detect_complementary_set(
    event_group_id: str,
    legs: list[EventGroupLeg],
    quotes: dict[str, Quote],
    fees: FeeModelRegistry,
    *,
    max_ticket_stake: Decimal | None = None,
    tick_by_venue: dict[str, Decimal] | None = None,
) -> ArbOpportunity | None:
    """Single-venue multi-outcome arb: buy every outcome so exactly one pays 1.

    Every leg must be a mutually-exclusive outcome of the same event. Sizing
    is bounded by the thinnest leg's depth and by `max_ticket_stake`, same as
    the cross-venue detector.
    """
    tick_by_venue = tick_by_venue or {}
    if len(legs) < 2:
        return None

    unit_costs: list[Decimal] = []
    depths: list[Decimal | None] = []
    resolved: list[tuple[EventGroupLeg, Quote, FeeModel]] = []
    for leg in legs:
        q = quotes.get(leg.outcome_id)
        if q is None:
            return None
        fee_model = fees.get(leg.venue_id)
        if fee_model is None:
            return None
        unit_costs.append(leg_unit_cost(q.ask, fee_model, is_buy=True))
        depths.append(q.ask_size)
        resolved.append((leg, q, fee_model))

    if net_edge_per_contract(unit_costs) <= 0:
        return None

    tick = max(
        (tick_by_venue.get(leg.venue_id, Decimal("0")) for leg, _, _ in resolved),
        default=Decimal("0"),
    )
    qty = tradeable_qty(
        unit_cost=sum(unit_costs, Decimal("0")),
        depths=depths,
        max_stake=max_ticket_stake,
        tick=tick,
    )
    if qty <= 0:
        return None

    total_stake = Decimal("0")
    arb_legs: list[ArbLeg] = []
    for leg, q, fee_model in resolved:
        fee = fee_model.fee(price=q.ask, qty=qty, is_buy=True)
        total_stake += q.ask * qty + fee
        arb_legs.append(
            ArbLeg(
                outcome_id=leg.outcome_id,
                venue_id=leg.venue_id,
                is_buy=True,
                price=q.ask,
                qty=qty,
                fee=fee,
            )
        )

    profit = qty - total_stake
    if profit <= 0:
        return None

    return ArbOpportunity(
        event_group_id=event_group_id,
        legs=tuple(arb_legs),
        total_stake=total_stake,
        guaranteed_profit=profit,
        guaranteed_profit_bps=(profit / total_stake) * Decimal(10_000),
    )
```

Update the call in `arbys/ingest/engine_runtime.py`:

```python
            comp = detect_complementary_set(
                f"{group_id}:{venue_id}",
                legs,
                quotes,
                self._fees,
                max_ticket_stake=self._max_ticket_stake,
            )
```

- [ ] **Step 4: Run the full backend suite**

Run: `venv\Scripts\python.exe -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add arbys/shared/arb_engine.py arbys/ingest/engine_runtime.py tests/shared/test_arb_engine.py
git commit -m "fix(engine): size complementary-set arbs to real depth too"
```

---

### Task 6: Broker rejects orders larger than the resting size

**Files:**
- Modify: `arbys/shared/paper_broker.py:164-188`
- Test: `tests/shared/test_paper_broker.py`

**Interfaces:**
- Produces: a new rejection reason string `"insufficient_liquidity"`, distinct from `"no_liquidity"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/shared/test_paper_broker.py`, following the fixtures already in that file:

```python
async def test_order_larger_than_resting_size_is_rejected(broker, book):
    book.put(Quote(outcome_id="o1", bid=Decimal("0.40"), ask=Decimal("0.45"),
                   ask_size=Decimal("3")))
    order, fill, reason = broker.apply_fill(
        account_id="acct",
        outcome_id="o1",
        is_buy=True,
        qty=Decimal("100"),
        limit_price=Decimal("0.50"),
    )
    assert fill is None
    assert reason == "insufficient_liquidity"


async def test_order_within_resting_size_fills(broker, book):
    book.put(Quote(outcome_id="o1", bid=Decimal("0.40"), ask=Decimal("0.45"),
                   ask_size=Decimal("3")))
    order, fill, reason = broker.apply_fill(
        account_id="acct",
        outcome_id="o1",
        is_buy=True,
        qty=Decimal("3"),
        limit_price=Decimal("0.50"),
    )
    assert reason is None
    assert fill is not None and fill.qty == Decimal("3")


async def test_unknown_size_still_fills_any_qty(broker, book):
    # None = unknown. POST /quotes omits sizes, and those must keep working.
    book.put(Quote(outcome_id="o1", bid=Decimal("0.40"), ask=Decimal("0.45")))
    order, fill, reason = broker.apply_fill(
        account_id="acct",
        outcome_id="o1",
        is_buy=True,
        qty=Decimal("100"),
        limit_price=Decimal("0.50"),
    )
    assert reason is None
    assert fill is not None


async def test_known_empty_still_reports_no_liquidity(broker, book):
    # 0 and "too small" are different failures and must stay distinguishable.
    book.put(Quote(outcome_id="o1", bid=Decimal("0.40"), ask=Decimal("0.45"),
                   ask_size=Decimal("0")))
    order, fill, reason = broker.apply_fill(
        account_id="acct",
        outcome_id="o1",
        is_buy=True,
        qty=Decimal("1"),
        limit_price=Decimal("0.50"),
    )
    assert reason == "no_liquidity"
```

> If `tests/shared/test_paper_broker.py` has no `broker`/`book` fixtures, build them inline the way the existing tests in that file do rather than inventing new ones.

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/shared/test_paper_broker.py -k "resting or unknown_size or known_empty" -v`
Expected: FAIL — the 100-qty order fills instead of being rejected.

- [ ] **Step 3: Write minimal implementation**

In `arbys/shared/paper_broker.py`, in `_preview_fill`, replace the resting-size check:

```python
        # A one-sided book keeps its live side and synthesises the missing one
        # at size 0, so the live side stays tradeable. Filling against the
        # synthesised side would be a trade into an empty book.
        #
        # None means the venue did not report depth — most quotes, including
        # every hand-pushed one — and must still fill, or POST /quotes stops
        # working. A known size smaller than the order is a different failure:
        # the engine sizes to depth, so this fires when the book moved between
        # detection and execution. Reject rather than partial-fill, because a
        # partial on one leg of a two-leg arb leaves an unhedged position.
        resting = quote.ask_size if is_buy else quote.bid_size
        if resting is not None:
            if resting <= 0:
                return "no_liquidity"
            if qty > resting:
                return "insufficient_liquidity"
```

- [ ] **Step 4: Run the full backend suite**

Run: `venv\Scripts\python.exe -m pytest -q`
Expected: PASS. Any pre-existing test that filled more than the resting size now legitimately rejects — fix the test's quote size, don't weaken the guard.

- [ ] **Step 5: Commit**

```bash
git add arbys/shared/paper_broker.py tests/shared/test_paper_broker.py
git commit -m "fix(broker): reject orders larger than the resting size"
```

---

### Task 7: `/monitored` exposes the net figures

**Files:**
- Modify: `arbys/backend/schemas.py:61-73`
- Modify: `arbys/backend/app.py:153-220`
- Test: `tests/test_backend_e2e.py`

**Interfaces:**
- Produces four new `MonitoredGroupOut` fields: `net_edge: Decimal | None`, `max_tradeable_qty: Decimal | None`, `net_max_profit: Decimal | None`, `capital_required: Decimal | None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_backend_e2e.py`, following the existing client fixture pattern:

```python
async def test_monitored_reports_net_figures(client):
    # Register a cross-venue group and push quotes with known depth.
    await client.post("/event-groups", json={
        "id": "eg-net", "title": "A vs B",
        "legs": [
            {"outcome_id": "k:YES", "venue_id": "kalshi", "is_yes_side": True},
            {"outcome_id": "k:NO", "venue_id": "kalshi", "is_yes_side": False},
            {"outcome_id": "p:LONG", "venue_id": "polymarket_us", "is_yes_side": True},
            {"outcome_id": "p:SHORT", "venue_id": "polymarket_us", "is_yes_side": False},
        ],
    })
    for oid, bid, ask, size in [
        ("k:YES", "0.30", "0.32", "412"),
        ("k:NO", "0.66", "0.69", "1156"),
        ("p:LONG", "0.33", "0.35", "2616"),
        ("p:SHORT", "0.62", "0.66", "9"),
    ]:
        await client.post("/quotes", json={
            "outcome_id": oid, "bid": bid, "ask": ask, "ask_size": size,
        })

    r = await client.get("/monitored")
    assert r.status_code == 200
    group = next(g for g in r.json() if g["id"] == "eg-net")

    # Best pair is K-Yes 0.32 + P-No 0.66 = 0.98 gross, negative after fees.
    assert Decimal(group["net_edge"]) < 0
    # Thinnest leg of that pair is p:SHORT at 9.
    assert Decimal(group["max_tradeable_qty"]) == Decimal("9")
    assert Decimal(group["net_max_profit"]) == (
        Decimal(group["net_edge"]) * Decimal("9")
    )
    assert Decimal(group["capital_required"]) > 0


async def test_monitored_net_fields_null_without_quotes(client):
    await client.post("/event-groups", json={
        "id": "eg-empty", "title": "C vs D",
        "legs": [
            {"outcome_id": "k2:YES", "venue_id": "kalshi", "is_yes_side": True},
            {"outcome_id": "p2:SHORT", "venue_id": "polymarket_us", "is_yes_side": False},
        ],
    })
    r = await client.get("/monitored")
    group = next(g for g in r.json() if g["id"] == "eg-empty")
    assert group["net_edge"] is None
    assert group["max_tradeable_qty"] is None
    assert group["net_max_profit"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/test_backend_e2e.py -k "net_figures or net_fields_null" -v`
Expected: FAIL — `KeyError: 'net_edge'`

- [ ] **Step 3: Write minimal implementation**

Add to `MonitoredGroupOut` in `arbys/backend/schemas.py`:

```python
    # Net-of-fee figures for the cheapest tradeable YES+NO pair. None when the
    # pair is not fully quoted. net_edge may be negative: the row still needs
    # to state its position, and 12 of 12 gross-positive pairs measured on
    # 2026-08-22 were negative once fees applied.
    net_edge: Decimal | None  # profit per contract, after both legs' fees
    max_tradeable_qty: Decimal | None  # thinnest leg's depth; None = unknown
    net_max_profit: Decimal | None  # net_edge * qty, after the stake cap
    capital_required: Decimal | None  # total stake for that qty
```

In `arbys/backend/app.py`, inside the `/monitored` loop after the existing best-ask bookkeeping, compute the pair explicitly. Add the imports:

```python
from ..shared.arb_engine import leg_unit_cost, net_edge_per_contract
from ..shared.qty import tradeable_qty
from .state import max_ticket_stake
```

and the computation:

```python
            # The cheapest *tradeable pair*, which is not the same as
            # best_yes_ask + best_no_ask: those two can come from the same
            # venue. Evaluate each (yes, no) pair and keep the cheapest.
            net_edge: Decimal | None = None
            max_qty: Decimal | None = None
            net_max_profit: Decimal | None = None
            capital_required: Decimal | None = None

            quoted = {
                leg.outcome_id: s.quotebook.get(leg.outcome_id) for leg in g.legs
            }
            best_unit: Decimal | None = None
            for y in (leg for leg in g.legs if leg.is_yes_side):
                yq = quoted.get(y.outcome_id)
                y_fm = s.fees.get(y.venue_id)
                if yq is None or y_fm is None:
                    continue
                for n in (leg for leg in g.legs if not leg.is_yes_side):
                    nq = quoted.get(n.outcome_id)
                    n_fm = s.fees.get(n.venue_id)
                    if nq is None or n_fm is None:
                        continue
                    y_unit = leg_unit_cost(yq.ask, y_fm, is_buy=True)
                    n_unit = leg_unit_cost(nq.ask, n_fm, is_buy=True)
                    unit = y_unit + n_unit
                    if best_unit is not None and unit >= best_unit:
                        continue
                    best_unit = unit
                    net_edge = net_edge_per_contract([y_unit, n_unit])
                    qty = tradeable_qty(
                        unit_cost=unit,
                        depths=[yq.ask_size, nq.ask_size],
                        max_stake=max_ticket_stake(),
                    )
                    max_qty = qty
                    net_max_profit = net_edge * qty
                    capital_required = unit * qty
```

Pass all four into the `MonitoredGroupOut(...)` construction.

- [ ] **Step 4: Run the full backend suite**

Run: `venv\Scripts\python.exe -m pytest -q` then `venv\Scripts\python.exe -m ruff check .`
Expected: PASS and clean.

- [ ] **Step 5: Commit**

```bash
git add arbys/backend/schemas.py arbys/backend/app.py tests/test_backend_e2e.py
git commit -m "feat(api): /monitored reports net edge, tradeable size and net profit"
```

---

### Task 8: Document Part A

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the Config section**

Add under the feature-flag list:

```markdown
- `ARBYS_MAX_TICKET_STAKE` — max total capital in one arb ticket, default 200,
  `0` disables. Sizing is depth-driven and one Polymarket US level has shown
  419,882 contracts resting, so without this a single ticket would consume the
  book. **This does not replace `ARBYS_MAX_OUTCOME_QTY`** — that caps
  cumulative open units per outcome per account at execute time, this caps one
  ticket at detection time. At ~$1.00 all-in per contract pair, $200 is ~198
  contracts, so roughly 2.5 tickets on one outcome before the position cap
  binds.
```

- [ ] **Step 2: Replace the "Known defects — None currently tracked" section**

```markdown
## Known defects

None currently tracked.

Previously listed here and now fixed:

**Sizing ignored book depth** (fixed 2026-08-22). `arb_engine` set
`qty = target_payoff` with `DEFAULT_TARGET_PAYOFF = 100`, so every opportunity
was sized at a flat 100 contracts whether the book held 3 or 419,882. Sizing is
now `min(depth, stake_budget)` via `shared/qty.py:tradeable_qty`.

**The paper broker filled more than was resting** (fixed 2026-08-22).
`_preview_fill` blocked only an explicit size `0` and never compared `qty` to
`resting`, so an order for 100 filled completely against a book with 3
available. It now returns `insufficient_liquidity`, distinct from
`no_liquidity`. **Reject, not partial-fill** — a partial on one leg of a
two-leg arb leaves an unhedged position, the one outcome the design exists to
avoid.

**The detection gate was dimensionally wrong** (fixed 2026-08-22). It compared
a per-contract cost against a total payoff (`total_unit_cost >= target_payoff`
with `target_payoff = 100`), so it never fired; the downstream `profit <= 0`
check happened to reduce to the correct test. Never a live defect, but it broke
the moment `qty` stopped equalling payoff. The gate is now an explicit
per-contract `net_edge_per_contract(...) <= 0`.
```

- [ ] **Step 3: Note that complementary-set detection is live**

In the Architecture section under `arbys/ingest/`, add:

```markdown
  `engine_runtime` runs **two** detectors per evaluation:
  `detect_cross_venue_two_leg` across venues, and `detect_complementary_set`
  once per venue. Every group carries 2 legs per venue (`:YES`/`:NO`,
  `:LONG`/`:SHORT`), so the complementary detector always has a candidate set —
  meaning **intra-venue arbs are already detected**. A Kalshi book crossed at
  `YES 0.47 + NO 0.52 = 0.99` was observed on 2026-08-22 (1 of 245 groups); it
  produced no opportunity only because fees put it at 1.0249.
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record the sizing and broker fixes in CLAUDE.md"
```

---

# Part B — The dense table (frontend)

---

### Task 9: Types and the two pure helpers

**Files:**
- Modify: `frontend/src/api/types.ts:29-42`
- Modify: `frontend/src/lib/combo.ts`

**Interfaces:**
- Consumes: the four `/monitored` fields from Task 7.
- Produces:
  - `MonitoredGroup` gains `net_edge`, `max_tradeable_qty`, `net_max_profit`, `capital_required`, all `string | null`
  - `bestPair(group: MonitoredGroup): BestPair` where `BestPair = { combo: Combo | null; both: boolean; size: number | null }`
  - `splitTitle(title: string): { matchup: string; market: string | null }`

- [ ] **Step 1: Add the type fields**

In `frontend/src/api/types.ts`, add to `MonitoredGroup`:

```ts
  /** Net profit per contract after both legs' fees. Negative is normal and
   *  must be shown — gross-positive pairs are usually net-negative. */
  net_edge: string | null;
  /** Thinnest leg's depth for the best pair. null = venue reported none. */
  max_tradeable_qty: string | null;
  /** net_edge * max_tradeable_qty, after the ticket stake cap. */
  net_max_profit: string | null;
  /** Total stake needed to open that position. */
  capital_required: string | null;
```

- [ ] **Step 2: Add the helpers to `lib/combo.ts`**

```ts
export interface BestPair {
  /** The cheaper of the two combos; null when neither is fully quoted. */
  combo: Combo | null;
  /** Both combos favorable at once. Rare: needs a venue's own YES+NO to
   *  cross, which Polymarket US cannot do structurally (its short side is
   *  derived) but Kalshi can — measured 1 of 245 groups on 2026-08-22. */
  both: boolean;
  /** Tradeable size for the pair. 0 = known empty, null = unknown. */
  size: number | null;
}

/** Size available across both legs, under the three-state rule.
 *
 *  Check order matters: an explicit 0 on either leg means known-empty and
 *  nothing is tradeable; null means unknown and imposes no ceiling. Reversing
 *  these either offers a fill into an empty book or rejects every quote pushed
 *  via POST /quotes, which omits sizes entirely.
 */
function pairSize(combo: Combo): number | null {
  const legs = [combo.yesLeg, combo.noLeg];
  const sizes = legs.map((l) => (l?.ask_size == null ? null : Number(l.ask_size)));
  if (sizes.some((s) => s !== null && s <= 0)) return 0;
  const known = sizes.filter((s): s is number => s !== null && Number.isFinite(s));
  return known.length > 0 ? Math.min(...known) : null;
}

export function bestPair(group: MonitoredGroup): BestPair {
  const [a, b] = buildCombos(group);
  const quoted = [a, b].filter((c) => c.total != null);
  if (quoted.length === 0) return { combo: null, both: false, size: null };
  const combo = quoted.reduce((lo, c) => (c.total! < lo.total! ? c : lo));
  return {
    combo,
    both: a.favorable && b.favorable,
    size: pairSize(combo),
  };
}

/** Split "Team A vs Team B — Over 41.5 (2026-09-13)" into its two parts.
 *
 *  Matches the *spaced* em-dash only: a name containing a bare em-dash would
 *  otherwise be cut in half. The trailing date is dropped because the Start
 *  column already carries it.
 */
export function splitTitle(title: string): {
  matchup: string;
  market: string | null;
} {
  const withoutDate = title.replace(/\s*\(\d{4}-\d{2}-\d{2}\)\s*$/, "");
  const idx = withoutDate.indexOf(" — ");
  if (idx === -1) return { matchup: withoutDate.trim(), market: null };
  return {
    matchup: withoutDate.slice(0, idx).trim(),
    market: withoutDate.slice(idx + 3).trim() || null,
  };
}
```

- [ ] **Step 3: Verify it typechecks**

Run: `cd frontend && npm run build`
Expected: PASS (`tsc -b` clean).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/lib/combo.ts
git commit -m "feat(frontend): bestPair and splitTitle helpers, net field types"
```

---

### Task 10: `OpportunityRow`

**Files:**
- Create: `frontend/src/components/OpportunityRow.tsx`

**Interfaces:**
- Consumes: `bestPair`, `splitTitle`, `eventClock`, `categoryOf`, `comboState`, `findOpportunity`, `buyOutcomeIds` from `lib/combo`; `PriceMove` from `hooks/usePriceMoves`.
- Produces: `<OpportunityRow group categoryLabel opportunities filled onFilled priceMoves now />`

- [ ] **Step 1: Write the component**

```tsx
import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ArbOpportunity, MonitoredGroup } from "../api/types";
import type { PriceMove } from "../hooks/usePriceMoves";
import { api } from "../api/client";
import {
  askToCents,
  bestPair,
  buyOutcomeIds,
  categoryOf,
  comboState,
  eventClock,
  findOpportunity,
  KALSHI,
  splitTitle,
} from "../lib/combo";

interface Props {
  group: MonitoredGroup;
  opportunities: ArbOpportunity[];
  filled: boolean;
  onFilled: (groupId: string) => void;
  priceMoves: Map<string, PriceMove>;
  now: number;
}

function fmtQty(n: number | null): string {
  if (n == null) return "?";
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  if (n >= 1_000) return `${(n / 1000).toFixed(1)}k`;
  return String(Math.round(n * 100) / 100);
}

function fmtCents(v: string | null): string {
  if (v == null) return "—";
  const n = Number(v) * 100;
  if (!Number.isFinite(n)) return "—";
  return `${n >= 0 ? "+" : ""}${n.toFixed(1)}¢`;
}

function fmtUsd(v: string | null): string {
  if (v == null) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return `${n < 0 ? "−" : ""}$${Math.abs(n).toFixed(2)}`;
}

export function OpportunityRow({
  group,
  opportunities,
  filled,
  onFilled,
  priceMoves,
  now,
}: Props) {
  const qc = useQueryClient();
  const pair = bestPair(group);
  const { matchup, market } = splitTitle(group.title);
  const clock = eventClock(group, now);
  const cat = categoryOf(group);

  // The stripe stays GROSS of fees, matching the card's green outline. It is a
  // venue-divergence signal in its own right and is deliberately not the same
  // test as whether the button is enabled. See CLAUDE.md.
  const grossArb = pair.combo?.favorable ?? false;
  const stale =
    pair.combo?.yesLeg?.is_stale === true || pair.combo?.noLeg?.is_stale === true;
  const noSize = pair.size === 0;

  const opportunity =
    pair.combo && !stale && !noSize
      ? findOpportunity(opportunities, group, pair.combo)
      : null;
  const state = pair.combo ? comboState(pair.combo, opportunity) : "no-quotes";

  const exec = useMutation({
    mutationFn: () => {
      if (opportunity == null) throw new Error("no matching opportunity");
      return api.executeArb(opportunity.event_group_id, buyOutcomeIds(opportunity));
    },
    onSuccess: () => {
      onFilled(group.id);
      qc.invalidateQueries({ queryKey: ["opps"] });
      qc.invalidateQueries({ queryKey: ["paper"] });
    },
  });

  const pairLabel = (() => {
    if (!pair.combo) return "—";
    const yesTag = pair.combo.yesVenue === KALSHI ? "K-Yes" : "P-Yes";
    const noTag = pair.combo.noVenue === KALSHI ? "K-No" : "P-No";
    return `${yesTag} ${askToCents(pair.combo.yesLeg?.ask ?? null)} + ${noTag} ${askToCents(
      pair.combo.noLeg?.ask ?? null,
    )}`;
  })();

  const moved =
    (pair.combo?.yesLeg && priceMoves.get(pair.combo.yesLeg.outcome_id)) ||
    (pair.combo?.noLeg && priceMoves.get(pair.combo.noLeg.outcome_id));

  return (
    <tr className={grossArb ? "vt-row-arb" : undefined}>
      <td>
        <span
          className="vt-dot"
          style={
            clock.phase === "live"
              ? { animation: "vt-pulse 1.2s ease-in-out infinite" }
              : undefined
          }
        />
      </td>
      <td>
        <span className="tag tag-neutral vt-cat">{cat.label}</span>
      </td>
      <td className="vt-ellipsis" title={group.title}>
        {matchup}
      </td>
      <td className="vt-muted">{market ?? "—"}</td>
      <td
        className="vt-mono"
        title={
          group.start_time
            ? new Date(group.start_time).toLocaleString()
            : "no scheduled start reported by either venue"
        }
      >
        {clock.text}
      </td>
      <td className={`vt-mono ${stale ? "vt-stale" : ""} ${moved ? "vt-move" : ""}`}>
        {pairLabel}
      </td>
      <td className={`vt-mono vt-num ${noSize ? "vt-size-zero" : ""}`}>
        {fmtQty(pair.size)}
      </td>
      <td
        className="vt-mono vt-num"
        title={
          pair.combo?.edge != null
            ? `gross ${(pair.combo.edge * 100).toFixed(1)}¢ before fees`
            : undefined
        }
      >
        {stale ? (
          <span className="vt-stale">stale</span>
        ) : (
          <span
            className={
              group.net_edge != null && Number(group.net_edge) > 0
                ? "vt-edge-pos"
                : "vt-muted"
            }
          >
            {fmtCents(group.net_edge)}
            {pair.both && (
              <span
                className="vt-both"
                title="both combos favorable — a venue's own book is crossed"
              >
                *
              </span>
            )}
          </span>
        )}
      </td>
      <td className="vt-mono vt-num" title={`capital ${fmtUsd(group.capital_required)}`}>
        {fmtUsd(group.net_max_profit)}
      </td>
      <td>
        {filled ? (
          <span className="vt-filled-inline">✓ filled</span>
        ) : (
          <button
            type="button"
            className={`btn vt-fill ${state === "ready" ? "vt-fill-active" : "btn-secondary"}`}
            disabled={state !== "ready" || noSize || stale || exec.isPending}
            onClick={() => exec.mutate()}
            title={
              exec.isError
                ? `execution failed: ${exec.error?.message ?? ""}`
                : noSize
                  ? "nothing resting on one leg — the broker would reject this"
                  : undefined
            }
          >
            {exec.isError
              ? "failed"
              : exec.isPending
                ? "…"
                : noSize
                  ? "no size"
                  : state === "ready"
                    ? "Fill"
                    : state === "waiting"
                      ? "waiting"
                      : "—"}
          </button>
        )}
      </td>
    </tr>
  );
}
```

- [ ] **Step 2: Verify it typechecks**

Run: `cd frontend && npm run build`
Expected: PASS. It is not rendered yet, so only type errors can surface.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/OpportunityRow.tsx
git commit -m "feat(frontend): OpportunityRow with net edge, size and net profit"
```

---

### Task 11: `OpportunityTable` with sortable headers

**Files:**
- Create: `frontend/src/components/OpportunityTable.tsx`

**Interfaces:**
- Consumes: `OpportunityRow` (Task 10); `compareGroups`, `groupStartDate`, `bestPair` from `lib/combo`.
- Produces: `<OpportunityTable groups opportunities filledMap onFilled priceMoves now />`

- [ ] **Step 1: Write the component**

```tsx
import { useMemo, useState } from "react";
import type { ArbOpportunity, MonitoredGroup } from "../api/types";
import type { PriceMove } from "../hooks/usePriceMoves";
import { bestPair, categoryOf, compareGroups, groupStartDate, splitTitle } from "../lib/combo";
import { OpportunityRow } from "./OpportunityRow";

type SortKey = "start" | "cat" | "matchup" | "size" | "edge" | "profit";
type SortDir = "asc" | "desc";

interface Props {
  groups: MonitoredGroup[];
  opportunities: ArbOpportunity[];
  filledMap: Record<string, boolean>;
  onFilled: (groupId: string) => void;
  priceMoves: Map<string, PriceMove>;
  now: number;
}

const COLUMNS: { key: SortKey | null; label: string; numeric?: boolean }[] = [
  { key: null, label: "" },
  { key: "cat", label: "Cat" },
  { key: "matchup", label: "Matchup" },
  { key: null, label: "Market" },
  { key: "start", label: "Start" },
  { key: null, label: "Best pair" },
  { key: "size", label: "Size", numeric: true },
  { key: "edge", label: "Edge", numeric: true },
  { key: "profit", label: "Net $", numeric: true },
  { key: null, label: "" },
];

function num(v: string | null): number | null {
  if (v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/** Nulls always sort last, whichever direction is active — otherwise an
 *  unquoted row would jump to the top the moment you sort by edge. */
function cmpNullable(a: number | null, b: number | null): number {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  return a - b;
}

export function OpportunityTable({
  groups,
  opportunities,
  filledMap,
  onFilled,
  priceMoves,
  now,
}: Props) {
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({
    key: "start",
    dir: "asc",
  });

  const sorted = useMemo(() => {
    const dir = sort.dir === "asc" ? 1 : -1;
    const primary = (a: MonitoredGroup, b: MonitoredGroup): number => {
      switch (sort.key) {
        case "start":
          return cmpNullable(groupStartDate(a), groupStartDate(b));
        case "cat":
          return categoryOf(a).label.localeCompare(categoryOf(b).label);
        case "matchup":
          return splitTitle(a.title).matchup.localeCompare(splitTitle(b.title).matchup);
        case "size":
          return cmpNullable(bestPair(a).size, bestPair(b).size);
        case "edge":
          return cmpNullable(num(a.net_edge), num(b.net_edge));
        case "profit":
          return cmpNullable(num(a.net_max_profit), num(b.net_max_profit));
      }
    };
    return groups.slice().sort((a, b) => {
      const p = primary(a, b);
      // Always tiebreak on compareGroups. Without it equal keys fall back to
      // /monitored's dict-insertion order, which shifts as discovery registers
      // games — rows would reshuffle between polls and a click could land on
      // the wrong event.
      return p !== 0 ? p * dir : compareGroups(a, b);
    });
  }, [groups, sort]);

  const toggle = (key: SortKey) =>
    setSort((s) =>
      s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" },
    );

  return (
    <table className="table vt-table">
      <thead>
        <tr>
          {COLUMNS.map((c, i) => (
            <th
              key={i}
              className={c.numeric ? "vt-num" : undefined}
              onClick={c.key ? () => toggle(c.key!) : undefined}
              style={c.key ? { cursor: "pointer", userSelect: "none" } : undefined}
              title={c.key ? `sort by ${c.label.toLowerCase()}` : undefined}
            >
              {c.label}
              {c.key && sort.key === c.key ? (sort.dir === "asc" ? " ▲" : " ▼") : ""}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {sorted.map((g) => (
          <OpportunityRow
            key={g.id}
            group={g}
            opportunities={opportunities}
            filled={filledMap[g.id] === true}
            onFilled={onFilled}
            priceMoves={priceMoves}
            now={now}
          />
        ))}
      </tbody>
    </table>
  );
}
```

- [ ] **Step 2: Verify it typechecks**

Run: `cd frontend && npm run build`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/OpportunityTable.tsx
git commit -m "feat(frontend): sortable OpportunityTable, stable by default"
```

---

### Task 12: Wire into TerminalPage, delete the card, add CSS

**Files:**
- Modify: `frontend/src/pages/TerminalPage.tsx:7,49,162-190`
- Delete: `frontend/src/components/OpportunityCard.tsx`
- Modify: `frontend/src/index.css`

- [ ] **Step 1: Swap the grid for the table**

In `TerminalPage.tsx`, replace the `OpportunityCard` import with:

```tsx
import { OpportunityTable } from "../components/OpportunityTable";
```

Change the filled-state type — the table tracks a boolean per group, not which combo:

```tsx
  const [filledMap, setFilledMap] = useState<Record<string, boolean>>({});
```

Replace the card grid (the `<div style={{ display: "grid", ... }}>` block and its contents) with:

```tsx
            <OpportunityTable
              groups={filtered}
              opportunities={opportunities}
              filledMap={filledMap}
              onFilled={(id) => setFilledMap((m) => ({ ...m, [id]: true }))}
              priceMoves={priceMoves}
              now={now}
            />
```

Change the section's padding so the table can go edge-to-edge, and drop `overflowX: "hidden"` so a narrow window scrolls the table rather than clipping it:

```tsx
        <section
          className="vt-scroll"
          style={{ minHeight: 0, overflow: "auto", padding: 0 }}
        >
```

- [ ] **Step 2: Delete the card**

```bash
git rm frontend/src/components/OpportunityCard.tsx
```

`BlueprintCard.tsx` stays — `AdminPage.tsx` imports it in seven places.

- [ ] **Step 3: Replace the card CSS with table CSS**

In `frontend/src/index.css`, delete `.vt-card`, `.vt-card.vt-arb`, `.vt-combo`, `.vt-combo:disabled`, `.vt-combo-active`, `.vt-combo-active:hover`, and `.vt-filled`. Add:

```css
/* — compact table density —
   The .table primitive from the industry design system supplies borders,
   header treatment and hover. This overrides padding and font-size only; no
   new colors, radii or type scales. */
.vt-table { font-size: 12px; }
.vt-table th {
  padding: 5px 8px;
  position: sticky;
  top: 0;
  background: var(--color-bg);
  z-index: 1;
  white-space: nowrap;
}
.vt-table td {
  padding: 5px 8px;
  white-space: nowrap;
}
.vt-num { text-align: right; }
.vt-muted { color: color-mix(in srgb, var(--color-text) 45%, transparent); }
.vt-ellipsis {
  max-width: 22ch;
  overflow: hidden;
  text-overflow: ellipsis;
}
.vt-cat { font-size: 9px; padding: 1px 4px; }

.vt-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-accent);
}

/* Gross-of-fees divergence, same signal the card's green outline carried.
   Deliberately NOT the same test as whether Fill is enabled. */
.vt-row-arb {
  box-shadow: inset 2px 0 0 var(--vt-green);
  background: color-mix(in srgb, var(--vt-green) 5%, transparent);
}
.vt-edge-pos { color: var(--vt-green-dark); font-weight: 600; }
.vt-size-zero { color: var(--vt-green-dark); text-decoration: line-through; opacity: 0.6; }
.vt-both { font-weight: 700; margin-left: 2px; }

.vt-fill {
  font-size: 10px;
  padding: 2px 7px;
  line-height: 1.3;
}
.vt-fill:disabled { cursor: not-allowed; }
.vt-fill-active {
  background: var(--vt-green) !important;
  color: #fff !important;
  border-color: var(--vt-green) !important;
}
.vt-fill-active:hover { background: var(--vt-green-dark) !important; }
.vt-filled-inline { font-size: 10px; color: var(--vt-green-dark); }
```

- [ ] **Step 4: Verify lint and build**

Run: `cd frontend && npm run lint && npm run build`
Expected: both clean. Fix any unused-import warnings left behind by the deletion.

- [ ] **Step 5: Commit**

```bash
git add -A frontend/src
git commit -m "feat(frontend): replace the card grid with the dense table"
```

---

### Task 13: Verify against the live app

No test runner exists for the frontend, so this is the real gate. Both servers must be running from the repo root.

- [ ] **Step 1: Start the stack**

```bash
venv/Scripts/python.exe -m uvicorn arbys.backend.app:app --host 127.0.0.1 --port 8000
cd frontend && npm run dev
```

Open `http://localhost:5173` — note Vite binds `::1`, so `127.0.0.1:5173` may not resolve.

- [ ] **Step 2: Confirm each row state appears**

- [ ] A green-striped row whose Edge shows a **negative** net figure and whose button is not `Fill`. This is the expected gross-vs-net case, not a bug.
- [ ] A row with Size `0`, struck through, showing `no size` and no green button.
- [ ] A row with Size `?` (a leg on the REST path, which cannot report depth).
- [ ] Market column showing `O 41.5` for a totals group and `—` for a moneyline.

- [ ] **Step 3: Confirm ordering is stable**

- [ ] Watch the table across at least four 3s polls without touching it. No row may change position.
- [ ] Click Edge, then Size, then Net $, then Start. Each reorders once, then stays put across polls except where the underlying value genuinely changed.

- [ ] **Step 4: Confirm sizing is honest end-to-end**

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/monitored" |
  Where-Object { $_.max_tradeable_qty } |
  Select-Object -First 5 id, net_edge, max_tradeable_qty, net_max_profit, capital_required
```

- [ ] `capital_required` never exceeds 200.
- [ ] `max_tradeable_qty` matches the thinnest `ask_size` of the chosen pair.
- [ ] Any live opportunity from `/opportunities` has `qty` equal to that group's `max_tradeable_qty`, not 100.

- [ ] **Step 5: Commit any fixes and run the full bar**

```bash
venv\Scripts\python.exe -m pytest -q
venv\Scripts\python.exe -m ruff check .
cd frontend && npm run lint && npm run build
```

```bash
git add -A
git commit -m "fix: address issues found verifying the table against live data"
```

---

## Self-Review

**Spec coverage**

| spec section | task |
| --- | --- |
| A1 detection gate + depth sizing | 1, 2, 4 |
| A1 three-state depth rule | 2 |
| A2 stake cap vs outcome cap | 3, 8 |
| A3 broker rejection | 6 |
| A4 four `/monitored` fields | 7 |
| B1 files | 9–12 |
| B2 compact density | 12 |
| B3 columns incl. net Edge and Net $ | 10, 12 |
| B4 `bestPair` / `splitTitle` | 9 |
| B5 ordering + `compareGroups` tiebreak | 11 |
| B6 row states | 10 |
| B7 CSS | 12 |
| Testing | 1–7, 13 |

**Gap found and closed:** the spec's non-goal claimed intra-venue arbs are undetectable. `detect_complementary_set` runs per venue on every group, so they already are. Task 5 makes that detector depth-aware and Task 8 corrects the documentation. **The spec's non-goal on this point is wrong and should be struck.**

**Deviation recorded:** the spec says `ARBYS_MAX_TICKET_STAKE=0` disables the cap but does not say what bounds sizing when depth is also unknown. Task 2 keeps `LEGACY_UNBOUNDED_QTY = 100` for exactly that case, so disabling reproduces today's behaviour rather than producing an unbounded ticket.

**Circular import caught:** `sizing.py` imports `ArbOpportunity` from `arb_engine`, so `arb_engine` cannot import `sizing`. Task 2 therefore creates `tradeable_qty` in a new leaf module `shared/qty.py` that imports nothing from the package; both `arb_engine` and `sizing` import from it, and `sizing` re-exports for continuity.

**Type consistency:** `tradeable_qty(*, unit_cost, depths, max_stake, tick)` is used identically in Tasks 4, 5 and 7. `bestPair` returns `{combo, both, size}` in Task 9 and is consumed with those names in Tasks 10 and 11. `filledMap` is `Record<string, boolean>` in Tasks 11 and 12.
