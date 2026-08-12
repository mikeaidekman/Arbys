# In-Game Divergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make in-play cross-venue divergence trustworthy — add MLB totals, poll live games fast, and label opportunities whose legs disagree about what time it is so execution can verify them against a fresh quote instead of a memory.

**Architecture:** Liveness is derived from `EventGroup.start_time` by a pure helper. `AppState` turns that into a memoised set of in-play `outcome_id`s, which two consumers read: the Polymarket US adapter (to poll those slugs on a fast concurrent tier) and `QuoteBook` (to apply a tight staleness ceiling). `EngineRuntime` labels — never suppresses — in-play opportunities whose leg ages differ by more than a threshold; the execute endpoint force-refreshes the flagged leg and rejects if the edge has evaporated.

**Tech Stack:** Python 3.11+, httpx (async), asyncio, FastAPI, pytest (`asyncio_mode = "auto"`).

**Spec:** [docs/superpowers/specs/2026-08-12-in-game-divergence-design.md](../specs/2026-08-12-in-game-divergence-design.md)

## Global Constraints

- **All money and all prices are `Decimal`. Never float.** Convert via `Decimal(str(v))`.
- Prices are probabilities in `[0, 1]`; `Quote.__post_init__` enforces range and `ask >= bid`.
- Domain types are `@dataclass(frozen=True)`; enums are `StrEnum`.
- **`arbys/shared/` is pure domain — no I/O, no framework imports.** No `httpx`, no SQLAlchemy, no FastAPI. `liveness.py` and the `QuoteBook` hook must respect this: they take values and callables, never fetch.
- `outcome_id` is venue-native and not portable; always carry `venue_id` with it.
- **Tests never hit a real venue.** REST paths mock with `httpx.MockTransport`.
- Run everything from the repo root with `venv\Scripts\python.exe`, never bare `python`.
- Green-build bar: `venv\Scripts\python.exe -m pytest -q` (**179 today**) and `venv\Scripts\python.exe -m ruff check .`; in `frontend/`, `npm run build`.
- **mypy is NOT part of the bar** — 47 pre-existing errors. Do not start a cleanup.
- **Default behaviour must not change when the new knobs are absent.** Every new hook defaults to `None`; with no hook, `QuoteBook`, the adapter and the engine must behave byte-identically to today. Several tests below assert exactly this.
- `ArbOpportunity` gains a field, but `repositories.insert_opportunity` reads named fields explicitly, so **the flag is transient and not persisted**. No migration.

## File Structure

| File | Responsibility | Task |
| --- | --- | --- |
| `arbys/shared/liveness.py` | **create** — pure `is_in_play` + per-sport windows | 1 |
| `arbys/discovery/polymarket_us.py` | `TOTAL_TYPES` gains MLB | 2 |
| `arbys/discovery/service.py` | `TOTALS_SPORTS` gains MLB | 2 |
| `arbys/shared/quotebook.py` | optional per-outcome `max_age_for` hook | 3 |
| `arbys/backend/state.py` | memoised `live_outcome_ids`; wiring; `refresh_quotes` | 4, 5, 7 |
| `arbys/adapters/polymarket_us.py` | two-tier concurrent polling; `refresh()` | 5 |
| `arbys/shared/arb_engine.py` | `ArbOpportunity.unconfirmed_stale_leg` | 6 |
| `arbys/ingest/engine_runtime.py` | skew labelling | 6 |
| `arbys/backend/app.py` | execute-time verification | 7 |
| `CLAUDE.md`, `.env.example` | docs + config | 8 |

---

### Task 1: Liveness helper

**Files:**
- Create: `arbys/shared/liveness.py`
- Test: `tests/shared/test_liveness.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `DEFAULT_IN_PLAY_WINDOW: timedelta`
  - `IN_PLAY_WINDOWS: dict[str, timedelta]`
  - `sport_of(event_group_id: str) -> str`
  - `is_in_play(start_time: datetime | None, sport: str, now: datetime) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/shared/test_liveness.py`:

```python
from datetime import UTC, datetime, timedelta

from arbys.shared.liveness import is_in_play, sport_of

START = datetime(2026, 8, 12, 18, 0, tzinfo=UTC)


def test_before_start_is_not_in_play():
    assert is_in_play(START, "mlb", START - timedelta(seconds=1)) is False


def test_at_start_is_in_play():
    assert is_in_play(START, "mlb", START) is True


def test_inside_window_is_in_play():
    assert is_in_play(START, "mlb", START + timedelta(hours=2)) is True


def test_past_window_is_not_in_play():
    assert is_in_play(START, "mlb", START + timedelta(hours=99)) is False


def test_missing_start_time_is_never_in_play():
    """Hand-registered groups and venues that report no time have none."""
    assert is_in_play(None, "mlb", START) is False


def test_unknown_sport_falls_back_to_the_default_window():
    """A new league must not crash liveness, and must not read as dead."""
    assert is_in_play(START, "quidditch", START + timedelta(minutes=30)) is True


