import asyncio
import json
from decimal import Decimal

import httpx
import pytest
import websockets

from arbys.adapters.polymarket import PolymarketAdapter, _BookState, _quote_from_book


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


# --------------------------- REST discovery (existing) ---------------------------


@pytest.mark.asyncio
async def test_list_markets_parses_gamma_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "gamma-api.polymarket.com" in str(request.url)
        return httpx.Response(
            200,
            json=[
                {
                    "id": "m1",
                    "clobTokenIds": ["tok_yes", "tok_no"],
                    "outcomes": ["Yes", "No"],
                }
            ],
        )

    client = _mock_client(handler)
    a = PolymarketAdapter(http_client=client)
    outcomes = await a.list_markets()
    assert len(outcomes) == 2
    assert outcomes[0].id == "tok_yes"
    assert outcomes[0].venue_id == "polymarket"
    await a.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_list_markets_handles_stringified_arrays():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"id": "m1", "clobTokenIds": '["t1","t2"]', "outcomes": '["Yes","No"]'}
            ],
        )

    client = _mock_client(handler)
    a = PolymarketAdapter(http_client=client)
    outcomes = await a.list_markets()
    assert [o.id for o in outcomes] == ["t1", "t2"]
    await a.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_quote_returns_normalized_prices():
    def handler(request: httpx.Request) -> httpx.Response:
        side = request.url.params.get("side")
        price = "0.42" if side == "buy" else "0.40"
        return httpx.Response(200, json={"price": price})

    client = _mock_client(handler)
    a = PolymarketAdapter(http_client=client, outcome_ids=["tok"])
    q = await a._fetch_quote("tok")
    assert q is not None
    assert q.ask == Decimal("0.42")
    assert q.bid == Decimal("0.40")
    await a.close()
    await client.aclose()


# --------------------------- Pure book-state ------------------------------------


def test_book_state_best_bid_ask():
    b = _BookState()
    b.replace_side("BUY", [{"price": "0.40", "size": "10"}, {"price": "0.42", "size": "5"}])
    b.replace_side("SELL", [{"price": "0.45", "size": "10"}, {"price": "0.44", "size": "5"}])
    assert b.best_bid() == Decimal("0.42")
    assert b.best_ask() == Decimal("0.44")


def test_book_state_apply_change_removes_when_zero_size():
    b = _BookState()
    b.replace_side("BUY", [{"price": "0.40", "size": "10"}, {"price": "0.42", "size": "5"}])
    b.apply_change("BUY", "0.42", "0")
    assert b.best_bid() == Decimal("0.40")


def test_book_state_apply_change_updates_size_and_adds_levels():
    b = _BookState()
    b.replace_side("SELL", [{"price": "0.50", "size": "10"}])
    b.apply_change("SELL", "0.48", "3")
    assert b.best_ask() == Decimal("0.48")


def test_quote_from_book_handles_one_sided_book():
    b = _BookState()
    b.replace_side("BUY", [{"price": "0.30", "size": "1"}])
    q = _quote_from_book("tok", b)
    assert q is not None
    assert q.bid == q.ask == Decimal("0.30")


def test_quote_from_book_empty_returns_none():
    assert _quote_from_book("tok", _BookState()) is None


# --------------------------- Event application ----------------------------------


def test_apply_book_event_yields_quote():
    a = PolymarketAdapter(outcome_ids=["tok"], use_websocket=False)
    q = a._apply_event(
        {
            "event_type": "book",
            "asset_id": "tok",
            "bids": [{"price": "0.40", "size": "10"}],
            "asks": [{"price": "0.44", "size": "10"}],
        }
    )
    assert q is not None and q.bid == Decimal("0.40") and q.ask == Decimal("0.44")


def test_apply_price_change_event_updates_book():
    a = PolymarketAdapter(outcome_ids=["tok"], use_websocket=False)
    a._apply_event(
        {
            "event_type": "book",
            "asset_id": "tok",
            "bids": [{"price": "0.40", "size": "10"}],
            "asks": [{"price": "0.44", "size": "10"}],
        }
    )
    q = a._apply_event(
        {
            "event_type": "price_change",
            "asset_id": "tok",
            "changes": [{"side": "BUY", "price": "0.42", "size": "3"}],
        }
    )
    assert q is not None and q.bid == Decimal("0.42")


