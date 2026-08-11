# Polymarket US Migration — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Polymarket international integration with Polymarket US at feature parity, fix the Polymarket fee model (currently returns zero), and add the matcher groundwork that Phase 2 spreads will need.

**Architecture:** A new REST adapter polls `gateway.polymarket.us/v1/markets/{slug}/bbo` and emits two `Quote`s per market (`{slug}:LONG` and `{slug}:SHORT`, the latter price-inverted). A single new discovery module replaces three, because `/v2/leagues/{slug}/events` returns every market type for a league in one call. The `venue_id` becomes `polymarket_us` everywhere; international code is deleted, not left dormant.

**Tech Stack:** Python 3.11+, httpx (async), SQLAlchemy 2 async, Alembic, pytest (`asyncio_mode = "auto"`), FastAPI. Frontend: Vite + React 19 + TS.

**Spec:** [docs/superpowers/specs/2026-08-11-polymarket-us-migration-design.md](../specs/2026-08-11-polymarket-us-migration-design.md)

## Global Constraints

- **All money and all prices are `Decimal`. Never float.** Convert via `Decimal(str(v))` — the API returns JSON floats for `line` (e.g. `21.5`) and decimal strings for prices (e.g. `"0.4550"`).
- Prices are probabilities in `[0, 1]`. `Quote.__post_init__` enforces range and `ask >= bid`; a construction that violates either raises `ValueError`.
- Domain types are `@dataclass(frozen=True)`; enums are `StrEnum`.
- `arbys/shared/` is **pure domain — no I/O, no framework imports.** No `httpx`, no SQLAlchemy, no FastAPI in that package.
- `outcome_id` values are venue-native and not portable. Never key cross-venue logic on `outcome_id` alone; always carry `venue_id`.
- **Tests never hit a real venue.** REST paths mock with `httpx.MockTransport`.
- `pytest` runs with `asyncio_mode = "auto"` — async tests need no decorator, but existing files use explicit `@pytest.mark.asyncio` and that is fine to match.
- **Migrations must never build DDL from `Base.metadata`.** Each revision describes its own change in explicit `op.*` calls.
- Run everything from the repo root with `venv\Scripts\python.exe`, never a bare `python`.
- Green-build bar, must stay passing: `venv\Scripts\python.exe -m pytest -q` and `venv\Scripts\python.exe -m ruff check .`; in `frontend/`, `npm run build`.
- **mypy is NOT part of the bar** — 47 pre-existing errors across 17 files. Do not start a cleanup. Annotating new code is welcome.
- Frontend: style only via the design system's semantic classes and CSS custom properties. **No new hex colors, radii, or type scales.** Phase 1 changes label text only.
- The venue string is exactly `polymarket_us` (snake case) in all backend/DB code, and `"Polymarket US"` in user-facing frontend copy.

## File Structure

| File | Responsibility | Task |
| --- | --- | --- |
| `arbys/shared/fees.py` | add `PolymarketUsFeeModel`, remove `PolymarketFeeModel` | 1 |
| `arbys/adapters/polymarket_us.py` | **create** — REST `/bbo` poll, LONG/SHORT quote derivation | 2 |
| `arbys/discovery/polymarket_us.py` | **create** — league events → `VenueGame` for moneyline + totals + tennis | 3 |
| `arbys/discovery/kalshi_sports.py` | add `anchor` field to `VenueGame` | 4 |
| `arbys/discovery/matcher.py` | `anchor` in bucket key; `yes_key` dispatch | 4 |
| `arbys/discovery/service.py` | swap imports to the new module | 5 |
| `arbys/backend/state.py` | fee registry + adapter factory | 5 |
| `arbys/db/migrations/versions/0005_*.py` | **create** — purge `polymarket` rows | 6 |
| `frontend/src/**` | 4 hardcoded venue strings | 7 |
| `CLAUDE.md`, `docs/RUNBOOK.md`, `.env.example` | docs + config | 8 |
| `scripts/smoke_polymarket_us.py` | **create** — manual live verification | 8 |

**Deleted** (Task 5): `arbys/adapters/polymarket.py`, `arbys/discovery/polymarket_sports.py`, `arbys/discovery/polymarket_tennis.py`, `arbys/discovery/polymarket_totals.py`, `tests/adapters/test_polymarket.py`, `tests/discovery/test_polymarket_sports.py`, `scripts/smoke_polymarket_ws.py`.

---

### Task 1: Polymarket US fee model

Fees gate whether something is called an arbitrage, so per CLAUDE.md the fee test is written **first**. `PolymarketFeeModel` currently returns zero, which overstates every net edge on a Polymarket leg.

**Files:**
- Modify: `arbys/shared/fees.py:53-66`
- Test: `tests/shared/test_fees.py`

**Interfaces:**
- Consumes: nothing
- Produces: `PolymarketUsFeeModel(venue_id: str = "polymarket_us", rate: Decimal = Decimal("0.06"))` with `fee(*, price: Decimal, qty: Decimal, is_buy: bool) -> Decimal`. Used by Task 5 in the `AppState.fees` registry.

- [ ] **Step 1: Write the failing test**

Append to `tests/shared/test_fees.py`:

```python
from decimal import Decimal

from arbys.shared.fees import PolymarketUsFeeModel


def test_polymarket_us_fee_peaks_at_a_coin_flip():
    """Official schedule: fee = 0.06 * C * p * (1-p).

    Max at p=0.50 -> 0.06 * 0.25 = 0.015/contract = $1.50 per 100.
    """
    model = PolymarketUsFeeModel()
    fee = model.fee(price=Decimal("0.50"), qty=Decimal("100"), is_buy=True)
    assert fee == Decimal("1.5000")


def test_polymarket_us_fee_vanishes_at_the_extremes():
    model = PolymarketUsFeeModel()
    assert model.fee(price=Decimal("0"), qty=Decimal("100"), is_buy=True) == Decimal("0")
    assert model.fee(price=Decimal("1"), qty=Decimal("100"), is_buy=True) == Decimal("0")


def test_polymarket_us_fee_is_cheaper_than_kalshi_at_the_same_price():
    """0.06 vs Kalshi's 0.07 — same shape, lower coefficient."""
    from arbys.shared.fees import KalshiFeeModel

    price, qty = Decimal("0.45"), Decimal("100")
    poly = PolymarketUsFeeModel().fee(price=price, qty=qty, is_buy=True)
    kalshi = KalshiFeeModel().fee(price=price, qty=qty, is_buy=True)
    assert poly < kalshi


def test_polymarket_us_fee_is_zero_for_nonpositive_qty():
    model = PolymarketUsFeeModel()
    assert model.fee(price=Decimal("0.5"), qty=Decimal("0"), is_buy=True) == Decimal("0")
    assert model.fee(price=Decimal("0.5"), qty=Decimal("-5"), is_buy=True) == Decimal("0")


def test_polymarket_us_venue_id():
    assert PolymarketUsFeeModel().venue_id == "polymarket_us"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/shared/test_fees.py -q -k polymarket_us`
Expected: FAIL — `ImportError: cannot import name 'PolymarketUsFeeModel'`

- [ ] **Step 3: Replace the fee model**

In `arbys/shared/fees.py`, **delete** the `PolymarketFeeModel` class (lines 53-66) and put this in its place:

```python
@dataclass(frozen=True)
class PolymarketUsFeeModel:
    """Polymarket US taker fee.

    Official schedule: ``fee = 0.06 * C * p * (1 - p)``, the same shape as
    Kalshi's with a lower coefficient. Peaks at a coin flip ($1.50 per 100
    contracts at p=0.50) and vanishes at the extremes.

    Two deliberate omissions:

    * The maker rebate (-0.0125) is not modelled. The paper broker fills
      against the ask as a taker, so a maker rebate would never apply.
    * Polymarket US rounds to the cent per contract using banker's rounding
      and we do not round at all, so modelled fees come out slightly low and
      marginal edges look slightly better than they are. This matches the
      existing understatement on the Kalshi side.
    """

    venue_id: str = "polymarket_us"
    rate: Decimal = Decimal("0.06")

    def fee(self, *, price: Decimal, qty: Decimal, is_buy: bool) -> Decimal:
        if qty <= 0:
            return Decimal("0")
        return self.rate * price * (Decimal("1") - price) * qty
```

- [ ] **Step 4: Run the fee tests**

Run: `venv\Scripts\python.exe -m pytest tests/shared/test_fees.py -q`
Expected: PASS. Any test still referencing `PolymarketFeeModel` will fail — update those references to `PolymarketUsFeeModel` with `venue_id="polymarket_us"`.

- [ ] **Step 5: Commit**

```bash
git add arbys/shared/fees.py tests/shared/test_fees.py
git commit -m "fix(fees): model the Polymarket US taker fee instead of zero

PolymarketFeeModel returned zero, so every net edge published on a
Polymarket leg was overstated by up to 1.25c/contract at a coin flip -
larger than most edges the scanner detects.

Polymarket US charges 0.06 * C * p * (1-p), the same shape as Kalshi's
0.07. Expect published net edges to fall; that is this defect being
fixed, not a regression."
```

