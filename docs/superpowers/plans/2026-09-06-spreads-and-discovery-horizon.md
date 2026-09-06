# Spreads and Discovery Horizon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discover full-game spread markets on Kalshi and Polymarket US for MLB, NFL and NCAAF as cross-venue event groups, and bound every discovery pass to games within `ARBYS_DISCOVERY_HORIZON_DAYS` Eastern days.

**Architecture:** Two new parsers normalise each venue's spread into one canonical `VenueGame` — positive `line`, `anchor` = code of the team that must win by more than it, outcomes keyed by team code — so the existing matcher pairs them on `(anchor, line)` with no logic change beyond id and title. A new `horizon.py` holds the date rule; the discovery service applies it to every venue list before matching and the Kalshi event parsers apply it before their per-event market call.

**Tech Stack:** Python 3.11, httpx (`MockTransport` in tests), pytest with `asyncio_mode = "auto"`, ruff.

Spec: `docs/superpowers/specs/2026-09-06-spreads-and-discovery-horizon-design.md`.

## Global Constraints

- Run everything from the repo root with `venv\Scripts\python.exe`; never a bare `python`.
- `venv\Scripts\python.exe -m pytest -q` must stay green; `venv\Scripts\python.exe -m ruff check .` must stay clean. mypy is **not** part of the bar (71 pre-existing errors) — do not start a cleanup.
- All money and prices are `Decimal`. Lines are `Decimal`, never float.
- Tests never hit a real venue: REST is mocked with `httpx.MockTransport`.
- `arbys/shared/` must not gain imports of httpx, SQLAlchemy or FastAPI. (This plan does not touch it.)
- `outcome_id` values are venue-native; carry `venue_id` with them.
- Nothing in the engine, broker, settlement, ticket service, auto-trader, adapters, DB schema or frontend changes.
- Commit after each task with a scoped `git add` naming only the task's files. Before each commit run `git status --porcelain` and confirm nothing else is staged (first column `M`/`D`/`A`).
- Branch: `spreads` (already created and checked out). Do not use a worktree — `arbys` is an editable install pinned to this directory.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## File Structure

| file | responsibility |
| --- | --- |
| `arbys/discovery/horizon.py` (create) | `ARBYS_DISCOVERY_HORIZON_DAYS` config, Eastern today, `within_horizon`, `filter_horizon` |
| `arbys/discovery/kalshi_spreads.py` (create) | Kalshi spread fetcher: series registry, ticker-suffix anchor, `strike_type` guard |
| `arbys/discovery/polymarket_us.py` (modify) | `SPREAD_TYPES`, `fetch_polymarket_us_spreads`, title cross-check |
| `arbys/discovery/teams.py` (modify) | `code_aliases` on `TeamResolver`; `AZ→ARI`, `JAC→JAX` |
| `arbys/discovery/matcher.py` (modify) | spread group id and title; `anchor_participant()` |
| `arbys/discovery/kalshi_sports.py` (modify) | `horizon_days` on `fetch_kalshi_team_games`, pre-market-call check |
| `arbys/discovery/kalshi_totals.py` (modify) | same for totals |
| `arbys/discovery/service.py` (modify) | `_discover_pair` helper, horizon filter, `SPREADS_SPORTS`, `ARBYS_ENABLE_SPREADS` |
| `tests/discovery/test_horizon.py` (create) | date rule, config, Kalshi pre-filter |
| `tests/discovery/test_spreads.py` (create) | both parsers, end-to-end convention |
| `tests/discovery/test_teams.py`, `test_matcher.py`, `test_service.py`, `test_polymarket_us.py` (modify) | aliases, id/title, service wiring, renamed phase-1 test |
| `.env.example`, `CLAUDE.md`, `docs/RUNBOOK.md` (modify) | the two knobs, Phase 2 rewritten as wired |

---

### Task 1: Team code aliases

**Files:**
- Modify: `arbys/discovery/teams.py` (`TeamResolver.__init__` ~line 74, `by_code` ~line 114, `MLB_RESOLVER` line 184, the `NFL_RESOLVER = TeamResolver(NFL_TEAMS…)` line)
- Test: `tests/discovery/test_teams.py`

**Interfaces:**
- Produces: `TeamResolver(teams, aliases=None, code_aliases: dict[str, str] | None = None)`; `by_code(code)` now also resolves alias codes to the canonical `Team`; module constants `MLB_CODE_ALIASES = {"AZ": "ARI"}`, `NFL_CODE_ALIASES = {"JAC": "JAX"}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/discovery/test_teams.py`. Check its imports first; add `NFL_RESOLVER` to the `from arbys.discovery.teams import …` line if it is not already there.

```python
def test_kalshi_code_alias_resolves_to_the_canonical_team():
    """Kalshi writes the Diamondbacks as AZ in its totals and spread tickers
    (KXMLBSPREAD-26SEP061410AZHOU-AZ2, observed 2026-09-06) and the Jaguars as
    JAC; our tables and Polymarket US say ARI and JAX. The alias must yield
    the canonical Team so outcome keys and spread anchors read ARI/JAX on
    both venues and match."""
    ari = MLB_RESOLVER.by_code("AZ")
    assert ari is not None and ari.code == "ARI"
    assert MLB_RESOLVER.by_code("az") is ari
    jax = NFL_RESOLVER.by_code("JAC")
    assert jax is not None and jax.code == "JAX"


def test_unknown_code_is_still_none():
    assert MLB_RESOLVER.by_code("ZZZ") is None


def test_split_team_codes_sees_through_code_aliases():
    """Both games were dropped from totals before the alias existed."""
    from arbys.discovery.kalshi_totals import split_team_codes

    assert split_team_codes("AZHOU", MLB_RESOLVER) == ("AZ", "HOU")
    assert split_team_codes("CLEJAC", NFL_RESOLVER) == ("CLE", "JAC")
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv\Scripts\python.exe -m pytest tests/discovery/test_teams.py -q -k "alias or unknown_code or sees_through"`
Expected: 2 failures (`by_code("AZ")` is None; `split_team_codes` returns None), 1 pass.

- [ ] **Step 3: Implement**

In `arbys/discovery/teams.py`, change the constructor signature and add the alias map at the end of `__init__`:

```python
    def __init__(
        self,
        teams: tuple[Team, ...],
        aliases: dict[str, str] | None = None,
        code_aliases: dict[str, str] | None = None,
    ) -> None:
```

…existing body unchanged…, then after the `for alias, code in (aliases or {}).items():` loop:

```python
        # Venue codes that differ from ours. Kalshi writes the Diamondbacks as
        # "AZ" and the Jaguars as "JAC" in its totals and spread tickers, where
        # our tables (and Polymarket US) use "ARI" / "JAX". Resolved to the
        # canonical Team so `.code` downstream is always ours, which is what
        # lets a Kalshi anchor match a Polymarket one.
        self._code_aliases: dict[str, Team] = {}
        for alias, code in (code_aliases or {}).items():
            team = self._by_code.get(code.upper())
            if team is not None:
                self._code_aliases[alias.upper()] = team
```

Replace `by_code`:

```python
    def by_code(self, code: str) -> Team | None:
        key = code.upper()
        return self._by_code.get(key) or self._code_aliases.get(key)
```

Above `MLB_RESOLVER = …` (line 184) add and use:

```python
# Kalshi ticker codes that differ from ours. Observed 2026-09-06 in
# KXMLBSPREAD-26SEP061410AZHOU; the same code appears in KXMLBTOTAL.
MLB_CODE_ALIASES = {"AZ": "ARI"}

MLB_RESOLVER = TeamResolver(MLB_TEAMS, aliases=MLB_ALIASES, code_aliases=MLB_CODE_ALIASES)
```

Find the NFL resolver line (`grep -n "^NFL_RESOLVER" arbys/discovery/teams.py`) and change it to:

```python
# Observed 2026-09-06 in KXNFLSPREAD-26SEP13CLEJAC.
NFL_CODE_ALIASES = {"JAC": "JAX"}

NFL_RESOLVER = TeamResolver(NFL_TEAMS, code_aliases=NFL_CODE_ALIASES)
```

(If `NFL_RESOLVER` already passes `aliases=…`, keep that argument and add `code_aliases=`.)

- [ ] **Step 4: Run the discovery tests**

Run: `venv\Scripts\python.exe -m pytest tests/discovery -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add arbys/discovery/teams.py tests/discovery/test_teams.py
git status --porcelain
git commit -m "fix(discovery): resolve Kalshi's AZ and JAC codes to ARI and JAX

split_team_codes could not split AZHOU or CLEJAC, so both games were
dropped from totals on every pass. Spreads use the same code split.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Horizon module

**Files:**
- Create: `arbys/discovery/horizon.py`
- Test: `tests/discovery/test_horizon.py`

**Interfaces:**
- Produces:
  - `DEFAULT_HORIZON_DAYS: int = 3`
  - `discovery_horizon_days() -> int` — reads `ARBYS_DISCOVERY_HORIZON_DAYS`; `0` disables; negative clamps to 0; non-integer falls back to the default with a warning
  - `eastern_today() -> date`
  - `within_horizon(game_date: date, *, days: int, today: date | None = None) -> bool`
  - `filter_horizon(games: list[VenueGame], *, days: int, today: date | None = None) -> list[VenueGame]`
- Must not import `kalshi_sports` at runtime (Task 7 makes `kalshi_sports` import this module). Use `TYPE_CHECKING`.

- [ ] **Step 1: Write the failing tests**

Create `tests/discovery/test_horizon.py`:

```python
from datetime import date

from arbys.discovery.horizon import (
    DEFAULT_HORIZON_DAYS,
    discovery_horizon_days,
    filter_horizon,
    within_horizon,
)
from arbys.discovery.kalshi_sports import VenueGame
from arbys.discovery.teams import NFL_RESOLVER

TODAY = date(2026, 9, 6)


def _game(day: date, venue: str = "kalshi") -> VenueGame:
    a = NFL_RESOLVER.by_code("NE")
    b = NFL_RESOLVER.by_code("SEA")
    assert a is not None and b is not None
    return VenueGame(
        sport="nfl",
        venue_id=venue,
        game_date=day,
        teams=(a, b),
        outcome_ids={"NE": f"{venue}-{day}-ne", "SEA": f"{venue}-{day}-sea"},
        ref=f"{venue}-{day}",
    )


def test_last_day_of_the_window_is_inside_and_the_next_is_out():
    assert within_horizon(date(2026, 9, 9), days=3, today=TODAY)
    assert not within_horizon(date(2026, 9, 10), days=3, today=TODAY)


def test_today_and_the_past_are_inside():
    """The horizon bounds the future. Finished games are removed by
    retirement, not by this rule."""
    assert within_horizon(TODAY, days=3, today=TODAY)
    assert within_horizon(date(2026, 9, 1), days=3, today=TODAY)


def test_zero_disables():
    assert within_horizon(date(2030, 1, 1), days=0, today=TODAY)


def test_filter_keeps_near_games_and_drops_far_ones():
    near, far = _game(date(2026, 9, 8)), _game(date(2026, 9, 13))
    assert filter_horizon([near, far], days=3, today=TODAY) == [near]
    assert filter_horizon([near, far], days=0, today=TODAY) == [near, far]


