from decimal import Decimal

import httpx
import pytest

from arbys.adapters.draftkings import DraftKingsAdapter, draftkings_enabled


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def test_draftkings_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ARBYS_ENABLE_DRAFTKINGS", raising=False)
    assert draftkings_enabled() is False


def test_draftkings_enabled_env(monkeypatch):
    monkeypatch.setenv("ARBYS_ENABLE_DRAFTKINGS", "1")
    assert draftkings_enabled() is True


@pytest.mark.asyncio
async def test_list_markets_emits_outcomes():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "events": [
                    {
                        "id": "evt1",
                        "name": "A vs B",
                        "displayGroups": [
                            {
                                "markets": [
                                    {
                                        "id": "m1",
                                        "outcomes": [
                                            {"label": "A", "oddsAmerican": -200},
                                            {"label": "B", "oddsAmerican": 150},
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                ]
            },
        )

    client = _mock_client(handler)
    a = DraftKingsAdapter(league_ids=["42"], http_client=client)
    outcomes = await a.list_markets()
    assert [o.label for o in outcomes] == ["A vs B: A", "A vs B: B"]
    assert outcomes[0].id == "dks:evt1:m1:0"
    await a.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_stream_quotes_converts_american_to_prob():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            200,
            json={
                "events": [
                    {
                        "id": "evt1",
                        "name": "A vs B",
                        "displayGroups": [
                            {
                                "markets": [
                                    {
                                        "id": "m1",
                                        "outcomes": [
                                            {"label": "A", "oddsAmerican": -200},
                                            {"label": "B", "oddsAmerican": 150},
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                ]
            },
        )

    client = _mock_client(handler)
    a = DraftKingsAdapter(league_ids=["42"], poll_interval_s=0, http_client=client)

    seen = []
    async for q in a.stream_quotes():
        seen.append(q)
        if len(seen) >= 2:
            break

    assert seen[0].ask == Decimal("2") / Decimal("3")  # -200 -> 2/3
    assert seen[1].ask == Decimal("100") / Decimal("250")  # +150 -> 100/250 = 0.4
    await a.close()
    await client.aclose()
