import asyncio
import base64
import contextlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import websockets
from cryptography.hazmat.primitives.asymmetric import ed25519

from arbys.adapters.polymarket_us_auth import PolymarketUsCredentials
from arbys.adapters.polymarket_us_ws import (
    WS_SIGN_PATH,
    PolymarketUsWebSocketAdapter,
    frame_age_s,
)
from arbys.shared.quotebook import QuoteBook
from arbys.shared.types import Quote


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
    """The venue caps a single subscribe message at 100 markets."""
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

    # Three connections, one subscribe each: the shard size and the
    # per-message cap are both 100, so the split is visible either way. Sorted
    # because the shards connect concurrently and may arrive in any order.
    sizes = sorted(len(f["subscribe"]["marketSlugs"]) for f in frames)
    assert sizes == [50, 100, 100]


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


@pytest.mark.asyncio
async def test_the_subscription_is_sharded_across_connections():
    """Every shard gets its own socket, and all of them reach the consumer.

    A single connection silently stops streaming most of its markets past
    some ceiling - measured 2026-08-25, eight of nine provably-live slugs were
    shed when subscribed alongside 564 others on one socket. The failure has no
    error and no disconnect; quotes just stop arriving for an arbitrary subset,
    which is indistinguishable from a quiet market until a stale leg invents an
    arbitrage against a live one. Splitting the subscription is the fix, so the
    split itself is what this pins.
    """
    connections: list[list[str]] = []

    async def handler(ws):
        sub = json.loads(await ws.recv())
        asked = sub["subscribe"]["marketSlugs"]
        connections.append(asked)
        for slug in asked:
            await ws.send(_frame(slug, [{"px": {"value": "0.4000"}, "qty": "7"}], []))
        await asyncio.sleep(5)

    creds, _ = _creds()
    oids = [f"s{i}:LONG" for i in range(5)]
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = PolymarketUsWebSocketAdapter(
            outcome_ids=oids, creds=creds, url=f"ws://127.0.0.1:{port}", shard_size=2
        )
        assert adapter.shards == [["s0", "s1"], ["s2", "s3"], ["s4"]]

        got: set[str] = set()

        async def collect():
            async for q in adapter.stream_quotes():
                got.add(q.outcome_id)
                if len({o for o in got if o.endswith(":LONG")}) >= 5:
                    return

        await asyncio.wait_for(collect(), timeout=5)
        await adapter.close()

    # Three sockets, and — the point — a quote from the slug on every one of
    # them, including the last shard that a single oversized subscription is
    # what silently starves.
    assert len(connections) == 3
    assert sorted(sorted(c) for c in connections) == [["s0", "s1"], ["s2", "s3"], ["s4"]]
    assert {f"s{i}:LONG" for i in range(5)} <= got


_SHARD_HANDLER_NOTE = """Iterating rather than sleeping is what makes these
tests measure the socket closing: the loop ends when the client goes away,
whereas a sleeping handler would still look "open" long after it had."""


def _tracking_handler(open_now: set[int]):
    async def handler(ws):
        open_now.add(id(ws))
        try:
            async for _msg in ws:  # see _SHARD_HANDLER_NOTE
                pass
        finally:
            open_now.discard(id(ws))

    return handler


