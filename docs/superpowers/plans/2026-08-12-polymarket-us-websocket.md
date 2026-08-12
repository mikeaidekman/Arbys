# Polymarket US WebSocket Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace polling with a pushed market feed on Polymarket US, and fix the two quote-accuracy defects that probing the WebSocket surfaced.

**Architecture:** A new `PolymarketUsWebSocketAdapter` connects to `wss://api.polymarket.us/v1/ws/markets` with Ed25519-signed handshake headers, subscribes in batches of 100 slugs to the **full** market-data feed, and reads top-of-book from `bids[0]`/`offers[0]` — which is where real `qty` lives. `state.py` selects it over the REST adapter when credentials are present, exactly as `_kalshi_factory` already does for Kalshi.

**Tech Stack:** Python 3.11+, `websockets` 17.0.1, `cryptography` 50.0.0 (Ed25519), pytest with in-process `websockets.serve`.

**Spec:** [docs/superpowers/specs/2026-08-12-polymarket-us-websocket-design.md](../specs/2026-08-12-polymarket-us-websocket-design.md)

## Global Constraints

- **All money and all prices are `Decimal`. Never float.** Convert via `Decimal(str(v))`.
- `Quote.__post_init__` enforces `[0,1]` and `ask >= bid`; a violation raises `ValueError` and the quote must be dropped, never repaired.
- `outcome_id` stays `{slug}:LONG` / `{slug}:SHORT`. This is a transport swap, not a data-model change.
- **`arbys/shared/` is pure domain — no I/O, no framework imports.** The broker change in Task 3 must not import httpx or websockets.
- **Tests never hit a real venue.** WS paths use an in-process `websockets.serve`; see `tests/adapters/test_kalshi_ws.py`.
- Run everything from the repo root with `venv\Scripts\python.exe`.
- Green-build bar: `venv\Scripts\python.exe -m pytest -q` (**190 today**), `venv\Scripts\python.exe -m ruff check .`, and `npm run build` in `frontend/`.
- **mypy is NOT part of the bar** — 47 pre-existing errors. Do not start a cleanup.
- The signature must cover the path **`/v1/ws/markets`**. Signing `/` is rejected with HTTP 401 — verified against the live venue 2026-08-12.
- The REST adapter is **kept**, not deleted. It is the only path that works without credentials.

## File Structure

| File | Responsibility | Task |
| --- | --- | --- |
| `arbys/adapters/polymarket_us.py` | size fix + one-sided books in shared parsing | 1 |
| `arbys/shared/paper_broker.py` | refuse fills against a zero-size side | 2 |
| `arbys/adapters/polymarket_us_ws.py` | **create** — the WS adapter | 3 |
| `arbys/backend/state.py` | creds-gated factory | 4 |
| `CLAUDE.md`, `docs/RUNBOOK.md` | docs | 4 |

Order matters: Tasks 1 and 2 are the paired halves of the one-sided-book fix, and Task 2 must land with or before Task 1 reaches production data, or a synthesised bid becomes a phantom sale.

---

### Task 1: Honest sizes and one-sided books

**Files:**
- Modify: `arbys/adapters/polymarket_us.py` (`quotes_from_bbo`)
- Test: `tests/adapters/test_polymarket_us.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `quotes_from_bbo(slug, market_data) -> list[Quote]` — unchanged signature, changed behaviour
  - `quotes_from_levels(slug, bids, offers) -> list[Quote]` — new; Task 3 uses it for the WS ladder

Two changes, both driven by measurement:

- `bidDepth`/`askDepth` are **counts of price levels**, not sizes. Measured on `aec-mlb-tb-ath-2026-08-12`: `/bbo` reported `bidDepth 49` while `/book` returned 49 levels whose best held 287,926.98 contracts. `/bbo` cannot report size at all, so it must report `0` — "unknown" — rather than a level count masquerading as a size.
- A market with an ask and no bids currently yields no quote, losing a tradeable buy. Live example: `aec-mlb-phi-stl-2026-08-12`, 419,882 offered at 0.0050 against an empty bid side.

- [ ] **Step 1: Write the failing test**

Append to `tests/adapters/test_polymarket_us.py`:

```python
def test_bbo_reports_unknown_size_not_the_depth_counter():
    """bidDepth/askDepth count price levels, not contracts. Measured on
    aec-mlb-tb-ath-2026-08-12: bidDepth 49 against a true best-bid size of
    287,926.98. Reporting 49 as a size is worse than reporting nothing."""
    quotes = {q.outcome_id: q for q in quotes_from_bbo("s", BBO["marketData"])}
    assert quotes["s:LONG"].bid_size == Decimal("0")
    assert quotes["s:LONG"].ask_size == Decimal("0")
    assert quotes["s:SHORT"].bid_size == Decimal("0")
    assert quotes["s:SHORT"].ask_size == Decimal("0")