def test_config_default_and_parsing(monkeypatch):
    monkeypatch.delenv("ARBYS_DISCOVERY_HORIZON_DAYS", raising=False)
    assert discovery_horizon_days() == DEFAULT_HORIZON_DAYS == 3
    monkeypatch.setenv("ARBYS_DISCOVERY_HORIZON_DAYS", "7")
    assert discovery_horizon_days() == 7
    monkeypatch.setenv("ARBYS_DISCOVERY_HORIZON_DAYS", "0")
    assert discovery_horizon_days() == 0
    monkeypatch.setenv("ARBYS_DISCOVERY_HORIZON_DAYS", "-2")
    assert discovery_horizon_days() == 0
    monkeypatch.setenv("ARBYS_DISCOVERY_HORIZON_DAYS", "soon")
    assert discovery_horizon_days() == 3
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv\Scripts\python.exe -m pytest tests/discovery/test_horizon.py -q`
Expected: collection error, `No module named 'arbys.discovery.horizon'`.

- [ ] **Step 3: Implement**

Create `arbys/discovery/horizon.py`:

```python
"""How far into the future discovery looks.

Discovery had no time bound: it registered every game both venues listed, and
Kalshi lists NFL a week or more ahead. On 2026-09-03 the local database held
174 upcoming groups of which 127 started more than 7 days out — past the fill
rule (``ARBYS_MAX_DAYS_TO_START``), so they could never trade and were pure
subscription cost. Spreads triple the group count, which makes that waste the
dominant load.

Nearly all the trading value sits inside a short window. Local ledger, 1,248
fills over 2026-08-28..09-03: 1,177 on game day, 21 one day out, 32 at two to
three days, 16 at four to seven, 2 beyond. A 3-day horizon forgoes 1.4% of
fills and 0.4% of expected profit.

The rule is judged on ``game_date``, which both venues carry as an **Eastern
calendar date** (Kalshi from its ticker, Polymarket US via ``_eastern_date``).
Using the date rather than an exact start means the two venues always agree
on whether a game is in, so a game never has one leg inside the window and
one outside. Day granularity: "3 days" admits a game up to the end of the
third calendar day after today.

The horizon bounds the *future* only. Past games pass, because retirement —
a complete pass no longer finding a group — is what removes finished games,
and a game can only move toward the window, never out of it.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:  # avoid a runtime cycle: kalshi_sports imports this module
    from .kalshi_sports import VenueGame

log = logging.getLogger(__name__)

DEFAULT_HORIZON_DAYS = 3


def discovery_horizon_days() -> int:
    """``ARBYS_DISCOVERY_HORIZON_DAYS``. ``0`` disables the bound."""
    raw = os.environ.get("ARBYS_DISCOVERY_HORIZON_DAYS")
    if raw is None:
        return DEFAULT_HORIZON_DAYS
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning(
            "ARBYS_DISCOVERY_HORIZON_DAYS=%r is not an integer; using %d",
            raw,
            DEFAULT_HORIZON_DAYS,
        )
        return DEFAULT_HORIZON_DAYS


def eastern_today() -> date:
    """Today in Eastern time — the calendar both venues' ``game_date`` uses."""
    now = datetime.now(UTC)
    try:
        return now.astimezone(ZoneInfo("America/New_York")).date()
    except (ValueError, ZoneInfoNotFoundError):  # pragma: no cover - no tzdata
        return now.date()


def within_horizon(game_date: date, *, days: int, today: date | None = None) -> bool:
    """Is the game on or before ``today + days``? ``days <= 0`` admits all."""
    if days <= 0:
        return True
    today = today or eastern_today()
    return game_date <= today + timedelta(days=days)


def filter_horizon(
    games: list[VenueGame], *, days: int, today: date | None = None
) -> list[VenueGame]:
    """The games inside the horizon, in their original order."""
    if days <= 0:
        return games
    today = today or eastern_today()
    return [g for g in games if within_horizon(g.game_date, days=days, today=today)]
```

- [ ] **Step 4: Run to verify they pass**

Run: `venv\Scripts\python.exe -m pytest tests/discovery/test_horizon.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add arbys/discovery/horizon.py tests/discovery/test_horizon.py
git status --porcelain
git commit -m "feat(discovery): a horizon rule for how far ahead discovery looks

ARBYS_DISCOVERY_HORIZON_DAYS, default 3, judged on the Eastern game date
both venues carry so the two sides always agree. Not wired yet.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Spread group id and title in the matcher

**Files:**
- Modify: `arbys/discovery/matcher.py` (`CrossVenueMatch.event_group_id`, `event_group_title`; add `anchor_participant`)
- Test: `tests/discovery/test_matcher.py` (uses the existing `_spread(venue, anchor, line)` helper near line 246)

**Interfaces:**
- Produces: `CrossVenueMatch.anchor_participant() -> Participant`; spread id `"{sport}-{a}-{b}-{date}-spread-{ANCHOR}-{line}"`; spread title `"{A full} vs {B full} — {anchor full} -{line} ({date})"`.
- Existing `yes_key()` and `_pair_key` are unchanged and already handle spreads.

- [ ] **Step 1: Write the failing tests**

Append to `tests/discovery/test_matcher.py` (after `test_same_anchor_different_lines_do_not_match`). The `_spread` helper builds an LAD/CHC game; `team_a` is the alphabetically first code, so `CHC`.

```python
def _spread_match(anchor: str, line: str):
    matches = match_games(
        [_spread("kalshi", anchor, line)], [_spread("polymarket_us", anchor, line)]
    )
    assert len(matches) == 1
    return matches[0]


def test_spread_group_id_carries_anchor_and_line():
    """CIN -2.5 and MIL -2.5 on the same game are different bets, so the
    anchor is part of identity, not just of the bucket key."""
    m = _spread_match("CHC", "2.5")
    assert m.event_group_id() == "mlb-CHC-LAD-2026-08-05-spread-CHC-2.5"
    assert m.event_group_title() == (
        "Chicago Cubs vs Los Angeles Dodgers — Chicago Cubs -2.5 (2026-08-05)"
    )


def test_spread_anchored_on_team_b_names_team_b():
    m = _spread_match("LAD", "1.5")
    assert m.event_group_id() == "mlb-CHC-LAD-2026-08-05-spread-LAD-1.5"
    assert m.event_group_title().endswith("— Los Angeles Dodgers -1.5 (2026-08-05)")


def test_opposite_anchors_get_distinct_ids():
    assert _spread_match("LAD", "2.5").event_group_id() != _spread_match("CHC", "2.5").event_group_id()


def test_spread_group_marks_the_anchor_legs_as_yes_on_both_venues():
    group = match_to_event_group(_spread_match("CHC", "2.5"))
    assert len(group.legs) == 4
    yes = {leg.outcome_id for leg in group.legs if leg.is_yes_side}
    assert yes == {"kalshi-C", "polymarket_us-C"}


def test_spread_id_parses_as_market_type_spread_downstream():
    """The account page slices tickets by the segment after the date."""
    from arbys.backend.performance import parse_group_id

    assert parse_group_id("mlb-CHC-LAD-2026-08-05-spread-CHC-2.5") == ("mlb", "spread")


def test_total_and_moneyline_ids_are_unchanged():
    from decimal import Decimal

    base = _game("kalshi", ("LAD", "CHC"), "2026-08-05", {"LAD": "K1", "CHC": "K2"})
    poly = _game("polymarket_us", ("LAD", "CHC"), "2026-08-05", {"LAD": "P1", "CHC": "P2"})
    assert match_games([base], [poly])[0].event_group_id() == "mlb-CHC-LAD-2026-08-05"
    tk = replace(base, market_type="total", line=Decimal("8.5"),
                 outcome_ids={"OVER": "ko", "UNDER": "ku"})
    tp = replace(poly, market_type="total", line=Decimal("8.5"),
                 outcome_ids={"OVER": "po", "UNDER": "pu"})
    assert match_games([tk], [tp])[0].event_group_id() == "mlb-CHC-LAD-2026-08-05-total-8.5"
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv\Scripts\python.exe -m pytest tests/discovery/test_matcher.py -q -k spread`
Expected: `test_spread_group_id_carries_anchor_and_line`, `test_spread_anchored_on_team_b_names_team_b` and `test_opposite_anchors_get_distinct_ids` fail (id is `…-spread-2.5` with no anchor; title has no market part). Others pass.

- [ ] **Step 3: Implement**

In `arbys/discovery/matcher.py`, replace `event_group_id` and `event_group_title` and add `anchor_participant` inside `CrossVenueMatch`:

```python
    def anchor_participant(self) -> Participant:
        """The team a spread's line is stated for. ``team_a`` when unset, which
        keeps this in step with ``yes_key()``."""
        if self.anchor == self.team_b.code:
            return self.team_b
        return self.team_a

    def event_group_id(self) -> str:
        base = f"{self.sport}-{self.team_a.code}-{self.team_b.code}-{self.game_date}"
        if self.market_type == "spread":
            # The anchor is part of identity: CIN -2.5 and MIL -2.5 on the same
            # game are different bets and must not share an id. Downstream id
            # parsers read the segment after the date as the market type and
            # the rest as opaque, so `spread-CIN-2.5` slices as "spread".
            anchor = self.anchor_participant().code
            return f"{base}-spread-{anchor}-{_fmt_line(self.line)}"
        if self.market_type != "moneyline":
            return f"{base}-{self.market_type}-{_fmt_line(self.line)}"
        return base

    def event_group_title(self) -> str:
        matchup = f"{self.team_a.full_name} vs {self.team_b.full_name}"
        if self.market_type == "total":
            return f"{matchup} — Over {_fmt_line(self.line)} ({self.game_date})"
        if self.market_type == "spread":
            anchor = self.anchor_participant().full_name
            return f"{matchup} — {anchor} -{_fmt_line(self.line)} ({self.game_date})"
        return f"{matchup} ({self.game_date})"
```

Also update the `match_to_event_group` docstring's second sentence to: `The canonical proposition is "team_a wins" for a moneyline, "the total goes over the line" for a total and "the anchor wins by more than the line" for a spread; legs matching it are ``is_yes_side=True``.`

- [ ] **Step 4: Run to verify they pass**