def test_sport_of_reads_the_group_id_prefix():
    assert sport_of("mlb-ARI-COL-2026-08-11") == "mlb"
    assert sport_of("nfl-ARI-LAC-2026-09-13-total-44.5") == "nfl"
    assert sport_of("wta-OSAKA-RYBAKINA-2026-08-12") == "wta"
    assert sport_of("") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/shared/test_liveness.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'arbys.shared.liveness'`

- [ ] **Step 3: Write the module**

Create `arbys/shared/liveness.py`:

```python
"""Is a real-world event currently being played?

Pure: takes a start time and a clock reading, returns a bool. No I/O, so this
is safe to import from anywhere including the adapters and the quote book.

**Why derive this from the clock rather than read a venue's `live` flag.**
Polymarket US does publish `live` / `period` / `ended` on its league events
payload, and those are more accurate than a window. But discovery refreshes
only every ``ARBYS_DISCOVERY_INTERVAL_S`` (600s in practice), so a stored flag
would be up to ten minutes stale — worse, for this purpose, than a clock that
is always current.

Windows are deliberately generous. Extra innings, overtime and rain delays all
stretch real games, and the two failure directions are not symmetric:
over-polling a finished market costs a few HTTP calls against markets that
have closed anyway, while under-polling a live one is the exact failure this
module exists to prevent.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# Generous by design — see module docstring.
DEFAULT_IN_PLAY_WINDOW = timedelta(hours=5)

IN_PLAY_WINDOWS: dict[str, timedelta] = {
    "mlb": timedelta(hours=5),   # extra innings, rain delays
    "nfl": timedelta(hours=5),   # overtime, long reviews
    "nba": timedelta(hours=4),
    "atp": timedelta(hours=6),   # five-setters run long
    "wta": timedelta(hours=5),
}


def sport_of(event_group_id: str) -> str:
    """Leading segment of a discovery group id: ``"mlb-ARI-COL-…"`` -> ``"mlb"``.

    The same convention ``CATEGORY_LABELS`` in ``frontend/src/lib/combo.ts``
    relies on.
    """
    return event_group_id.split("-", 1)[0].lower()


def is_in_play(start_time: datetime | None, sport: str, now: datetime) -> bool:
    """True while ``now`` sits inside this sport's window after ``start_time``.

    ``start_time is None`` is never in play: hand-registered groups and venues
    that report no time have nothing to measure from.
    """
    if start_time is None:
        return False
    window = IN_PLAY_WINDOWS.get(sport.lower(), DEFAULT_IN_PLAY_WINDOW)
    return start_time <= now < start_time + window
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv\Scripts\python.exe -m pytest tests/shared/test_liveness.py -q`
Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add arbys/shared/liveness.py tests/shared/test_liveness.py
git commit -m "feat(shared): add pure in-play liveness helper

Derived from start_time rather than Polymarket US's live flag: discovery
refreshes every 600s, so a stored flag would be up to ten minutes stale,
which is worse than a clock that is always current.

Windows are generous on purpose. Over-polling a finished market costs a
few HTTP calls; under-polling a live one is the failure that matters."
```

---

### Task 2: MLB totals

**Files:**
- Modify: `arbys/discovery/polymarket_us.py` (`TOTAL_TYPES`)
- Modify: `arbys/discovery/service.py` (`TOTALS_SPORTS`)
- Test: `tests/discovery/test_polymarket_us.py`

**Interfaces:**
- Consumes: nothing
- Produces: no new symbols; `fetch_polymarket_us_totals` now accepts `sport="mlb"`

Measured 2026-08-12: Kalshi 154 MLB total markets / 14 games, Polymarket US 39 / 13 games, 12 shared games → **36 matched groups**. Polymarket US quotes only the middle three strikes (7.5/8.5/9.5) against Kalshi's eleven; only shared strikes can match, and those three are the in-play-interesting ones.

- [ ] **Step 1: Write the failing test**

Append to `tests/discovery/test_polymarket_us.py`:

```python
MLB_TOTALS = {
    "events": [
        {
            "slug": "mlb-hou-sf-2026-08-12",
            "title": "Houston Astros vs. San Francisco Giants",
            "startTime": "2026-08-12T22:45:00Z",
            "teams": [
                {"name": "Houston Astros", "abbreviation": "hou"},
                {"name": "San Francisco Giants", "abbreviation": "sf"},
            ],
            "markets": [
                {
                    "slug": "tsc-mlb-hou-sf-2026-08-12-8pt5",
                    "sportsMarketType": "baseball_team_full_game_total",
                    "line": 8.5,
                    "marketSides": [
                        {"long": True, "team": None, "description": "Over"},
                        {"long": False, "team": None, "description": "Under"},
                    ],
                },
                {
                    "slug": "tsc-mlb-hou-sf-2026-08-12-f5-4pt5",
                    "sportsMarketType": "baseball_team_first_five_total",
                    "line": 4.5,
                    "marketSides": [
                        {"long": True, "team": None, "description": "Over"},
                        {"long": False, "team": None, "description": "Under"},
                    ],
                },
            ],
        }
    ]
}


@pytest.mark.asyncio
async def test_mlb_full_game_totals_are_parsed():
    from arbys.discovery.teams import MLB_RESOLVER as R

    client = _client(MLB_TOTALS)
    games = await fetch_polymarket_us_totals(resolver=R, sport="mlb", http_client=client)
    await client.aclose()
    assert len(games) == 1
    g = games[0]
    assert g.market_type == "total"
    assert g.line == Decimal("8.5")
    assert g.outcome_ids == {
        "OVER": "tsc-mlb-hou-sf-2026-08-12-8pt5:LONG",
        "UNDER": "tsc-mlb-hou-sf-2026-08-12-8pt5:SHORT",
    }


@pytest.mark.asyncio
async def test_first_five_totals_are_still_skipped():
    """First-five is a later phase. The payload carries one; it must not leak
    in as if it were a full-game total — they are different bets on the same
    game and would collide in the matcher's bucket."""
    from arbys.discovery.teams import MLB_RESOLVER as R

    client = _client(MLB_TOTALS)
    games = await fetch_polymarket_us_totals(resolver=R, sport="mlb", http_client=client)
    await client.aclose()
    assert all(g.line == Decimal("8.5") for g in games)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/discovery/test_polymarket_us.py -q -k mlb`
Expected: FAIL — `assert len(games) == 1` gets 0, because `baseball_team_full_game_total` is not in `TOTAL_TYPES`.

- [ ] **Step 3: Widen the type filter**

In `arbys/discovery/polymarket_us.py`:

```python
# Full-game totals only. First-five, halves and quarters are a later phase:
# they are different bets on the same game and would collide in the matcher's
# bucket key if admitted here.
TOTAL_TYPES = frozenset(
    {
        "football_team_full_game_total",
        "baseball_team_full_game_total",
    }
)
```

- [ ] **Step 4: Register MLB for totals discovery**

In `arbys/discovery/service.py`, replace the `TOTALS_SPORTS` block:

```python
# Sports whose over/under markets both venues quote.
#
# Polymarket US lists only the middle few strikes per MLB game (e.g.
# 7.5/8.5/9.5) against Kalshi's full ladder (2.5-12.5). Only shared strikes
# can match, so expect a small subset — measured 2026-08-12, 36 groups across
# 12 games. Those middle strikes are the ones worth having in play: a
# coin-flip total is what moves hardest when runs actually score.
TOTALS_SPORTS: tuple[tuple[str, TeamResolver], ...] = (
    ("nfl", NFL_RESOLVER),
    ("mlb", MLB_RESOLVER),
)
```

- [ ] **Step 5: Run the discovery tests**

Run: `venv\Scripts\python.exe -m pytest tests/discovery/ -q`
Expected: PASS.

- [ ] **Step 6: Verify against the live venues**

```bash
venv\Scripts\python.exe -c "import asyncio; from arbys.discovery.service import discover_totals_event_groups; from arbys.discovery.teams import MLB_RESOLVER; print(len(asyncio.run(discover_totals_event_groups('mlb', MLB_RESOLVER))))"
```

Expected: a non-zero count (36 on 2026-08-12; varies with the slate). Zero in the MLB offseason is correct — check the date before debugging.

- [ ] **Step 7: Commit**

```bash
git add arbys/discovery/polymarket_us.py arbys/discovery/service.py tests/discovery/test_polymarket_us.py
git commit -m "feat(discovery): wire MLB totals

Polymarket US carries baseball_team_full_game_total and Kalshi lists
KXMLBTOTAL. Held back during the venue port so it had exactly one
behavioural variable; that is now discharged.

Measured: 36 matched groups across 12 shared games. Polymarket US quotes
only the middle strikes (7.5/8.5/9.5) against Kalshi's 2.5-12.5 ladder,
and only shared strikes can match - which is fine, because the middle is
where in-play movement lives."
```

---

### Task 3: Per-outcome staleness ceiling

**Files:**
- Modify: `arbys/shared/quotebook.py`
- Test: `tests/shared/test_quotebook_staleness.py`

**Interfaces:**
- Consumes: nothing
- Produces: `QuoteBook(..., max_age_for: Callable[[str], float | None] | None = None)`. Task 4 supplies the callable.

- [ ] **Step 1: Write the failing test**

Append to `tests/shared/test_quotebook_staleness.py`:

```python
def test_per_outcome_hook_overrides_the_global_ceiling():
    """A live outcome gets a tight ceiling; a quiet one keeps the global."""
    now = [1000.0]
    book = QuoteBook(
        max_age_s=600.0,
        clock=lambda: now[0],
        max_age_for=lambda oid: 15.0 if oid == "live" else None,
    )
    book.upsert(Quote(outcome_id="live", bid=Decimal("0.4"), ask=Decimal("0.5")))
    book.upsert(Quote(outcome_id="quiet", bid=Decimal("0.4"), ask=Decimal("0.5")))

    now[0] += 20.0  # past the live ceiling, far inside the global one
    assert book.get("live") is None
    assert book.get("quiet") is not None