def test_one_sided_book_still_yields_a_usable_ask():
    """An ask with no bids is a real buying opportunity. Dropping the market
    hides it."""
    one_sided = {"bestAsk": {"value": "0.0050"}, "bestBid": None}
    quotes = {q.outcome_id: q for q in quotes_from_bbo("s", one_sided)}
    assert quotes["s:LONG"].ask == Decimal("0.0050")
    assert quotes["s:LONG"].bid == Decimal("0.0050")  # synthesised
    assert quotes["s:LONG"].bid_size == Decimal("0")  # ...and marked unfillable


def test_book_with_neither_side_yields_nothing():
    assert quotes_from_bbo("s", {}) == []


def test_quotes_from_levels_uses_real_top_of_book_quantities():
    """The WS ladder carries qty, which is the whole reason for using the
    full subscription instead of the lite one."""
    from arbys.adapters.polymarket_us import quotes_from_levels

    bids = [{"px": {"value": "0.9650"}, "qty": "287926.98"},
            {"px": {"value": "0.9600"}, "qty": "10.0"}]
    offers = [{"px": {"value": "0.9700"}, "qty": "1234.5"}]
    quotes = {q.outcome_id: q for q in quotes_from_levels("s", bids, offers)}

    long_q = quotes["s:LONG"]
    assert long_q.bid == Decimal("0.9650")
    assert long_q.ask == Decimal("0.9700")
    assert long_q.bid_size == Decimal("287926.98")
    assert long_q.ask_size == Decimal("1234.5")

    short_q = quotes["s:SHORT"]
    assert short_q.bid == Decimal("0.0300")   # 1 - 0.9700
    assert short_q.ask == Decimal("0.0350")   # 1 - 0.9650
    assert short_q.bid_size == Decimal("1234.5")
    assert short_q.ask_size == Decimal("287926.98")


def test_quotes_from_levels_handles_an_empty_bid_side():
    from arbys.adapters.polymarket_us import quotes_from_levels

    quotes = {
        q.outcome_id: q
        for q in quotes_from_levels("s", [], [{"px": {"value": "0.0050"}, "qty": "419882.67"}])
    }
    long_q = quotes["s:LONG"]
    assert long_q.ask_size == Decimal("419882.67")
    assert long_q.bid_size == Decimal("0")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/adapters/test_polymarket_us.py -q`
Expected: FAIL — the size tests get `36`/`37` from the depth counters, and `quotes_from_levels` does not exist.

- [ ] **Step 3: Rewrite the quote construction**

In `arbys/adapters/polymarket_us.py`, replace `quotes_from_bbo` and add `quotes_from_levels`:

```python
def _pair_to_quotes(
    slug: str,
    bid: Decimal | None,
    ask: Decimal | None,
    bid_size: Decimal,
    ask_size: Decimal,
) -> list[Quote]:
    """Build the LONG/SHORT pair from one side's top of book.

    A one-sided book keeps its live side and synthesises the missing one at
    the same price with **size 0**. Dropping the market instead would hide a
    genuinely tradeable ask — live example: 419,882 contracts offered at
    0.0050 with no bids at all.

    Size 0 is load-bearing, not cosmetic: ``paper_broker`` refuses to fill
    against a zero-size side, which is what stops the synthesised bid from
    becoming a sale into an empty book.
    """
    if bid is None and ask is None:
        return []
    if bid is None:
        bid, bid_size = ask, Decimal("0")  # type: ignore[assignment]
    if ask is None:
        ask, ask_size = bid, Decimal("0")  # type: ignore[assignment]
    assert bid is not None and ask is not None

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
        log.debug("dropping malformed book for %s: bid=%s ask=%s", slug, bid, ask)
        return []