---

### Task 2: Polymarket US market-data adapter

**Files:**
- Create: `arbys/adapters/polymarket_us.py`
- Test: `tests/adapters/test_polymarket_us.py`

**Interfaces:**
- Consumes: `MarketDataAdapter` from `arbys/adapters/base.py`; `Outcome`, `Quote`, `Side` from `arbys/shared/types.py`
- Produces:
  - `GATEWAY_BASE = "https://gateway.polymarket.us"`
  - `split_outcome_id(outcome_id: str) -> tuple[str, str]` → `(slug, "LONG"|"SHORT")`
  - `quotes_from_bbo(slug: str, market_data: dict) -> list[Quote]` → 0 or 2 quotes
  - `PolymarketUsAdapter(*, poll_interval_s: float = 5.0, outcome_ids: list[str] | None = None, http_client: httpx.AsyncClient | None = None)` with `venue_id = "polymarket_us"`, `list_markets()`, `stream_quotes()`, `close()`

Real `/bbo` payload shape, captured 2026-08-11:

```json
{"marketData": {
  "marketSlug": "aec-mlb-cle-det-2026-08-11",
  "bestBid": {"value": "0.4500", "currency": "USD"},
  "bestAsk": {"value": "0.4550", "currency": "USD"},
  "bidDepth": 36, "askDepth": 37,
  "lastTradePx": {"value": "0.4550", "currency": "USD"},
  "openInterest": "27560.2200", "sharesTraded": "38475.0100"
}}
```

- [ ] **Step 1: Write the failing test**

Create `tests/adapters/test_polymarket_us.py`:

```python
import asyncio
from decimal import Decimal

import httpx
import pytest

from arbys.adapters.polymarket_us import (
    PolymarketUsAdapter,
    quotes_from_bbo,
    split_outcome_id,
)

BBO = {
    "marketData": {
        "marketSlug": "aec-mlb-cle-det-2026-08-11",
        "bestBid": {"value": "0.4500", "currency": "USD"},
        "bestAsk": {"value": "0.4550", "currency": "USD"},
        "bidDepth": 36,
        "askDepth": 37,
    }
}


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def test_split_outcome_id():
    assert split_outcome_id("aec-mlb-cle-det-2026-08-11:LONG") == (
        "aec-mlb-cle-det-2026-08-11",
        "LONG",
    )
    assert split_outcome_id("aec-mlb-cle-det-2026-08-11:SHORT") == (
        "aec-mlb-cle-det-2026-08-11",
        "SHORT",
    )


def test_long_quote_is_the_book_as_given():
    quotes = {q.outcome_id: q for q in quotes_from_bbo("slug1", BBO["marketData"])}
    long_q = quotes["slug1:LONG"]
    assert long_q.bid == Decimal("0.4500")
    assert long_q.ask == Decimal("0.4550")
    assert long_q.bid_size == Decimal("36")
    assert long_q.ask_size == Decimal("37")


def test_short_quote_inverts_and_swaps_both_price_and_size():
    """The SHORT side of a binary is 1 - the LONG side, with sides swapped.

    Buying SHORT at its ask means lifting the LONG bid, so:
        short.bid = 1 - long.ask = 1 - 0.4550 = 0.5450
        short.ask = 1 - long.bid = 1 - 0.4500 = 0.5500
    Sizes swap with the prices they belong to.

    Getting this backwards is silent - it yields plausible prices that
    invent edges - so the expectations here are hand-computed.
    """
    quotes = {q.outcome_id: q for q in quotes_from_bbo("slug1", BBO["marketData"])}
    short_q = quotes["slug1:SHORT"]
    assert short_q.bid == Decimal("0.5450")
    assert short_q.ask == Decimal("0.5500")
    assert short_q.bid_size == Decimal("37")
    assert short_q.ask_size == Decimal("36")


def test_long_and_short_asks_sum_to_more_than_one():
    """Sanity invariant: you can never buy both sides for under a dollar on
    one venue. If this fails, the inversion is wrong."""
    quotes = quotes_from_bbo("slug1", BBO["marketData"])
    total = sum(q.ask for q in quotes)
    assert total > Decimal("1")


def test_missing_side_yields_no_quotes():
    assert quotes_from_bbo("slug1", {"bestBid": {"value": "0.45"}}) == []
    assert quotes_from_bbo("slug1", {}) == []


def test_crossed_book_is_dropped_rather_than_raising():
    """Quote.__post_init__ rejects ask < bid. A crossed feed must not kill
    the poll loop."""
    crossed = {"bestBid": {"value": "0.60"}, "bestAsk": {"value": "0.40"}}
    assert quotes_from_bbo("slug1", crossed) == []


@pytest.mark.asyncio
async def test_stream_quotes_polls_bbo_once_per_slug():
    """LONG and SHORT share one HTTP call - slugs are deduplicated."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert "gateway.polymarket.us" in str(request.url)
        return httpx.Response(200, json=BBO)

    client = _mock_client(handler)
    adapter = PolymarketUsAdapter(
        outcome_ids=["slug1:LONG", "slug1:SHORT"],
        http_client=client,
        poll_interval_s=0.01,
    )
    seen = []
    async for q in adapter.stream_quotes():
        seen.append(q)
        if len(seen) == 2:
            break
    assert len(calls) == 1
    assert {q.outcome_id for q in seen} == {"slug1:LONG", "slug1:SHORT"}
    await adapter.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_http_error_yields_no_quote_and_does_not_raise():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = _mock_client(handler)
    adapter = PolymarketUsAdapter(
        outcome_ids=["slug1:LONG"], http_client=client, poll_interval_s=0.01
    )
    got = []

    async def drain():
        async for q in adapter.stream_quotes():
            got.append(q)

    task = asyncio.create_task(drain())
    await asyncio.sleep(0.05)
    task.cancel()
    assert got == []
    await adapter.close()
    await client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/adapters/test_polymarket_us.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'arbys.adapters.polymarket_us'`

- [ ] **Step 3: Write the adapter**

Create `arbys/adapters/polymarket_us.py`:

```python
"""Polymarket US market-data adapter.

Polymarket US is a separate CFTC-regulated exchange from Polymarket
international, with its own order book. Shares are not fungible between them
and prices diverge, so this is a distinct venue, not a different endpoint for
the same one.

Uses the public gateway (``gateway.polymarket.us``), which needs no API key,
no KYC and no wallet. The authenticated WebSocket at ``api.polymarket.us``
requires Ed25519 credentials and identity verification; it is deliberately not
used here. When it is added, follow the ``_kalshi_factory`` pattern in
``backend/state.py``.

A Polymarket US market is a single binary contract with a long and a short
side - structurally like a Kalshi market rather than like Polymarket
international's two-token pair. So ``outcome_id`` follows the Kalshi
convention: ``{market_slug}:LONG`` / ``{market_slug}:SHORT``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ..shared.types import Outcome, Quote, Side
from .base import MarketDataAdapter

GATEWAY_BASE = "https://gateway.polymarket.us"

LONG = "LONG"
SHORT = "SHORT"

log = logging.getLogger(__name__)


def split_outcome_id(outcome_id: str) -> tuple[str, str]:
    """``"slug:LONG"`` -> ``("slug", "LONG")``.

    Slugs contain hyphens but never colons, so rsplit is unambiguous.
    """
    slug, _, side = outcome_id.rpartition(":")
    return slug, side.upper()


def _price(node: Any) -> Decimal | None:
    """Pull a Decimal out of a ``{"value": "0.4550", "currency": "USD"}`` node."""
    if not isinstance(node, dict):
        return None
    try:
        return Decimal(str(node["value"]))
    except (KeyError, TypeError, InvalidOperation):
        return None


def _size(value: Any) -> Decimal:
    """Resting depth at top of book. Unknown or malformed reads as 0."""
    try:
        return Decimal(str(value))
    except (TypeError, InvalidOperation):
        return Decimal("0")


def quotes_from_bbo(slug: str, market_data: dict[str, Any]) -> list[Quote]:
    """Derive both sides' top-of-book from one ``/bbo`` payload.

    The long side is the book as reported. The short side of a binary is its
    complement: buying SHORT at its ask means lifting the LONG bid, so prices
    invert *and* the sizes swap along with them::

        short.bid  = 1 - long.ask      short.bid_size = long.ask_size
        short.ask  = 1 - long.bid      short.ask_size = long.bid_size

    Returns an empty list rather than raising when the book is one-sided,
    malformed, or crossed - a bad tick must not kill the poll loop.
    """
    bid = _price(market_data.get("bestBid"))
    ask = _price(market_data.get("bestAsk"))
    if bid is None or ask is None:
        return []

    bid_size = _size(market_data.get("bidDepth"))
    ask_size = _size(market_data.get("askDepth"))
    one = Decimal("1")
    try:
        return [
            Quote(
                outcome_id=f"{slug}:{LONG}",
                bid=bid,
                ask=ask,
                bid_size=bid_size,
                ask_size=ask_size,
            ),
            Quote(
                outcome_id=f"{slug}:{SHORT}",
                bid=one - ask,
                ask=one - bid,
                bid_size=ask_size,
                ask_size=bid_size,
            ),
        ]
    except ValueError:
        # Quote.__post_init__ rejects out-of-range or crossed books.
        log.debug("dropping malformed bbo for %s: bid=%s ask=%s", slug, bid, ask)
        return []


class PolymarketUsAdapter(MarketDataAdapter):
    venue_id = "polymarket_us"

    def __init__(
        self,
        *,
        poll_interval_s: float = 5.0,
        outcome_ids: list[str] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._poll_interval_s = poll_interval_s
        self._outcome_ids = outcome_ids or []
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=10.0, base_url=GATEWAY_BASE
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    def _slugs(self) -> list[str]:
        """Distinct market slugs behind the subscribed outcome ids.

        LONG and SHORT share one market and therefore one HTTP call. Sorted
        so the poll order is deterministic.
        """
        return sorted({split_outcome_id(oid)[0] for oid in self._outcome_ids})

    async def list_markets(self, *, league: str = "mlb") -> list[Outcome]:
        resp = await self._http.get(
            f"{GATEWAY_BASE}/v2/leagues/{league}/events", params={"limit": 200}
        )
        resp.raise_for_status()
        outcomes: list[Outcome] = []
        for event in resp.json().get("events") or []:
            for market in event.get("markets") or []:
                slug = market.get("slug")
                if not slug:
                    continue
                title = market.get("question") or slug
                for side_label, side_enum in ((LONG, Side.YES), (SHORT, Side.NO)):
                    outcomes.append(
                        Outcome(
                            id=f"{slug}:{side_label}",
                            venue_id=self.venue_id,
                            market_id=slug,
                            label=f"{title} ({side_label})",
                            side=side_enum,
                        )
                    )
        return outcomes

    async def _fetch_quotes(self, slug: str) -> list[Quote]:
        try:
            resp = await self._http.get(f"{GATEWAY_BASE}/v1/markets/{slug}/bbo")
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError):
            return []
        market_data = body.get("marketData")
        if not isinstance(market_data, dict):
            return []
        return quotes_from_bbo(slug, market_data)

    async def stream_quotes(self) -> AsyncIterator[Quote]:
        slugs = self._slugs()
        if not slugs:
            return
        while True:
            for slug in slugs:
                for quote in await self._fetch_quotes(slug):
                    yield quote
            await asyncio.sleep(self._poll_interval_s)
```

