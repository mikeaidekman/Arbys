"""End-to-end smoke test exercising the FastAPI app in-process.

Uses manual quote push (no live adapters) so the test is hermetic.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from arbys.backend import state as state_module
from arbys.backend.app import create_app


@pytest.fixture(autouse=True)
def _reset_state():
    state_module.STATE = None
    yield
    state_module.STATE = None


def test_end_to_end_scan_and_paper_execute():
    client = TestClient(create_app())

    # Register an event group with two legs on two venues.
    r = client.post(
        "/event-groups",
        json={
            "id": "eg-1",
            "title": "Will X happen?",
            "legs": [
                {"outcome_id": "poly-yes", "venue_id": "polymarket", "is_yes_side": True},
                {"outcome_id": "kals-no", "venue_id": "kalshi", "is_yes_side": False},
            ],
        },
    )
    assert r.status_code == 201

    # Push quotes that create an arb: 0.40 (poly YES) + 0.50 (kals NO) = 0.90 < 1.
    assert client.post("/quotes", json={"outcome_id": "poly-yes", "bid": "0.40", "ask": "0.40"}).status_code == 204
    assert client.post("/quotes", json={"outcome_id": "kals-no", "bid": "0.50", "ask": "0.50"}).status_code == 204

    # An opportunity should have been detected.
    r = client.get("/opportunities")
    opps = r.json()
    assert len(opps) >= 1
    assert opps[0]["event_group_id"] == "eg-1"

    # Paper-execute the top opportunity.
    r = client.post("/paper/execute", json={"opportunity_index": 0})
    assert r.status_code == 200
    assert len(r.json()) == 2  # two orders

    # Portfolio should show positions on both venues.
    r = client.get("/paper/default")
    body = r.json()
    assert "poly-yes" in body["positions"]
    assert "kals-no" in body["positions"]
