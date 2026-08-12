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
    """Prices come straight from /bbo. Sizes do not exist on this endpoint -
    bidDepth/askDepth count price levels, not contracts - so they read 0,
    meaning unknown. See test_bbo_reports_unknown_size_not_the_depth_counter."""
    quotes = {q.outcome_id: q for q in quotes_from_bbo("slug1", BBO["marketData"])}
    long_q = quotes["slug1:LONG"]
    assert long_q.bid == Decimal("0.4500")
    assert long_q.ask == Decimal("0.4550")
    assert long_q.bid_size == Decimal("0")
    assert long_q.ask_size == Decimal("0")


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
    # /bbo carries no sizes; the size swap is asserted against a real ladder
    # in test_quotes_from_levels_uses_real_top_of_book_quantities.
    assert short_q.bid_size == Decimal("0")
    assert short_q.ask_size == Decimal("0")


def test_long_and_short_asks_sum_to_more_than_one():
    """Sanity invariant: you can never buy both sides for under a dollar on
    one venue. If this fails, the inversion is wrong."""
    quotes = quotes_from_bbo("slug1", BBO["marketData"])
    assert sum(q.ask for q in quotes) > Decimal("1")


def test_book_with_neither_side_yields_nothing():
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
    assert quotes["s:LONG"].bid == Decimal("0.0050")
    assert quotes["s:LONG"].bid_size == Decimal("0")


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
    assert short_q.bid == Decimal("0.0300")
    assert short_q.ask == Decimal("0.0350")
    assert short_q.bid_size == Decimal("1234.5")
    assert short_q.ask_size == Decimal("287926.98")


def test_quotes_from_levels_handles_an_empty_bid_side():
    from arbys.adapters.polymarket_us import quotes_from_levels

    quotes = {
        q.outcome_id: q
        for q in quotes_from_levels("s", [], [{"px": {"value": "0.0050"}, "qty": "419882.67"}])
    }
    assert quotes["s:LONG"].ask_size == Decimal("419882.67")
    assert quotes["s:LONG"].bid_size == Decimal("0")
