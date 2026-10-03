"""Venue settlement readers: a side's payout, or None when not final.

The payloads are trimmed copies of what both venues returned on 2026-10-03
for Gaubas v Wendelken (2026-08-27), which Wendelken won, and for an open
market on each.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from arbys.adapters import settlement
from arbys.adapters.settlement import (
    fetch_kalshi_settlement,
    fetch_polymarket_us_settlement,
)

D = Decimal


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    async def instant(_s: float) -> None:
        return None

    monkeypatch.setattr(settlement, "_sleep", instant)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _kalshi(status: str, result: str):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/markets/KXATPMATCH-26AUG27GAUWEN-GAU")
        return httpx.Response(200, json={"market": {"status": status, "result": result}})

    return handler


async def test_kalshi_finalized_no_pays_the_no_side():
    async with _client(_kalshi("finalized", "no")) as c:
        assert await fetch_kalshi_settlement(c, "KXATPMATCH-26AUG27GAUWEN-GAU:YES") == D("0")
        assert await fetch_kalshi_settlement(c, "KXATPMATCH-26AUG27GAUWEN-GAU:NO") == D("1")


async def test_kalshi_finalized_yes_pays_the_yes_side():
    async with _client(_kalshi("settled", "yes")) as c:
        assert await fetch_kalshi_settlement(c, "KXATPMATCH-26AUG27GAUWEN-GAU:YES") == D("1")


async def test_kalshi_open_or_determined_market_is_not_final():
    async with _client(_kalshi("active", "")) as c:
        assert await fetch_kalshi_settlement(c, "KXATPMATCH-26AUG27GAUWEN-GAU:YES") is None
    # Result known but not yet paid out: wait for `finalized`.
    async with _client(_kalshi("determined", "yes")) as c:
        assert await fetch_kalshi_settlement(c, "KXATPMATCH-26AUG27GAUWEN-GAU:YES") is None


async def test_kalshi_tie_settles_at_its_scalar_value():
    # NFL preseason, 2026-08-28: expiration_value "Tie".
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "market": {
                    "status": "finalized",
                    "result": "scalar",
                    "settlement_value_dollars": "0.5000",
                }
            },
        )

    async with _client(handler) as c:
        assert await fetch_kalshi_settlement(c, "KXNFLGAME-26AUG28SEAKC-SEA:YES") == D("0.5")
        assert await fetch_kalshi_settlement(c, "KXNFLGAME-26AUG28SEAKC-SEA:NO") == D("0.5")


async def test_a_429_is_waited_out_and_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "10"}, text="<html>")
        return httpx.Response(200, json={"settlement": 1})

    async with _client(handler) as c:
        got = await fetch_polymarket_us_settlement(c, "aec-mlb-cin-chc-2026-08-28:LONG")
    assert got == D("1")
    assert len(calls) == 3


async def test_kalshi_void_result_is_left_open():
    async with _client(_kalshi("finalized", "void")) as c:
        assert await fetch_kalshi_settlement(c, "KXATPMATCH-26AUG27GAUWEN-GAU:YES") is None


async def test_kalshi_errors_read_as_unknown():
    async with _client(lambda r: httpx.Response(429)) as c:
        assert await fetch_kalshi_settlement(c, "KXATPMATCH-26AUG27GAUWEN-GAU:YES") is None

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with _client(boom) as c:
        assert await fetch_kalshi_settlement(c, "KXATPMATCH-26AUG27GAUWEN-GAU:YES") is None


def _pm(status: int, body: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/markets/aec-atp-vilgau-harwen-2026-08-27/settlement"
        return httpx.Response(status, json=body)

    return handler


async def test_polymarket_settlement_is_the_long_side_and_short_is_its_complement():
    async with _client(_pm(200, {"settlement": 0})) as c:
        slug = "aec-atp-vilgau-harwen-2026-08-27"
        assert await fetch_polymarket_us_settlement(c, f"{slug}:LONG") == D("0")
        assert await fetch_polymarket_us_settlement(c, f"{slug}:SHORT") == D("1")


async def test_polymarket_fractional_walkover_settlement_is_honoured():
    async with _client(_pm(200, {"settlement": 0.53})) as c:
        slug = "aec-atp-vilgau-harwen-2026-08-27"
        assert await fetch_polymarket_us_settlement(c, f"{slug}:LONG") == D("0.53")
        assert await fetch_polymarket_us_settlement(c, f"{slug}:SHORT") == D("0.47")


async def test_polymarket_unsettled_market_answers_404():
    body = {"code": 5, "message": "Settlement not found for market x", "details": []}
    async with _client(_pm(404, body)) as c:
        assert (
            await fetch_polymarket_us_settlement(c, "aec-atp-vilgau-harwen-2026-08-27:LONG")
            is None
        )


async def test_polymarket_out_of_range_settlement_is_refused():
    async with _client(_pm(200, {"settlement": 2})) as c:
        assert (
            await fetch_polymarket_us_settlement(c, "aec-atp-vilgau-harwen-2026-08-27:LONG")
            is None
        )


async def test_unrecognised_outcome_ids_are_not_looked_up():
    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    async with _client(never) as c:
        assert await fetch_kalshi_settlement(c, "TICKER:LONG") is None
        assert await fetch_polymarket_us_settlement(c, "slug:YES") is None
        assert await fetch_polymarket_us_settlement(c, "no-side-at-all") is None