async def _wait_for(predicate, *, timeout_s: float = 3.0) -> None:
    for _ in range(int(timeout_s / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_cancelling_the_consumer_closes_every_shard_socket():
    """No shard may outlive the stream that spawned it.

    ``restart_ingest`` tears ingest down and rebuilds it on every discovery
    pass that changes anything, so an orphaned socket here would compound: each
    one keeps its markets subscribed and keeps counting against the
    per-connection ceiling that sharding exists to respect - invisibly, because
    the leaked connection is the one still being served.
    """
    open_now: set[int] = set()
    creds, _ = _creds()
    async with websockets.serve(_tracking_handler(open_now), "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = PolymarketUsWebSocketAdapter(
            outcome_ids=["a:LONG", "b:LONG", "c:LONG"],
            creds=creds,
            url=f"ws://127.0.0.1:{port}",
            shard_size=1,
        )

        async def drain():
            async for _q in adapter.stream_quotes():
                pass

        task = asyncio.create_task(drain())
        await _wait_for(lambda: len(open_now) == 3)
        assert len(open_now) == 3, f"only {len(open_now)} shard(s) connected"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _wait_for(lambda: not open_now)
        assert open_now == set(), f"{len(open_now)} shard socket(s) left open"


@pytest.mark.asyncio
async def test_close_shuts_the_shards_down_without_cancelling_the_consumer():
    """``close()`` must end the shards by itself.

    Cancelling the consumer happens to close them, because the cancellation
    lands inside the generator's own await and runs its cleanup. ``close()`` is
    the other order - ``_stop_ingest`` calls it on every adapter - and it
    cannot rely on that, nor on the garbage collector: a shard runs in its own
    task, which the event loop keeps alive regardless of what becomes of the
    generator that spawned it.
    """
    open_now: set[int] = set()
    creds, _ = _creds()
    async with websockets.serve(_tracking_handler(open_now), "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = PolymarketUsWebSocketAdapter(
            outcome_ids=["a:LONG", "b:LONG", "c:LONG"],
            creds=creds,
            url=f"ws://127.0.0.1:{port}",
            shard_size=1,
        )

        async def drain():
            async for _q in adapter.stream_quotes():
                pass

        task = asyncio.create_task(drain())
        await _wait_for(lambda: len(open_now) == 3)
        assert len(open_now) == 3, f"only {len(open_now)} shard(s) connected"

        # The consumer is left running on purpose, so nothing but close() can
        # be what takes the sockets down.
        await adapter.close()
        await _wait_for(lambda: not open_now)
        assert open_now == set(), f"{len(open_now)} shard socket(s) left open"

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# --- the venue's own clock -------------------------------------------------
#
# A replayed snapshot and a live book arrive identically. `transactTime` is the
# only field that tells them apart, and until 2026-08-25 nothing read it:
# quotes were stamped on arrival, so a book hours out of date entered as fresh
# and no age check could withhold it.


def test_frame_age_reads_the_venue_clock_including_nanoseconds():
    """The venue stamps 9 fractional digits; datetime takes at most 6."""
    now = datetime(2026, 8, 25, 17, 53, 17, tzinfo=UTC).timestamp()
    age = frame_age_s("2026-08-25T12:04:49.999866252Z", now=now)
    assert age is not None
    assert 20907 < age < 20908  # ~5.8h, the real lag on a replayed snapshot

    assert frame_age_s("2026-08-25T17:53:07Z", now=now) == 10.0
    assert frame_age_s("2026-08-25T17:53:07.500Z", now=now) == pytest.approx(9.5)


def test_a_frame_from_the_future_is_clamped_rather_than_trusted():
    """Clock skew must not be able to make a stale quote look fresh."""
    now = datetime(2026, 8, 25, 17, 53, 17, tzinfo=UTC).timestamp()
    assert frame_age_s("2026-08-25T17:53:27Z", now=now) == 0.0


def test_an_unparseable_timestamp_reports_unknown_not_zero():
    """None means fall back to arrival time; 0 would assert freshness."""
    assert frame_age_s(None) is None
    assert frame_age_s("") is None
    assert frame_age_s("not-a-time") is None
    assert frame_age_s(12345) is None


@pytest.mark.asyncio
async def test_a_replayed_snapshot_arrives_already_stale():
    """A frame whose transactTime is hours old must not enter the book fresh.

    This is the defect that produced false arbitrage: the venue answers a
    subscribe with a cached book, we stamped it on arrival, and a leg hours out
    of date sat in the opportunity set against a live one on the other venue.
    """
    stale_stamp = (datetime.now(UTC) - timedelta(hours=6)).strftime(
        "%Y-%m-%dT%H:%M:%S.000000000Z"
    )

    async def handler(ws):
        await ws.recv()
        await ws.send(
            json.dumps(
                {
                    "marketData": {
                        "marketSlug": "slug1",
                        "transactTime": stale_stamp,
                        "bids": [{"px": {"value": "0.4000"}, "qty": "5"}],
                        "offers": [{"px": {"value": "0.4200"}, "qty": "7"}],
                    }
                }
            )
        )
        await asyncio.sleep(5)

    creds, _ = _creds()
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = PolymarketUsWebSocketAdapter(
            outcome_ids=["slug1:LONG"],
            creds=creds,
            url=f"ws://127.0.0.1:{port}",
            backstop_after_s=0,  # isolate the WS path
        )
        got = []

        async def collect():
            async for q in adapter.stream_quotes():
                got.append(q)
                return

        # close() in a finally: a hung adapter must fail this test, not hang
        # it - websockets.serve waits on any socket the adapter leaks.
        try:
            await asyncio.wait_for(collect(), timeout=5)
        finally:
            await adapter.close()

    assert got[0].source_age_s is not None
    assert got[0].source_age_s > 21_000  # ~6h, reported by the venue itself

    # The book must withhold it immediately, not 600s from now.
    book = QuoteBook(max_age_s=600.0)
    book.upsert(got[0])
    assert book.get("slug1:LONG") is None, "a 6h-old book entered as tradeable"
    aged = book.get_with_age("slug1:LONG")
    assert aged is not None and aged[1] > 21_000  # still reportable, so the UI explains


@pytest.mark.asyncio
async def test_the_backstop_rereads_markets_the_socket_abandons():
    """A market the socket never serves is corrected from the public gateway.

    The feed is push-only, so nothing else can replace that quote. Measured
    2026-08-25 the venue simply stops sending for a persistent subset and
    answers a resubscribe with an hours-old snapshot regardless of shard size,
    so no amount of resharding reaches these markets.
    """

    async def handler(ws):
        await ws.recv()
        await asyncio.sleep(30)  # connected, healthy, and silent - the failure

    asked: list[str] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        slug = str(request.url).rsplit("/", 2)[-2]
        asked.append(slug)
        return httpx.Response(
            200,
            json={
                "marketData": {
                    "marketSlug": slug,
                    "bestBid": {"value": "0.1900"},
                    "bestAsk": {"value": "0.2300"},
                }
            },
        )

    creds, _ = _creds()
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = PolymarketUsWebSocketAdapter(
            outcome_ids=["quiet:LONG"],
            creds=creds,
            url=f"ws://127.0.0.1:{port}",
            backstop_after_s=0.05,
            backstop_sweep_s=0.05,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(gateway), timeout=5.0
            ),
        )
        got: dict[str, Quote] = {}

        async def collect():
            async for q in adapter.stream_quotes():
                got[q.outcome_id] = q
                if len(got) >= 2:
                    return

        # close() in a finally: a hung adapter must fail this test, not hang
        # it - websockets.serve waits on any socket the adapter leaks.
        try:
            await asyncio.wait_for(collect(), timeout=5)
        finally:
            await adapter.close()

    assert asked and asked[0] == "quiet"
    assert got["quiet:LONG"].bid == Decimal("0.1900")
    assert got["quiet:LONG"].ask == Decimal("0.2300")
    # /bbo cannot report depth, and unknown must stay unknown rather than
    # borrowing the ladder's numbers.
    assert got["quiet:LONG"].ask_size is None
    # The short side stays derived from the long one.
    assert got["quiet:SHORT"].bid == Decimal("0.7700")


@pytest.mark.asyncio
async def test_the_backstop_leaves_a_live_market_alone():
    """It corrects abandoned markets, never competes with a working socket."""

    async def handler(ws):
        await ws.recv()
        while True:
            await ws.send(
                json.dumps(
                    {
                        "marketData": {
                            "marketSlug": "busy",
                            "transactTime": datetime.now(UTC).strftime(
                                "%Y-%m-%dT%H:%M:%S.000000Z"
                            ),
                            "bids": [{"px": {"value": "0.4000"}, "qty": "5"}],
                            "offers": [{"px": {"value": "0.4200"}, "qty": "7"}],
                        }
                    }
                )
            )
            await asyncio.sleep(0.02)

    asked: list[str] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, json={})

    creds, _ = _creds()
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = PolymarketUsWebSocketAdapter(
            outcome_ids=["busy:LONG"],
            creds=creds,
            url=f"ws://127.0.0.1:{port}",
            backstop_after_s=0.5,
            backstop_sweep_s=0.05,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(gateway), timeout=5.0
            ),
        )
        seen = 0

        async def collect():
            nonlocal seen
            async for _q in adapter.stream_quotes():
                seen += 1
                if seen >= 20:
                    return

        # close() in a finally: a hung adapter must fail this test, not hang
        # it - websockets.serve waits on any socket the adapter leaks.
        try:
            await asyncio.wait_for(collect(), timeout=5)
        finally:
            await adapter.close()

    assert seen >= 20
    assert asked == [], f"backstop fetched a market the socket was serving: {asked}"


