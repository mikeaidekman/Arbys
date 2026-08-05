from decimal import Decimal

import httpx
import pytest

from arbys.adapters.kalshi import KalshiAdapter
from arbys.shared.types import Side


def _mock_client(handler):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=5.0,
        base_url="https://api.elections.kalshi.com/trade-api/v2",
    )


@pytest.mark.asyncio
async def test_list_markets_produces_yes_and_no_outcomes():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "markets": [
                    {"ticker": "PRES-2024-DJT", "title": "Trump wins", "subtitle": ""}
                ]
            },
        )

    client = _mock_client(handler)
    a = KalshiAdapter(http_client=client)
    outcomes = await a.list_markets()
    assert len(outcomes) == 2
    sides = {o.side for o in outcomes}
    assert sides == {Side.YES, Side.NO}
    yes = next(o for o in outcomes if o.side is Side.YES)
    assert yes.id == "PRES-2024-DJT:YES"
    assert yes.market_id == "PRES-2024-DJT"
    await a.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_quote_yes_side_from_orderbook():
    """Legacy orderbook (cents) schema."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "orderbook": {
                    "yes": [[45, 10]],  # best YES bid = 45c
                    "no": [[48, 5]],    # best NO bid = 48c -> YES ask = 100-48 = 52c
                }
            },
        )

    client = _mock_client(handler)
    a = KalshiAdapter(http_client=client)
    q = await a._fetch_quote("TKR:YES")
    assert q is not None
    assert q.bid == Decimal("0.45")
    assert q.ask == Decimal("0.52")
    await a.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_quote_yes_side_from_orderbook_fp():
    """Current (2025+) orderbook_fp (dollar-string) schema."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "orderbook_fp": {
                    "yes_dollars": [["0.45", "10.00"]],
                    "no_dollars": [["0.48", "5.00"]],
                }
            },
        )

    client = _mock_client(handler)
    a = KalshiAdapter(http_client=client)
    q = await a._fetch_quote("TKR:YES")
    assert q is not None
    assert q.bid == Decimal("0.45")
    assert q.ask == Decimal("0.52")
    await a.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_quote_no_side_from_orderbook_fp():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "orderbook_fp": {
                    "yes_dollars": [["0.45", "10.00"]],
                    "no_dollars": [["0.48", "5.00"]],
                }
            },
        )

    client = _mock_client(handler)
    a = KalshiAdapter(http_client=client)
    q = await a._fetch_quote("TKR:NO")
    assert q is not None
    assert q.bid == Decimal("0.48")
    assert q.ask == Decimal("0.55")
    await a.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_quote_orderbook_fp_takes_highest_of_multiple_levels():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "orderbook_fp": {
                    "yes_dollars": [["0.30", "50.00"], ["0.45", "10.00"], ["0.40", "20.00"]],
                    "no_dollars": [["0.48", "5.00"], ["0.30", "40.00"]],
                }
            },
        )

    client = _mock_client(handler)
    a = KalshiAdapter(http_client=client)
    q = await a._fetch_quote("TKR:YES")
    assert q is not None
    assert q.bid == Decimal("0.45")
    assert q.ask == Decimal("0.52")
    await a.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_quote_orderbook_fp_one_sided():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"orderbook_fp": {"yes_dollars": [], "no_dollars": [["0.7340", "408.00"]]}},
        )

    client = _mock_client(handler)
    a = KalshiAdapter(http_client=client)
    q = await a._fetch_quote("TKR:YES")
    assert q is not None
    assert q.bid == Decimal("0")
    assert q.ask == Decimal("0.2660")
    await a.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_quote_missing_orderbook_returns_edges():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"orderbook": {}})

    client = _mock_client(handler)
    a = KalshiAdapter(http_client=client)
    q = await a._fetch_quote("TKR:YES")
    assert q is not None
    assert q.bid == Decimal("0")
    assert q.ask == Decimal("1")
    await a.close()
    await client.aclose()