def quotes_from_bbo(slug: str, market_data: dict[str, Any]) -> list[Quote]:
    """Top of book from a ``/bbo`` payload.

    **Sizes are reported as 0 — unknown.** ``bidDepth``/``askDepth`` count
    price *levels*, not contracts: measured on aec-mlb-tb-ath-2026-08-12,
    ``bidDepth`` was 49 while the true best-bid size was 287,926.98. This
    endpoint cannot report size at all, and a level count that looks like a
    size is worse than an explicit unknown. The WebSocket path carries real
    quantities; use it when credentials are available.
    """
    return _pair_to_quotes(
        slug,
        _price(market_data.get("bestBid")),
        _price(market_data.get("bestAsk")),
        Decimal("0"),
        Decimal("0"),
    )


def quotes_from_levels(
    slug: str, bids: list[dict[str, Any]], offers: list[dict[str, Any]]
) -> list[Quote]:
    """Top of book from a full ladder — ``[{"px": {"value": …}, "qty": …}]``.

    Only level 0 is read. The rest of the ladder is why the full subscription
    costs more bandwidth than the lite one, and the ``qty`` on level 0 is what
    that bandwidth buys.
    """

    def top(levels: list[dict[str, Any]]) -> tuple[Decimal | None, Decimal]:
        if not levels:
            return None, Decimal("0")
        return _price(levels[0].get("px")), _size(levels[0].get("qty"))

    bid, bid_size = top(bids)
    ask, ask_size = top(offers)
    return _pair_to_quotes(slug, bid, ask, bid_size, ask_size)
```

- [ ] **Step 4: Run the adapter tests**

Run: `venv\Scripts\python.exe -m pytest tests/adapters/test_polymarket_us.py -q`
Expected: PASS. `test_missing_side_yields_no_quotes` will now fail if it asserted `[]` for a one-sided book — update it to assert only the both-sides-missing case, which `test_book_with_neither_side_yields_nothing` already covers.

- [ ] **Step 5: Commit**

```bash
git add arbys/adapters/polymarket_us.py tests/adapters/test_polymarket_us.py
git commit -m "fix(adapters): report unknown sizes honestly, keep one-sided books

bidDepth/askDepth count price levels, not contracts. Measured on
aec-mlb-tb-ath-2026-08-12: bidDepth 49 against a true best-bid size of
287,926.98. /bbo cannot report size, so it now reports 0 - unknown -
rather than a level count that reads as a size.

A market with an ask and no bids was dropped entirely, hiding a
tradeable buy; live example, 419,882 offered at 0.0050 against an empty
bid side. The missing side is now synthesised at size 0, which the next
commit teaches the broker to refuse."
```

---

### Task 2: Broker refuses zero-size fills

**Files:**
- Modify: `arbys/shared/paper_broker.py` (`_preview_fill`)
- Test: `tests/shared/test_paper_broker.py`

**Interfaces:**
- Consumes: quotes carrying `bid_size`/`ask_size` (Task 1)
- Produces: `_preview_fill` may now return the rejection string `"no_liquidity"`

This is the second, mandatory half of Task 1. `_preview_fill` reads `quote.bid` for sells with no size check, so a synthesised bid would let the broker report selling into a book with no buyers — inflating paper P&L.

- [ ] **Step 1: Write the failing test**

Append to `tests/shared/test_paper_broker.py`:

```python
def test_sell_into_a_zero_size_bid_is_rejected():
    """A one-sided book synthesises the missing side at size 0. Without this
    guard the broker would report selling into a book with no buyers."""
    from decimal import Decimal

    from arbys.shared.quotebook import QuoteBook
    from arbys.shared.types import Quote

    book = QuoteBook(max_age_s=None)
    book.upsert(
        Quote(
            outcome_id="x",
            bid=Decimal("0.0050"),
            ask=Decimal("0.0050"),
            bid_size=Decimal("0"),      # nobody is bidding
            ask_size=Decimal("419882"),
        )
    )
    broker = _broker(book)  # existing helper in this file
    broker.hydrate_balance("acct", Decimal("1000"))
    broker.hydrate_position("acct", "x", qty=Decimal("10"), avg_price=Decimal("0.5"),
                            realized_pnl=Decimal("0"))

    order, fill, reason = broker.apply_fill(
        account_id="acct", outcome_id="x", is_buy=False,
        qty=Decimal("1"), limit_price=Decimal("0"),
    )
    assert fill is None
    assert reason == "no_liquidity"