Run: `venv\Scripts\python.exe -m pytest tests/discovery/test_matcher.py tests/discovery/test_totals.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add arbys/discovery/matcher.py tests/discovery/test_matcher.py
git status --porcelain
git commit -m "feat(matcher): spread groups carry the anchor in id and title

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Kalshi spread fetcher

**Files:**
- Create: `arbys/discovery/kalshi_spreads.py`
- Test: `tests/discovery/test_spreads.py` (create; Task 5 and 6 extend it)

**Interfaces:**
- Consumes: `within_horizon`, `discovery_horizon_days` (Task 2); `split_team_codes`, `_TICKER_RE` from `kalshi_totals`; `_get_with_retry`, `_parse_ticker_date`, `parse_ticker_start`, `KALSHI_BASE`, `_REQUEST_SPACING_S`, `VenueGame` from `kalshi_sports`; `TeamResolver.by_code` with aliases (Task 1).
- Produces:
  - `SPREADS_SERIES: dict[str, str]`
  - `anchor_code_from_ticker(market_ticker: str, event_ticker: str, codes: tuple[str, str]) -> str | None`
  - `async fetch_kalshi_spreads(*, resolver, sport, series_ticker=None, http_client=None, limit=100, horizon_days: int | None = None) -> list[VenueGame]`

- [ ] **Step 1: Write the failing tests**

Create `tests/discovery/test_spreads.py`:

```python
"""Spreads: both venue parsers and the cross-venue convention.

Every fixture is modelled on payloads observed live on 2026-09-06; see
docs/superpowers/specs/2026-09-06-spreads-and-discovery-horizon-design.md.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx

from arbys.discovery.kalshi_spreads import anchor_code_from_ticker, fetch_kalshi_spreads
from arbys.discovery.teams import MLB_RESOLVER, NFL_RESOLVER

# --- Kalshi -----------------------------------------------------------------

KALSHI_EVENTS = {"events": [{"event_ticker": "KXNFLSPREAD-26SEP09NESEA"}]}
KALSHI_MARKETS = {
    "markets": [
        {"ticker": "KXNFLSPREAD-26SEP09NESEA-SEA4", "floor_strike": 3.5,
         "strike_type": "greater", "yes_sub_title": "Seattle wins by over 3.5 points"},
        {"ticker": "KXNFLSPREAD-26SEP09NESEA-NE4", "floor_strike": 3.5,
         "strike_type": "greater", "yes_sub_title": "New England wins by over 3.5 points"},
        # A strike type other than "greater" would invert the proposition.
        {"ticker": "KXNFLSPREAD-26SEP09NESEA-NE8", "floor_strike": 7.5, "strike_type": "less"},
        # Suffix names a team that is not in this game.
        {"ticker": "KXNFLSPREAD-26SEP09NESEA-KC4", "floor_strike": 3.5, "strike_type": "greater"},
        # No line.
        {"ticker": "KXNFLSPREAD-26SEP09NESEA-SEA8", "floor_strike": None, "strike_type": "greater"},
    ]
}


def _kalshi_client(events, markets, calls: list[str] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=events)
        return httpx.Response(200, json=markets)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=5.0,
        base_url="https://api.elections.kalshi.com/trade-api/v2",
    )


def test_anchor_code_is_whichever_team_code_prefixes_the_suffix():
    assert anchor_code_from_ticker("KXNFLSPREAD-26SEP09NESEA-SEA4", "KXNFLSPREAD-26SEP09NESEA", ("NE", "SEA")) == "SEA"
    assert anchor_code_from_ticker("KXNFLSPREAD-26SEP09NESEA-NE12", "KXNFLSPREAD-26SEP09NESEA", ("NE", "SEA")) == "NE"
    # Neither team, wrong event, or no digits.
    assert anchor_code_from_ticker("KXNFLSPREAD-26SEP09NESEA-KC4", "KXNFLSPREAD-26SEP09NESEA", ("NE", "SEA")) is None
    assert anchor_code_from_ticker("KXNFLSPREAD-26SEP10SFLAR-SF5", "KXNFLSPREAD-26SEP09NESEA", ("NE", "SEA")) is None
    assert anchor_code_from_ticker("KXNFLSPREAD-26SEP09NESEA-SEA", "KXNFLSPREAD-26SEP09NESEA", ("NE", "SEA")) is None


async def test_kalshi_one_game_per_team_and_line_anchored_on_the_ticker_team():
    client = _kalshi_client(KALSHI_EVENTS, KALSHI_MARKETS)
    games = await fetch_kalshi_spreads(
        resolver=NFL_RESOLVER, sport="nfl", http_client=client, horizon_days=0
    )
    await client.aclose()

    assert {(g.anchor, g.line) for g in games} == {("SEA", Decimal("3.5")), ("NE", Decimal("3.5"))}
    sea = next(g for g in games if g.anchor == "SEA")
    assert sea.market_type == "spread"
    assert isinstance(sea.line, Decimal)
    # YES is the anchor covering; the other team's key is the complement.
    assert sea.outcome_ids == {
        "SEA": "KXNFLSPREAD-26SEP09NESEA-SEA4:YES",
        "NE": "KXNFLSPREAD-26SEP09NESEA-SEA4:NO",
    }
    assert {t.code for t in sea.teams} == {"NE", "SEA"}
    assert sea.game_date == date(2026, 9, 9)
    assert sea.start_time is None  # NFL tickers carry no HHMM
    assert sea.ref == "KXNFLSPREAD-26SEP09NESEA-SEA4"


async def test_kalshi_alias_code_yields_the_canonical_anchor():
    """KXMLBSPREAD-26SEP061410AZHOU-AZ2 observed live. AZ must come out as ARI
    so it matches Polymarket's ARI, and the MLB ticker's HHMM is the start."""
    events = {"events": [{"event_ticker": "KXMLBSPREAD-26SEP061410AZHOU"}]}
    markets = {"markets": [{"ticker": "KXMLBSPREAD-26SEP061410AZHOU-AZ2",
                            "floor_strike": 1.5, "strike_type": "greater"}]}
    client = _kalshi_client(events, markets)
    games = await fetch_kalshi_spreads(
        resolver=MLB_RESOLVER, sport="mlb", http_client=client, horizon_days=0
    )
    await client.aclose()

    assert len(games) == 1
    g = games[0]
    assert g.anchor == "ARI"
    assert g.outcome_ids == {
        "ARI": "KXMLBSPREAD-26SEP061410AZHOU-AZ2:YES",
        "HOU": "KXMLBSPREAD-26SEP061410AZHOU-AZ2:NO",
    }
    assert g.start_time == datetime(2026, 9, 6, 18, 10, tzinfo=UTC)  # 14:10 EDT


async def test_kalshi_event_past_the_horizon_makes_no_market_call():
    """The per-event /markets request is what costs against Kalshi's rate
    limit; a game beyond the horizon is skipped before it is made."""
    far = {"events": [{"event_ticker": "KXNFLSPREAD-30SEP09NESEA"}]}
    calls: list[str] = []
    client = _kalshi_client(far, KALSHI_MARKETS, calls)
    games = await fetch_kalshi_spreads(
        resolver=NFL_RESOLVER, sport="nfl", http_client=client, horizon_days=3
    )
    assert games == []
    assert not any(p.endswith("/markets") for p in calls)

    calls.clear()
    games = await fetch_kalshi_spreads(
        resolver=NFL_RESOLVER, sport="nfl", http_client=client, horizon_days=0
    )
    await client.aclose()
    assert len(games) == 2
    assert any(p.endswith("/markets") for p in calls)


async def test_kalshi_unknown_sport_raises():
    import pytest

    with pytest.raises(ValueError):
        await fetch_kalshi_spreads(resolver=NFL_RESOLVER, sport="curling", horizon_days=0)
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv\Scripts\python.exe -m pytest tests/discovery/test_spreads.py -q`
Expected: collection error, `No module named 'arbys.discovery.kalshi_spreads'`.

- [ ] **Step 3: Implement**

Create `arbys/discovery/kalshi_spreads.py`:

```python
"""Kalshi spread discovery.

A spread event mirrors the totals event for the same game — same
``<yyMONdd[hhmm]><CODES>`` stem, different series — and lists **one market per
(team, line)**: ``KXNFLSPREAD-26SEP09NESEA-SEA4`` is "Seattle wins by over 3.5
points", with the line in ``floor_strike`` (3.5) and the team in the ticker
suffix. The integer in the suffix was ``floor_strike + 0.5`` on all 1,480
markets observed on 2026-09-06; it is not used.

Every market becomes a ``VenueGame`` with ``market_type="spread"``, a positive
``line`` and ``anchor`` set to the team that must win by more than it. YES is
the anchor covering, so ``outcome_ids[anchor]`` is ``"<ticker>:YES"`` and the
other team's key is ``":NO"``. ``fetch_polymarket_us_spreads`` produces the
same canonical form, which is what lets the matcher pair the two venues on
``(anchor, line)`` alone — see the spec for the price evidence.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation

import httpx

from .horizon import discovery_horizon_days, within_horizon
from .kalshi_sports import (
    _REQUEST_SPACING_S,
    KALSHI_BASE,
    VenueGame,
    _get_with_retry,
    _parse_ticker_date,
    parse_ticker_start,
)
from .kalshi_totals import _TICKER_RE, split_team_codes
from .teams import TeamResolver

log = logging.getLogger(__name__)

# Kalshi spread series per sport.
SPREADS_SERIES = {
    "mlb": "KXMLBSPREAD",
    "nfl": "KXNFLSPREAD",
    "ncaaf": "KXNCAAFSPREAD",
    # Both exist and returned zero open events on 2026-09-06 (off-season), so
    # they are registered here but neither sport is in SPREADS_SPORTS.
    "nba": "KXNBASPREAD",
    "wnba": "KXWNBASPREAD",
}

# The only strike type whose YES reads "anchor wins by MORE than the line".
# Any other would invert the proposition, so it is refused rather than guessed.
_EXPECTED_STRIKE_TYPE = "greater"


def anchor_code_from_ticker(
    market_ticker: str, event_ticker: str, codes: tuple[str, str]
) -> str | None:
    """Which of the event's two (raw Kalshi) codes a spread market is for.

    The suffix is ``<CODE><N>`` and codes vary in width, so rather than a
    regex the suffix is tested against each known code. Exactly one must fit,
    which also enforces that the anchor is one of the event's own teams.
    Returns the code as Kalshi wrote it (``"AZ"``); the caller resolves it.
    """
    prefix = f"{event_ticker}-"
    if not market_ticker.startswith(prefix):
        return None
    rest = market_ticker[len(prefix):]
    hits = [c for c in codes if rest.startswith(c) and rest[len(c):].isdigit()]
    return hits[0] if len(hits) == 1 else None


async def fetch_kalshi_spreads(
    *,
    resolver: TeamResolver,
    sport: str,
    series_ticker: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 100,
    horizon_days: int | None = None,
) -> list[VenueGame]:
    """One VenueGame per (game, anchor, line).

    ``horizon_days`` bounds how far ahead a game may be; ``None`` reads
    ``ARBYS_DISCOVERY_HORIZON_DAYS``. Applied before the per-event market call.
    """
    series = series_ticker or SPREADS_SERIES.get(sport)
    if series is None:
        raise ValueError(f"no Kalshi spreads series known for sport {sport!r}")
    days = discovery_horizon_days() if horizon_days is None else horizon_days
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=15.0, base_url=KALSHI_BASE)
    try:
        resp = await _get_with_retry(
            client, "/events", {"series_ticker": series, "status": "open", "limit": limit}
        )
        resp.raise_for_status()
        events = resp.json().get("events", [])

        games: list[VenueGame] = []
        for ev in events:
            games.extend(
                await _parse_spread_event(client, ev, resolver, sport=sport, horizon_days=days)
            )
            await asyncio.sleep(_REQUEST_SPACING_S)
        return games
    finally:
        if owns_client:
            await client.aclose()


async def _parse_spread_event(
    client: httpx.AsyncClient,
    event: dict,
    resolver: TeamResolver,
    *,
    sport: str,
    horizon_days: int,
) -> list[VenueGame]:
    ticker = event.get("event_ticker") or ""
    m = _TICKER_RE.match(ticker)
    if not m:
        return []
    _datepart, codes = m.groups()

    game_date = _parse_ticker_date(ticker)
    if game_date is None:
        return []

    pair = split_team_codes(codes, resolver)
    if pair is None:
        log.debug("kalshi spreads: unsplittable codes %r in %s", codes, ticker)
        return []
    team_a, team_b = resolver.by_code(pair[0]), resolver.by_code(pair[1])
    if team_a is None or team_b is None:
        return []

    # The market call is what costs a request; a game past the horizon is
    # skipped before it is made.
    if not within_horizon(game_date, days=horizon_days):
        return []

    resp = await _get_with_retry(client, "/markets", {"event_ticker": ticker, "limit": 60})
    if resp.status_code != 200:
        return []

    out: list[VenueGame] = []
    for mk in resp.json().get("markets", []):
        mkt_ticker = mk.get("ticker")
        if not mkt_ticker:
            continue
        raw_anchor = anchor_code_from_ticker(mkt_ticker, ticker, pair)
        if raw_anchor is None:
            log.debug("kalshi spreads: suffix names neither team in %s", mkt_ticker)
            continue
        strike_type = mk.get("strike_type")
        if strike_type != _EXPECTED_STRIKE_TYPE:
            log.warning(
                "kalshi spreads: %s has strike_type %r, not %r; skipped",
                mkt_ticker, strike_type, _EXPECTED_STRIKE_TYPE,
            )
            continue
        strike = mk.get("floor_strike")
        if strike is None:
            continue
        try:
            line = Decimal(str(strike))
        except (InvalidOperation, ValueError):
            continue
        if line <= 0:
            continue
        anchor, other = (team_a, team_b) if raw_anchor == pair[0] else (team_b, team_a)
        out.append(
            VenueGame(
                sport=sport,
                venue_id="kalshi",
                game_date=game_date,
                teams=(team_a, team_b),
                outcome_ids={
                    anchor.code: f"{mkt_ticker}:YES",
                    other.code: f"{mkt_ticker}:NO",
                },
                ref=mkt_ticker,
                market_type="spread",
                line=line,
                anchor=anchor.code,
                start_time=parse_ticker_start(ticker),
            )
        )
    return out