def test_apply_event_ignores_unknown_types():
    a = PolymarketAdapter(outcome_ids=["tok"], use_websocket=False)
    assert a._apply_event({"event_type": "tick_size_change", "asset_id": "tok"}) is None


# --------------------------- WS end-to-end --------------------------------------


class _WsServer:
    """In-process WS server that captures subscription and pushes queued events."""

    def __init__(self):
        self.subscription: dict | None = None
        self._client_ready = asyncio.Event()
        self._to_send: asyncio.Queue[str] = asyncio.Queue()
        self._server = None
        self.port: int = 0
        self.close_after_subscribe = False
        self.close_count = 0

    async def start(self):
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def push(self, obj) -> None:
        await self._to_send.put(json.dumps(obj))

    async def _handle(self, ws):
        try:
            first = await ws.recv()
        except websockets.ConnectionClosed:
            return
        self.subscription = json.loads(first)
        self._client_ready.set()
        if self.close_after_subscribe:
            self.close_count += 1
            await ws.close()
            return
        closed_task = asyncio.create_task(ws.wait_closed())
        try:
            while True:
                get_task = asyncio.create_task(self._to_send.get())
                done, _ = await asyncio.wait(
                    [get_task, closed_task], return_when=asyncio.FIRST_COMPLETED
                )
                if closed_task in done:
                    get_task.cancel()
                    return
                try:
                    await ws.send(get_task.result())
                except websockets.ConnectionClosed:
                    return
        finally:
            closed_task.cancel()


@pytest.fixture
async def ws_server():
    s = _WsServer()
    await s.start()
    yield s
    await s.stop()


@pytest.mark.asyncio
async def test_ws_stream_yields_quotes_from_book_event(ws_server):
    a = PolymarketAdapter(
        outcome_ids=["tok"],
        use_websocket=True,
        ws_url=ws_server.url,
        ws_backoff_initial_s=0.01,
        ws_backoff_max_s=0.05,
    )

    async def push_soon():
        await asyncio.sleep(0.1)
        await ws_server.push(
            {
                "event_type": "book",
                "asset_id": "tok",
                "bids": [{"price": "0.40", "size": "10"}],
                "asks": [{"price": "0.44", "size": "10"}],
            }
        )

    _push_task = asyncio.create_task(push_soon())

    stream = a.stream_quotes()
    q = await asyncio.wait_for(stream.__anext__(), timeout=3.0)
    assert q.bid == Decimal("0.40") and q.ask == Decimal("0.44")
    assert ws_server.subscription == {"assets_ids": ["tok"], "type": "market"}

    await stream.aclose()
    await a.close()
    _push_task.cancel()


@pytest.mark.asyncio
async def test_ws_stream_falls_back_to_rest_after_repeated_failures(ws_server):
    """When WS closes repeatedly, adapter should start REST polling."""
    ws_server.close_after_subscribe = True

    call_count = {"n": 0}

    def rest_handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        side = request.url.params.get("side")
        price = "0.55" if side == "buy" else "0.50"
        return httpx.Response(200, json={"price": price})

    http = _mock_client(rest_handler)
    a = PolymarketAdapter(
        outcome_ids=["tok"],
        http_client=http,
        use_websocket=True,
        ws_url=ws_server.url,
        ws_backoff_initial_s=0.01,
        ws_backoff_max_s=0.02,
        ws_fallback_failure_threshold=2,
        ws_fallback_window_s=5.0,
        poll_interval_s=0.05,
    )

    stream = a.stream_quotes()
    q = await asyncio.wait_for(stream.__anext__(), timeout=5.0)
    assert q.bid == Decimal("0.50") and q.ask == Decimal("0.55")
    assert ws_server.close_count >= 2
    assert call_count["n"] >= 2

    await stream.aclose()
    await a.close()
    await http.aclose()


@pytest.mark.asyncio
async def test_stream_quotes_empty_outcome_ids_returns_immediately():
    a = PolymarketAdapter(outcome_ids=[], use_websocket=True)
    async for _ in a.stream_quotes():
        pytest.fail("should not yield")
    await a.close()