def test_buy_against_a_live_ask_still_fills_on_a_one_sided_book():
    """The point of keeping one-sided books: the ask is real."""
    from decimal import Decimal

    from arbys.shared.quotebook import QuoteBook
    from arbys.shared.types import Quote

    book = QuoteBook(max_age_s=None)
    book.upsert(
        Quote(
            outcome_id="x",
            bid=Decimal("0.0050"),
            ask=Decimal("0.0050"),
            bid_size=Decimal("0"),
            ask_size=Decimal("419882"),
        )
    )
    broker = _broker(book)
    broker.hydrate_balance("acct", Decimal("1000"))

    order, fill, reason = broker.apply_fill(
        account_id="acct", outcome_id="x", is_buy=True,
        qty=Decimal("1"), limit_price=Decimal("1"),
    )
    assert reason is None
    assert fill is not None
```

> If `tests/shared/test_paper_broker.py` has no `_broker(book)` helper, read
> the file's existing construction of `PaperExecutionAdapter` and build the
> broker the same way inline. Do not invent a different construction.

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/shared/test_paper_broker.py -q -k zero_size`
Expected: FAIL — the sell fills, `reason` is `None`.

- [ ] **Step 3: Add the size check**

In `arbys/shared/paper_broker.py`, in `_preview_fill`, immediately after the `quote is None` check:

```python
        quote = self._book.get(outcome_id)
        if quote is None:
            return "no_quote"
        # A one-sided book synthesises the missing side at size 0 so its live
        # side stays usable. Filling against the synthesised side would be a
        # trade into an empty book.
        resting = quote.ask_size if is_buy else quote.bid_size
        if resting <= 0:
            return "no_liquidity"
```

- [ ] **Step 4: Run the broker tests**

Run: `venv\Scripts\python.exe -m pytest tests/shared/ -q`
Expected: PASS.

> Pre-existing tests may construct `Quote` without sizes, which default to
> `Decimal("0")` and would now be rejected. If any fail, give those fixtures a
> realistic non-zero size — that is the fixture being unrealistic, not the
> guard being wrong. Do **not** weaken the guard to `< 0`.

- [ ] **Step 5: Commit**

```bash
git add arbys/shared/paper_broker.py tests/shared/test_paper_broker.py
git commit -m "fix(broker): refuse to fill against a zero-size side

_preview_fill read quote.bid for sells with no size check, so the
synthesised bid on a one-sided book would let the broker report selling
into a book with no buyers - inflating paper P&L in the same direction
the stale-quote problem does.

This is the mandatory second half of keeping one-sided books: restoring
the quote without this guard fixes one phantom by creating another."
```

---

### Task 3: The WebSocket adapter

**Files:**
- Create: `arbys/adapters/polymarket_us_ws.py`
- Test: `tests/adapters/test_polymarket_us_ws.py`

**Interfaces:**
- Consumes: `quotes_from_levels` (Task 1); `PolymarketUsCredentials`, `auth_headers` from `arbys/adapters/polymarket_us_auth.py`
- Produces: `PolymarketUsWebSocketAdapter(*, outcome_ids, creds, url=DEFAULT_WS_URL, initial_backoff_s=1.0, max_backoff_s=30.0)` with `venue_id = "polymarket_us"`, `list_markets()`, `stream_quotes()`, `close()`

- [ ] **Step 1: Write the failing test**

Create `tests/adapters/test_polymarket_us_ws.py`:

```python
import asyncio
import base64
import json
from decimal import Decimal

import pytest
import websockets
from cryptography.hazmat.primitives.asymmetric import ed25519

from arbys.adapters.polymarket_us_auth import PolymarketUsCredentials
from arbys.adapters.polymarket_us_ws import WS_SIGN_PATH, PolymarketUsWebSocketAdapter


def _creds() -> tuple[PolymarketUsCredentials, ed25519.Ed25519PublicKey]:
    key = ed25519.Ed25519PrivateKey.generate()
    return PolymarketUsCredentials(key_id="kid", secret_key=key), key.public_key()


def _frame(slug: str, bids, offers) -> str:
    return json.dumps(
        {
            "requestId": "r1",
            "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
            "marketData": {"marketSlug": slug, "bids": bids, "offers": offers},
        }
    )


@pytest.mark.asyncio
async def test_handshake_signs_the_ws_path_and_snapshot_parses():
    """Signing anything other than /v1/ws/markets is rejected by the live
    venue with a 401, so the signed path is asserted explicitly."""
    seen_headers = {}
    seen_subscribe = asyncio.get_event_loop().create_future()

    async def handler(ws):
        seen_headers.update(dict(ws.request.headers))
        raw = await ws.recv()
        if not seen_subscribe.done():
            seen_subscribe.set_result(json.loads(raw))
        await ws.send(
            _frame(
                "slug1",
                [{"px": {"value": "0.9650"}, "qty": "287926.98"}],
                [{"px": {"value": "0.9700"}, "qty": "1234.5"}],
            )
        )
        await asyncio.sleep(5)

    creds, public_key = _creds()
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = PolymarketUsWebSocketAdapter(
            outcome_ids=["slug1:LONG", "slug1:SHORT"],
            creds=creds,
            url=f"ws://127.0.0.1:{port}",
        )
        got = []
        async for q in adapter.stream_quotes():
            got.append(q)
            if len(got) >= 2:
                break
        await adapter.close()

    assert seen_headers["x-pm-access-key"] == "kid"
    signature = base64.b64decode(seen_headers["x-pm-signature"])
    message = f"{seen_headers['x-pm-timestamp']}GET{WS_SIGN_PATH}".encode()
    public_key.verify(signature, message)  # raises if the wrong path was signed

    sub = await asyncio.wait_for(seen_subscribe, timeout=2)
    assert sub["subscribe"]["subscriptionType"] == "SUBSCRIPTION_TYPE_MARKET_DATA"
    assert sub["subscribe"]["marketSlugs"] == ["slug1"]

    by_id = {q.outcome_id: q for q in got}
    assert by_id["slug1:LONG"].bid_size == Decimal("287926.98")
    assert by_id["slug1:LONG"].ask_size == Decimal("1234.5")


@pytest.mark.asyncio
async def test_a_malformed_frame_does_not_drop_the_connection():
    async def handler(ws):
        await ws.recv()
        await ws.send("not json at all")
        await ws.send(_frame("slug1", [], [{"px": {"value": "0.0050"}, "qty": "419882.67"}]))
        await asyncio.sleep(5)

    creds, _ = _creds()
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = PolymarketUsWebSocketAdapter(
            outcome_ids=["slug1:LONG"], creds=creds, url=f"ws://127.0.0.1:{port}"
        )
        got = []
        async for q in adapter.stream_quotes():
            got.append(q)
            break
        await adapter.close()
    assert got[0].outcome_id == "slug1:LONG"


@pytest.mark.asyncio
async def test_subscriptions_are_batched_at_one_hundred_slugs():
    """The venue caps a subscription at 100 markets."""
    frames = []

    async def handler(ws):
        while True:
            frames.append(json.loads(await ws.recv()))

    creds, _ = _creds()
    oids = [f"s{i}:LONG" for i in range(250)]
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = PolymarketUsWebSocketAdapter(
            outcome_ids=oids, creds=creds, url=f"ws://127.0.0.1:{port}"
        )
        task = asyncio.create_task(_drain(adapter))
        await asyncio.sleep(0.5)
        task.cancel()
        await adapter.close()

    sizes = [len(f["subscribe"]["marketSlugs"]) for f in frames]
    assert sizes == [100, 100, 50]


async def _drain(adapter):
    async for _q in adapter.stream_quotes():
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/adapters/test_polymarket_us_ws.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'arbys.adapters.polymarket_us_ws'`

- [ ] **Step 3: Write the adapter**

Create `arbys/adapters/polymarket_us_ws.py`:

```python
"""Polymarket US authenticated market-data WebSocket.

Both legs of a cross-venue group push once this is in use: Kalshi already
does, and polling was the reason the two could describe different moments
mid-game.

Protocol details the published docs omit, established against the live venue
on 2026-08-12:

* The handshake signature must cover **/v1/ws/markets**. Signing "/" is
  rejected with HTTP 401.
* Subscribing yields an immediate snapshot per market, then live deltas.
* Each frame is ``{requestId, subscriptionType, marketData: {...}}``.

We subscribe to the **full** ``SUBSCRIPTION_TYPE_MARKET_DATA`` rather than the
lite variant for one reason: lite reports ``bidDepth``/``askDepth``, which
count price *levels* rather than contracts, and only the full ladder carries
real ``qty``. Only level 0 is read.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator
from typing import Any

import websockets

from ..shared.types import Outcome, Quote, Side
from .base import MarketDataAdapter
from .polymarket_us import LONG, SHORT, quotes_from_levels, split_outcome_id
from .polymarket_us_auth import PolymarketUsCredentials, auth_headers

DEFAULT_WS_URL = "wss://api.polymarket.us/v1/ws/markets"
WS_SIGN_PATH = "/v1/ws/markets"
SUBSCRIPTION_TYPE = "SUBSCRIPTION_TYPE_MARKET_DATA"
MAX_SLUGS_PER_SUBSCRIPTION = 100

log = logging.getLogger(__name__)


class PolymarketUsWebSocketAdapter(MarketDataAdapter):
    venue_id = "polymarket_us"

    def __init__(
        self,
        *,
        outcome_ids: list[str],
        creds: PolymarketUsCredentials,
        url: str = DEFAULT_WS_URL,
        initial_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
    ) -> None:
        self._outcome_ids = outcome_ids or []
        self._creds = creds
        self._url = url
        self._initial_backoff_s = initial_backoff_s
        self._max_backoff_s = max_backoff_s
        self._slugs = sorted({split_outcome_id(o)[0] for o in self._outcome_ids})

    async def close(self) -> None:
        """Nothing to release: the connection lives inside stream_quotes and
        is closed by its context manager when the iterator is dropped."""
        return None

    async def list_markets(self) -> list[Outcome]:
        return [
            Outcome(
                id=f"{slug}:{side}",
                venue_id=self.venue_id,
                market_id=slug,
                label=f"{slug} ({side})",
                side=Side.YES if side == LONG else Side.NO,
            )
            for slug in self._slugs
            for side in (LONG, SHORT)
        ]

    async def stream_quotes(self) -> AsyncIterator[Quote]:
        if not self._slugs:
            log.info("PolymarketUsWebSocketAdapter: no slugs to subscribe to")
            return
        backoff = self._initial_backoff_s
        while True:
            try:
                async for quote in self._connect_and_stream():
                    backoff = self._initial_backoff_s
                    yield quote
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("polymarket_us WS disconnect: %s; retry in %.1fs", exc, backoff)
            await asyncio.sleep(backoff * (0.5 + random.random()))
            backoff = min(backoff * 2, self._max_backoff_s)

    async def _connect_and_stream(self) -> AsyncIterator[Quote]:
        headers = auth_headers(self._creds, "GET", WS_SIGN_PATH)
        log.info("polymarket_us WS connecting: %d slug(s)", len(self._slugs))
        async with websockets.connect(
            self._url,
            additional_headers=headers,
            max_size=2**22,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            # A reconnect resubscribes from scratch, which re-requests a
            # snapshot for every market. That is what keeps the quote book
            # from serving pre-disconnect prices across the gap.
            for i in range(0, len(self._slugs), MAX_SLUGS_PER_SUBSCRIPTION):
                batch = self._slugs[i : i + MAX_SLUGS_PER_SUBSCRIPTION]
                await ws.send(
                    json.dumps(
                        {
                            "subscribe": {
                                "requestId": f"arbys-{i // MAX_SLUGS_PER_SUBSCRIPTION}",
                                "subscriptionType": SUBSCRIPTION_TYPE,
                                "marketSlugs": batch,
                            }
                        }
                    )
                )
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue  # one bad frame must not drop the connection
                for quote in self._quotes_from_message(msg):
                    yield quote

    def _quotes_from_message(self, msg: Any) -> list[Quote]:
        if not isinstance(msg, dict):
            return []
        data = msg.get("marketData")
        if not isinstance(data, dict):
            return []  # acks, errors, and other subscription types
        slug = data.get("marketSlug")
        if not slug or slug not in set(self._slugs):
            return []
        bids = data.get("bids") or []
        offers = data.get("offers") or []
        if not isinstance(bids, list) or not isinstance(offers, list):
            return []
        return quotes_from_levels(str(slug), bids, offers)
```

- [ ] **Step 4: Run the WS tests**

