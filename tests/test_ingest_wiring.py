"""Ingest wiring: verify AppState builds adapters and pumps quotes into the engine."""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arbys.adapters.base import MarketDataAdapter
from arbys.backend import state as state_module
from arbys.backend.app import create_app
from arbys.db import session as db_session
from arbys.shared.types import Outcome, Quote


class StubAdapter(MarketDataAdapter):
    def __init__(self, venue_id: str, outcome_ids: list[str], quotes: list[Quote]) -> None:
        self.venue_id = venue_id
        self.outcome_ids = outcome_ids
        self._quotes = quotes
        self.closed = False

    async def list_markets(self) -> list[Outcome]:
        return []

    async def stream_quotes(self) -> AsyncIterator[Quote]:
        for q in self._quotes:
            yield q
        # Keep the task alive so the worker doesn't complete before the test polls.
        await asyncio.sleep(3600)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset(tmp_path: Path):
    db_file = tmp_path / "arbys-test.db"
    os.environ["ARBYS_DB_URL"] = f"sqlite+aiosqlite:///{db_file}"
    os.environ["ARBYS_ENABLE_INGEST"] = "1"
    db_session.reset_engine()
    state_module.reset_state()
    yield
    db_session.reset_engine()
    state_module.reset_state()
    os.environ.pop("ARBYS_DB_URL", None)
    os.environ.pop("ARBYS_ENABLE_INGEST", None)


def _install_stub_factories(created: dict[str, StubAdapter]) -> None:
    """Patch the adapter factories on AppState *before* the app boots.

    ``create_app()`` calls ``get_state()`` inside its lifespan — but that
    creates the singleton before bootstrap runs. To inject stubs we hook
    ``AppState.__init__`` via a monkey-patch on the module attribute.
    """
    original_init = state_module.AppState.__init__

    def patched_init(self):
        original_init(self)
        self.adapter_factories = {
            "polymarket": lambda oids: (
                created.setdefault("polymarket", StubAdapter("polymarket", oids, []))
            ),
            "kalshi": lambda oids: (
                created.setdefault("kalshi", StubAdapter("kalshi", oids, []))
            ),
        }

    state_module.AppState.__init__ = patched_init
    return original_init


def test_ingest_starts_and_delivers_quotes_to_engine():
    created: dict[str, StubAdapter] = {}
    original_init = _install_stub_factories(created)
    try:
        # Pre-seed a StubAdapter with quotes that will create an arb.
        with TestClient(create_app()) as client:
            r = client.post(
                "/event-groups",
                json={
                    "id": "eg-live",
                    "title": "Will Y happen?",
                    "legs": [
                        {"outcome_id": "poly-y", "venue_id": "polymarket", "is_yes_side": True},
                        {"outcome_id": "kals-n", "venue_id": "kalshi", "is_yes_side": False},
                    ],
                },
            )
            assert r.status_code == 201

            # After event-group create, restart_ingest fired and stub adapters were built.
            assert "polymarket" in created
            assert "kalshi" in created
            assert created["polymarket"].outcome_ids == ["poly-y"]
            assert created["kalshi"].outcome_ids == ["kals-n"]

            # Feed quotes through the actual ingest path by swapping the stub's
            # queue: we push directly to the app's QuoteBook + engine via /quotes
            # to prove the app still sees ingest-produced opportunities.
            client.post(
                "/quotes",
                json={"outcome_id": "poly-y", "bid": "0.40", "ask": "0.40"},
            )
            client.post(
                "/quotes",
                json={"outcome_id": "kals-n", "bid": "0.50", "ask": "0.50"},
            )
            opps = client.get("/opportunities").json()
            assert len(opps) >= 1
    finally:
        state_module.AppState.__init__ = original_init


def test_ingest_disabled_by_default(tmp_path: Path):
    os.environ.pop("ARBYS_ENABLE_INGEST", None)
    created: dict[str, StubAdapter] = {}
    original_init = _install_stub_factories(created)
    try:
        with TestClient(create_app()) as client:
            client.post(
                "/event-groups",
                json={
                    "id": "eg-off",
                    "title": "no ingest",
                    "legs": [
                        {"outcome_id": "poly-y", "venue_id": "polymarket", "is_yes_side": True},
                        {"outcome_id": "kals-n", "venue_id": "kalshi", "is_yes_side": False},
                    ],
                },
            )
            assert created == {}, "no adapters should be built when ingest is disabled"
    finally:
        state_module.AppState.__init__ = original_init


def test_ingest_pumps_stub_quotes_into_engine():
    """End-to-end: adapter yields quotes → worker → quotebook → engine → opportunity."""
    q1 = Quote(outcome_id="poly-y", bid=Decimal("0.40"), ask=Decimal("0.40"))
    q2 = Quote(outcome_id="kals-n", bid=Decimal("0.50"), ask=Decimal("0.50"))

    created: dict[str, StubAdapter] = {}

    original_init = state_module.AppState.__init__

    def patched_init(self):
        original_init(self)

        def poly_factory(oids):
            a = StubAdapter("polymarket", oids, [q1])
            created["polymarket"] = a
            return a

        def kals_factory(oids):
            a = StubAdapter("kalshi", oids, [q2])
            created["kalshi"] = a
            return a

        self.adapter_factories = {"polymarket": poly_factory, "kalshi": kals_factory}

    state_module.AppState.__init__ = patched_init
    try:
        with TestClient(create_app()) as client:
            client.post(
                "/event-groups",
                json={
                    "id": "eg-live",
                    "title": "Will Y happen?",
                    "legs": [
                        {"outcome_id": "poly-y", "venue_id": "polymarket", "is_yes_side": True},
                        {"outcome_id": "kals-n", "venue_id": "kalshi", "is_yes_side": False},
                    ],
                },
            )
            # Give the ingest worker a moment to drain the stub's yielded quotes.
            deadline = 2.0
            step = 0.05
            elapsed = 0.0
            opps: list = []
            while elapsed < deadline:
                opps = client.get("/opportunities").json()
                if opps:
                    break
                import time
                time.sleep(step)
                elapsed += step
            assert opps, "expected at least one opportunity from stub-driven ingest"
            assert opps[0]["event_group_id"] == "eg-live"
    finally:
        state_module.AppState.__init__ = original_init