```

- [ ] **Step 4: Run to verify they pass**

Run: `venv\Scripts\python.exe -m pytest tests/discovery/test_spreads.py -q`
Expected: 5 passed.

- [ ] **Step 5: Lint and commit**

```bash
venv\Scripts\python.exe -m ruff check arbys/discovery tests/discovery
git add arbys/discovery/kalshi_spreads.py tests/discovery/test_spreads.py
git status --porcelain
git commit -m "feat(discovery): Kalshi spread fetcher

One VenueGame per (team, line), anchored on the ticker-suffix team, line
from floor_strike, strike_type must be greater. Horizon checked before
the per-event market call.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Polymarket US spread fetcher

**Files:**
- Modify: `arbys/discovery/polymarket_us.py` (imports at top; add `SPREAD_TYPES` after `TOTAL_TYPES` ~line 82; add `_SPREAD_TITLE_RE`, `_title_agrees`, `fetch_polymarket_us_spreads` after `fetch_polymarket_us_totals` ~line 326)
- Modify: `tests/discovery/test_polymarket_us.py` (rename `test_spread_markets_are_skipped_in_phase_1`, line 124)
- Test: `tests/discovery/test_spreads.py` (extend)

**Interfaces:**
- Consumes: `_fetch_events`, `_parse_utc`, `_live_flags`, `_resolve_team`, `_sides`, `_line`, `_eastern_date`, `LEAGUE_SLUGS` (all existing in the module).
- Produces:
  - `SPREAD_TYPES: frozenset[str]`
  - `_title_agrees(title: object, anchor_name: object, line: Decimal) -> bool | None`
  - `async fetch_polymarket_us_spreads(*, resolver, sport, http_client=None, limit=200) -> list[VenueGame]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/discovery/test_spreads.py`. Add to the imports at the top of the file:

```python
from arbys.discovery.polymarket_us import _title_agrees, fetch_polymarket_us_spreads
```

Then append:

```python
# --- Polymarket US ------------------------------------------------------------


def _pm_client(payload) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "gateway.polymarket.us" in str(request.url)
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def _pm_event(markets: list[dict]) -> dict:
    return {
        "events": [
            {
                "slug": "mlb-mil-cin-2026-09-06",
                "startTime": "2026-09-06T16:10:00Z",
                "live": False,
                "ended": False,
                "teams": [
                    {"name": "Milwaukee Brewers", "displayAbbreviation": "MIL", "safeName": "Brewers"},
                    {"name": "Cincinnati Reds", "displayAbbreviation": "CIN", "safeName": "Reds"},
                ],
                "markets": markets,
            }
        ]
    }


def _pm_spread(
    slug: str,
    line,
    title: str | None,
    *,
    kind: str = "baseball_team_full_game_spread",
    long_team: str = "Milwaukee Brewers",
    short_team: str = "Cincinnati Reds",
) -> dict:
    market = {
        "slug": slug,
        "sportsMarketType": kind,
        "line": line,
        "marketSides": [
            {"long": True, "team": {"name": long_team}},
            {"long": False, "team": {"name": short_team}},
        ],
    }
    if title is not None:
        market["title"] = title
    return market


# Observed live 2026-09-06. `line` is signed relative to the first-listed
# team; the title names the team that must win by more than |line|.
POS = _pm_spread("asc-mlb-mil-cin-2026-09-06-pos-2pt5", 2.5, "Cincinnati Reds wins by over 2.5 runs")
NEG = _pm_spread("asc-mlb-mil-cin-2026-09-06-neg-2pt5", -2.5, "Milwaukee Brewers wins by over 2.5 runs")


async def _pm_games(markets: list[dict]):
    client = _pm_client(_pm_event(markets))
    try:
        return await fetch_polymarket_us_spreads(
            resolver=MLB_RESOLVER, sport="mlb", http_client=client
        )
    finally:
        await client.aclose()


async def test_polymarket_negative_line_anchors_the_first_team():
    games = await _pm_games([NEG])
    assert len(games) == 1
    g = games[0]
    assert (g.market_type, g.anchor, g.line) == ("spread", "MIL", Decimal("2.5"))
    assert isinstance(g.line, Decimal)
    assert g.outcome_ids == {
        "MIL": "asc-mlb-mil-cin-2026-09-06-neg-2pt5:LONG",
        "CIN": "asc-mlb-mil-cin-2026-09-06-neg-2pt5:SHORT",
    }
    assert g.game_date == date(2026, 9, 6)
    assert g.start_time == datetime(2026, 9, 6, 16, 10, tzinfo=UTC)
    assert (g.live, g.ended) == (False, False)


async def test_polymarket_positive_line_anchors_the_second_team_with_the_same_outcome_ids():
    """Only the anchor flips with the sign. First team is always LONG."""
    games = await _pm_games([POS])
    assert len(games) == 1
    g = games[0]
    assert (g.anchor, g.line) == ("CIN", Decimal("2.5"))
    assert g.outcome_ids == {
        "MIL": "asc-mlb-mil-cin-2026-09-06-pos-2pt5:LONG",
        "CIN": "asc-mlb-mil-cin-2026-09-06-pos-2pt5:SHORT",
    }


async def test_polymarket_title_naming_the_other_team_is_skipped(caplog):
    """Two venue fields contradicting each other is the signal that the sign
    convention moved. A backwards sign near even money is a 2-4c phantom edge
    the plausible-edge ceiling cannot see, so the market is refused."""
    bad = _pm_spread("asc-mlb-mil-cin-2026-09-06-pos-2pt5", 2.5, "Milwaukee Brewers wins by over 2.5 runs")
    with caplog.at_level("WARNING"):
        games = await _pm_games([bad])
    assert games == []
    assert "contradicts" in caplog.text


async def test_polymarket_title_with_a_different_line_is_skipped():
    bad = _pm_spread("asc-mlb-mil-cin-2026-09-06-pos-2pt5", 2.5, "Cincinnati Reds wins by over 1.5 runs")
    assert await _pm_games([bad]) == []


async def test_polymarket_unparseable_title_is_accepted_and_counted(caplog):
    """The structure is the source of truth; the title is a guard. A reworded
    title disables the guard visibly rather than silently zeroing the league."""
    odd = _pm_spread("asc-mlb-mil-cin-2026-09-06-pos-2pt5", 2.5, "Reds to cover the run line")
    with caplog.at_level("WARNING"):
        games = await _pm_games([odd])
    assert len(games) == 1
    assert "matched no known pattern" in caplog.text


async def test_polymarket_missing_title_is_accepted():
    games = await _pm_games([_pm_spread("asc-mlb-mil-cin-2026-09-06-pos-2pt5", 2.5, None)])
    assert len(games) == 1


async def test_polymarket_long_side_that_is_not_the_first_team_is_skipped():
    swapped = _pm_spread(
        "asc-mlb-mil-cin-2026-09-06-neg-2pt5", -2.5, "Cincinnati Reds wins by over 2.5 runs",
        long_team="Cincinnati Reds", short_team="Milwaukee Brewers",
    )
    assert await _pm_games([swapped]) == []


async def test_polymarket_zero_and_missing_lines_are_skipped():
    zero = _pm_spread("asc-mlb-mil-cin-2026-09-06-pos-0", 0, "Cincinnati Reds wins by over 0 runs")
    missing = _pm_spread("asc-mlb-mil-cin-2026-09-06-pos-2pt5", None, "Cincinnati Reds wins by over 2.5 runs")
    assert await _pm_games([zero, missing]) == []


async def test_polymarket_period_spreads_are_ignored():
    """First-five, half and quarter spreads are Phase 3 and distinct types."""
    f5 = _pm_spread(
        "asc-mlb-mil-cin-2026-09-06-f5-pos-1pt5", 1.5,
        "Cincinnati Reds wins by over 1.5 runs in first 5 innings",
        kind="baseball_team_first_five_spread",
    )
    assert await _pm_games([f5]) == []


async def test_polymarket_winner_and_total_markets_are_not_spreads():
    winner = {"slug": "aec-mlb-mil-cin-2026-09-06", "sportsMarketType": "baseball_team_full_game_winner",
              "marketSides": [{"long": True, "team": {"name": "Milwaukee Brewers"}},
                              {"long": False, "team": {"name": "Cincinnati Reds"}}]}
    assert await _pm_games([winner]) == []


def test_title_agrees_is_tri_state():
    assert _title_agrees("Cincinnati Reds wins by over 2.5 runs", "Cincinnati Reds", Decimal("2.5")) is True
    assert _title_agrees("Seattle Seahawks wins by over 21.5 points", "Seattle Seahawks", Decimal("21.5")) is True
    assert _title_agrees("Tar Heels wins by over 20.5 points", "Tar Heels", Decimal("20.5")) is True
    assert _title_agrees("Cincinnati Reds wins by over 2.5 runs", "Milwaukee Brewers", Decimal("2.5")) is False
    assert _title_agrees("Cincinnati Reds wins by over 1.5 runs", "Cincinnati Reds", Decimal("2.5")) is False
    assert _title_agrees("Reds to cover", "Cincinnati Reds", Decimal("2.5")) is None
    assert _title_agrees(None, "Cincinnati Reds", Decimal("2.5")) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv\Scripts\python.exe -m pytest tests/discovery/test_spreads.py -q`
Expected: collection error, `cannot import name '_title_agrees'`.

- [ ] **Step 3: Implement**

In `arbys/discovery/polymarket_us.py`:

Add `import re` to the standard-library imports at the top (there is `import logging` at line 22; `re` is not imported yet).

After `TOTAL_TYPES` (ends ~line 82) add:

```python
# Full-game spreads. Period spreads (`baseball_team_first_five_spread`,
# `football_team_first_half_spread`, the four quarters) are distinct types and
# are Phase 3. Verified live 2026-09-06 on mlb, nfl and cfb.
SPREAD_TYPES = frozenset(
    {
        "baseball_team_full_game_spread",
        "football_team_full_game_spread",
        # Presumed by analogy with the winner and total types; unverified,
        # both basketball leagues were out of season on 2026-09-06.
        "basketball_team_full_game_spread",
    }
)

# "Seattle Seahawks wins by over 3.5 points" / "Milwaukee Brewers wins by over
# 2.5 runs" — the venue's own statement of which team the line is for.
_SPREAD_TITLE_RE = re.compile(
    r"^(?P<team>.+?) wins by over (?P<line>\d+(?:\.\d+)?) (?:runs|points)$"
)
```

After `fetch_polymarket_us_totals` add:

```python
def _title_agrees(title: object, anchor_name: object, line: Decimal) -> bool | None:
    """Does the venue's title name the derived anchor and line?

    ``None`` when the title matches no known pattern, so the guard cannot run.
    ``False`` when it matches and disagrees — two venue fields contradicting
    each other, which is the signal that the sign convention has moved.

    String comparison on ``teams[].name``, not resolution: the title uses the
    same string the event's ``teams[]`` carries, bare mascot included on CFB
    (``"Tar Heels wins by over 20.5 points"``), and matched on all 5,267
    markets observed on 2026-09-06.
    """
    if not isinstance(title, str):
        return None
    m = _SPREAD_TITLE_RE.match(title.strip())
    if m is None:
        return None
    try:
        title_line = Decimal(m.group("line"))
    except InvalidOperation:
        return None
    return m.group("team") == anchor_name and title_line == line


async def fetch_polymarket_us_spreads(
    *,
    resolver: TeamResolver,
    sport: str,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 200,
) -> list[VenueGame]:
    """Full-game spreads, one VenueGame per (game, anchor, line).

    One market per **signed line on the first-listed team**. ``line`` is
    signed (``pos-2pt5`` carries 2.5, ``neg-2pt5`` carries -2.5) and the long
    side is always that first team covering it, so:

    * ``line < 0`` — the first team must win by more than |line|. It is the
      anchor, and LONG is the anchor covering (≡ Kalshi YES).
    * ``line > 0`` — the *second* team must win by more than |line|. It is
      the anchor, and LONG is the complement (≡ Kalshi NO).

    Pinned by live prices on 2026-09-06: ``asc-nfl-ne-sea-…-pos-3pt5`` long
    0.51/0.52 against Kalshi ``NESEA-SEA4`` NO 0.51/0.52; ``neg-3pt5`` long
    0.26/0.27 against ``NESEA-NE4`` YES 0.25/0.27. Outcomes are therefore
    always first team → ``:LONG``, second team → ``:SHORT``; only the anchor
    flips with the sign.

    Two guards, both structural. The long side's team must be the first-listed
    team, or the derivation above does not hold. The title must name the
    derived anchor and line, or the market is skipped — a backwards sign near
    even money is a 2-4c phantom edge that no clock-based guard and not the
    15c plausible-edge ceiling can see. A title matching no pattern is
    accepted and counted once per pass, so a rewording disables the guard
    visibly rather than silently zeroing the league.
    """
    league = LEAGUE_SLUGS.get(sport, sport)
    events = await _fetch_events(league, http_client, limit)

    games: list[VenueGame] = []
    unparsed_titles = 0
    for event in events:
        start_time = _parse_utc(event.get("startTime"))
        live, ended = _live_flags(event)
        if start_time is None:
            continue
        event_teams = [t for t in (event.get("teams") or []) if isinstance(t, dict)]
        if len(event_teams) != 2:
            continue
        resolved = [_resolve_team(t, resolver) for t in event_teams]
        if any(t is None for t in resolved):
            continue
        first, second = resolved[0], resolved[1]
        assert first is not None and second is not None  # narrowed above

        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            if market.get("sportsMarketType") not in SPREAD_TYPES:
                continue
            slug = market.get("slug")
            signed = _line(market)
            pair = _sides(market)
            if not slug or signed is None or signed == 0 or pair is None:
                continue
            long_side, short_side = pair
            long_team = _resolve_team(long_side.get("team"), resolver)
            short_team = _resolve_team(short_side.get("team"), resolver)
            if (
                long_team is None
                or short_team is None
                or long_team.code != first.code
                or short_team.code != second.code
            ):
                log.warning(
                    "polymarket_us spreads: %s sides do not follow teams[] order; skipped",
                    slug,
                )
                continue

            anchor_idx = 0 if signed < 0 else 1
            anchor = first if anchor_idx == 0 else second
            line = abs(signed)
            verdict = _title_agrees(market.get("title"), event_teams[anchor_idx].get("name"), line)
            if verdict is None:
                unparsed_titles += 1
            elif verdict is False:
                log.warning(
                    "polymarket_us spreads: %s title %r contradicts derived anchor %s %s; skipped",
                    slug, market.get("title"), anchor.code, line,
                )
                continue

            games.append(
                VenueGame(
                    sport=sport,
                    venue_id="polymarket_us",
                    game_date=_eastern_date(start_time),
                    teams=(first, second),
                    outcome_ids={
                        first.code: f"{slug}:LONG",
                        second.code: f"{slug}:SHORT",
                    },
                    ref=str(slug),
                    market_type="spread",
                    line=line,
                    anchor=anchor.code,
                    start_time=start_time,
                    live=live,
                    ended=ended,
                )
            )
    if unparsed_titles:
        log.warning(
            "polymarket_us spreads[%s]: %d market title(s) matched no known pattern; "
            "the title cross-check did not run for them",
            sport, unparsed_titles,
        )
    return games
```

Then in `tests/discovery/test_polymarket_us.py` rename the test at line 124:

```python
@pytest.mark.asyncio
async def test_moneyline_fetcher_yields_only_moneyline_games():
    """Spreads have their own fetcher (`fetch_polymarket_us_spreads`). The
    payload contains one and the moneyline fetcher must not emit it."""
    client = _client(MLB_EVENTS)
    games = await fetch_polymarket_us_games(
        resolver=MLB_RESOLVER, sport="mlb", http_client=client
    )
    assert all(g.market_type == "moneyline" for g in games)
    await client.aclose()
```

- [ ] **Step 4: Run to verify they pass**

Run: `venv\Scripts\python.exe -m pytest tests/discovery/test_spreads.py tests/discovery/test_polymarket_us.py -q`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
venv\Scripts\python.exe -m ruff check arbys/discovery tests/discovery
git add arbys/discovery/polymarket_us.py tests/discovery/test_spreads.py tests/discovery/test_polymarket_us.py
git status --porcelain
git commit -m "feat(discovery): Polymarket US spread fetcher

The signed line is the source: long is the first-listed team covering it,
so a negative line anchors the first team and a positive one the second.
The venue's title must name the derived anchor or the market is skipped.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: End-to-end convention test

**Files:**
- Test: `tests/discovery/test_spreads.py` (extend)

**Interfaces:**
- Consumes: `fetch_kalshi_spreads` (Task 4), `fetch_polymarket_us_spreads` (Task 5), `match_games`, `match_to_event_group` (Task 3).

- [ ] **Step 1: Write the tests**

Add to the imports at the top of `tests/discovery/test_spreads.py`:

```python
from arbys.discovery.matcher import match_games, match_to_event_group
```

Append:

```python
# --- Cross-venue: THE convention test -------------------------------------------

KALSHI_MILCIN_EVENTS = {"events": [{"event_ticker": "KXMLBSPREAD-26SEP061210MILCIN"}]}


def _kalshi_milcin(*suffixes_and_strikes: tuple[str, float]) -> dict:
    return {
        "markets": [
            {"ticker": f"KXMLBSPREAD-26SEP061210MILCIN-{suffix}",
             "floor_strike": strike, "strike_type": "greater"}
            for suffix, strike in suffixes_and_strikes
        ]
    }


async def _both(kalshi_markets: dict, pm_markets: list[dict]):
    k_client = _kalshi_client(KALSHI_MILCIN_EVENTS, kalshi_markets)
    p_client = _pm_client(_pm_event(pm_markets))
    try:
        kalshi = await fetch_kalshi_spreads(
            resolver=MLB_RESOLVER, sport="mlb", http_client=k_client, horizon_days=0
        )
        poly = await fetch_polymarket_us_spreads(
            resolver=MLB_RESOLVER, sport="mlb", http_client=p_client
        )
    finally:
        await k_client.aclose()
        await p_client.aclose()
    return kalshi, poly


async def test_a_positive_polymarket_line_pairs_with_kalshi_yes_on_the_second_team():
    """Kalshi CIN3 ("Cincinnati wins by over 2.5 runs") and Polymarket
    pos-2pt5 (Brewers +2.5, long = Brewers) are the same binary with opposite
    sides: the Reds covering is Kalshi YES and Polymarket SHORT.

    Pinned live on 2026-09-06 16:20Z, NFL: asc-nfl-ne-sea-…-pos-3pt5 long
    0.51/0.52 vs KXNFLSPREAD-26SEP09NESEA-SEA4 NO 0.51/0.52; pos-10pt5 long
    0.72/0.74 vs SFLAR-LAR11 NO 0.72/0.74. Getting this backwards near even
    money produces a 2-4c phantom edge the plausible-edge ceiling cannot
    see, which is why this test exists.
    """
    kalshi, poly = await _both(_kalshi_milcin(("CIN3", 2.5)), [POS])
    matches = match_games(kalshi, poly)
    assert len(matches) == 1
    group = match_to_event_group(matches[0])

    assert group.id == "mlb-CIN-MIL-2026-09-06-spread-CIN-2.5"
    assert group.title == "Cincinnati Reds vs Milwaukee Brewers — Cincinnati Reds -2.5 (2026-09-06)"
    assert group.start_time == datetime(2026, 9, 6, 16, 10, tzinfo=UTC)
    yes = {leg.outcome_id for leg in group.legs if leg.is_yes_side}
    no = {leg.outcome_id for leg in group.legs if not leg.is_yes_side}
    assert yes == {
        "KXMLBSPREAD-26SEP061210MILCIN-CIN3:YES",
        "asc-mlb-mil-cin-2026-09-06-pos-2pt5:SHORT",
    }
    assert no == {
        "KXMLBSPREAD-26SEP061210MILCIN-CIN3:NO",
        "asc-mlb-mil-cin-2026-09-06-pos-2pt5:LONG",
    }
    assert {leg.venue_id for leg in group.legs} == {"kalshi", "polymarket_us"}


async def test_a_negative_polymarket_line_pairs_with_kalshi_yes_on_the_first_team():
    """neg-3pt5 long 0.26/0.27 vs NESEA-NE4 YES 0.25/0.27; neg-10pt5 long
    0.08/0.11 vs SFLAR-SF11 YES 0.08/0.14 (2026-09-06)."""
    kalshi, poly = await _both(_kalshi_milcin(("MIL3", 2.5)), [NEG])
    matches = match_games(kalshi, poly)
    assert len(matches) == 1
    group = match_to_event_group(matches[0])
    assert group.id == "mlb-CIN-MIL-2026-09-06-spread-MIL-2.5"
    yes = {leg.outcome_id for leg in group.legs if leg.is_yes_side}
    assert yes == {
        "KXMLBSPREAD-26SEP061210MILCIN-MIL3:YES",
        "asc-mlb-mil-cin-2026-09-06-neg-2pt5:LONG",
    }


async def test_opposite_anchors_on_the_same_line_never_pair_across_venues():
    """Kalshi MIL3 (Brewers by more than 2.5) against Polymarket pos-2pt5
    (Reds by more than 2.5) share a line and a game and are different bets."""
    kalshi, poly = await _both(_kalshi_milcin(("MIL3", 2.5)), [POS])
    assert match_games(kalshi, poly) == []


async def test_a_full_ladder_yields_one_group_per_shared_anchor_and_line():
    """Kalshi lists ±1.5/±2.5/±3.5 for MLB, Polymarket only ±1.5/±2.5; the
    intersection is what becomes groups. Observed 4 per MLB game live."""
    pm = [
        _pm_spread("asc-mlb-mil-cin-2026-09-06-neg-1pt5", -1.5, "Milwaukee Brewers wins by over 1.5 runs"),
        _pm_spread("asc-mlb-mil-cin-2026-09-06-neg-2pt5", -2.5, "Milwaukee Brewers wins by over 2.5 runs"),
        _pm_spread("asc-mlb-mil-cin-2026-09-06-pos-1pt5", 1.5, "Cincinnati Reds wins by over 1.5 runs"),
        _pm_spread("asc-mlb-mil-cin-2026-09-06-pos-2pt5", 2.5, "Cincinnati Reds wins by over 2.5 runs"),
    ]
    kalshi, poly = await _both(
        _kalshi_milcin(("MIL2", 1.5), ("MIL3", 2.5), ("MIL4", 3.5),
                       ("CIN2", 1.5), ("CIN3", 2.5), ("CIN4", 3.5)),
        pm,
    )
    ids = sorted(m.event_group_id() for m in match_games(kalshi, poly))
    assert ids == [
        "mlb-CIN-MIL-2026-09-06-spread-CIN-1.5",
        "mlb-CIN-MIL-2026-09-06-spread-CIN-2.5",
        "mlb-CIN-MIL-2026-09-06-spread-MIL-1.5",
        "mlb-CIN-MIL-2026-09-06-spread-MIL-2.5",
    ]
```