- [ ] **Step 4: Run the adapter tests**

Run: `venv\Scripts\python.exe -m pytest tests/adapters/test_polymarket_us.py -q`
Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add arbys/adapters/polymarket_us.py tests/adapters/test_polymarket_us.py
git commit -m "feat(adapters): add the Polymarket US market-data adapter

Polls the public gateway's /bbo endpoint - no API key, no KYC. Each
market yields two quotes: {slug}:LONG as reported, and {slug}:SHORT as
its complement with prices inverted and sizes swapped.

That inversion is the only real arithmetic here and a side error is
silent, producing plausible prices that invent edges, so it is tested
against hand-computed values plus a both-asks-exceed-one invariant."
```

---

### Task 3: Polymarket US discovery

One module replaces three. `/v2/leagues/{slug}/events` returns every market type for a league in a single call, so team sports, totals and tennis differ only in which leagues they request and which `sportsMarketType` values they keep.

**Files:**
- Create: `arbys/discovery/polymarket_us.py`
- Test: `tests/discovery/test_polymarket_us.py`

**Interfaces:**
- Consumes: `VenueGame`, `_parse_utc` from `arbys/discovery/kalshi_sports.py`; `OVER`, `UNDER` from `arbys/discovery/matcher.py`; `TeamResolver` from `arbys/discovery/teams.py`; `Player`, `parse_vs_title` from `arbys/discovery/players.py`
- Produces:
  - `fetch_polymarket_us_games(*, resolver: TeamResolver, sport: str, http_client=None) -> list[VenueGame]`
  - `fetch_polymarket_us_totals(*, resolver: TeamResolver, sport: str, http_client=None) -> list[VenueGame]`
  - `fetch_polymarket_us_tennis(*, http_client=None) -> list[VenueGame]`

  All three return `VenueGame(venue_id="polymarket_us", ...)` with `outcome_ids` values in `{slug}:LONG` / `{slug}:SHORT` form. Consumed by Task 5's `service.py`.

Real payload facts, captured 2026-08-11:
- Moneyline sides carry `team: {"name": "Cleveland Guardians", "abbreviation": "cle"}`; the `long` side is the first team in the slug.
- Totals sides carry `team: null` and `description` of `"Over"` / `"Under"`; `line` is a JSON float (`21.5`).
- Tennis `teams[].name` is the player's full name (`"Naomi Osaka"`).
- `startTime` is a UTC instant (`"2026-08-11T22:40:00Z"`).

- [ ] **Step 1: Write the failing test**

Create `tests/discovery/test_polymarket_us.py`:

```python
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from arbys.discovery.polymarket_us import (
    fetch_polymarket_us_games,
    fetch_polymarket_us_tennis,
    fetch_polymarket_us_totals,
)
from arbys.discovery.teams import MLB_RESOLVER, NFL_RESOLVER

MLB_EVENTS = {
    "events": [
        {
            "slug": "mlb-cle-det-2026-08-11",
            "title": "Cleveland Guardians vs. Detroit Tigers",
            "startTime": "2026-08-11T22:40:00Z",
            "teams": [
                {"name": "Cleveland Guardians", "abbreviation": "cle"},
                {"name": "Detroit Tigers", "abbreviation": "det"},
            ],
            "markets": [
                {
                    "slug": "aec-mlb-cle-det-2026-08-11",
                    "sportsMarketType": "baseball_team_full_game_winner",
                    "marketSides": [
                        {"long": True, "team": {"name": "Cleveland Guardians"}},
                        {"long": False, "team": {"name": "Detroit Tigers"}},
                    ],
                },
                {
                    "slug": "asc-mlb-cle-det-2026-08-11-pos-2pt5",
                    "sportsMarketType": "baseball_team_full_game_spread",
                    "line": 2.5,
                    "marketSides": [
                        {"long": True, "team": {"name": "Cleveland Guardians"}},
                        {"long": False, "team": {"name": "Detroit Tigers"}},
                    ],
                },
            ],
        }
    ]
}

NFL_TOTALS = {
    "events": [
        {
            "slug": "nfl-gb-pit-2026-08-13",
            "title": "Green Bay Packers vs. Pittsburgh Steelers",
            "startTime": "2026-08-13T23:00:00Z",
            "teams": [
                {"name": "Green Bay Packers", "abbreviation": "gb"},
                {"name": "Pittsburgh Steelers", "abbreviation": "pit"},
            ],
            "markets": [
                {
                    "slug": "tsc-nfl-gb-pit-2026-08-13-total-21pt5",
                    "sportsMarketType": "football_team_full_game_total",
                    "line": 21.5,
                    "marketSides": [
                        {"long": True, "team": None, "description": "Over"},
                        {"long": False, "team": None, "description": "Under"},
                    ],
                }
            ],
        }
    ]
}

TENNIS_EVENTS = {
    "events": [
        {
            "slug": "wta-naoosa-eleryb-2026-08-11",
            "title": "Naomi Osaka vs. Elena Rybakina",
            "startTime": "2026-08-11T14:00:00Z",
            "teams": [
                {"name": "Naomi Osaka", "abbreviation": "naoosa"},
                {"name": "Elena Rybakina", "abbreviation": "eleryb"},
            ],
            "markets": [
                {
                    "slug": "aec-wta-naoosa-eleryb-2026-08-11",
                    "sportsMarketType": "tennis_match_winner",
                    "marketSides": [
                        {"long": True, "team": {"name": "Naomi Osaka"}},
                        {"long": False, "team": {"name": "Elena Rybakina"}},
                    ],
                }
            ],
        }
    ]
}