Run: `venv\Scripts\python.exe -m pytest tests/adapters/test_polymarket_us_ws.py -q`
Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add arbys/adapters/polymarket_us_ws.py tests/adapters/test_polymarket_us_ws.py
git commit -m "feat(adapters): Polymarket US market-data WebSocket

Both legs push once this is wired; polling was the reason the two venues
could describe different moments mid-game.

Uses the full MARKET_DATA subscription rather than the lite one because
lite reports depth counters rather than contract quantities, and only
the full ladder carries real qty. Only level 0 is read.

The handshake signature must cover /v1/ws/markets - signing / is
rejected with 401, verified against the live venue - so the test
verifies the signed path with the matching public key rather than
trusting the constant."
```

---

### Task 4: Wire it in and document

**Files:**
- Modify: `arbys/backend/state.py`
- Modify: `CLAUDE.md`, `docs/RUNBOOK.md`
- Test: `tests/test_ingest_wiring.py`

**Interfaces:**
- Consumes: `PolymarketUsWebSocketAdapter` (Task 3), `creds_from_env` (already shipped)
- Produces: no new symbols

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ingest_wiring.py`:

```python
def test_factory_picks_websocket_when_credentials_are_present(monkeypatch, tmp_path):
    import base64

    from cryptography.hazmat.primitives.asymmetric import ed25519

    from arbys.backend.state import _default_adapter_factories
    from arbys.adapters.polymarket_us_ws import PolymarketUsWebSocketAdapter

    key = ed25519.Ed25519PrivateKey.generate()
    path = tmp_path / "pm.key"
    path.write_text(base64.b64encode(key.private_bytes_raw()).decode(), encoding="utf-8")
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", "kid")
    monkeypatch.setenv("POLYMARKET_US_PRIVATE_KEY_PATH", str(path))

    adapter = _default_adapter_factories()["polymarket_us"](["s:LONG"])
    assert isinstance(adapter, PolymarketUsWebSocketAdapter)


def test_factory_falls_back_to_rest_without_credentials(monkeypatch):
    from arbys.backend.state import _default_adapter_factories
    from arbys.adapters.polymarket_us import PolymarketUsAdapter

    monkeypatch.delenv("POLYMARKET_US_API_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_US_PRIVATE_KEY_PATH", raising=False)

    adapter = _default_adapter_factories()["polymarket_us"](["s:LONG"])
    assert isinstance(adapter, PolymarketUsAdapter)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv\Scripts\python.exe -m pytest tests/test_ingest_wiring.py -q -k factory_picks`
Expected: FAIL — the factory always returns the REST adapter.

- [ ] **Step 3: Add the creds gate**

In `arbys/backend/state.py`, inside `_default_adapter_factories`, add alongside `_kalshi_factory`:

```python
    def _polymarket_us_factory(oids: list[str]) -> MarketDataAdapter:
        creds = polymarket_us_creds_from_env()
        if creds is not None:
            log.info("using Polymarket US WebSocket adapter (authenticated, real-time)")
            return PolymarketUsWebSocketAdapter(outcome_ids=oids, creds=creds)
        log.info("using Polymarket US REST poll adapter (no credentials set)")
        return PolymarketUsAdapter(
            outcome_ids=oids, poll_interval_s=polymarket_us_poll_s()
        )
```

and register it:

```python
        "polymarket_us": _polymarket_us_factory,
```

Imports:

```python
from ..adapters.polymarket_us_auth import creds_from_env as polymarket_us_creds_from_env
from ..adapters.polymarket_us_ws import PolymarketUsWebSocketAdapter
```

> There is **no fallback on auth failure at runtime.** If the handshake starts
> failing the adapter retries under backoff; silently downgrading to REST
> would hide a revoked credential indefinitely, visible only as degraded fill
> quality.

- [ ] **Step 4: Run the whole suite and lint**

Run: `venv\Scripts\python.exe -m pytest -q && venv\Scripts\python.exe -m ruff check .`
Expected: PASS and clean.

- [ ] **Step 5: Update the docs**

In `CLAUDE.md`, under **Venues**, replace the REST-only paragraph:

```markdown
The adapter is **WebSocket-first with a REST-poll fallback**, selected by
`_polymarket_us_factory` on whether `POLYMARKET_US_API_KEY_ID` +
`POLYMARKET_US_PRIVATE_KEY_PATH` are set — the same shape as
`_kalshi_factory`. With credentials, both legs of a cross-venue group push.

The handshake signature must cover **`/v1/ws/markets`**; signing `/` is
rejected with 401. Verify credentials with
`scripts/verify_polymarket_us_creds.py`, which also checks clock skew — the
30s signing tolerance means a skewed clock fails every request in a way that
looks exactly like a bad key.

We subscribe to the **full** `SUBSCRIPTION_TYPE_MARKET_DATA`, not the lite
variant, because `bidDepth`/`askDepth` count price **levels**, not contracts —
measured 49 against a true best-bid size of 287,926.98 — and only the full
ladder carries real `qty`. REST `/bbo` cannot report size at all and reports
`0`, meaning unknown.

**One-sided books are kept**, with the missing side synthesised at size 0 so a
live ask stays tradeable. `paper_broker` refuses to fill against a zero-size
side; without that guard the synthesised bid would let it report selling into
an empty book.
```

In `docs/RUNBOOK.md`, update the adapter-template section to mention
`polymarket_us_ws.py` alongside the Kalshi WS as the credential-gated pattern.

- [ ] **Step 6: Commit**

```bash
git add arbys/backend/state.py tests/test_ingest_wiring.py CLAUDE.md docs/RUNBOOK.md
git commit -m "feat: select the Polymarket US WebSocket when credentials exist

Mirrors _kalshi_factory: WS when POLYMARKET_US_API_KEY_ID and
POLYMARKET_US_PRIVATE_KEY_PATH are set, REST otherwise. The REST adapter
stays - it is the only path that works without KYC.

No runtime fallback on auth failure: the adapter retries under backoff
rather than silently downgrading, because a silent downgrade would hide
a revoked credential indefinitely."
```

---

### Task 5: Restart, reset paper state, verify live

**Files:** none — operational.

- [ ] **Step 1: Restart the backend on the new code**

Stop any running uvicorn (including orphaned `--reload` workers, which hold
port 8000 while serving nothing), then start detached:

```bash
venv\Scripts\python.exe -m uvicorn arbys.backend.app:app --host 127.0.0.1 --port 8000
```

- [ ] **Step 2: Confirm the WebSocket is the selected adapter**

The log line `using Polymarket US WebSocket adapter (authenticated, real-time)`
must appear. If it says REST poll instead, `.env` is not being read or the
credentials failed to load — run `scripts/verify_polymarket_us_creds.py`.

- [ ] **Step 3: Reset the paper account**

```bash
curl -s -X POST http://127.0.0.1:8000/paper/default/reset
```

This wipes balances, positions, orders and fills, and re-seeds
`DEFAULT_STARTING_BALANCE` per venue. It exists precisely so history recorded
under wrong prices does not contaminate a fresh measurement.

- [ ] **Step 4: Verify quotes and sizes are real**

```bash
venv\Scripts\python.exe -c "import httpx; m=httpx.get('http://127.0.0.1:8000/monitored',timeout=90).json(); pl=[l for g in m for l in g['legs'] if l['venue_id']=='polymarket_us' and l.get('bid_size')]; print('polymarket legs with a size:', len(pl)); print(pl[0] if pl else 'none')"
```

Expected: sizes in the hundreds or thousands, **not** small integers like 6 or
49. A page of two-digit sizes means the depth-counter bug is still live.

- [ ] **Step 5: Confirm both venues are fresh**

```bash
venv\Scripts\python.exe -c "import httpx; m=httpx.get('http://127.0.0.1:8000/monitored',timeout=90).json(); ages={}; [ages.setdefault(l['venue_id'],[]).append(l['quote_age_s']) for g in m for l in g['legs'] if l.get('quote_age_s') is not None]; print({v: round(sum(a)/len(a),1) for v,a in ages.items()})"
```

Expected: mean quote age for `polymarket_us` drops to a few seconds — this is
the whole point of the change, and it is what makes the in-game work tractable.

---

## Post-implementation

The in-game divergence spec
([2026-08-12-in-game-divergence-design.md](../specs/2026-08-12-in-game-divergence-design.md))
must be revised, not deleted. Its tasks 3 and 5 lose most of their purpose once
both legs push; tasks 1, 2, 4, 6 and 8 are unaffected; task 7 stays but becomes
a rare path rather than a routine one. Do that revision before implementing it.