def test_hook_returning_none_falls_back_to_the_global():
    now = [1000.0]
    book = QuoteBook(max_age_s=600.0, clock=lambda: now[0], max_age_for=lambda _oid: None)
    book.upsert(Quote(outcome_id="x", bid=Decimal("0.4"), ask=Decimal("0.5")))
    now[0] += 100.0
    assert book.get("x") is not None
    now[0] += 600.0
    assert book.get("x") is None


def test_no_hook_is_byte_identical_to_today():
    """The hook is additive. Without one, nothing about staleness changes."""
    now = [1000.0]
    book = QuoteBook(max_age_s=600.0, clock=lambda: now[0])
    book.upsert(Quote(outcome_id="x", bid=Decimal("0.4"), ask=Decimal("0.5")))
    now[0] += 599.0
    assert book.get("x") is not None
    now[0] += 2.0
    assert book.get("x") is None


def test_snapshot_and_purge_respect_the_per_outcome_ceiling():
    now = [1000.0]
    book = QuoteBook(
        max_age_s=600.0,
        clock=lambda: now[0],
        max_age_for=lambda oid: 15.0 if oid == "live" else None,
    )
    book.upsert(Quote(outcome_id="live", bid=Decimal("0.4"), ask=Decimal("0.5")))
    book.upsert(Quote(outcome_id="quiet", bid=Decimal("0.4"), ask=Decimal("0.5")))
    now[0] += 20.0
    assert set(book.snapshot()) == {"quiet"}
    assert book.purge_stale() == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/shared/test_quotebook_staleness.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'max_age_for'`

- [ ] **Step 3: Add the hook**

In `arbys/shared/quotebook.py`, change `__init__` and `_is_stale`:

```python
    def __init__(
        self,
        *,
        max_age_s: float | None = DEFAULT_MAX_AGE_S,
        clock: Callable[[], float] = time.monotonic,
        max_age_for: Callable[[str], float | None] | None = None,
    ) -> None:
        self._quotes: dict[str, tuple[Quote, float]] = {}
        self._lock = threading.Lock()
        self._max_age_s = max_age_s
        self._clock = clock
        # Per-outcome ceiling. Returning None means "use the global". 600s is
        # right for a quiet pre-game market that legitimately does not tick for
        # minutes; it is indefensible in play, where a ten-minute-old quote is
        # not a price, it is a memory.
        self._max_age_for = max_age_for
```

`_is_stale` needs the outcome id, so give it one and update the three call sites:

```python
    def _ceiling_for(self, outcome_id: str) -> float | None:
        if self._max_age_for is not None:
            override = self._max_age_for(outcome_id)
            if override is not None:
                return override
        return self._max_age_s

    def _is_stale(self, outcome_id: str, at: float, now: float | None = None) -> bool:
        ceiling = self._ceiling_for(outcome_id)
        if ceiling is None:
            return False
        return ((self._clock() if now is None else now) - at) > ceiling
```

Update the callers:

```python
    def get(self, outcome_id: str) -> Quote | None:
        with self._lock:
            entry = self._quotes.get(outcome_id)
            if entry is None:
                return None
            quote, at = entry
            if self._is_stale(outcome_id, at):
                return None
            return replace(quote)

    def snapshot(self) -> dict[str, Quote]:
        with self._lock:
            now = self._clock()
            return {
                oid: replace(q)
                for oid, (q, at) in self._quotes.items()
                if not self._is_stale(oid, at, now)
            }

    def purge_stale(self) -> int:
        with self._lock:
            now = self._clock()
            dead = [
                oid for oid, (_q, at) in self._quotes.items() if self._is_stale(oid, at, now)
            ]
            for oid in dead:
                del self._quotes[oid]
            return len(dead)
```

> The hook is called under `self._lock`. It must not block or call back into
> the book — Task 4's implementation is a set membership test against a
> memoised frozenset, which satisfies that.

- [ ] **Step 4: Run the quotebook tests**

Run: `venv\Scripts\python.exe -m pytest tests/shared/ -q`
Expected: PASS, including every pre-existing staleness test unchanged.

- [ ] **Step 5: Commit**

```bash
git add arbys/shared/quotebook.py tests/shared/test_quotebook_staleness.py
git commit -m "feat(quotebook): allow a per-outcome staleness ceiling

600s is right for a quiet pre-game market that legitimately does not
tick for minutes. It is indefensible in play, where a ten-minute-old
quote is not a price, it is a memory.