@pytest.mark.asyncio
async def test_repeated_stale_replays_do_not_hold_the_backstop_off():
    """A market answered only with the same old snapshot is still abandoned.

    The socket looks busy for it - frames keep arriving - but every one is a
    replay of a book the venue left hours ago, so the market is never once
    tradeable. If arriving frames counted as service, the backstop would be
    starved for exactly the markets that need it most.
    """
    stale_stamp = (datetime.now(UTC) - timedelta(hours=6)).strftime(
        "%Y-%m-%dT%H:%M:%S.000000000Z"
    )

    async def handler(ws):
        await ws.recv()
        while True:
            await ws.send(
                json.dumps(
                    {
                        "marketData": {
                            "marketSlug": "replayed",
                            "transactTime": stale_stamp,
                            "bids": [{"px": {"value": "0.9900"}, "qty": "40"}],
                            "offers": [{"px": {"value": "0.9900"}, "qty": "40"}],
                        }
                    }
                )
            )
            await asyncio.sleep(0.02)

    asked: list[str] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url).rsplit("/", 2)[-2])
        return httpx.Response(
            200,
            json={
                "marketData": {
                    "marketSlug": "replayed",
                    "bestBid": {"value": "0.0100"},
                    "bestAsk": {"value": "0.0200"},
                }
            },
        )

    creds, _ = _creds()
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = PolymarketUsWebSocketAdapter(
            outcome_ids=["replayed:LONG"],
            creds=creds,
            url=f"ws://127.0.0.1:{port}",
            backstop_after_s=0.05,
            backstop_sweep_s=0.05,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(gateway), timeout=5.0
            ),
        )
        corrected: list[Quote] = []

        async def collect():
            async for q in adapter.stream_quotes():
                # The corrected quote is the one with no venue-reported age.
                if q.outcome_id == "replayed:LONG" and q.source_age_s is None:
                    corrected.append(q)
                    return

        try:
            await asyncio.wait_for(collect(), timeout=5)
        finally:
            await adapter.close()

    assert asked, "the backstop never fired for a market fed only stale replays"
    assert corrected[0].ask == Decimal("0.0200")