def _client(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "gateway.polymarket.us" in str(request.url)
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


@pytest.mark.asyncio
async def test_moneyline_maps_teams_to_long_and_short_outcome_ids():
    client = _client(MLB_EVENTS)
    games = await fetch_polymarket_us_games(
        resolver=MLB_RESOLVER, sport="mlb", http_client=client
    )
    assert len(games) == 1
    game = games[0]
    assert game.venue_id == "polymarket_us"
    assert game.market_type == "moneyline"
    assert game.outcome_ids == {
        "CLE": "aec-mlb-cle-det-2026-08-11:LONG",
        "DET": "aec-mlb-cle-det-2026-08-11:SHORT",
    }
    assert game.start_time == datetime(2026, 8, 11, 22, 40, tzinfo=UTC)
    await client.aclose()


@pytest.mark.asyncio
async def test_spread_markets_are_skipped_in_phase_1():
    """Spreads are Phase 2. The payload contains one; it must not appear."""
    client = _client(MLB_EVENTS)
    games = await fetch_polymarket_us_games(
        resolver=MLB_RESOLVER, sport="mlb", http_client=client
    )
    assert all(g.market_type == "moneyline" for g in games)
    await client.aclose()


@pytest.mark.asyncio
async def test_totals_key_by_over_under_and_carry_a_decimal_line():
    client = _client(NFL_TOTALS)
    games = await fetch_polymarket_us_totals(
        resolver=NFL_RESOLVER, sport="nfl", http_client=client
    )
    assert len(games) == 1
    game = games[0]
    assert game.market_type == "total"
    assert game.line == Decimal("21.5")
    assert isinstance(game.line, Decimal)
    assert game.outcome_ids == {
        "OVER": "tsc-nfl-gb-pit-2026-08-13-total-21pt5:LONG",
        "UNDER": "tsc-nfl-gb-pit-2026-08-13-total-21pt5:SHORT",
    }
    await client.aclose()


@pytest.mark.asyncio
async def test_tennis_resolves_players_from_structured_names():
    client = _client(TENNIS_EVENTS)
    matches = await fetch_polymarket_us_tennis(http_client=client)
    assert len(matches) == 1
    match = matches[0]
    assert set(match.outcome_ids) == {"OSAKA", "RYBAKINA"}
    assert match.outcome_ids["OSAKA"].endswith(":LONG")
    await client.aclose()


@pytest.mark.asyncio
async def test_unknown_team_is_dropped_not_raised():
    payload = {
        "events": [
            {
                "slug": "mlb-xxx-yyy-2026-08-11",
                "startTime": "2026-08-11T22:40:00Z",
                "teams": [{"name": "Springfield Isotopes"}, {"name": "Shelbyville"}],
                "markets": [
                    {
                        "slug": "aec-mlb-xxx-yyy-2026-08-11",
                        "sportsMarketType": "baseball_team_full_game_winner",
                        "marketSides": [
                            {"long": True, "team": {"name": "Springfield Isotopes"}},
                            {"long": False, "team": {"name": "Shelbyville"}},
                        ],
                    }
                ],
            }
        ]
    }
    client = _client(payload)
    games = await fetch_polymarket_us_games(
        resolver=MLB_RESOLVER, sport="mlb", http_client=client
    )
    assert games == []
    await client.aclose()


@pytest.mark.asyncio
async def test_event_without_start_time_is_dropped():
    payload = {
        "events": [
            {
                "slug": "mlb-cle-det-2026-08-11",
                "teams": [
                    {"name": "Cleveland Guardians"},
                    {"name": "Detroit Tigers"},
                ],
                "markets": [
                    {
                        "slug": "aec-mlb-cle-det-2026-08-11",
                        "sportsMarketType": "baseball_team_full_game_winner",
                        "marketSides": [
                            {"long": True, "team": {"name": "Cleveland Guardians"}},
                            {"long": False, "team": {"name": "Detroit Tigers"}},
                        ],
                    }
                ],
            }
        ]
    }
    client = _client(payload)
    games = await fetch_polymarket_us_games(
        resolver=MLB_RESOLVER, sport="mlb", http_client=client
    )
    assert games == []
    await client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/discovery/test_polymarket_us.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'arbys.discovery.polymarket_us'`

- [ ] **Step 3: Write the discovery module**

Create `arbys/discovery/polymarket_us.py`:

```python
"""Polymarket US discovery.

``GET /v2/leagues/{slug}/events`` returns every market type for a league in
one call, so team sports, totals and tennis all come from the same request and
differ only in which ``sportsMarketType`` values they keep.

Two traps that the international integration had do not exist here:

* **No 100-row cap.** International's flat ``/markets`` capped at 100 rows
  ordered by 24h volume, where league games never outranked politics. This
  endpoint is league-scoped, so there is nothing to outrank.
* **No question parsing.** International only identified teams in prose.
  Polymarket US returns ``teams[].name`` ("Arizona Diamondbacks") structured,
  which the existing resolvers accept directly.

``startTime`` is a clean UTC instant, so no date heuristics are needed on this
side at all - the matcher's start-time comparison works directly.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from .kalshi_sports import VenueGame, _parse_utc
from .matcher import OVER, UNDER
from .players import Player, _last_name_code
from .teams import TeamResolver

GATEWAY_BASE = "https://gateway.polymarket.us"

log = logging.getLogger(__name__)

# Our sport label -> Polymarket US league slug.
LEAGUE_SLUGS = {
    "mlb": "mlb",
    "nfl": "nfl",
    "nba": "nba",
}

TENNIS_LEAGUES = ("atp", "wta")

# NBA is unverified: /v2/leagues/nba/events returned zero events on
# 2026-08-11 (offseason), the same condition that leaves KXNBAGAME unverified
# on the Kalshi side. The type string below was confirmed against WNBA.
MONEYLINE_TYPES = frozenset(
    {
        "baseball_team_full_game_winner",
        "football_team_full_game_winner",
        "basketball_team_full_game_winner",
    }
)
TENNIS_WINNER_TYPES = frozenset({"tennis_match_winner"})

# NFL only in Phase 1. Polymarket US also carries MLB and NBA totals; those
# are held back so the venue port has exactly one behavioural variable.
TOTAL_TYPES = frozenset({"football_team_full_game_total"})


async def _fetch_events(
    league: str, http_client: httpx.AsyncClient | None, limit: int
) -> list[dict[str, Any]]:
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.get(
            f"{GATEWAY_BASE}/v2/leagues/{league}/events", params={"limit": limit}
        )
        resp.raise_for_status()
        events = resp.json().get("events") or []
        return [e for e in events if isinstance(e, dict)]
    finally:
        if owns_client:
            await client.aclose()


def _sides(market: dict[str, Any]) -> tuple[dict, dict] | None:
    """The (long, short) pair, or None when the market is not binary."""
    sides = market.get("marketSides") or []
    longs = [s for s in sides if isinstance(s, dict) and s.get("long")]
    shorts = [s for s in sides if isinstance(s, dict) and not s.get("long")]
    if len(longs) != 1 or len(shorts) != 1:
        return None
    return longs[0], shorts[0]


def _line(market: dict[str, Any]) -> Decimal | None:
    """The strike, as Decimal. The API sends a JSON float (21.5)."""
    raw = market.get("line")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError):
        return None


def _team_name(side: dict[str, Any]) -> str | None:
    team = side.get("team")
    if not isinstance(team, dict):
        return None
    name = team.get("name")
    return name if isinstance(name, str) else None


async def fetch_polymarket_us_games(
    *,
    resolver: TeamResolver,
    sport: str,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 200,
) -> list[VenueGame]:
    """Moneyline games for a team sport."""
    league = LEAGUE_SLUGS.get(sport, sport)
    events = await _fetch_events(league, http_client, limit)

    games: list[VenueGame] = []
    for event in events:
        start_time = _parse_utc(event.get("startTime"))
        if start_time is None:
            continue
        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            if market.get("sportsMarketType") not in MONEYLINE_TYPES:
                continue
            pair = _sides(market)
            slug = market.get("slug")
            if pair is None or not slug:
                continue
            long_side, short_side = pair

            long_name, short_name = _team_name(long_side), _team_name(short_side)
            if long_name is None or short_name is None:
                continue
            team_long = resolver.by_polymarket_name(long_name)
            team_short = resolver.by_polymarket_name(short_name)
            if team_long is None or team_short is None:
                continue

            games.append(
                VenueGame(
                    sport=sport,
                    venue_id="polymarket_us",
                    game_date=start_time.date(),
                    teams=(team_long, team_short),
                    outcome_ids={
                        team_long.code: f"{slug}:LONG",
                        team_short.code: f"{slug}:SHORT",
                    },
                    ref=str(slug),
                    market_type="moneyline",
                    start_time=start_time,
                )
            )
    return games


async def fetch_polymarket_us_totals(
    *,
    resolver: TeamResolver,
    sport: str,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 200,
) -> list[VenueGame]:
    """Over/under games, one VenueGame per (game, line).

    Totals sides carry ``team: null`` and are labelled Over / Under, so the
    participants come from the event's ``teams`` array rather than the market.
    The canonical TRUE side is OVER, which is the long side.
    """
    league = LEAGUE_SLUGS.get(sport, sport)
    events = await _fetch_events(league, http_client, limit)

    games: list[VenueGame] = []
    for event in events:
        start_time = _parse_utc(event.get("startTime"))
        if start_time is None:
            continue
        event_teams = event.get("teams") or []
        if len(event_teams) != 2:
            continue
        resolved = [
            resolver.by_polymarket_name(t.get("name") or "")
            for t in event_teams
            if isinstance(t, dict)
        ]
        if len(resolved) != 2 or any(t is None for t in resolved):
            continue

        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            if market.get("sportsMarketType") not in TOTAL_TYPES:
                continue
            slug = market.get("slug")
            line = _line(market)
            if not slug or line is None or _sides(market) is None:
                continue
            games.append(
                VenueGame(
                    sport=sport,
                    venue_id="polymarket_us",
                    game_date=start_time.date(),
                    teams=(resolved[0], resolved[1]),
                    outcome_ids={
                        OVER: f"{slug}:LONG",
                        UNDER: f"{slug}:SHORT",
                    },
                    ref=str(slug),
                    market_type="total",
                    line=line,
                    start_time=start_time,
                )
            )
    return games


async def fetch_polymarket_us_tennis(
    *,
    http_client: httpx.AsyncClient | None = None,
    limit: int = 200,
) -> list[VenueGame]:
    """ATP + WTA match winners.

    Players come from the market's own sides, which carry full names, so
    there is no title to split.
    """
    matches: list[VenueGame] = []
    for league in TENNIS_LEAGUES:
        events = await _fetch_events(league, http_client, limit)
        for event in events:
            start_time = _parse_utc(event.get("startTime"))
            if start_time is None:
                continue
            for market in event.get("markets") or []:
                if not isinstance(market, dict):
                    continue
                if market.get("sportsMarketType") not in TENNIS_WINNER_TYPES:
                    continue
                pair = _sides(market)
                slug = market.get("slug")
                if pair is None or not slug:
                    continue
                long_side, short_side = pair
                long_name, short_name = _team_name(long_side), _team_name(short_side)
                if long_name is None or short_name is None:
                    continue
                p_long = Player(code=_last_name_code(long_name), full_name=long_name)
                p_short = Player(code=_last_name_code(short_name), full_name=short_name)
                if not p_long.code or not p_short.code or p_long.code == p_short.code:
                    continue

                matches.append(
                    VenueGame(
                        sport="tennis",
                        venue_id="polymarket_us",
                        game_date=start_time.date(),
                        teams=(p_long, p_short),
                        outcome_ids={
                            p_long.code: f"{slug}:LONG",
                            p_short.code: f"{slug}:SHORT",
                        },
                        ref=str(slug),
                        market_type="moneyline",
                        start_time=start_time,
                    )
                )
    return matches
```

- [ ] **Step 4: Run the discovery tests**

Run: `venv\Scripts\python.exe -m pytest tests/discovery/test_polymarket_us.py -q`
Expected: PASS, 6 tests.

If `_last_name_code` turns out to be private-by-convention and ruff objects to the import, add a public alias in `players.py`:

```python
def last_name_code(full_name: str) -> str:
    """Public alias — canonical player code from a full name."""
    return _last_name_code(full_name)
```

and import that instead.

- [ ] **Step 5: Commit**

```bash
git add arbys/discovery/polymarket_us.py tests/discovery/test_polymarket_us.py
git commit -m "feat(discovery): add Polymarket US discovery

One module replaces three: /v2/leagues/{slug}/events returns every
market type for a league in a single call, so moneyline, totals and
tennis differ only in which sportsMarketType values they keep.

Both international traps are gone. The endpoint is league-scoped so
there is no 100-row volume cap to outrank, and teams arrive structured
as teams[].name so parse_vs_question is not needed."
```

---

### Task 4: Matcher taxonomy groundwork

`_pair_key` buckets on `(sport, market_type, line, team-pair)`. That is correct for moneyline and totals and **wrong for spreads**: a spread's line is meaningless without the team it is stated for, and the two venues anchor it differently (Kalshi names the team in the ticker, Polymarket anchors to slug position). Bucketing without the anchor pairs `CLE -2.5` against `DET -2.5` and invents an arb — the same failure mode the totals line already guards against.

Phase 1 adds the field and threads it through. It is `None` for every market type shipping in Phase 1, so behaviour must be **bit-identical** to today.

**Files:**
- Modify: `arbys/discovery/kalshi_sports.py:30-59` (add `anchor` to `VenueGame`)
- Modify: `arbys/discovery/matcher.py:16-53` (add `anchor` to `CrossVenueMatch`, dispatch `yes_key`), `:91-99` (`_pair_key`), `:161-172` (construction)
- Test: `tests/discovery/test_matcher.py`

**Interfaces:**
- Consumes: `VenueGame` from Task 3's producers
- Produces: `VenueGame.anchor: str | None = None`; `CrossVenueMatch.anchor: str | None = None`; `_pair_key` returning a 5-tuple `(sport, market_type, line, anchor, frozenset[codes])`

- [ ] **Step 1: Write the failing test**

Append to `tests/discovery/test_matcher.py`:

```python
def test_anchor_defaults_to_none_and_preserves_today_behaviour():
    """Phase 1 ships no market type that sets an anchor. Two games that
    matched before must still match."""
    from datetime import UTC, datetime

    from arbys.discovery.kalshi_sports import VenueGame
    from arbys.discovery.matcher import match_games
    from arbys.discovery.teams import MLB_RESOLVER

    cle = MLB_RESOLVER.by_code("CLE")
    det = MLB_RESOLVER.by_code("DET")
    start = datetime(2026, 8, 11, 22, 40, tzinfo=UTC)

    kalshi = VenueGame(
        sport="mlb",
        venue_id="kalshi",
        game_date=start.date(),
        teams=(cle, det),
        outcome_ids={"CLE": "K1:YES", "DET": "K1:NO"},
        ref="K1",
        start_time=start,
    )
    poly = VenueGame(
        sport="mlb",
        venue_id="polymarket_us",
        game_date=start.date(),
        teams=(cle, det),
        outcome_ids={"CLE": "p1:LONG", "DET": "p1:SHORT"},
        ref="p1",
        start_time=start,
    )
    matches = match_games([kalshi], [poly])
    assert len(matches) == 1
    assert matches[0].anchor is None


def test_different_anchors_do_not_match():
    """The guard Phase 2 depends on: the same line stated for opposite teams
    is not the same bet. Without this, CLE -2.5 pairs with DET -2.5 and
    invents an arb."""
    from datetime import UTC, datetime
    from decimal import Decimal

    from arbys.discovery.kalshi_sports import VenueGame
    from arbys.discovery.matcher import match_games
    from arbys.discovery.teams import MLB_RESOLVER

    cle = MLB_RESOLVER.by_code("CLE")
    det = MLB_RESOLVER.by_code("DET")
    start = datetime(2026, 8, 11, 22, 40, tzinfo=UTC)

    def game(venue: str, anchor: str) -> VenueGame:
        return VenueGame(
            sport="mlb",
            venue_id=venue,
            game_date=start.date(),
            teams=(cle, det),
            outcome_ids={"CLE": f"{venue}:YES", "DET": f"{venue}:NO"},
            ref=venue,
            market_type="spread",
            line=Decimal("-2.5"),
            anchor="CLE",
            start_time=start,
        )

    same = match_games([game("kalshi", "CLE")], [game("polymarket_us", "CLE")])
    assert len(same) == 1
    assert same[0].anchor == "CLE"

    kalshi_cle = game("kalshi", "CLE")
    poly_det = VenueGame(
        sport="mlb",
        venue_id="polymarket_us",
        game_date=start.date(),
        teams=(cle, det),
        outcome_ids={"CLE": "p:YES", "DET": "p:NO"},
        ref="p",
        market_type="spread",
        line=Decimal("-2.5"),
        anchor="DET",
        start_time=start,
    )
    assert match_games([kalshi_cle], [poly_det]) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/discovery/test_matcher.py -q -k anchor`
Expected: FAIL — `TypeError: VenueGame.__init__() got an unexpected keyword argument 'anchor'`

- [ ] **Step 3: Add the anchor field to `VenueGame`**

In `arbys/discovery/kalshi_sports.py`, add after the `line` field (currently line 54):

```python
    # Which participant a signed line is stated for. None for market types
    # whose line needs no anchor (moneyline has no line; a total's Over/Under
    # is symmetric). Set for spreads, where "CLE -2.5" and "DET -2.5" are
    # different bets that would otherwise share a bucket and invent an arb.
    # Unused in Phase 1 - every market type currently wired leaves it None.
    anchor: str | None = None
```

- [ ] **Step 4: Thread it through the matcher**

In `arbys/discovery/matcher.py`:

Add to `CrossVenueMatch` after `line` (currently line 24):

```python
    anchor: str | None = None
```

Replace `yes_key` (lines 26-32) with a dispatch:

```python
    def yes_key(self) -> str:
        """Which ``outcome_ids`` key represents the group's TRUE proposition.

        Dispatch rather than a conditional so Phase 2 registers a new market
        type without reopening this method.
        """
        if self.market_type == "total":
            return OVER
        if self.market_type == "spread":
            # The canonical TRUE side is "the anchored participant covers".
            return self.anchor or self.team_a.code
        return self.team_a.code
```

Replace `_pair_key` (lines 91-99):

```python
def _pair_key(game: VenueGame) -> tuple[str, str, str, str, frozenset[str]]:
    """Bucket key. Market type, line and anchor are all part of identity.

    An Over 44.5 and an Over 47.5 on the same game are different bets. So are
    ``CLE -2.5`` and ``DET -2.5`` — same line, opposite anchor — which is why
    the anchor cannot be left out even though nothing sets it yet.
    """
    return (
        game.sport,
        game.market_type,
        _fmt_line(game.line),
        game.anchor or "",
        frozenset(t.code for t in game.teams),
    )
```

In the `matches.append(...)` block (currently lines 162-172), add after `line=anchor.line,`:

```python
                    anchor=anchor.anchor,
```

> Note: the loop variable is already called `anchor` (the anchor *game* of a
> date cluster). `anchor.anchor` reads badly but is correct. Do not rename the
> loop variable in this task — it is used throughout `match_games` and
> renaming it enlarges the diff for no benefit.

Add `anchor` to the `matches.sort` key (currently lines 173-181), after `m.market_type`:

```python
            m.anchor or "",
```

- [ ] **Step 5: Run the matcher tests**

Run: `venv\Scripts\python.exe -m pytest tests/discovery/ -q`
Expected: PASS, including every pre-existing matcher test unchanged. If any pre-existing test fails, the `anchor=None` default is not being honoured somewhere — fix that rather than editing the old test.

- [ ] **Step 6: Commit**

```bash
git add arbys/discovery/kalshi_sports.py arbys/discovery/matcher.py tests/discovery/test_matcher.py
git commit -m "refactor(discovery): carry a line anchor through the matcher

A spread's line is meaningless without the participant it is stated for,
and the venues anchor differently: Kalshi names the team in the ticker,
Polymarket anchors to slug position. Bucketing on (sport, type, line)
alone pairs CLE -2.5 with DET -2.5 and invents an arb, the same failure
mode the totals line already guards against.

Nothing sets an anchor yet - every Phase 1 market type leaves it None,
so behaviour is unchanged. This lands now so Phase 2 registers a market
type instead of reopening the matcher while also debugging sign
conventions."
```

---

### Task 5: Wire the venue in and delete the international integration

**Files:**
- Modify: `arbys/discovery/service.py:12-35`, `:49-52`, `:70-73`, `:93-97`
- Modify: `arbys/backend/state.py:31-36`, `:138-144`, `:150-154`
- Modify: `tests/test_ingest_wiring.py`, `tests/test_backend_e2e.py`, `tests/shared/test_quotebook_staleness.py`, `tests/shared/test_arb_engine.py`, `tests/discovery/test_service.py`, `tests/discovery/test_matcher.py`, `tests/discovery/test_totals.py`, `tests/discovery/test_tennis_matching.py`
- Delete: `arbys/adapters/polymarket.py`, `arbys/discovery/polymarket_sports.py`, `arbys/discovery/polymarket_tennis.py`, `arbys/discovery/polymarket_totals.py`, `tests/adapters/test_polymarket.py`, `tests/discovery/test_polymarket_sports.py`, `scripts/smoke_polymarket_ws.py`

**Interfaces:**
- Consumes: `PolymarketUsFeeModel` (Task 1), `PolymarketUsAdapter` (Task 2), `fetch_polymarket_us_games` / `_totals` / `_tennis` (Task 3)
- Produces: an `AppState` whose `fees`, `paper_brokers` and `adapter_factories` are all keyed `polymarket_us`

- [ ] **Step 1: Delete the international integration**

```bash
git rm arbys/adapters/polymarket.py \
       arbys/discovery/polymarket_sports.py \
       arbys/discovery/polymarket_tennis.py \
       arbys/discovery/polymarket_totals.py \
       tests/adapters/test_polymarket.py \
       tests/discovery/test_polymarket_sports.py \
       scripts/smoke_polymarket_ws.py
```

- [ ] **Step 2: Run the suite to enumerate every break**

Run: `venv\Scripts\python.exe -m pytest -q`
Expected: many collection errors and failures. **This is the task's worklist** — capture the output before proceeding.

- [ ] **Step 3: Update discovery service**

In `arbys/discovery/service.py`, replace the three Polymarket imports (lines 16-18) with:

```python
from .polymarket_us import (
    fetch_polymarket_us_games,
    fetch_polymarket_us_tennis,
    fetch_polymarket_us_totals,
)
```

Then swap each call site:

```python
# discover_team_sport_event_groups (line ~51)
        fetch_polymarket_us_games(resolver=resolver, sport=sport),

# discover_totals_event_groups (line ~72)
        fetch_polymarket_us_totals(resolver=resolver, sport=sport),

# discover_tennis_event_groups (line ~95)
        fetch_polymarket_us_tennis(),
```

Update the log lines in both team-sport and totals passes so the field name matches the venue:

```python
    log.info(
        "discovery[%s]: kalshi=%d polymarket_us=%d matched=%d",
        sport, len(kalshi_games), len(poly_games), len(matches),
    )
```

Update the `TOTALS_SPORTS` comment (lines 30-32), which is now wrong — Polymarket US *does* carry MLB totals:

```python
# Sports whose over/under markets both venues quote. MLB is absent by choice,
# not necessity: Polymarket US carries baseball_team_full_game_total and
# Kalshi lists KXMLBTOTAL, so adding ("mlb", MLB_RESOLVER) here should work.
# It is held back so the Polymarket US port has exactly one behavioural
# variable; wire it once the port is proven live.
TOTALS_SPORTS: tuple[tuple[str, TeamResolver], ...] = (
    ("nfl", NFL_RESOLVER),
)
```

- [ ] **Step 4: Update `AppState` wiring**

In `arbys/backend/state.py`:

Import (lines 22, 31-36):

```python
from ..adapters.polymarket_us import PolymarketUsAdapter
...
from ..shared.fees import (
    FeeModelRegistry,
    KalshiFeeModel,
    PolymarketUsFeeModel,
    SportsbookFeeModel,
)
```

Add the poll-interval reader next to the other env helpers (after `quote_max_age_s`, around line 97):

```python
DEFAULT_POLYMARKET_US_POLL_S = 5.0


def polymarket_us_poll_s() -> float:
    """Seconds between Polymarket US /bbo sweeps.

    Measured 2026-08-11: 53 concurrent /bbo calls returned in 1.46s with no
    rate limiting, so 5s is comfortable. Floor of 1s guards a typo from
    hammering the gateway.
    """
    raw = os.environ.get("ARBYS_POLYMARKET_US_POLL_S")
    if raw is None:
        return DEFAULT_POLYMARKET_US_POLL_S
    try:
        return max(1.0, float(raw))
    except ValueError:
        return DEFAULT_POLYMARKET_US_POLL_S
```

Factory (line 139) — no credential gating, the WS is not in Phase 1:

```python
        "polymarket_us": lambda oids: PolymarketUsAdapter(
            outcome_ids=oids, poll_interval_s=polymarket_us_poll_s()
        ),
```

Fee registry (lines 150-154):

```python
        self.fees: FeeModelRegistry = {
            "polymarket_us": PolymarketUsFeeModel(),
            "kalshi": KalshiFeeModel(),
            "draftkings": SportsbookFeeModel("draftkings"),
        }
```

Also update the docstring in `quote_max_age_s` (line 87) which references "a delisted Polymarket token" — that phrasing is about international token ids. Change to "a delisted Polymarket market".

- [ ] **Step 5: Update every remaining reference**

These files referenced `polymarket` before the deletions (enumerated 2026-08-11). Re-run the grep to confirm the current set, since Steps 3–4 will have changed some already:

```bash
grep -rn "polymarket" tests/ scripts/ arbys/ --include=*.py
```

| File | What to change |
| --- | --- |
| `tests/test_ingest_wiring.py` | adapter-factory venue key |
| `tests/test_backend_e2e.py` | venue string in position/balance fixtures |
| `tests/shared/test_arb_engine.py` | leg `venue_id` |
| `tests/shared/test_quotebook_staleness.py` | leg `venue_id` |
| `tests/shared/test_fees.py` | already done in Task 1 — verify no stragglers |
| `tests/discovery/test_service.py` | patched fetch-function names |
| `tests/discovery/test_matcher.py` | `VenueGame.venue_id` fixtures |
| `tests/discovery/test_totals.py` | `VenueGame.venue_id` + OVER/UNDER outcome ids |
| `tests/discovery/test_tennis_matching.py` | `VenueGame.venue_id` |
| `tests/discovery/test_ticker_start.py` | comment/docstring mentions only |
| `tests/discovery/test_teams.py`, `test_players.py` | `by_polymarket_name` calls — **method name is unchanged**, leave alone |
| `scripts/smoke_cross_venue_mlb.py`, `scripts/smoke_ingest_live.py`, `scripts/probe_tennis.py` | imports and venue strings |

Two rules for the mechanical pass:

- Replace the venue string `"polymarket"` with `"polymarket_us"`.
- Replace token-id-shaped `outcome_id` fixtures (`"tok_yes"`, long hex strings) with the slug form `"some-slug:LONG"` / `"some-slug:SHORT"`, so fixtures match what the adapter now emits.

Do **not** rename `TeamResolver.by_polymarket_name` — it resolves a team name that both books spell identically, and renaming it widens the diff for no gain.

`tests/test_backend_e2e.py::test_open_positions_hydrate_once_per_venue` is load-bearing per CLAUDE.md: it covers the restart-hydration path that once triple-counted positions. Keep `venue_id` in the position key; only the venue string changes.

- [ ] **Step 6: Verify the retirement guard still holds**

`discover_all_event_groups` returns `(groups, complete)`, and `complete=False` when any sub-pass raised. The caller skips retirement in that case, so a venue outage is not read as every game being delisted. This is load-bearing and easy to break while swapping the three fetch calls.

Run: `venv\Scripts\python.exe -m pytest tests/discovery/test_service.py -q`
Expected: PASS, including whichever test covers the incomplete-pass path.

If no such test exists, add one:

```python
@pytest.mark.asyncio
async def test_incomplete_pass_skips_retirement(monkeypatch):
    """A Polymarket US outage must not retire every group."""
    import arbys.discovery.service as svc

    async def boom(**kwargs):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(svc, "fetch_polymarket_us_games", boom)
    groups, complete = await svc.discover_all_event_groups()
    assert complete is False
```

- [ ] **Step 7: Run the full suite and the linter**

Run: `venv\Scripts\python.exe -m pytest -q`
Expected: PASS, all tests.

Run: `venv\Scripts\python.exe -m ruff check .`
Expected: clean. Unused imports left behind by the deletions are the likely finding.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat: switch the Polymarket venue to Polymarket US

Renames the venue to polymarket_us and deletes the international
adapter and its three discovery modules outright rather than leaving
them dormant.

venue_id is carried alongside outcome_id on every leg, position and
fill precisely because outcome ids are venue-native and not portable.
Leaving the string 'polymarket' pointing at a different exchange would
make that identifier lie, and it is what live execution will route on.

Also corrects the TOTALS_SPORTS comment: Polymarket US does carry MLB
totals, so MLB is held back by choice rather than ruled out."
```

---

### Task 6: Purge Polymarket international rows

Paper rows under `venue_id='polymarket'` are deleted, not remapped: their `outcome_id`s are international CLOB token ids that identify nothing on the US book, so remapping is not possible even in principle.

**Files:**
- Create: `arbys/db/migrations/versions/0005_polymarket_us_venue.py`
- Test: `tests/db/test_migrations_match_models.py` (existing replay covers it — verify, don't rewrite)

**Interfaces:**
- Consumes: schema as of `0004_event_group_source`
- Produces: revision `0005_polymarket_us_venue`, `down_revision = "0004_event_group_source"`

Tables carrying `venue_id`: `market`, `paper_balance`, `paper_order`, `paper_position`. `event_group_leg` has no `venue_id` — it reaches the venue through `outcome.market_id -> market.venue_id`. `paper_fill` reaches it through `paper_order`.

- [ ] **Step 1: Write the migration**

Create `arbys/db/migrations/versions/0005_polymarket_us_venue.py`:

```python
"""replace the polymarket venue with polymarket_us

Revision ID: 0005_polymarket_us_venue
Revises: 0004_event_group_source
Create Date: 2026-08-11

Polymarket's international book is not tradeable from the US. Polymarket US
is a separate CFTC-regulated exchange with its own order book; shares are not
fungible between the two.

Rows under venue_id='polymarket' are deleted rather than remapped. Their
outcome_id values are international CLOB token ids, which identify nothing on
the US book - there is no correct target to remap them to. This discards
simulated paper history, which is acceptable because it is a paper account.

Deletion order follows the foreign keys inward: fills before orders, legs and
quotes before outcomes, outcomes before markets, markets before the venue row.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_polymarket_us_venue"
down_revision: str | None = "0004_event_group_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "polymarket"
_NEW = "polymarket_us"


def upgrade() -> None:
    # --- paper trading state -------------------------------------------------
    op.execute(
        f"""
        DELETE FROM paper_fill
         WHERE order_id IN (
               SELECT id FROM paper_order WHERE venue_id = '{_OLD}'
         )
        """
    )
    op.execute(f"DELETE FROM paper_order WHERE venue_id = '{_OLD}'")
    op.execute(f"DELETE FROM paper_position WHERE venue_id = '{_OLD}'")
    op.execute(f"DELETE FROM paper_balance WHERE venue_id = '{_OLD}'")

    # --- market reference data ----------------------------------------------
    # event_group_leg and quote both hang off outcome, which hangs off market.
    op.execute(
        f"""
        DELETE FROM event_group_leg
         WHERE outcome_id IN (
               SELECT o.id FROM outcome o
                 JOIN market m ON m.id = o.market_id
                WHERE m.venue_id = '{_OLD}'
         )
        """
    )
    op.execute(
        f"""
        DELETE FROM quote
         WHERE outcome_id IN (
               SELECT o.id FROM outcome o
                 JOIN market m ON m.id = o.market_id
                WHERE m.venue_id = '{_OLD}'
         )
        """
    )
    op.execute(
        f"""
        DELETE FROM outcome
         WHERE market_id IN (SELECT id FROM market WHERE venue_id = '{_OLD}')
        """
    )
    op.execute(f"DELETE FROM market WHERE venue_id = '{_OLD}'")

    # Groups left with fewer than two venues can no longer produce a
    # cross-venue arb; drop them so discovery re-registers cleanly.
    op.execute(
        """
        DELETE FROM event_group
         WHERE id NOT IN (SELECT DISTINCT event_group_id FROM event_group_leg)
        """
    )
    op.execute("DELETE FROM arb_opportunity")

    # --- the venue row itself ------------------------------------------------
    op.execute(f"DELETE FROM venue WHERE id = '{_OLD}'")
    op.execute(
        f"""
        INSERT INTO venue (id, name, kind)
        SELECT '{_NEW}', 'Polymarket US', 'exchange'
         WHERE NOT EXISTS (SELECT 1 FROM venue WHERE id = '{_NEW}')
        """
    )


def downgrade() -> None:
    # The deleted rows are gone; this restores only the venue row so the
    # chain stays reversible in shape.
    op.execute(f"DELETE FROM venue WHERE id = '{_NEW}'")
    op.execute(
        f"""
        INSERT INTO venue (id, name, kind)
        SELECT '{_OLD}', 'Polymarket', 'exchange'
         WHERE NOT EXISTS (SELECT 1 FROM venue WHERE id = '{_OLD}')
        """
    )
```

- [ ] **Step 2: Verify the migration chain replays from empty**

Run: `venv\Scripts\python.exe -m pytest tests/db/test_migrations_match_models.py -q`
Expected: PASS. This replays every revision from an empty database and diffs the result against `create_all`. Per CLAUDE.md, a missing or wrong migration fails here rather than at the next deploy.

- [ ] **Step 3: Verify against the real local database**

```bash
venv\Scripts\python.exe -m alembic upgrade head
venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('arbys-local.db'); print('polymarket rows left:', [c.execute(f'SELECT COUNT(*) FROM {t} WHERE venue_id=\"polymarket\"').fetchone()[0] for t in ('market','paper_balance','paper_order','paper_position')]); print('venues:', c.execute('SELECT id FROM venue').fetchall())"
```

Expected: `polymarket rows left: [0, 0, 0, 0]` and a venue list containing `polymarket_us` but not `polymarket`.

> `arbys-local.db` is a ~177 MB gitignored artifact. Do not commit it. If the
> upgrade fails partway, delete the file and let `bootstrap()` rebuild it —
> nothing in it is precious.

- [ ] **Step 4: Commit**

```bash
git add arbys/db/migrations/versions/0005_polymarket_us_venue.py
git commit -m "feat(db): purge Polymarket international rows for the US venue

Deletes rather than remaps. The stored outcome_id values are
international CLOB token ids that identify nothing on the US book, so
there is no correct target to remap them to.

Written as explicit op.execute calls frozen at this revision, never
from Base.metadata - 0001_initial did that and broke every later
revision on a fresh database."
```

---

### Task 7: Frontend venue labels

**Files:**
- Modify: `frontend/src/pages/TerminalPage.tsx:19`, `:130`
- Modify: `frontend/src/pages/AdminPage.tsx:7`, `:20`, `:30`, `:201`
- Modify: `frontend/src/components/OpportunityCard.tsx:105`

**Interfaces:**
- Consumes: the backend's `venue_id` string `polymarket_us` (Task 5)
- Produces: no new exports

Label text only. Per CLAUDE.md, no new hex colors, radii, or type scales — the design system is a light-ground brief with no dark mode, and none of that changes here.

- [ ] **Step 1: Update `TerminalPage.tsx`**

Line 19:

```ts
const VENUES = ["Polymarket US", "Kalshi"];
```

Line 130:

```tsx
        <span className="tag tag-accent">Polymarket US · connected</span>
```

- [ ] **Step 2: Update `AdminPage.tsx`**

Line 7:

```ts
const VENUES = ["polymarket_us", "kalshi", "draftkings"] as const;
```

Lines 20, 30 and 201 each contain a default leg. In all three, change:

```ts
    { outcome_id: "", venue_id: "polymarket_us", is_yes_side: true },
```

- [ ] **Step 3: Update `OpportunityCard.tsx`**

Line 105:

```ts
  const polyLeg = group.legs.find((l) => l.venue_id === "polymarket_us" && l.is_yes_side);
```

- [ ] **Step 4: Confirm nothing else references the old string**

```bash
grep -rn "polymarket" frontend/src/
```

Expected: only `polymarket_us` / `Polymarket US` hits.

- [ ] **Step 5: Typecheck and lint**

```bash
cd frontend
npm run build
npm run lint
```

Expected: both clean. `npm run build` runs `tsc -b` and is the real typecheck for frontend code. Note the linter is **oxlint**, not eslint.

- [ ] **Step 6: Commit**

```bash
git add frontend/src
git commit -m "feat(ui): label the venue Polymarket US"
```

---

### Task 8: Docs, config, and a live smoke script

CLAUDE.md carries three statements this change falsifies. All were true when written and remain true of the *international* book, so they are corrected rather than deleted.

**Files:**
- Modify: `CLAUDE.md`, `docs/RUNBOOK.md`, `.env.example`
- Create: `scripts/smoke_polymarket_us.py`

**Interfaces:**
- Consumes: `fetch_polymarket_us_games` (Task 3), `PolymarketUsAdapter` (Task 2)
- Produces: nothing importable

- [ ] **Step 1: Add config to `.env.example`**

```
# Seconds between Polymarket US /bbo sweeps. Measured 2026-08-11: 53
# concurrent calls returned in 1.46s with no rate limiting.
ARBYS_POLYMARKET_US_POLL_S=5
```

- [ ] **Step 2: Correct the three stale CLAUDE.md claims**

1. Under the discovery section, the sentence *"Only NFL totals are wired: Kalshi lists `KXMLBTOTAL` but Polymarket carries no baseball totals (moneyline, NRFI and player props only)"* — replace with:

```markdown
  Only NFL totals are wired. That is now a choice rather than a limit:
  Polymarket US carries `baseball_team_full_game_total` and Kalshi lists
  `KXMLBTOTAL`, so MLB totals should match. They are held back so the
  Polymarket US port had exactly one behavioural variable. Wire them by
  adding `("mlb", MLB_RESOLVER)` to `TOTALS_SPORTS` once the port is proven
  live. (Polymarket *international* genuinely carried no baseball totals,
  which is why this used to read as impossible.)
```

2. The paragraph beginning *"**Neither venue publishes a live score or game clock** (checked 2026-08-08)"* — replace the opening with:

```markdown
  **Kalshi publishes no live score or game clock** (checked 2026-08-08), and
  neither did Polymarket international. **Polymarket US does**: its
  `/v2/leagues/{slug}/events` payload carries `score`, `period`, `live` and
  `ended`. Verified 2026-08-11 — `wtt-diychi-sreaku-2026-08-11` returned
  `score: "11-8, 11-6, 5-5"`, `period: "S3"`, `live: true`. The decision to
  rely on the countdown alone still stands, but no longer because the data is
  unavailable.
```

3. The "Two traps" paragraph — reframe as history:

```markdown
  Two traps, both of which silently returned zero groups rather than
  erroring. Kalshi sends a **bare city** (`"Atlanta"`) except where a city
  fields two teams (`"New York Y"`) — still live, still handled by
  `TeamResolver`. The second was Polymarket *international*'s flat
  `/markets` endpoint capping at 100 rows ordered by 24h volume, where
  league games never outranked politics; that one is **historical**, since
  Polymarket US's `/v2/leagues/{slug}/events` is league-scoped. Team-name
  parsing (`parse_vs_question`) is likewise gone — Polymarket US returns
  `teams[].name` structured.
```

Also update: the Architecture bullet describing `arbys/adapters/`, the `arbys/discovery/` bullet (which names the Polymarket tag-slug path), the **Gross vs net** section's fee discussion (Polymarket now has a real fee), and the venue list in the opening paragraph.

- [ ] **Step 3: Add the NBA caveat to CLAUDE.md**

Next to the existing `KXNBAGAME` note:

```markdown
  NBA is unverified on **both** venues: `KXNBAGAME` had no open events in the
  offseason, and `/v2/leagues/nba/events` returned zero on 2026-08-11 for the
  same reason. The Polymarket US type string
  `basketball_team_full_game_winner` was confirmed against **WNBA** instead.
  Recheck both when the season opens.
```

- [ ] **Step 4: Update `docs/RUNBOOK.md`**

Retarget the adding-a-venue and troubleshooting sections. Replace the Polymarket WebSocket reference (`wss://ws-subscriptions-clob.polymarket.com/ws/market`, around line 162) with a note that Phase 1 is REST-poll only against `gateway.polymarket.us`, and that the authenticated WS at `wss://api.polymarket.us/v1/ws/markets` needs KYC plus Ed25519 keys and is not wired.

- [ ] **Step 5: Write the smoke script**

Create `scripts/smoke_polymarket_us.py`:

```python
"""Live smoke test for the Polymarket US integration.

Hits the real gateway. Not part of the test suite - the suite never touches a
real venue. Run from the repo root:

    venv\\Scripts\\python.exe scripts/smoke_polymarket_us.py
"""

from __future__ import annotations

import asyncio

from arbys.adapters.polymarket_us import PolymarketUsAdapter
from arbys.discovery.polymarket_us import fetch_polymarket_us_games
from arbys.discovery.teams import MLB_RESOLVER


async def main() -> None:
    games = await fetch_polymarket_us_games(resolver=MLB_RESOLVER, sport="mlb")
    print(f"discovered {len(games)} MLB moneyline games")
    if not games:
        print("NO GAMES - check the league slug and sportsMarketType filters")
        return

    for game in games[:3]:
        print(f"  {game.ref}  start={game.start_time}  {game.outcome_ids}")

    outcome_ids = [oid for g in games[:3] for oid in g.outcome_ids.values()]
    adapter = PolymarketUsAdapter(outcome_ids=outcome_ids, poll_interval_s=1.0)
    print(f"\npolling {len(outcome_ids)} outcomes...")

    seen: dict[str, tuple[str, str]] = {}
    async for quote in adapter.stream_quotes():
        seen[quote.outcome_id] = (str(quote.bid), str(quote.ask))
        if len(seen) >= len(outcome_ids):
            break
    await adapter.close()

    for oid, (bid, ask) in sorted(seen.items()):
        print(f"  {oid:60} bid={bid} ask={ask}")

    # Both sides of one market must cost more than a dollar. If this trips,
    # the LONG/SHORT inversion is backwards.
    from decimal import Decimal

    by_slug: dict[str, list[Decimal]] = {}
    for oid, (_bid, ask) in seen.items():
        by_slug.setdefault(oid.rpartition(":")[0], []).append(Decimal(ask))
    for slug, asks in by_slug.items():
        if len(asks) == 2 and sum(asks) <= Decimal("1"):
            print(f"\nBAD: {slug} asks sum to {sum(asks)} <= 1 - inversion is wrong")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 6: Run the smoke script**

Run: `venv\Scripts\python.exe scripts/smoke_polymarket_us.py`
Expected: a non-zero game count, quotes on every subscribed outcome, and no `BAD:` line.

> If discovery returns zero games, check the league slug against
> `GET https://gateway.polymarket.us/v2/sports` before touching the parser —
> in the MLB offseason zero is the correct answer.

- [ ] **Step 7: Final full verification**

```bash
venv\Scripts\python.exe -m pytest -q
venv\Scripts\python.exe -m ruff check .
cd frontend && npm run build && npm run lint
```

Expected: all green. Do not claim completion without seeing this output.

- [ ] **Step 8: Commit**

```bash
git add CLAUDE.md docs/RUNBOOK.md .env.example scripts/smoke_polymarket_us.py
git commit -m "docs: correct three CLAUDE.md claims the US venue falsifies

All three were true when written and remain true of the international
book, so they are corrected rather than deleted:

- Polymarket US does carry MLB totals, so those are held back by choice
  rather than ruled out
- Polymarket US does publish live score, period and live/ended
- the 100-row /markets cap and question-parsing were international-only

Also records that NBA is now unverified on both venues, and that the
Polymarket US basketball type string was confirmed against WNBA."
```

---

## Post-Phase-1 expectation

Once Task 1 lands, published **net** edges will fall across the board — `PolymarketFeeModel` returned zero and the real rate is `0.06·p·(1-p)`, roughly 1.5¢/contract at a coin flip. The gross divergences measured on 2026-08-11 topped out at 2.75¢ across 34 MLB moneylines, so it is plausible that few or no net arbs survive on MLB moneyline once fees are correct.

That is the scanner telling the truth, not a regression, and not a reason to revert Task 1. The **gross** signal (green card outline, the nav's "N arbs" count) is unaffected and remains the divergence indicator — see the "Gross vs net is deliberate in the UI" section of CLAUDE.md.

If net arbs vanish entirely, the productive next move is Phase 2 (spreads), where the books are deeper and the venues disagree more, rather than loosening the fee model.