Additive: with no hook, staleness behaviour is byte-identical to today,
which is asserted directly."
```

---

### Task 4: Memoised live-outcome set

**Files:**
- Modify: `arbys/backend/state.py`
- Test: `tests/test_backend_e2e.py`

**Interfaces:**
- Consumes: `is_in_play`, `sport_of` (Task 1); `max_age_for` (Task 3)
- Produces on `AppState`:
  - `live_outcome_ids() -> frozenset[str]` — memoised, 1s TTL
  - `invalidate_live_cache() -> None`
  - `live_quote_max_age_s() -> float` module function, reads `ARBYS_LIVE_QUOTE_MAX_AGE_S`, default 15.0

- [ ] **Step 1: Write the failing test**

Append to `tests/test_backend_e2e.py`:

```python
def test_live_outcome_ids_only_covers_in_play_groups(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from arbys.backend.state import AppState
    from arbys.shared.types import EventGroup, EventGroupLeg

    now = datetime(2026, 8, 12, 20, 0, tzinfo=UTC)
    s = AppState()
    s._now = lambda: now  # injected clock, see implementation

    live = EventGroup(
        id="mlb-AAA-BBB-2026-08-12",
        title="in play",
        start_time=now - timedelta(hours=1),
        legs=(EventGroupLeg(outcome_id="live-1", venue_id="kalshi", is_yes_side=True),),
    )
    future = EventGroup(
        id="mlb-CCC-DDD-2026-08-12",
        title="not started",
        start_time=now + timedelta(hours=3),
        legs=(EventGroupLeg(outcome_id="future-1", venue_id="kalshi", is_yes_side=True),),
    )
    s.event_groups.update({live.id: live, future.id: future})

    assert s.live_outcome_ids() == frozenset({"live-1"})


def test_live_outcome_ids_is_memoised_and_invalidated(monkeypatch):
    """The QuoteBook hook fires on every get(), so this must not rescan the
    group table each time."""
    from datetime import UTC, datetime, timedelta

    from arbys.backend.state import AppState
    from arbys.shared.types import EventGroup, EventGroupLeg

    now = datetime(2026, 8, 12, 20, 0, tzinfo=UTC)
    s = AppState()
    s._now = lambda: now

    g = EventGroup(
        id="mlb-AAA-BBB-2026-08-12",
        title="in play",
        start_time=now - timedelta(hours=1),
        legs=(EventGroupLeg(outcome_id="live-1", venue_id="kalshi", is_yes_side=True),),
    )
    s.event_groups[g.id] = g
    assert s.live_outcome_ids() == frozenset({"live-1"})

    # Mutating the table without invalidating must not be picked up yet —
    # that is what proves the cache is real.
    s.event_groups["mlb-XXX-YYY-2026-08-12"] = EventGroup(
        id="mlb-XXX-YYY-2026-08-12",
        title="also in play",
        start_time=now - timedelta(hours=1),
        legs=(EventGroupLeg(outcome_id="live-2", venue_id="kalshi", is_yes_side=True),),
    )
    assert s.live_outcome_ids() == frozenset({"live-1"})

    s.invalidate_live_cache()
    assert s.live_outcome_ids() == frozenset({"live-1", "live-2"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/test_backend_e2e.py -q -k live_outcome`
Expected: FAIL — `AttributeError: 'AppState' object has no attribute 'live_outcome_ids'`

- [ ] **Step 3: Add the env reader**

In `arbys/backend/state.py`, next to `polymarket_us_poll_s`:

```python
DEFAULT_LIVE_QUOTE_MAX_AGE_S = 15.0


def live_quote_max_age_s() -> float:
    """Staleness ceiling for outcomes belonging to an in-play group.

    The global 600s exists so a quiet market is not discarded. In play that
    reasoning inverts: nothing is quiet, and a ten-minute-old quote is a
    memory rather than a price.
    """
    raw = os.environ.get("ARBYS_LIVE_QUOTE_MAX_AGE_S")
    if raw is None:
        return DEFAULT_LIVE_QUOTE_MAX_AGE_S
    try:
        return max(1.0, float(raw))
    except ValueError:
        return DEFAULT_LIVE_QUOTE_MAX_AGE_S
```

- [ ] **Step 4: Add the memoised set to `AppState`**

Add these imports at the top of `state.py`:

```python
from datetime import UTC, datetime

from ..shared.liveness import is_in_play, sport_of
```

In `AppState.__init__`, **before** `self.quotebook` is constructed:

```python
        # Injected so tests can pin it; production uses the wall clock.
        self._now: Callable[[], datetime] = lambda: datetime.now(UTC)
        self._live_cache: frozenset[str] = frozenset()
        self._live_cache_at: float = 0.0
        self._live_cache_ttl_s: float = 1.0
```

Then construct the quotebook with the hook:

```python
        self.quotebook = QuoteBook(
            max_age_s=quote_max_age_s(),
            max_age_for=self._max_age_for_outcome,
        )
```

And add the methods:

```python
    def _max_age_for_outcome(self, outcome_id: str) -> float | None:
        """Tight ceiling for in-play outcomes, global for everything else.

        Called from inside QuoteBook's lock on every get(), so it must stay a
        set-membership test — hence the memoised frozenset rather than a scan.
        """
        if outcome_id in self.live_outcome_ids():
            return live_quote_max_age_s()
        return None

    def live_outcome_ids(self) -> frozenset[str]:
        """Outcome ids belonging to a group that is currently being played.

        **Memoised deliberately.** This feeds the QuoteBook staleness hook,
        which fires once per leg per evaluation, and evaluation runs on every
        inbound quote. Rebuilding a ~1300-entry set at that rate would put a
        full scan of the group table on the hottest path in the system. One
        second of staleness is irrelevant against windows measured in hours.
        """
        monotonic = time.monotonic()
        if monotonic - self._live_cache_at < self._live_cache_ttl_s:
            return self._live_cache
        now = self._now()
        live: set[str] = set()
        for group in self.event_groups.values():
            if not is_in_play(group.start_time, sport_of(group.id), now):
                continue
            for leg in group.legs:
                live.add(leg.outcome_id)
        self._live_cache = frozenset(live)
        self._live_cache_at = monotonic
        return self._live_cache

    def invalidate_live_cache(self) -> None:
        """Force the next ``live_outcome_ids`` call to rebuild."""
        self._live_cache_at = 0.0
```

Add `import time` at the top if not already present.

> The test pins `_live_cache_at = 0.0` semantics: a fresh `AppState` has
> `_live_cache_at = 0.0`, and `time.monotonic()` on a running process is well
> above `1.0`, so the first call always builds.

- [ ] **Step 5: Invalidate when groups change**

In `arbys/discovery/service.py`, inside `DiscoveryService.run_once`, immediately after the `if changed:` block gains its restart:

```python
        if changed:
            self._state.invalidate_live_cache()
            await self._state.restart_ingest()
```

And in `AppState.bootstrap`, after the event-group hydration loop:

```python
        self.invalidate_live_cache()
```

- [ ] **Step 6: Run the tests**

Run: `venv\Scripts\python.exe -m pytest tests/test_backend_e2e.py tests/shared/ -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add arbys/backend/state.py arbys/discovery/service.py tests/test_backend_e2e.py
git commit -m "feat(state): memoised set of in-play outcome ids

Liveness belongs to an event group, but the adapter and quote book both
work in outcome ids; AppState is the only place that can bridge them.

Memoised with a 1s TTL because this feeds the QuoteBook staleness hook,
which fires once per leg per evaluation on every inbound quote. A naive
implementation would put a full scan of ~1300 legs on the hottest path
in the system. A second of staleness is nothing against in-play windows
measured in hours."
```

---

### Task 5: Two-tier concurrent polling and refresh

**Files:**
- Modify: `arbys/adapters/polymarket_us.py`
- Modify: `arbys/backend/state.py` (factory wiring, `refresh_quotes`)
- Test: `tests/adapters/test_polymarket_us.py`

**Interfaces:**
- Consumes: `AppState.live_outcome_ids` (Task 4)
- Produces:
  - `PolymarketUsAdapter(..., live_poll_interval_s: float = 1.0, live_outcome_ids: Callable[[], frozenset[str]] | None = None, max_concurrency: int = 20)`
  - `PolymarketUsAdapter.refresh(outcome_ids: list[str]) -> list[Quote]`
  - `polymarket_us_live_poll_s() -> float` in `state.py`
  - `AppState.refresh_quotes(outcome_ids: list[str]) -> int` — used by Task 7

- [ ] **Step 1: Write the failing test**

Append to `tests/adapters/test_polymarket_us.py`:

```python
@pytest.mark.asyncio
async def test_live_slugs_are_polled_more_often_than_the_rest():
    """A full MLB slate is ~60 slugs; the live tier must not wait on the
    base sweep to see them."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=BBO)

    client = _mock_client(handler)
    adapter = PolymarketUsAdapter(
        outcome_ids=["live:LONG", "cold:LONG"],
        http_client=client,
        poll_interval_s=0.5,
        live_poll_interval_s=0.01,
        live_outcome_ids=lambda: frozenset({"live:LONG"}),
    )
    seen = 0
    async for _q in adapter.stream_quotes():
        seen += 1
        if seen >= 6:
            break
    await adapter.close()
    await client.aclose()

    live_calls = sum(1 for c in calls if "/live/" in c)
    cold_calls = sum(1 for c in calls if "/cold/" in c)
    assert live_calls > cold_calls


@pytest.mark.asyncio
async def test_one_failing_slug_does_not_abort_the_sweep():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/bad/" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, json=BBO)

    client = _mock_client(handler)
    adapter = PolymarketUsAdapter(
        outcome_ids=["bad:LONG", "good:LONG"],
        http_client=client,
        poll_interval_s=0.01,
        live_poll_interval_s=0.01,
        live_outcome_ids=lambda: frozenset({"bad:LONG", "good:LONG"}),
    )
    got = []
    async for q in adapter.stream_quotes():
        got.append(q.outcome_id)
        if len(got) >= 2:
            break
    await adapter.close()
    await client.aclose()
    assert all(o.startswith("good") for o in got)


@pytest.mark.asyncio
async def test_refresh_fetches_named_outcomes_on_demand():
    """Execution needs a way to re-ask about one leg without waiting for a
    poll tick."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=BBO)

    client = _mock_client(handler)
    adapter = PolymarketUsAdapter(outcome_ids=[], http_client=client)
    quotes = await adapter.refresh(["slug1:LONG", "slug1:SHORT"])
    await adapter.close()
    await client.aclose()

    assert len(calls) == 1  # LONG and SHORT share one market
    assert {q.outcome_id for q in quotes} == {"slug1:LONG", "slug1:SHORT"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/adapters/test_polymarket_us.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'live_poll_interval_s'`

- [ ] **Step 3: Rewrite the adapter's polling**

In `arbys/adapters/polymarket_us.py`, extend `__init__`:

```python
    def __init__(
        self,
        *,
        poll_interval_s: float = 5.0,
        live_poll_interval_s: float = 1.0,
        outcome_ids: list[str] | None = None,
        http_client: httpx.AsyncClient | None = None,
        live_outcome_ids: Callable[[], frozenset[str]] | None = None,
        max_concurrency: int = 20,
    ) -> None:
        self._poll_interval_s = poll_interval_s
        self._live_poll_interval_s = live_poll_interval_s
        self._outcome_ids = outcome_ids or []
        self._live_outcome_ids = live_outcome_ids
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=10.0)
        self._sem = asyncio.Semaphore(max_concurrency)
```

Add `from collections.abc import AsyncIterator, Callable` to the imports.

Add a concurrent multi-slug fetch and the public `refresh`:

```python
    async def _fetch_many(self, slugs: list[str]) -> list[Quote]:
        """Fetch several slugs at once.

        The live tier has a deadline the sequential loop cannot meet: a full
        MLB slate is ~15 concurrent games x ~4 markets = ~60 slugs, which at
        ~30ms each is ~1.8s — already over a 1s budget. Measured 2026-08-11,
        53 concurrent /bbo calls returned in 1.46s with no rate limiting.

        ``return_exceptions=True`` matters: one bad slug must not abort the
        whole sweep and stall every other market for a tick.
        """
        if not slugs:
            return []

        async def one(slug: str) -> list[Quote]:
            async with self._sem:
                return await self._fetch_quotes(slug)

        results = await asyncio.gather(*(one(s) for s in slugs), return_exceptions=True)
        out: list[Quote] = []
        for r in results:
            if isinstance(r, BaseException):
                continue
            out.extend(r)
        return out

    async def refresh(self, outcome_ids: list[str]) -> list[Quote]:
        """Re-fetch specific outcomes right now, bypassing the poll schedule.

        Used by the execution path to verify a leg whose quote may predate a
        market-moving event. Returns whatever came back; an empty list means
        the venue could not be reached, which is **not** evidence that a
        resting order is still there.
        """
        slugs = sorted({split_outcome_id(oid)[0] for oid in outcome_ids})
        return await self._fetch_many(slugs)

    def _live_slugs(self) -> set[str]:
        if self._live_outcome_ids is None:
            return set()
        live = self._live_outcome_ids()
        return {
            split_outcome_id(oid)[0] for oid in self._outcome_ids if oid in live
        }

    async def stream_quotes(self) -> AsyncIterator[Quote]:
        all_slugs = self._slugs()
        if not all_slugs:
            return
        # How many live ticks pass between full sweeps.
        ticks_per_sweep = max(
            1, int(round(self._poll_interval_s / max(self._live_poll_interval_s, 1e-6)))
        )
        tick = 0
        while True:
            live = self._live_slugs()
            if live:
                for quote in await self._fetch_many(sorted(live)):
                    yield quote
            if tick % ticks_per_sweep == 0:
                cold = [s for s in all_slugs if s not in live]
                for slug in cold:
                    for quote in await self._fetch_quotes(slug):
                        yield quote
            tick += 1
            await asyncio.sleep(
                self._live_poll_interval_s if live else self._poll_interval_s
            )
```

> The cold tier stays sequential on purpose: it has no deadline, and
> sequential polling is gentler on the gateway.

- [ ] **Step 4: Wire it in `state.py`**

Add the env reader next to `polymarket_us_poll_s`:

```python
DEFAULT_POLYMARKET_US_LIVE_POLL_S = 1.0


def polymarket_us_live_poll_s() -> float:
    """Poll interval for outcomes in an in-play group. 0.5s floor."""
    raw = os.environ.get("ARBYS_POLYMARKET_US_LIVE_POLL_S")
    if raw is None:
        return DEFAULT_POLYMARKET_US_LIVE_POLL_S
    try:
        return max(0.5, float(raw))
    except ValueError:
        return DEFAULT_POLYMARKET_US_LIVE_POLL_S
```

`_default_adapter_factories` is a module function with no `AppState`, so the factory must be built where `self` exists. In `AppState.__init__`, replace the plain assignment:

```python
        self.adapter_factories: dict[str, AdapterFactory] = _default_adapter_factories()
        self.adapter_factories["polymarket_us"] = lambda oids: PolymarketUsAdapter(
            outcome_ids=oids,
            poll_interval_s=polymarket_us_poll_s(),
            live_poll_interval_s=polymarket_us_live_poll_s(),
            live_outcome_ids=self.live_outcome_ids,
        )
```

Add `refresh_quotes` to `AppState`. An adapter must only be asked about its
own venue's outcomes, so the outcome→venue map is built from the group table:

```python
    def _venue_of_outcome(self) -> dict[str, str]:
        """outcome_id -> venue_id, from every registered group's legs.

        Needed because ``outcome_id`` is venue-native and not portable: asking
        Kalshi's adapter about a Polymarket slug is meaningless.
        """
        return {
            leg.outcome_id: leg.venue_id
            for group in self.event_groups.values()
            for leg in group.legs
        }

    async def refresh_quotes(self, outcome_ids: list[str]) -> int:
        """Force a fresh fetch of specific outcomes into the quote book.

        Returns how many quotes were refreshed. Adapters that push (Kalshi's
        authenticated WebSocket) have nothing to re-ask and are skipped; only
        adapters exposing ``refresh`` participate.

        A failure refreshes nothing and returns a lower count. It must never
        be read as "the resting order is still there" — the caller decides
        what to do with a leg it could not verify.
        """
        by_outcome = self._venue_of_outcome()
        refreshed = 0
        for adapter in self._adapters:
            refresh = getattr(adapter, "refresh", None)
            if refresh is None:
                continue
            mine = [
                oid for oid in outcome_ids if by_outcome.get(oid) == adapter.venue_id
            ]
            if not mine:
                continue
            with contextlib.suppress(Exception):
                for quote in await refresh(mine):
                    self.quotebook.upsert(quote)
                    refreshed += 1
        return refreshed
```

- [ ] **Step 5: Run the adapter tests**

Run: `venv\Scripts\python.exe -m pytest tests/adapters/test_polymarket_us.py -q`
Expected: PASS, 11 tests.

- [ ] **Step 6: Run the whole suite**

Run: `venv\Scripts\python.exe -m pytest -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add arbys/adapters/polymarket_us.py arbys/backend/state.py tests/adapters/test_polymarket_us.py
git commit -m "feat(adapters): two-tier polling with a concurrent live tier

In-play slugs poll at 1s, everything else stays at 5s. The live tier
fetches concurrently because it cannot meet its deadline otherwise: a
full MLB slate is ~60 slugs, ~1.8s sequentially against a 1s budget.
The cold tier stays sequential - no deadline, gentler on the gateway.

Adds refresh(), which the execution path needs to re-ask about a single
leg without waiting for a tick."
```

---

### Task 6: Skew labelling in the engine

**Files:**
- Modify: `arbys/shared/arb_engine.py` (`ArbOpportunity`)
- Modify: `arbys/ingest/engine_runtime.py`
- Test: `tests/test_engine_runtime.py`

**Interfaces:**
- Consumes: `is_in_play`, `sport_of` (Task 1)
- Produces:
  - `ArbOpportunity.unconfirmed_stale_leg: tuple[str, ...] = ()`
  - `EngineRuntime(..., max_skew_s: float = 3.0, now: Callable[[], datetime] | None = None)`

`repositories.insert_opportunity` reads named fields explicitly, so the new field is **transient and not persisted**. No migration.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_engine_runtime.py`:

```python
def _skew_setup(start_time, ages):
    """Build an engine whose book reports fixed per-outcome ages."""
    from datetime import UTC, datetime
    from decimal import Decimal

    from arbys.ingest.engine_runtime import EngineRuntime
    from arbys.shared.fees import ZeroFeeModel
    from arbys.shared.quotebook import QuoteBook
    from arbys.shared.types import EventGroup, EventGroupLeg, Quote

    book = QuoteBook(max_age_s=None)
    book.upsert(Quote(outcome_id="k", bid=Decimal("0.40"), ask=Decimal("0.45")))
    book.upsert(Quote(outcome_id="p", bid=Decimal("0.40"), ask=Decimal("0.45")))
    book.get_with_age = lambda oid: (book.get(oid), ages[oid])  # type: ignore[assignment]

    group = EventGroup(
        id="mlb-AAA-BBB-2026-08-12",
        title="t",
        start_time=start_time,
        legs=(
            EventGroupLeg(outcome_id="k", venue_id="kalshi", is_yes_side=True),
            EventGroupLeg(outcome_id="p", venue_id="polymarket_us", is_yes_side=False),
        ),
    )
    engine = EngineRuntime(
        quotebook=book,
        fees={"kalshi": ZeroFeeModel("kalshi"), "polymarket_us": ZeroFeeModel("polymarket_us")},
        max_skew_s=3.0,
        now=lambda: datetime(2026, 8, 12, 20, 0, tzinfo=UTC),
    )
    engine.register_group(group)
    return engine


def test_in_play_group_with_skewed_legs_is_flagged_not_dropped():
    """A stale leg may still be fillable - that is the opportunity. Dropping
    it throws away the profitable case along with the ghost."""
    from datetime import UTC, datetime

    start = datetime(2026, 8, 12, 19, 0, tzinfo=UTC)  # 1h ago -> in play
    engine = _skew_setup(start, {"k": 0.2, "p": 8.0})
    opps = engine.evaluate_now("mlb-AAA-BBB-2026-08-12")
    assert opps, "must still publish"
    assert opps[0].unconfirmed_stale_leg == ("p",)


def test_in_play_group_within_tolerance_is_not_flagged():
    from datetime import UTC, datetime

    start = datetime(2026, 8, 12, 19, 0, tzinfo=UTC)
    engine = _skew_setup(start, {"k": 0.2, "p": 0.9})
    opps = engine.evaluate_now("mlb-AAA-BBB-2026-08-12")
    assert opps
    assert opps[0].unconfirmed_stale_leg == ()


def test_pre_game_group_with_identical_skew_is_never_flagged():
    """Pre-game, legs sit unticked for minutes and skew carries no
    information. Flagging here would send every quiet market through a
    needless verification round trip."""
    from datetime import UTC, datetime

    start = datetime(2026, 8, 12, 23, 0, tzinfo=UTC)  # 3h away -> not started
    engine = _skew_setup(start, {"k": 0.2, "p": 8.0})
    opps = engine.evaluate_now("mlb-AAA-BBB-2026-08-12")
    assert opps
    assert opps[0].unconfirmed_stale_leg == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/test_engine_runtime.py -q -k skew`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'max_skew_s'`

- [ ] **Step 3: Add the field**

In `arbys/shared/arb_engine.py`, extend `ArbOpportunity`:

```python
@dataclass(frozen=True)
class ArbOpportunity:
    event_group_id: str
    legs: tuple[ArbLeg, ...]
    total_stake: Decimal
    guaranteed_profit: Decimal
    guaranteed_profit_bps: Decimal
    # Outcome ids whose quote predates the others by more than the skew
    # tolerance, on a group that is currently being played. Non-empty means
    # "this edge may be a latency artifact rather than a real divergence —
    # verify against a fresh quote before filling". Transient: not persisted.
    unconfirmed_stale_leg: tuple[str, ...] = ()
```

- [ ] **Step 4: Label in the engine**

In `arbys/ingest/engine_runtime.py`, extend `__init__`:

```python
    def __init__(
        self,
        *,
        quotebook: QuoteBook,
        fees: FeeModelRegistry,
        on_opportunity: OpportunityHandler | None = None,
        on_opportunities: OpportunitySetHandler | None = None,
        target_payoff: Decimal = DEFAULT_TARGET_PAYOFF,
        max_skew_s: float = 3.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        ...
        self._max_skew_s = max_skew_s
        self._now = now or (lambda: datetime.now(UTC))
```

Imports: `from datetime import UTC, datetime` and `from ..shared.liveness import is_in_play, sport_of`.

Add the helper and apply it at the end of `evaluate_now`:

```python
    def _stale_legs(self, group: EventGroup) -> tuple[str, ...]:
        """Legs whose quote is materially older than the freshest one.

        Only meaningful in play. Pre-game, every leg may legitimately be
        minutes old and the spread between them says nothing.
        """
        if self._max_skew_s <= 0:
            return ()
        if not is_in_play(group.start_time, sport_of(group.id), self._now()):
            return ()
        ages: dict[str, float] = {}
        for leg in group.legs:
            entry = self._book.get_with_age(leg.outcome_id)
            if entry is not None:
                ages[leg.outcome_id] = entry[1]
        if len(ages) < 2:
            return ()
        freshest = min(ages.values())
        return tuple(
            sorted(oid for oid, age in ages.items() if age - freshest > self._max_skew_s)
        )
```

At the end of `evaluate_now`, before `return found`:

```python
        stale = self._stale_legs(group)
        if stale:
            found = [replace(o, unconfirmed_stale_leg=stale) for o in found]
        return found
```

Add `from dataclasses import replace` to the imports.

- [ ] **Step 5: Run the engine tests**

Run: `venv\Scripts\python.exe -m pytest tests/test_engine_runtime.py -q`
Expected: PASS, including pre-existing tests unchanged.

- [ ] **Step 6: Commit**

```bash
git add arbys/shared/arb_engine.py arbys/ingest/engine_runtime.py tests/test_engine_runtime.py
git commit -m "feat(engine): label in-play opportunities with a stale leg

Kalshi pushes, Polymarket US polls, so mid-game the two legs can
describe different moments. That gap might be a real divergence or it
might be a latency artifact, and the quote alone cannot say which.

Labels rather than suppresses: if the resting order on the slower venue
has not been pulled yet, filling against it is real money. Dropping
these would throw away the profitable case along with the ghost.

Pre-game groups are never flagged - legs sit unticked for minutes there
and the spread between them carries no information."
```

---

### Task 7: Execute-time verification

**Files:**
- Modify: `arbys/backend/app.py:286-318`
- Modify: `arbys/backend/state.py` (`refresh_quotes` venue filter from Task 5)
- Test: `tests/test_backend_e2e.py`

**Interfaces:**
- Consumes: `ArbOpportunity.unconfirmed_stale_leg` (Task 6), `AppState.refresh_quotes` (Task 5)
- Produces: no new symbols; `POST /paper/execute` gains a 409 path

- [ ] **Step 1: Write the failing test**

`refresh_quotes` is `AppState`-level and directly testable; the endpoint branch
is three lines of control flow over it. Test the contract the endpoint depends
on, which is where the dangerous failure lives.

Append to `tests/test_backend_e2e.py`:

```python
@pytest.mark.asyncio
async def test_refresh_quotes_only_asks_an_adapter_about_its_own_venue():
    """outcome_id is venue-native and not portable - asking Kalshi's adapter
    about a Polymarket slug is meaningless."""
    from decimal import Decimal

    from arbys.backend.state import AppState
    from arbys.shared.types import EventGroup, EventGroupLeg, Quote

    asked: dict[str, list[str]] = {}

    class FakeAdapter:
        def __init__(self, venue_id):
            self.venue_id = venue_id

        async def refresh(self, outcome_ids):
            asked[self.venue_id] = list(outcome_ids)
            return [
                Quote(outcome_id=o, bid=Decimal("0.40"), ask=Decimal("0.45"))
                for o in outcome_ids
            ]

    class PushOnlyAdapter:
        venue_id = "kalshi"  # no refresh(): nothing to re-ask

    s = AppState()
    s.event_groups["g1"] = EventGroup(
        id="g1",
        title="t",
        legs=(
            EventGroupLeg(outcome_id="k1", venue_id="kalshi", is_yes_side=True),
            EventGroupLeg(outcome_id="p1", venue_id="polymarket_us", is_yes_side=False),
        ),
    )
    s._adapters = [PushOnlyAdapter(), FakeAdapter("polymarket_us")]

    n = await s.refresh_quotes(["k1", "p1"])
    assert asked == {"polymarket_us": ["p1"]}, "must not ask about the other venue"
    assert n == 1
    assert s.quotebook.get("p1") is not None


@pytest.mark.asyncio
async def test_refresh_quotes_survives_an_adapter_error_and_refreshes_nothing():
    """An unreachable venue is NOT evidence that a resting order is still
    there. It must report zero, not raise, and not invent a quote."""
    from arbys.backend.state import AppState
    from arbys.shared.types import EventGroup, EventGroupLeg

    class BoomAdapter:
        venue_id = "polymarket_us"

        async def refresh(self, outcome_ids):
            raise RuntimeError("gateway down")

    s = AppState()
    s.event_groups["g1"] = EventGroup(
        id="g1",
        title="t",
        legs=(EventGroupLeg(outcome_id="p1", venue_id="polymarket_us", is_yes_side=True),),
    )
    s._adapters = [BoomAdapter()]

    assert await s.refresh_quotes(["p1"]) == 0
    assert s.quotebook.get("p1") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/test_backend_e2e.py -q -k refresh_quotes`
Expected: FAIL — `AttributeError: 'AppState' object has no attribute 'refresh_quotes'` before Task 5 is applied; if Task 5 is already in, these pass and confirm its venue filter.


- [ ] **Step 3: Add verification to the endpoint**

In `arbys/backend/app.py`, inside `paper_execute`, immediately after `opp` is resolved and before the position-cap block:

```python
        # A flagged opportunity may be a latency artifact: one leg's quote
        # predates a market-moving event. Re-ask that venue before filling.
        # This is mandatory, not defensive — paper_broker fills at the
        # quotebook price unconditionally, so skipping it would report
        # in-game profits that live trading could not reproduce.
        if opp.unconfirmed_stale_leg:
            log.info(
                "verifying stale leg(s) %s on %s before filling",
                opp.unconfirmed_stale_leg,
                opp.event_group_id,
            )
            await s.refresh_quotes(list(opp.unconfirmed_stale_leg))
            confirmed = s.live_opportunities_for(opp.event_group_id)
            wanted_legs = {leg.outcome_id for leg in opp.legs if leg.is_buy}
            opp = next(
                (
                    c
                    for c in confirmed
                    if {leg.outcome_id for leg in c.legs if leg.is_buy} == wanted_legs
                ),
                None,
            )
            if opp is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "edge did not survive verification against a fresh quote; "
                        "the resting order was gone"
                    ),
                )
```

Ensure `log = logging.getLogger(__name__)` exists in `app.py`; add `import logging` if not.

- [ ] **Step 4: Confirm the endpoint branch by inspection**

The two behaviours the 409 path depends on are already asserted by Step 1:
`refresh_quotes` asks only the owning venue, and an adapter failure refreshes
nothing rather than raising. What remains is three lines of control flow, which
must read exactly as follows — in particular there is **no `else` and no
fallback to the pre-refresh `opp`**:

```
refresh the flagged legs
re-detect against the refreshed book
if nothing came back -> raise 409
```

A fallback here would let `paper_broker` fill at the remembered price, which is
the precise failure this task exists to prevent.

- [ ] **Step 5: Run the tests**

Run: `venv\Scripts\python.exe -m pytest tests/test_backend_e2e.py -q`
Expected: PASS.

- [ ] **Step 6: Run the whole suite and lint**

Run: `venv\Scripts\python.exe -m pytest -q && venv\Scripts\python.exe -m ruff check .`
Expected: PASS and clean.

- [ ] **Step 7: Commit**

```bash
git add arbys/backend/app.py arbys/backend/state.py tests/test_backend_e2e.py
git commit -m "feat(execute): verify a flagged stale leg before filling

Re-asks the slower venue about the flagged leg, then re-detects. Still
there at that price: the resting order was real, fill it. Gone: reject
with 409 rather than fill against a memory.

Mandatory rather than defensive. paper_broker fills at the quotebook
price unconditionally, so without this a stale quote always fills at the
pre-news price and paper P&L would show consistent in-game profits that
live trading could not reproduce."
```

---

### Task 8: Instrumentation, config, and docs

**Files:**
- Modify: `arbys/backend/app.py` (verification outcome logging)
- Modify: `.env.example`, `CLAUDE.md`

**Interfaces:**
- Consumes: everything above
- Produces: no new symbols

- [ ] **Step 1: Log the verification outcome**

In `arbys/backend/app.py`, replace the bare `log.info` from Task 7 with a before/after record. This is the measurement that decides whether in-game divergence is real:

```python
        if opp.unconfirmed_stale_leg:
            stale_ids = list(opp.unconfirmed_stale_leg)
            before = {
                oid: s.quotebook.get_with_age(oid) for oid in stale_ids
            }
            await s.refresh_quotes(stale_ids)
            after = {oid: s.quotebook.get_with_age(oid) for oid in stale_ids}
            confirmed = s.live_opportunities_for(opp.event_group_id)
            wanted_legs = {leg.outcome_id for leg in opp.legs if leg.is_buy}
            opp = next(
                (
                    c
                    for c in confirmed
                    if {leg.outcome_id for leg in c.legs if leg.is_buy} == wanted_legs
                ),
                None,
            )
            # The number that decides whether any of this is a real edge:
            # how often was a flagged leg still fillable, and did it depend on
            # resting size? Logged per attempt so it can be aggregated.
            for oid in stale_ids:
                b, a = before.get(oid), after.get(oid)
                log.info(
                    "stale-leg verification group=%s outcome=%s age_before=%.2f "
                    "ask_before=%s ask_after=%s ask_size_after=%s survived=%s",
                    body.event_group_id,
                    oid,
                    b[1] if b else -1.0,
                    b[0].ask if b else None,
                    a[0].ask if a else None,
                    a[0].ask_size if a else None,
                    opp is not None,
                )
            if opp is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "edge did not survive verification against a fresh quote; "
                        "the resting order was gone"
                    ),
                )
