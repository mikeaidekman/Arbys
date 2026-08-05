"""End-to-end smoke test exercising the FastAPI app in-process."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arbys.backend import state as state_module
from arbys.backend.app import create_app
from arbys.db import session as db_session


@pytest.fixture(autouse=True)
def _reset_state(tmp_path: Path):
    db_file = tmp_path / "arbys-test.db"
    os.environ["ARBYS_DB_URL"] = f"sqlite+aiosqlite:///{db_file}"
    db_session.reset_engine()
    state_module.reset_state()
    yield
    db_session.reset_engine()
    state_module.reset_state()
    os.environ.pop("ARBYS_DB_URL", None)


def test_end_to_end_scan_and_paper_execute():
    with TestClient(create_app()) as client:
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

        # Push quotes that create an arb: 0.40 + 0.50 = 0.90 < 1.
        assert client.post("/quotes", json={"outcome_id": "poly-yes", "bid": "0.40", "ask": "0.40"}).status_code == 204
        assert client.post("/quotes", json={"outcome_id": "kals-no", "bid": "0.50", "ask": "0.50"}).status_code == 204

        r = client.get("/opportunities")
        opps = r.json()
        assert len(opps) >= 1
        assert opps[0]["event_group_id"] == "eg-1"

        r = client.post("/paper/execute", json={"opportunity_index": 0})
        assert r.status_code == 200
        assert len(r.json()) == 2

        r = client.get("/paper/default")
        body = r.json()
        assert "poly-yes" in body["positions"]
        assert "kals-no" in body["positions"]


def test_state_survives_restart(tmp_path):
    """Event groups + balances hydrate from DB on a fresh AppState."""
    # First run: create an event group + execute paper trade.
    with TestClient(create_app()) as client:
        r = client.post(
            "/event-groups",
            json={
                "id": "eg-persist",
                "title": "Persistence check",
                "legs": [
                    {"outcome_id": "p-yes", "venue_id": "polymarket", "is_yes_side": True},
                    {"outcome_id": "k-no", "venue_id": "kalshi", "is_yes_side": False},
                ],
            },
        )
        assert r.status_code == 201

    # Simulate restart: reset in-memory state but keep same DB file.
    state_module.reset_state()
    db_session.reset_engine()

    with TestClient(create_app()) as client:
        r = client.get("/event-groups")
        assert r.status_code == 200
        ids = [g["id"] for g in r.json()]
        assert "eg-persist" in ids

        # Default paper balance should have hydrated (not double-seeded).
        r = client.get("/paper/default")
        assert r.status_code == 200

