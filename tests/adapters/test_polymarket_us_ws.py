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


def _frame(slug: str, bids: list, offers: list) -> str:
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
    venue with a 401, so the signed path is verified rather than assumed."""
    seen_headers: dict[str, str] = {}
    got_subscribe: asyncio.Future = asyncio.get_running_loop().create_future()

    async def handler(ws):
        seen_headers.update(dict(ws.request.headers))
        raw = await ws.recv()
        if not got_subscribe.done():
            got_subscribe.set_result(json.loads(raw))
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

    sub = await asyncio.wait_for(got_subscribe, timeout=2)
    assert sub["subscribe"]["subscriptionType"] == "SUBSCRIPTION_TYPE_MARKET_DATA"
    assert sub["subscribe"]["marketSlugs"] == ["slug1"]

    by_id = {q.outcome_id: q for q in got}
    assert by_id["slug1:LONG"].bid == Decimal("0.9650")
    assert by_id["slug1:LONG"].ask == Decimal("0.9700")
    # Real quantities, not the depth counters /bbo reports.
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

    # The one-sided frame after the junk still arrived, so the junk did not
    # kill the connection.
    assert got[0].outcome_id == "slug1:LONG"
    assert got[0].ask_size == Decimal("419882.67")
    assert got[0].bid_size == Decimal("0")  # known empty, not unknown


@pytest.mark.asyncio
async def test_subscriptions_are_batched_at_one_hundred_slugs():
    """The venue caps a subscription at 100 markets."""
    frames: list[dict] = []

    async def handler(ws):
        try:
            while True:
                frames.append(json.loads(await ws.recv()))
        except Exception:
            return

    creds, _ = _creds()
    oids = [f"s{i:03d}:LONG" for i in range(250)]
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = PolymarketUsWebSocketAdapter(
            outcome_ids=oids, creds=creds, url=f"ws://127.0.0.1:{port}"
        )

        async def drain():
            async for _q in adapter.stream_quotes():
                pass

        task = asyncio.create_task(drain())
        await asyncio.sleep(0.6)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await adapter.close()

    sizes = [len(f["subscribe"]["marketSlugs"]) for f in frames]
    assert sizes == [100, 100, 50]


@pytest.mark.asyncio
async def test_frames_for_unsubscribed_slugs_are_ignored():
    async def handler(ws):
        await ws.recv()
        await ws.send(_frame("someone-elses-slug", [], [{"px": {"value": "0.5"}, "qty": "1"}]))
        await ws.send(_frame("slug1", [], [{"px": {"value": "0.25"}, "qty": "9"}]))
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

    assert all(q.outcome_id.startswith("slug1") for q in got)