```

- [ ] **Step 2: Add config to `.env.example`**

```
# In-play tuning. A game is "in play" from its start time until a generous
# per-sport window closes (see arbys/shared/liveness.py).
#
# Poll interval for outcomes in an in-play group. 0.5s floor.
ARBYS_POLYMARKET_US_LIVE_POLL_S=1
# Staleness ceiling for in-play outcomes. The global 600s exists so a quiet
# market is not discarded; in play nothing is quiet and a ten-minute-old quote
# is a memory, not a price.
ARBYS_LIVE_QUOTE_MAX_AGE_S=15
# Max spread between two legs' quote ages, in play, before an opportunity is
# flagged for verification. Labelling only — flagged edges are still
# published, then re-checked against a fresh quote at execution. 0 disables.
ARBYS_MAX_QUOTE_SKEW_S=3
```

- [ ] **Step 3: Document in CLAUDE.md**

Add to the config list:

```markdown
- `ARBYS_POLYMARKET_US_LIVE_POLL_S` / `ARBYS_LIVE_QUOTE_MAX_AGE_S` /
  `ARBYS_MAX_QUOTE_SKEW_S` — in-play tuning. See **In-play divergence**.
```

Add a new section after **Gross vs net is deliberate in the UI**:

```markdown
### In-play divergence is a race, not an arbitrage