- [ ] **Step 2: Run to verify they pass**

Run: `venv\Scripts\python.exe -m pytest tests/discovery/test_spreads.py -q`
Expected: all pass. (These tests should pass immediately against Tasks 3–5; if any fails, the parser or matcher is wrong — fix the implementation, not the test.)

- [ ] **Step 3: Commit**

```bash
git add tests/discovery/test_spreads.py
git status --porcelain
git commit -m "test(discovery): pin the cross-venue spread sign convention

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Horizon pre-filter in the Kalshi team-sport and totals fetchers

**Files:**
- Modify: `arbys/discovery/kalshi_sports.py` (`fetch_kalshi_team_games` line 94, `_parse_kalshi_event` line 139)
- Modify: `arbys/discovery/kalshi_totals.py` (`fetch_kalshi_totals` line 70, `_parse_totals_event` line 101)
- Test: `tests/discovery/test_horizon.py` (extend)

**Interfaces:**
- Consumes: `within_horizon`, `discovery_horizon_days` (Task 2).
- Produces: `fetch_kalshi_team_games(..., horizon_days: int | None = None)`, `fetch_kalshi_totals(..., horizon_days: int | None = None)`; `_parse_kalshi_event(..., horizon_days: int = 0)`, `_parse_totals_event(..., horizon_days: int = 0)` (default 0 keeps any direct caller's behaviour).

- [ ] **Step 1: Write the failing tests**

Append to `tests/discovery/test_horizon.py`. Add imports at the top:

```python
import httpx

from arbys.discovery.kalshi_sports import fetch_kalshi_team_games
from arbys.discovery.kalshi_totals import fetch_kalshi_totals
```

Append:

```python
def _kalshi_client(events, markets, calls: list[str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=events)
        return httpx.Response(200, json=markets)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=5.0,
        base_url="https://api.elections.kalshi.com/trade-api/v2",
    )


NFL_MARKETS = {
    "markets": [
        {"ticker": "KXNFLGAME-30SEP09NESEA-NE", "yes_sub_title": "New England"},
        {"ticker": "KXNFLGAME-30SEP09NESEA-SEA", "yes_sub_title": "Seattle"},
    ]
}


async def test_kalshi_team_games_skip_the_market_call_past_the_horizon():
    """The per-event /markets request is the cost; a 2030 game must not incur
    it. With the horizon off the same event is fetched and parsed."""
    events = {"events": [{"event_ticker": "KXNFLGAME-30SEP09NESEA"}]}
    calls: list[str] = []
    client = _kalshi_client(events, NFL_MARKETS, calls)

    games = await fetch_kalshi_team_games(
        resolver=NFL_RESOLVER, sport="nfl", http_client=client, horizon_days=3
    )
    assert games == []
    assert not any(p.endswith("/markets") for p in calls)

    calls.clear()
    games = await fetch_kalshi_team_games(
        resolver=NFL_RESOLVER, sport="nfl", http_client=client, horizon_days=0
    )
    await client.aclose()
    assert len(games) == 1 and games[0].game_date == date(2030, 9, 9)
    assert any(p.endswith("/markets") for p in calls)


async def test_kalshi_totals_skip_the_market_call_past_the_horizon():
    events = {"events": [{"event_ticker": "KXNFLTOTAL-30SEP09NESEA"}]}
    markets = {"markets": [{"ticker": "KXNFLTOTAL-30SEP09NESEA-45", "floor_strike": 44.5}]}
    calls: list[str] = []
    client = _kalshi_client(events, markets, calls)

    games = await fetch_kalshi_totals(
        resolver=NFL_RESOLVER, sport="nfl", http_client=client, horizon_days=3
    )
    assert games == []
    assert not any(p.endswith("/markets") for p in calls)

    calls.clear()
    games = await fetch_kalshi_totals(
        resolver=NFL_RESOLVER, sport="nfl", http_client=client, horizon_days=0
    )
    await client.aclose()
    assert len(games) == 1
    assert any(p.endswith("/markets") for p in calls)


async def test_kalshi_fetchers_read_the_config_when_no_horizon_is_given(monkeypatch):
    events = {"events": [{"event_ticker": "KXNFLGAME-30SEP09NESEA"}]}
    calls: list[str] = []
    client = _kalshi_client(events, NFL_MARKETS, calls)
    monkeypatch.setenv("ARBYS_DISCOVERY_HORIZON_DAYS", "3")
    assert await fetch_kalshi_team_games(resolver=NFL_RESOLVER, sport="nfl", http_client=client) == []
    monkeypatch.setenv("ARBYS_DISCOVERY_HORIZON_DAYS", "0")
    assert len(await fetch_kalshi_team_games(resolver=NFL_RESOLVER, sport="nfl", http_client=client)) == 1
    await client.aclose()
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv\Scripts\python.exe -m pytest tests/discovery/test_horizon.py -q`
Expected: the three new tests fail with `TypeError: … unexpected keyword argument 'horizon_days'`.

- [ ] **Step 3: Implement**

`arbys/discovery/kalshi_sports.py` — add the import (after `from .teams import Team, TeamResolver`):

```python
from .horizon import discovery_horizon_days, within_horizon
```

Change `fetch_kalshi_team_games`:

```python
async def fetch_kalshi_team_games(
    *,
    resolver: TeamResolver,
    sport: str,
    series_ticker: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 100,
    horizon_days: int | None = None,
) -> list[VenueGame]:
    """Fetch open game events for a team sport and return a VenueGame per game.

    ``horizon_days`` bounds how far ahead a game may be (``None`` reads
    ``ARBYS_DISCOVERY_HORIZON_DAYS``); it is checked before the per-event
    market call, which is what costs against Kalshi's rate limit.
    """
    series = series_ticker or SERIES_TICKERS.get(sport)
    if series is None:
        raise ValueError(f"no Kalshi series ticker known for sport {sport!r}")
    days = discovery_horizon_days() if horizon_days is None else horizon_days
```

…and pass it: `game = await _parse_kalshi_event(client, ev, resolver, sport=sport, horizon_days=days)`.

Change `_parse_kalshi_event`'s signature and add the check right after the `game_date is None` return:

```python
async def _parse_kalshi_event(
    client: httpx.AsyncClient,
    event: dict,
    resolver: TeamResolver,
    *,
    sport: str = "mlb",
    horizon_days: int = 0,
) -> VenueGame | None:
    event_ticker = event.get("event_ticker") or ""
    if not event_ticker:
        return None

    game_date = _parse_ticker_date(event_ticker)
    if game_date is None:
        return None
    if not within_horizon(game_date, days=horizon_days):
        return None
```

`arbys/discovery/kalshi_totals.py` — add `from .horizon import discovery_horizon_days, within_horizon` to the imports; add `horizon_days: int | None = None` to `fetch_kalshi_totals`, compute `days = discovery_horizon_days() if horizon_days is None else horizon_days` after the `series is None` check, and call `_parse_totals_event(client, ev, resolver, sport=sport, horizon_days=days)`. Change `_parse_totals_event`:

```python
async def _parse_totals_event(
    client: httpx.AsyncClient,
    event: dict,
    resolver: TeamResolver,
    *,
    sport: str,
    horizon_days: int = 0,
) -> list[VenueGame]:
```

and insert, right before the `resp = await _get_with_retry(client, "/markets", …)` line:

```python
    # The market call is what costs a request; a game past the horizon is
    # skipped before it is made.
    if not within_horizon(game_date, days=horizon_days):
        return []
```

- [ ] **Step 4: Run to verify they pass**

Run: `venv\Scripts\python.exe -m pytest tests/discovery -q`
Expected: all pass, including the pre-existing `test_kalshi_sports.py` and `test_totals.py` (their fixtures use 2026 dates, which are inside any horizon measured from a later "today", and their fetch calls do not pass `horizon_days`, so they read the config default of 3 — a 2026-09-13 fixture date passes because the horizon only bounds the future).

If a pre-existing test fails because its fixture date is in the future relative to the machine clock, pass `horizon_days=0` in that test's fetch call rather than changing the fixture.

- [ ] **Step 5: Commit**

```bash
venv\Scripts\python.exe -m ruff check arbys/discovery tests/discovery
git add arbys/discovery/kalshi_sports.py arbys/discovery/kalshi_totals.py tests/discovery/test_horizon.py
git status --porcelain
git commit -m "feat(discovery): Kalshi fetchers skip the market call past the horizon

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Discovery service — helper, horizon, spreads registry, kill switch

**Files:**
- Modify: `arbys/discovery/service.py` (imports lines 13–30; `TOTALS_SPORTS` ~line 49; the three/four `discover_*` functions lines 57–135; `discover_all_event_groups` line 178)
- Modify: `tests/discovery/test_service.py` (every stub list that names `fetch_kalshi_totals`: lines ~69–70, ~128–129, ~173, ~259–260, ~300–301)

**Interfaces:**
- Consumes: `fetch_kalshi_spreads` (Task 4), `fetch_polymarket_us_spreads` (Task 5), `filter_horizon`, `discovery_horizon_days` (Task 2).
- Produces:
  - `SPREADS_SPORTS: tuple[tuple[str, TeamResolver], ...]`
  - `_spreads_enabled() -> bool` (reads `ARBYS_ENABLE_SPREADS`, default on)
  - `async _discover_pair(label: str, kalshi_coro, poly_coro, *, date_tolerance_days: int = 0) -> list[EventGroup]`
  - `async discover_spreads_event_groups(sport, resolver) -> list[EventGroup]`
  - existing `discover_team_sport_event_groups`, `discover_totals_event_groups`, `discover_tennis_event_groups`, `discover_ufc_event_groups` keep their signatures.

- [ ] **Step 1: Update the existing stubs so they keep passing**

In `tests/discovery/test_service.py`, every place that stubs `fetch_kalshi_totals` and `fetch_polymarket_us_totals` must also stub the two spread fetchers, or the new sub-pass would try the network. Make these edits:

Lines ~69–70 and ~128–129 (two occurrences of the pair):
```python
    monkeypatch.setattr(service_mod, "fetch_kalshi_totals", _empty)
    monkeypatch.setattr(service_mod, "fetch_polymarket_us_totals", _empty)
    monkeypatch.setattr(service_mod, "fetch_kalshi_spreads", _empty)
    monkeypatch.setattr(service_mod, "fetch_polymarket_us_spreads", _empty)
```

Lines ~173, ~259–260, ~300–301 (three tuples of names): append `"fetch_kalshi_spreads", "fetch_polymarket_us_spreads"` to each tuple, e.g.
```python
    for name in ("fetch_polymarket_us_games", "fetch_kalshi_tennis_matches",
                 "fetch_polymarket_us_tennis", "fetch_kalshi_totals",
                 "fetch_polymarket_us_totals", "fetch_kalshi_spreads",
                 "fetch_polymarket_us_spreads"):
```

Confirm with `grep -n "fetch_kalshi_totals" tests/discovery/test_service.py` that every occurrence now has a spreads neighbour.

- [ ] **Step 2: Write the failing tests**

Append to `tests/discovery/test_service.py`. Add `from datetime import date` is already imported; also add at top: `from arbys.discovery.teams import MLB_RESOLVER` is present. Append:

```python
def _dated_game(venue: str, day: date, sport: str = "mlb") -> VenueGame:
    lad = MLB_RESOLVER.by_code("LAD")
    chc = MLB_RESOLVER.by_code("CHC")
    assert lad is not None and chc is not None
    return VenueGame(
        sport=sport,
        venue_id=venue,
        game_date=day,
        teams=(lad, chc),
        outcome_ids={"LAD": f"{venue}-{day}-L", "CHC": f"{venue}-{day}-C"},
        ref=f"{venue}-{day}",
    )


async def _empty_fetch(**_):
    return []


ALL_FETCHERS = (
    "fetch_kalshi_team_games", "fetch_polymarket_us_games",
    "fetch_kalshi_totals", "fetch_polymarket_us_totals",
    "fetch_kalshi_spreads", "fetch_polymarket_us_spreads",
    "fetch_kalshi_tennis_matches", "fetch_polymarket_us_tennis",
)


@pytest.mark.asyncio
async def test_spreads_pass_runs_for_each_registered_sport_and_the_flag_removes_it(monkeypatch):
    seen: list[str] = []

    async def _spy(**kw):
        seen.append(kw["sport"])
        return []

    for name in ALL_FETCHERS:
        monkeypatch.setattr(service_mod, name, _empty_fetch)
    monkeypatch.setattr(service_mod, "fetch_kalshi_spreads", _spy)

    monkeypatch.delenv("ARBYS_ENABLE_SPREADS", raising=False)
    _groups, complete = await service_mod.discover_all_event_groups()
    assert complete
    assert sorted(seen) == ["mlb", "ncaaf", "nfl"]

    seen.clear()
    monkeypatch.setenv("ARBYS_ENABLE_SPREADS", "0")
    await service_mod.discover_all_event_groups()
    assert seen == []


@pytest.mark.asyncio
async def test_horizon_drops_a_far_game_from_a_team_sport_pass(monkeypatch):
    """A game in 2030 is on both venues and would match; the horizon keeps it
    out. With the horizon off it is registered."""
    near, far = date(2026, 8, 5), date(2030, 8, 5)

    async def fake_kalshi(**_):
        return [_dated_game("kalshi", near), _dated_game("kalshi", far)]

    async def fake_poly(**_):
        return [_dated_game("polymarket_us", near), _dated_game("polymarket_us", far)]

    monkeypatch.setattr(service_mod, "fetch_kalshi_team_games", fake_kalshi)
    monkeypatch.setattr(service_mod, "fetch_polymarket_us_games", fake_poly)

    monkeypatch.setenv("ARBYS_DISCOVERY_HORIZON_DAYS", "3")
    groups = await service_mod.discover_team_sport_event_groups("mlb", MLB_RESOLVER)
    assert [g.id for g in groups] == ["mlb-CHC-LAD-2026-08-05"]

    monkeypatch.setenv("ARBYS_DISCOVERY_HORIZON_DAYS", "0")
    groups = await service_mod.discover_team_sport_event_groups("mlb", MLB_RESOLVER)
    assert {g.id for g in groups} == {"mlb-CHC-LAD-2026-08-05", "mlb-CHC-LAD-2030-08-05"}


@pytest.mark.asyncio
async def test_horizon_applies_to_tennis_and_ufc_passes_too(monkeypatch):
    far = date(2030, 8, 5)

    async def fake_kalshi(**_):
        return [_dated_game("kalshi", far, sport="atp")]

    async def fake_poly(**_):
        return [_dated_game("polymarket_us", far, sport="atp")]

    monkeypatch.setattr(service_mod, "fetch_kalshi_tennis_matches", fake_kalshi)
    monkeypatch.setattr(service_mod, "fetch_polymarket_us_tennis", fake_poly)
    monkeypatch.setenv("ARBYS_DISCOVERY_HORIZON_DAYS", "3")
    assert await service_mod.discover_tennis_event_groups() == []
    assert await service_mod.discover_ufc_event_groups() == []
    monkeypatch.setenv("ARBYS_DISCOVERY_HORIZON_DAYS", "0")
    assert len(await service_mod.discover_tennis_event_groups()) == 1


@pytest.mark.asyncio
async def test_spreads_pass_builds_groups_from_both_fetchers(monkeypatch):
    from decimal import Decimal
    from dataclasses import replace

    def _spread(venue: str) -> VenueGame:
        base = _dated_game(venue, date(2026, 8, 5))
        return replace(base, market_type="spread", line=Decimal("2.5"), anchor="CHC")

    async def fake_kalshi(**_):
        return [_spread("kalshi")]

    async def fake_poly(**_):
        return [_spread("polymarket_us")]

    monkeypatch.setattr(service_mod, "fetch_kalshi_spreads", fake_kalshi)
    monkeypatch.setattr(service_mod, "fetch_polymarket_us_spreads", fake_poly)
    monkeypatch.setenv("ARBYS_DISCOVERY_HORIZON_DAYS", "0")
    groups = await service_mod.discover_spreads_event_groups("mlb", MLB_RESOLVER)
    assert [g.id for g in groups] == ["mlb-CHC-LAD-2026-08-05-spread-CHC-2.5"]
```

- [ ] **Step 3: Run to verify they fail**

Run: `venv\Scripts\python.exe -m pytest tests/discovery/test_service.py -q`
Expected: the four new tests fail (`fetch_kalshi_spreads` attribute missing on the module; `discover_spreads_event_groups` undefined; the 2030 game is registered). Pre-existing tests still pass.

- [ ] **Step 4: Implement**

In `arbys/discovery/service.py`:

Imports — add:

```python
from .horizon import discovery_horizon_days, filter_horizon
from .kalshi_spreads import fetch_kalshi_spreads
```

and add `fetch_polymarket_us_spreads,` to the `from .polymarket_us import (…)` list.

After `TOTALS_SPORTS` add:

```python
# Sports whose full-game spreads both venues quote. All three wired together
# on 2026-09-06 after the sign convention was pinned by live prices — see
# docs/superpowers/specs/2026-09-06-spreads-and-discovery-horizon-design.md.
# NBA and WNBA stay out until their seasons open, as with totals.
SPREADS_SPORTS: tuple[tuple[str, TeamResolver], ...] = (
    ("nfl", NFL_RESOLVER),
    ("mlb", MLB_RESOLVER),
    ("ncaaf", CFB_RESOLVER),
)


def _spreads_enabled() -> bool:
    """``ARBYS_ENABLE_SPREADS``, on by default.

    The kill switch for a scale problem found in production. Spreads roughly
    triple the group count, Kalshi carries every ticker on one socket with no
    known ceiling, and the Polymarket US per-connection ceiling was found only
    by measuring — so if quote ages creep at the new size this is a secret
    change and a restart rather than a redeploy. Existing spread groups
    retire on the next complete pass once it is off.
    """
    raw = os.environ.get("ARBYS_ENABLE_SPREADS", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


async def _discover_pair(
    label: str,
    kalshi_coro,
    poly_coro,
    *,
    date_tolerance_days: int = 0,
) -> list[EventGroup]:
    """Fetch both venues, bound them to the discovery horizon, match.

    Every sub-pass is this shape; the label is for the log line. The horizon
    is applied here, once, to every venue list before matching — the Kalshi
    fetchers also apply it before their per-event market call, but that is an
    optimisation of the same rule, not a second rule.
    """
    kalshi_games, poly_games = await asyncio.gather(kalshi_coro, poly_coro)
    days = discovery_horizon_days()
    kalshi_games = filter_horizon(kalshi_games, days=days)
    poly_games = filter_horizon(poly_games, days=days)
    matches = match_games(kalshi_games, poly_games, date_tolerance_days=date_tolerance_days)
    log.info(
        "discovery[%s]: kalshi=%d polymarket_us=%d matched=%d (horizon %dd)",
        label, len(kalshi_games), len(poly_games), len(matches), days,
    )
    return [match_to_event_group(m) for m in matches]
```

Replace the bodies of the four existing pass functions, **keeping their docstrings**:

```python
async def discover_team_sport_event_groups(
    sport: str, resolver: TeamResolver
) -> list[EventGroup]:
    """…existing docstring unchanged…"""
    return await _discover_pair(
        sport,
        fetch_kalshi_team_games(resolver=resolver, sport=sport),
        fetch_polymarket_us_games(resolver=resolver, sport=sport),
    )


async def discover_totals_event_groups(
    sport: str, resolver: TeamResolver
) -> list[EventGroup]:
    """…existing docstring unchanged…"""
    return await _discover_pair(
        f"{sport} totals",
        fetch_kalshi_totals(resolver=resolver, sport=sport),
        fetch_polymarket_us_totals(resolver=resolver, sport=sport),
    )


async def discover_spreads_event_groups(
    sport: str, resolver: TeamResolver
) -> list[EventGroup]:
    """Discover spread groups, one per (game, anchor, line).

    Only (anchor, line) pairs quoted on *both* venues survive the match.
    Kalshi lists ±1.5/±2.5/±3.5 per MLB game and Polymarket US ±1.5/±2.5, so
    expect four MLB groups a game; NFL and CFB ladders overlap on ~20-27.
    """
    return await _discover_pair(
        f"{sport} spreads",
        fetch_kalshi_spreads(resolver=resolver, sport=sport),
        fetch_polymarket_us_spreads(resolver=resolver, sport=sport),
    )
```

For `discover_tennis_event_groups` and `discover_ufc_event_groups`, keep the docstrings and replace each body with the `_discover_pair` call, passing `date_tolerance_days=1`:

```python
    return await _discover_pair(
        "tennis",
        fetch_kalshi_tennis_matches(),
        fetch_polymarket_us_tennis(),
        date_tolerance_days=1,
    )
```

```python
    return await _discover_pair(
        "ufc",
        fetch_kalshi_tennis_matches(series=UFC_SERIES),
        fetch_polymarket_us_tennis(leagues=UFC_LEAGUES, winner_types=UFC_WINNER_TYPES),
        date_tolerance_days=1,
    )
```

In `discover_all_event_groups`, replace the `results = await asyncio.gather(…)` expression with:

```python
    passes = [
        *(discover_team_sport_event_groups(s, r) for s, r in TEAM_SPORTS),
        *(discover_totals_event_groups(s, r) for s, r in TOTALS_SPORTS),
        discover_tennis_event_groups(),
        discover_ufc_event_groups(),
    ]
    if _spreads_enabled():
        passes.extend(discover_spreads_event_groups(s, r) for s, r in SPREADS_SPORTS)

    results = await asyncio.gather(
        *(_bounded(c) for c in passes),
        return_exceptions=True,
    )
```

- [ ] **Step 5: Run the whole suite**

Run: `venv\Scripts\python.exe -m pytest -q`
Expected: all pass (547 existing + the new ones). If any test outside `tests/discovery` fails, read it before touching anything — it should not.

- [ ] **Step 6: Lint and commit**

```bash
venv\Scripts\python.exe -m ruff check .
git add arbys/discovery/service.py tests/discovery/test_service.py
git status --porcelain
git commit -m "feat(discovery): spreads sub-passes, horizon on every pass, ARBYS_ENABLE_SPREADS

The four identical pair-discovery functions collapse into _discover_pair,
which applies the horizon to every venue list before matching. Spreads
run for NFL, MLB and NCAAF behind a default-on kill switch.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Config and docs

**Files:**
- Modify: `.env.example` (discovery block, ~line 55)
- Modify: `CLAUDE.md` (Commands test count; Architecture market-types paragraph; Config list; Phase 2 section)
- Modify: `docs/RUNBOOK.md` (§2.1 Auto-discovery)

**Interfaces:** none. Documentation of Tasks 1–8.

- [ ] **Step 1: `.env.example`**

Replace the discovery comment block:

```
# Auto-discovery: periodically scan Kalshi + Polymarket US for cross-venue
# sports games (MLB, NFL, NBA, ATP/WTA tennis) and auto-register matching
# event groups. Requires ARBYS_ENABLE_INGEST=1 to actually stream the
# discovered outcomes.
ARBYS_ENABLE_DISCOVERY=0
ARBYS_DISCOVERY_INTERVAL_S=600
```

with:

```
# Auto-discovery: periodically scan Kalshi + Polymarket US for the same
# real-world game and auto-register cross-venue event groups. Moneyline,
# totals and spreads for MLB, NFL, WNBA and NCAAF; ATP/WTA and UFC winners.
# Requires ARBYS_ENABLE_INGEST=1 to actually stream the discovered outcomes.
ARBYS_ENABLE_DISCOVERY=0
ARBYS_DISCOVERY_INTERVAL_S=600

