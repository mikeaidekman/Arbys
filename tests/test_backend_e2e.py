"""End-to-end smoke test exercising the FastAPI app in-process."""

from __future__ import annotations

import os
from decimal import Decimal
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


def _register(client, group_id, poly_outcome, kalshi_outcome):
    r = client.post(
        "/event-groups",
        json={
            "id": group_id,
            "title": f"Group {group_id}",
            "legs": [
                {"outcome_id": poly_outcome, "venue_id": "polymarket", "is_yes_side": True},
                {"outcome_id": kalshi_outcome, "venue_id": "kalshi", "is_yes_side": False},
            ],
        },
    )
    assert r.status_code == 201


def test_execute_by_event_group_picks_that_group(tmp_path):
    """Executing by descriptor must fill the named group, not a list position.

    The frontend merges websocket-pushed opportunities ahead of REST ones, so
    its array order differs from the server's. Passing a position from that
    array as opportunity_index can fill a different arb than the one shown.
    """
    with TestClient(create_app()) as client:
        _register(client, "eg-alpha", "a-yes", "a-no")
        _register(client, "eg-beta", "b-yes", "b-no")

        # Both groups carry a live arb; beta is cheaper so ordering differs
        # from registration order.
        for oid, px in (("a-yes", "0.40"), ("a-no", "0.50"),
                        ("b-yes", "0.10"), ("b-no", "0.30")):
            assert client.post(
                "/quotes", json={"outcome_id": oid, "bid": px, "ask": px}
            ).status_code == 204

        opps = client.get("/opportunities").json()
        assert {o["event_group_id"] for o in opps} >= {"eg-alpha", "eg-beta"}

        r = client.post(
            "/paper/execute",
            json={"event_group_id": "eg-beta", "outcome_ids": ["b-yes", "b-no"]},
        )
        assert r.status_code == 200, r.text
        assert len(r.json()) == 2

        positions = client.get("/paper/default").json()["positions"]
        assert "b-yes" in positions and "b-no" in positions
        # The other group must be untouched regardless of list ordering.
        assert "a-yes" not in positions and "a-no" not in positions


def test_execute_by_event_group_rejects_unknown_descriptor(tmp_path):
    with TestClient(create_app()) as client:
        _register(client, "eg-alpha", "a-yes", "a-no")
        for oid, px in (("a-yes", "0.40"), ("a-no", "0.50")):
            assert client.post(
                "/quotes", json={"outcome_id": oid, "bid": px, "ask": px}
            ).status_code == 204

        # Right group, legs that are not part of any live opportunity.
        r = client.post(
            "/paper/execute",
            json={"event_group_id": "eg-alpha", "outcome_ids": ["a-yes", "nope"]},
        )
        assert r.status_code == 404

        # Unknown group entirely.
        r = client.post("/paper/execute", json={"event_group_id": "eg-missing"})
        assert r.status_code == 404


def test_open_positions_hydrate_once_per_venue(tmp_path):
    """Regression: an open position must not fan out to every paper broker.

    `paper_position` is keyed on (account_id, venue_id, outcome_id). Before
    venue_id existed, restart hydration handed each row to all three brokers,
    so GET /paper summed the same position once per broker and reported qty
    and realized PnL inflated by the broker count.
    """
    with TestClient(create_app()) as client:
        r = client.post(
            "/event-groups",
            json={
                "id": "eg-hydrate",
                "title": "Hydration check",
                "legs": [
                    {"outcome_id": "p-yes", "venue_id": "polymarket", "is_yes_side": True},
                    {"outcome_id": "k-no", "venue_id": "kalshi", "is_yes_side": False},
                ],
            },
        )
        assert r.status_code == 201

        assert client.post(
            "/quotes", json={"outcome_id": "p-yes", "bid": "0.40", "ask": "0.40"}
        ).status_code == 204
        assert client.post(
            "/quotes", json={"outcome_id": "k-no", "bid": "0.50", "ask": "0.50"}
        ).status_code == 204

        assert client.post("/paper/execute", json={"opportunity_index": 0}).status_code == 200

        before = client.get("/paper/default").json()
        assert Decimal(before["positions"]["p-yes"]) > 0
        assert Decimal(before["positions"]["k-no"]) > 0

    # More than one broker must exist, or this test cannot detect fan-out.
    assert len(state_module.get_state().paper_brokers) > 1

    # Simulate restart: fresh in-memory state, same DB file.
    state_module.reset_state()
    db_session.reset_engine()

    with TestClient(create_app()) as client:
        after = client.get("/paper/default").json()

    # Compare as Decimal — the DB round-trip changes the string scale
    # ("100" -> "100.000000000000") without changing the value.
    def as_decimals(d: dict) -> dict:
        return {k: Decimal(v) for k, v in d.items()}

    assert as_decimals(after["positions"]) == as_decimals(before["positions"])
    assert as_decimals(after["realized_pnl"]) == as_decimals(before["realized_pnl"])