Pre-game the two venues agree closely, which is why net arbs are rare — fee
drag is ~3.25¢ at a coin flip against a measured maximum gross divergence of
2.75¢. Edges survive at the extremes: a 0.88/0.10 total carries 1.28¢ of drag
and cleared +0.72¢ on 2026-08-12, while an identical 2¢ gross edge at
0.36/0.62 lost 1.03¢.

In play, prices move and divergence can exceed the drag. But Kalshi pushes
over a WebSocket while Polymarket US is polled, so mid-game the two legs can
describe **different moments**. A gap between them is then either a real
divergence or a latency artifact, and the quote alone cannot say which.

`EngineRuntime` flags in-play opportunities whose leg ages differ by more than
`ARBYS_MAX_QUOTE_SKEW_S` (`unconfirmed_stale_leg`). It **labels rather than
suppresses on purpose**: if the resting order on the slower venue has not been
pulled yet, filling against it is real money. `POST /paper/execute`
force-refreshes the flagged leg and rejects with 409 if the edge did not
survive.

**That verification is mandatory, not defensive.** `paper_broker` fills at the
quotebook price unconditionally, so without it a stale quote always fills at
the pre-news price and paper P&L would show in-game profits live trading could
not reproduce.

Be clear about what this is: a **race with legging risk**, not a guaranteed
ticket — you can fill the fast leg and miss the slow one. And we run seconds,
not milliseconds, behind the news. Whether a mispriced resting order survives
that long is **unmeasured**; the verification logging exists to answer it.
Grep `stale-leg verification` and aggregate `survived=` by `ask_size_after`
before trusting this path with real money.
```

- [ ] **Step 4: Final verification**

```bash
venv\Scripts\python.exe -m pytest -q
venv\Scripts\python.exe -m ruff check .
cd frontend && npm run build && npm run lint
```

Expected: all green. Do not claim completion without seeing this output.

- [ ] **Step 5: Live smoke**

Restart the backend and confirm MLB totals register and nothing regressed:

```bash
venv\Scripts\python.exe -c "import httpx; from collections import Counter; m=httpx.get('http://127.0.0.1:8000/monitored',timeout=90).json(); print(len(m), Counter(g['id'].split('-')[0] for g in m)); print('fully quoted:', sum(1 for g in m if g['fully_quoted']))"
```

Expected: MLB total groups present (ids like `mlb-…-total-8.5`), and `fully_quoted` still equal to the group count.

- [ ] **Step 6: Commit**

```bash
git add arbys/backend/app.py .env.example CLAUDE.md
git commit -m "docs: record in-play divergence as a race, not an arbitrage

Adds the verification logging that answers the only question that
decides whether this path is real - how often a flagged leg is still
fillable, and whether that depends on resting size - and states plainly
that the number is currently unmeasured."
```

---

## Post-implementation

Two things are deliberately left open:

**The measurement.** `ARBYS_MAX_QUOTE_SKEW_S=3.0` is an estimate, not an observation. Once this has run through a live game, aggregate the `stale-leg verification` log lines: what fraction have `survived=True`, and does it correlate with `ask_size_after`? A high survival rate on thin books and a low one on deep books would be the expected shape, and it decides whether this is an edge or a mirage.

**Legging risk.** Nothing here protects against filling the fast leg and missing the slow one. For paper that is a reporting nuance; for real money it is the main obstacle, and it deserves its own design before this path touches a live account.
