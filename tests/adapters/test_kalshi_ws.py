"""Tests for the Kalshi authenticated WebSocket adapter."""

from __future__ import annotations

import asyncio
import base64
import json
from decimal import Decimal

import pytest
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from arbys.adapters.kalshi_ws import (
    KalshiWebSocketAdapter,
    _auth_headers,
    _MarketBook,
    load_kalshi_private_key,
)


@pytest.fixture
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


# --------------------------- unit tests: signing --------------------------------


def test_auth_headers_has_three_kalshi_headers(rsa_key):
    h = _auth_headers("key-id-uuid", rsa_key)
    assert h["KALSHI-ACCESS-KEY"] == "key-id-uuid"
    assert h["KALSHI-ACCESS-TIMESTAMP"].isdigit()
    sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
    assert len(sig) == 256  # 2048-bit RSA -> 256 bytes


def test_auth_headers_signature_verifies_against_public_key(rsa_key):
    h = _auth_headers("kid", rsa_key, path="/trade-api/ws/v2")
    payload = h["KALSHI-ACCESS-TIMESTAMP"] + "GET" + "/trade-api/ws/v2"
    sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
    rsa_key.public_key().verify(
        sig,
        payload.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_load_private_key_reads_pem(tmp_path, rsa_key):
    pem = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    p = tmp_path / "k.pem"
    p.write_bytes(pem)
    loaded = load_kalshi_private_key(str(p))
    assert isinstance(loaded, rsa.RSAPrivateKey)


# --------------------------- unit tests: book -----------------------------------


def test_book_snapshot_and_best_prices():
    b = _MarketBook()
    b.apply_snapshot(
        {
            "yes_dollars_fp": [["0.40", "10"], ["0.42", "5"]],
            "no_dollars_fp": [["0.55", "8"]],
        }
    )
    assert b.best_yes_bid() == Decimal("0.42")
    assert b.best_no_bid() == Decimal("0.55")


def test_book_delta_adds_and_removes_levels():
    b = _MarketBook()
    b.apply_snapshot({"yes_dollars_fp": [["0.40", "10"]], "no_dollars_fp": []})
    b.apply_delta("yes", "0.45", "3")
    assert b.best_yes_bid() == Decimal("0.45")
    # Negative delta zeroing out size -> level removed
    b.apply_delta("yes", "0.45", "-3")
    assert b.best_yes_bid() == Decimal("0.40")


def test_quotes_for_computes_ask_from_opposite_side(rsa_key):
    a = KalshiWebSocketAdapter(
        outcome_ids=["T:YES", "T:NO"],
        api_key_id="k",
        private_key=rsa_key,
    )
    a._books["T"].apply_snapshot(
        {"yes_dollars_fp": [["0.40", "10"]], "no_dollars_fp": [["0.55", "10"]]}
    )
    quotes = {q.outcome_id: q for q in a._quotes_for("T")}
    assert quotes["T:YES"].bid == Decimal("0.40")
    assert quotes["T:YES"].ask == Decimal("0.45")  # 1 - 0.55
    assert quotes["T:NO"].bid == Decimal("0.55")
    assert quotes["T:NO"].ask == Decimal("0.60")  # 1 - 0.40


# --------------------------- end-to-end WS --------------------------------------


class _WsServer:
    def __init__(self):
        self.subscription: dict | None = None
        self._to_send: asyncio.Queue[str] = asyncio.Queue()
        self._server = None
        self.port = 0

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
async def test_ws_stream_yields_quotes_from_snapshot(ws_server, rsa_key):
    a = KalshiWebSocketAdapter(
        outcome_ids=["TICK-A:YES", "TICK-A:NO"],
        api_key_id="k",
        private_key=rsa_key,
        url=ws_server.url,
        initial_backoff_s=0.01,
        max_backoff_s=0.05,
    )

    async def push_soon():
        await asyncio.sleep(0.1)
        await ws_server.push(
            {
                "type": "orderbook_snapshot",
                "sid": 1,
                "seq": 1,
                "msg": {
                    "market_ticker": "TICK-A",
                    "yes_dollars_fp": [["0.40", "10"]],
                    "no_dollars_fp": [["0.55", "10"]],
                },
            }
        )

    push_task = asyncio.create_task(push_soon())
    stream = a.stream_quotes()
    q1 = await asyncio.wait_for(stream.__anext__(), timeout=3.0)
    q2 = await asyncio.wait_for(stream.__anext__(), timeout=3.0)
    quotes = {q.outcome_id: q for q in (q1, q2)}
    assert quotes["TICK-A:YES"].bid == Decimal("0.40")
    assert quotes["TICK-A:YES"].ask == Decimal("0.45")

    assert ws_server.subscription["cmd"] == "subscribe"
    assert ws_server.subscription["params"]["channels"] == ["orderbook_delta"]
    assert ws_server.subscription["params"]["market_tickers"] == ["TICK-A"]

    await stream.aclose()
    push_task.cancel()


@pytest.mark.asyncio
async def test_ws_stream_applies_delta_after_snapshot(ws_server, rsa_key):
    a = KalshiWebSocketAdapter(
        outcome_ids=["TICK-B:YES"],
        api_key_id="k",
        private_key=rsa_key,
        url=ws_server.url,
        initial_backoff_s=0.01,
        max_backoff_s=0.05,
    )

    async def push_soon():
        await asyncio.sleep(0.1)
        await ws_server.push(
            {
                "type": "orderbook_snapshot",
                "msg": {
                    "market_ticker": "TICK-B",
                    "yes_dollars_fp": [["0.40", "10"]],
                    "no_dollars_fp": [["0.55", "10"]],
                },
            }
        )
        await asyncio.sleep(0.05)
        await ws_server.push(
            {
                "type": "orderbook_delta",
                "msg": {
                    "market_ticker": "TICK-B",
                    "price_dollars": "0.43",
                    "delta_fp": "5",
                    "side": "yes",
                },
            }
        )

    push_task = asyncio.create_task(push_soon())
    stream = a.stream_quotes()

    saw_new_bid = False
    for _ in range(8):
        q = await asyncio.wait_for(stream.__anext__(), timeout=3.0)
        if q.outcome_id == "TICK-B:YES" and q.bid == Decimal("0.43"):
            saw_new_bid = True
            break
    assert saw_new_bid

    await stream.aclose()
    push_task.cancel()


def test_outcome_ids_dedupe_to_tickers(rsa_key):
    a = KalshiWebSocketAdapter(
        outcome_ids=["X:YES", "X:NO", "Y:YES"],
        api_key_id="k",
        private_key=rsa_key,
    )
    assert a._market_tickers == ["X", "Y"]
