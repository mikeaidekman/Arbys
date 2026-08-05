from decimal import Decimal

import httpx
import pytest

from arbys.adapters.polymarket import PolymarketAdapter


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


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