# --- subscription repair ---------------------------------------------------
#
# The venue acknowledges a subscription as a whole, never per market. If it
# registers 97 of the 100 names we send, nothing says so, and those three stay
# dark for the life of the connection - we subscribe once at connect and never
# check. Re-sending a subscribe for just those costs one small message and
# needs no reconnect, because subscribing is additive.


def _subs(frames: list[dict]) -> list[list[str]]:
    return [f["subscribe"]["marketSlugs"] for f in frames if "subscribe" in f]


@pytest.mark.asyncio
async def test_a_market_the_socket_never_delivers_is_resubscribed():
    """The dark market gets asked for again, on the same connection."""
    got: list[dict] = []

    async def handler(ws):
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.01)
                got.append(json.loads(raw))
            except (TimeoutError, Exception):
                pass
            await ws.send(
                _frame("chatty", [{"px": {"value": "0.4000"}, "qty": "5"}],
                       [{"px": {"value": "0.4200"}, "qty": "7"}])
            )
            await asyncio.sleep(0.01)

    creds, _ = _creds()
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = PolymarketUsWebSocketAdapter(
            outcome_ids=["chatty:LONG", "dark:LONG"],
            creds=creds,
            url=f"ws://127.0.0.1:{port}",
            backstop_after_s=0,  # isolate the socket path
        )
        # Repair on a test-sized clock.
        import arbys.adapters.polymarket_us_ws as mod

        old_sweep, old_after = mod.REPAIR_SWEEP_S, mod.REPAIR_AFTER_S
        mod.REPAIR_SWEEP_S, mod.REPAIR_AFTER_S = 0.05, 0.05
        try:
            async def drain():
                async for _q in adapter.stream_quotes():
                    pass

            task = asyncio.create_task(drain())
            await _wait_for(
                lambda: any("dark" in b for b in _subs(got)[1:]), timeout_s=4.0
            )
        finally:
            mod.REPAIR_SWEEP_S, mod.REPAIR_AFTER_S = old_sweep, old_after
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await adapter.close()

    resubs = _subs(got)[1:]
    assert resubs, "the socket never re-subscribed anything"
    assert any("dark" in b for b in resubs), f"dark was never re-subscribed: {resubs}"
    assert all("chatty" not in b for b in resubs), (
        f"a market that was streaming fine got re-subscribed: {resubs}"
    )