# How far ahead discovery looks, in Eastern calendar days, for EVERY market
# type. Kalshi lists NFL a week or more out, and on 2026-09-03 127 of 174
# upcoming groups started beyond the 7-day fill rule — subscriptions that
# could never trade. Nearly all the edge is on game day: of 1,248 fills
# (2026-08-28..09-03), 1,177 were on game day and 18 were four or more days
# out, worth $0.67 of $177. A game that is not registered cannot be filled,
# so this is what bounds how long capital is locked; ARBYS_MAX_DAYS_TO_START
# stays as the backstop for hand-registered groups. Judged on the game date
# both venues carry, so the two sides always agree. 0 disables.
ARBYS_DISCOVERY_HORIZON_DAYS=3

# Run the spread sub-passes (MLB, NFL, NCAAF full-game spreads). On by
# default. The kill switch for a scale problem: spreads roughly triple the
# group count, Kalshi carries every ticker on one socket with no known
# ceiling, and the Polymarket US per-connection ceiling was found only by
# measuring. 0 removes the passes; existing spread groups then retire on the
# next complete discovery pass.
ARBYS_ENABLE_SPREADS=1
```

- [ ] **Step 2: `CLAUDE.md` — Architecture, market types**

Find the paragraph beginning `A \`market_type="spread"\` also carries an **anchor**` (in the `arbys/discovery/` bullet) and replace it with:

```markdown
  A `market_type="spread"` also carries an **anchor** — the participant its
  line is stated for — and the anchor is in the bucket key **and the group
  id** for the same reason the line is: `CIN -2.5` and `MIL -2.5` on one game
  are different bets. Wired 2026-09-06 for **nfl, mlb and ncaaf**
  (`SPREADS_SPORTS`, `SPREADS_SERIES`, `SPREAD_TYPES`); see **Phase 2 —
  spreads** below for the sign convention, which is the whole difficulty.
  Group ids read `nfl-NE-SEA-2026-09-09-spread-SEA-3.5` and titles
  `… — Seattle Seahawks -3.5 (…)`. Both id parsers (server
  `performance.py`, frontend `performance.ts`) already read the segment
  after the date as the market type, so `/account` gained a `spread` row
  with no change.
```

- [ ] **Step 3: `CLAUDE.md` — Config**

After the `ARBYS_MAX_DAYS_TO_START` entry add two entries:

```markdown
- `ARBYS_DISCOVERY_HORIZON_DAYS` — how far ahead discovery looks, in Eastern
  calendar days, for **every** market type, default 3, `0` disables. Kalshi
  lists NFL a week or more out, and on 2026-09-03 the local database held 174
  upcoming groups of which 127 started beyond the 7-day fill rule — pure
  subscription cost. The edge is on game day: of 1,248 fills over
  2026-08-28..09-03, 1,177 were on game day and 18 were four or more days
  out, worth $0.67 of $177. **A game that is not registered cannot be
  filled, so this is now what bounds capital lock**; `ARBYS_MAX_DAYS_TO_START`
  stays as the chokepoint backstop for hand-registered groups. Judged on
  `game_date`, which both venues carry as an Eastern date, so a game never
  has one leg inside the window and one outside. Applied once in
  `service._discover_pair` to every venue list, and again inside the Kalshi
  event parsers *before* their per-event `/markets` call, since the date is
  in the ticker — on a Sunday that skips most Kalshi requests, which is where
  the 429s came from. It does **not** lower the weekend peak: a Saturday CFB
  slate and a Sunday NFL slate are both inside 3 days by Thursday.
- `ARBYS_ENABLE_SPREADS` — run the spread sub-passes, **1 by default**. The
  kill switch for a scale problem found in production: spreads roughly triple
  the group count, Kalshi carries every ticker on one socket with no known
  ceiling, and the Polymarket US ceiling was found only by measuring. `0`
  removes the passes and existing spread groups retire on the next complete
  pass.
```

- [ ] **Step 4: `CLAUDE.md` — Phase 2**

Replace the `- **Phase 2 — spreads** (MLB + NFL). …` bullet under *Phase 2 and beyond* with:

```markdown
- **Phase 2 — spreads: wired 2026-09-06** for MLB, NFL and NCAAF. Both
  venues are normalised to one canonical form — a **positive** line and an
  **anchor**, the team that must win by more than it — with outcomes keyed by
  team code like a moneyline; the anchor's leg is the TRUE side. Kalshi is
  natively in that form: one market per (team, line), the team in the ticker
  suffix (`KXNFLSPREAD-26SEP09NESEA-SEA4`), the line in `floor_strike`,
  `strike_type` must be `greater` or the market is refused. Polymarket US
  lists one market per **signed line on the first-listed team**, and long is
  always that team covering it, so:

  | Polymarket `line` | anchor | LONG ≡ | pinned by (2026-09-06) |
  | --- | --- | --- | --- |
  | `-3.5` (`neg-3pt5`) | first team | Kalshi **YES** on `NE4` | PM 0.26/0.27 vs Kalshi YES 0.25/0.27 |
  | `+3.5` (`pos-3pt5`) | second team | Kalshi **NO** on `SEA4` | PM 0.51/0.52 vs Kalshi NO 0.51/0.52 |

  Outcomes are therefore *always* first team → `:LONG`, second → `:SHORT`;
  only the anchor flips with the sign. **Getting this backwards near even
  money is a 2–4¢ phantom edge that `ARBYS_MAX_PLAUSIBLE_EDGE` cannot see**,
  so the parser cross-checks its derivation against the venue's own `title`
  (`"Seattle Seahawks wins by over 3.5 points"`, which named the derived
  anchor on all 5,267 markets observed) and skips a market that contradicts
  it; a title matching no pattern is accepted and counted, so a rewording
  disables the guard visibly rather than zeroing the league.
  `tests/discovery/test_spreads.py` pins the convention end to end. Two
  Kalshi codes needed aliases on the way — `AZ` (Diamondbacks) and `JAC`
  (Jaguars) — and had been silently dropping those games from totals.
  Period spreads (`baseball_team_first_five_spread`, quarters, halves;
  `KXMLBF5SPREAD`) are distinct types and remain Phase 3.
```

- [ ] **Step 5: `CLAUDE.md` — test count**

After Task 10's full run, update `# 547 tests, must stay green` in **Commands** to the new count.

- [ ] **Step 6: `docs/RUNBOOK.md` §2.1**

After the paragraph ending `…still align across venues.` add:

```markdown
Three market types are discovered per team sport: moneyline, totals and
(since 2026-09-06) full-game spreads. A spread group's id carries the anchor
team and the line — `nfl-NE-SEA-2026-09-09-spread-SEA-3.5` is "Seattle wins
by more than 3.5", TRUE side Kalshi `SEA4:YES` and Polymarket `…-pos-3pt5:SHORT`.
`ARBYS_ENABLE_SPREADS=0` removes the spread passes without a redeploy.

Discovery only registers games starting within `ARBYS_DISCOVERY_HORIZON_DAYS`
(default 3) Eastern calendar days. A game further out is not fetched from
Kalshi at all — its per-event market call is skipped — and is dropped from
the Polymarket list before matching. Lower it to shed load, `0` to disable.
```

- [ ] **Step 7: Commit**

```bash
git add .env.example CLAUDE.md docs/RUNBOOK.md
git status --porcelain
git commit -m "docs: spreads are wired; the discovery horizon and its two knobs

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Full verification and a live dry run

**Files:** none new. `CLAUDE.md` test count (Task 9 Step 5).

- [ ] **Step 1: Full suite and lint**

```
venv\Scripts\python.exe -m pytest -q
venv\Scripts\python.exe -m ruff check .
```

Expected: all green, ruff clean. Record the test count and put it in `CLAUDE.md` **Commands**.

- [ ] **Step 2: Import-cycle check**

```
venv\Scripts\python.exe -c "import arbys.discovery.horizon, arbys.discovery.kalshi_sports, arbys.discovery.kalshi_spreads, arbys.discovery.service; print('ok')"
```

Expected: `ok`. (`horizon` imports `VenueGame` only under `TYPE_CHECKING`.)

- [ ] **Step 3: Live dry run (network; manual verification, not a test)**

```
venv\Scripts\python.exe scripts\discover_cross_venue.py --dry-run 2>&1 | Select-String -Pattern "discovery\[|spread" | Select-Object -First 60
```

Expected, on a day both venues list games:
- `discovery[mlb spreads]`, `discovery[nfl spreads]`, `discovery[ncaaf spreads]` log lines with non-zero `matched=` for leagues in season and within the horizon.
- Group ids of the shape `<sport>-<A>-<B>-<date>-spread-<ANCHOR>-<line>`; every listed game date within 3 days of today Eastern.
- For each spread group, `yes=True` on exactly the Kalshi `:YES` leg and the Polymarket leg that agrees with the sign rule (`:LONG` when the anchor is the first-listed team, `:SHORT` otherwise).
- No `contradicts` warnings. Any `matched no known pattern` warning means Polymarket reworded titles — report it.

Note the totals and moneyline counts in the same output: they should be **smaller** than before this branch, because the horizon now applies to them too.

- [ ] **Step 4: Commit the test count**

```bash
git add CLAUDE.md
git status --porcelain
git commit -m "docs: test count after spreads and the horizon

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 5: Report**

State plainly: test count, ruff result, the dry-run group counts per league (spreads, and the before/after effect of the horizon on moneyline and totals if visible), and any warning seen. Then the branch is ready for the finishing-a-development-branch decision (merge to `main`, deploy via the `Deploy` Action, and the §12 watch list in the spec).

---

## Self-review

**Spec coverage.**
- §1 canonical form → Tasks 4, 5 (VenueGame shape), 3 (yes side). ✓
- §2 Kalshi parser → Task 4. ✓
- §3 Polymarket parser, both guards, count-once warning → Task 5. ✓
- §4 id and title, downstream parsers unchanged → Task 3 (incl. `parse_group_id` test). ✓
- §5 registry, helper collapse, kill switch → Task 8. ✓
- §6 horizon module, service-level filter incl. tennis/UFC, Kalshi pre-filter → Tasks 2, 7, 8. ✓
- §7 code aliases → Task 1. ✓
- §8 config docs → Task 9. ✓
- §9 nothing else changes → no task touches those layers. ✓
- §10 tests → Tasks 1–8 as listed; renamed phase-1 test in Task 5. ✓
- §11 docs → Task 9. ✓
- §12 rollout → Task 10 Step 3 covers the dry run; deploy and the watch list are post-merge and stay in the spec. ✓

**Placeholder scan.** No TBD/TODO. Every code step has code. The one "…existing docstring unchanged…" marker in Task 8 refers to text the implementer can see in the file being edited, not to a neighbouring task.

**Type consistency.** `within_horizon(game_date, *, days, today=None)` and `filter_horizon(games, *, days, today=None)` are used with the same keyword names in Tasks 4, 7, 8. `horizon_days: int | None = None` on all three Kalshi fetchers; `horizon_days: int = 0` on the two private parsers. `anchor_code_from_ticker(market_ticker, event_ticker, codes)` returns the raw code and Task 4's parser resolves it. `_title_agrees(title, anchor_name, line) -> bool | None`. `_discover_pair(label, kalshi_coro, poly_coro, *, date_tolerance_days=0)`. `SPREADS_SPORTS` / `SPREADS_SERIES` / `SPREAD_TYPES` names are consistent between Tasks 4, 5, 8, 9.