@pytest.mark.asyncio
async def test_a_quiet_pregame_market_is_not_resubscribed():
    """It answered at connect and is simply quiet - that is normal.

    Re-subscribing every quiet pre-game market would be hundreds of pointless
    messages a minute, and each one makes the venue replay a snapshot.
    """
    got: list[dict] = []

    async def handler(ws):
        got.append(json.loads(await ws.recv()))
        # Both answer once, then go quiet, like a normal pre-game book.
        for slug in ("a", "b"):
            await ws.send(
                _frame(slug, [{"px": {"value": "0.4000"}, "qty": "5"}],
                       [{"px": {"value": "0.4200"}, "qty": "7"}])
            )
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.05)
                got.append(json.loads(raw))
            except TimeoutError:
                continue
            except Exception:
                return

    creds, _ = _creds()
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = PolymarketUsWebSocketAdapter(
            outcome_ids=["a:LONG", "b:LONG"],
            creds=creds,
            url=f"ws://127.0.0.1:{port}",
            backstop_after_s=0,
            priority_slugs=set,  # nothing is in-play
        )
        import arbys.adapters.polymarket_us_ws as mod

        old_sweep, old_after = mod.REPAIR_SWEEP_S, mod.REPAIR_AFTER_S
        mod.REPAIR_SWEEP_S, mod.REPAIR_AFTER_S = 0.05, 0.05
        try:
            seen = 0

            async def drain():
                nonlocal seen
                async for _q in adapter.stream_quotes():
                    seen += 1

            task = asyncio.create_task(drain())
            await _wait_for(lambda: seen >= 4, timeout_s=3.0)
            await asyncio.sleep(0.5)  # several repair sweeps would have fired
        finally:
            mod.REPAIR_SWEEP_S, mod.REPAIR_AFTER_S = old_sweep, old_after
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await adapter.close()

    assert _subs(got)[1:] == [], (
        f"quiet pre-game markets were re-subscribed: {_subs(got)[1:]}"
    )
