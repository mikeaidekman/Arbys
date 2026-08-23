"""End-to-end smoke test exercising the FastAPI app in-process."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arbys.backend import state as state_module
from arbys.backend.app import create_app
from arbys.db import repositories as repo
from arbys.db import session as db_session
from arbys.db.session import create_all, session_scope
from arbys.shared.types import EventGroup, EventGroupLeg


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
                    {"outcome_id": "poly-yes", "venue_id": "polymarket_us", "is_yes_side": True},
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
                    {"outcome_id": "p-yes", "venue_id": "polymarket_us", "is_yes_side": True},
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


async def test_event_group_start_time_round_trips_in_db():
    """start_time must survive the DB round trip and later re-registration."""
    start = datetime(2026, 8, 10, 23, 7, tzinfo=UTC)
    group = EventGroup(
        id="mlb-BOS-TOR-2026-08-10",
        title="Boston vs Toronto",
        start_time=start,
        legs=(
            EventGroupLeg(outcome_id="p-bos", venue_id="polymarket_us", is_yes_side=True),
            EventGroupLeg(outcome_id="k-tor", venue_id="kalshi", is_yes_side=False),
        ),
    )

    await create_all()
    async with session_scope() as session:
        await repo.ensure_venue(session, "polymarket_us", name="Polymarket US", kind="exchange")
        await repo.ensure_venue(session, "kalshi", name="Kalshi", kind="exchange")
        await repo.upsert_event_group(session, group)

    async with session_scope() as session:
        stored = next(g for g in await repo.list_event_groups(session) if g.id == group.id)
    assert stored.start_time is not None
    # SQLite drops the tzinfo on read; compare the instant, not the object.
    got = stored.start_time
    if got.tzinfo is None:
        got = got.replace(tzinfo=UTC)
    assert got == start

    # A later discovery pass whose venue reported no time must not erase it.
    async with session_scope() as session:
        await repo.upsert_event_group(
            session, EventGroup(id=group.id, title=group.title, legs=group.legs)
        )
    async with session_scope() as session:
        stored = next(g for g in await repo.list_event_groups(session) if g.id == group.id)
    assert stored.start_time is not None, "re-register wiped a known start_time"


def _register(client, group_id, poly_outcome, kalshi_outcome):
    r = client.post(
        "/event-groups",
        json={
            "id": group_id,
            "title": f"Group {group_id}",
            "legs": [
                {"outcome_id": poly_outcome, "venue_id": "polymarket_us", "is_yes_side": True},
                {"outcome_id": kalshi_outcome, "venue_id": "kalshi", "is_yes_side": False},
            ],
        },
    )
    assert r.status_code == 201


def test_opportunities_are_live_not_a_log():
    """/opportunities must show what is executable now, not everything seen.

    It used to be an append-only deque: re-detections piled up and nothing was
    removed when an edge died, so one busy market could fill the buffer and
    evict every other group's opportunity.
    """
    with TestClient(create_app()) as client:
        _register(client, "eg-live", "l-yes", "l-no")

        def quote(oid, px):
            assert client.post(
                "/quotes", json={"outcome_id": oid, "bid": px, "ask": px}
            ).status_code == 204

        quote("l-yes", "0.40")
        quote("l-no", "0.50")
        assert len(client.get("/opportunities?limit=500").json()) >= 1

        # Re-push identical quotes many times: the same edge, not new ones.
        for _ in range(25):
            quote("l-yes", "0.40")
            quote("l-no", "0.50")
        opps = client.get("/opportunities?limit=500").json()
        assert len(opps) == 1, f"duplicates accumulated: {len(opps)}"

        # Kill the edge — the entry must disappear, not linger.
        quote("l-no", "0.70")
        assert client.get("/opportunities?limit=500").json() == []

        # And come back when the edge returns.
        quote("l-no", "0.50")
        assert len(client.get("/opportunities?limit=500").json()) == 1


def test_busy_group_does_not_evict_another_groups_opportunity():
    """A market that re-ticks constantly must not crowd out other games.

    The root cause — re-detections accumulating instead of replacing — is
    pinned by test_opportunities_are_live_not_a_log, which fails on the old
    implementation after 25 re-pushes. This covers the user-visible
    consequence: with the set keyed by group, entries are bounded by the
    number of groups, so churn on one cannot displace another.
    """
    with TestClient(create_app()) as client:
        _register(client, "eg-busy", "b-yes", "b-no")
        _register(client, "eg-quiet", "q-yes", "q-no")

        def quote(oid, px):
            client.post("/quotes", json={"outcome_id": oid, "bid": px, "ask": px})

        quote("q-yes", "0.30")
        quote("q-no", "0.40")  # quiet group has a standing edge
        for i in range(40):
            # Busy group re-detects on every tick.
            quote("b-yes", "0.10")
            quote("b-no", "0.80" if i % 2 else "0.81")

        opps = client.get("/opportunities?limit=500").json()
        groups = {o["event_group_id"] for o in opps}
        assert "eg-quiet" in groups, "quiet group was evicted by a busy one"
        # One entry per group with an edge — never one per detection.
        assert len(opps) == len(groups) == 2, opps


def test_execute_prices_against_live_quotes_not_the_recorded_opportunity():
    """Filling must use current quotes; a dead edge must be refused."""
    with TestClient(create_app()) as client:
        _register(client, "eg-move", "m-yes", "m-no")

        def quote(oid, px):
            client.post("/quotes", json={"outcome_id": oid, "bid": px, "ask": px})

        quote("m-yes", "0.40")
        quote("m-no", "0.50")
        assert len(client.get("/opportunities").json()) == 1

        # Market moves against the edge before we execute.
        quote("m-no", "0.75")

        r = client.post(
            "/paper/execute",
            json={"event_group_id": "eg-move", "outcome_ids": ["m-yes", "m-no"]},
        )
        assert r.status_code == 409, r.text
        assert "live quotes" in r.json()["detail"]
        assert client.get("/paper/default").json()["positions"] == {}

        # Edge returns at a *different* price — the fill must use the new one.
        quote("m-no", "0.45")
        r = client.post(
            "/paper/execute",
            json={"event_group_id": "eg-move", "outcome_ids": ["m-yes", "m-no"]},
        )
        assert r.status_code == 200, r.text
        orders = client.get("/paper/default/orders").json()
        filled = {o["outcome_id"]: o for o in orders if o["status"] == "filled"}
        assert Decimal(filled["m-no"]["limit_price"]) == Decimal("0.45"), filled["m-no"]


def test_repeat_fills_stop_at_the_position_cap(monkeypatch):
    """The same edge stays published, so repeat clicks must not stack forever."""
    monkeypatch.setenv("ARBYS_MAX_OUTCOME_QTY", "250")
    with TestClient(create_app()) as client:
        _register(client, "eg-cap", "c-yes", "c-no")
        # 100 contracts resting on each ask, which is thinner than the stake
        # budget allows, so every ticket is exactly 100 units.
        for oid, px in (("c-yes", "0.40"), ("c-no", "0.50")):
            client.post(
                "/quotes",
                json={"outcome_id": oid, "bid": px, "ask": px, "ask_size": "100"},
            )

        # Each ticket is 100 units, so the third would exceed a 250 cap.
        assert client.post(
            "/paper/execute",
            json={"event_group_id": "eg-cap", "outcome_ids": ["c-yes", "c-no"]},
        ).status_code == 200
        assert client.post(
            "/paper/execute",
            json={"event_group_id": "eg-cap", "outcome_ids": ["c-yes", "c-no"]},
        ).status_code == 200

        r = client.post(
            "/paper/execute",
            json={"event_group_id": "eg-cap", "outcome_ids": ["c-yes", "c-no"]},
        )
        assert r.status_code == 409, r.text
        assert "position cap" in r.json()["detail"]

        positions = client.get("/paper/default").json()["positions"]
        assert Decimal(positions["c-yes"]) == Decimal("200")
        assert Decimal(positions["c-no"]) == Decimal("200")


def test_position_cap_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ARBYS_MAX_OUTCOME_QTY", "0")
    with TestClient(create_app()) as client:
        _register(client, "eg-nocap", "n-yes", "n-no")
        # Depth of 100 on each ask pins every ticket at 100 units.
        for oid, px in (("n-yes", "0.10"), ("n-no", "0.20")):
            client.post(
                "/quotes",
                json={"outcome_id": oid, "bid": px, "ask": px, "ask_size": "100"},
            )
        for _ in range(8):
            assert client.post(
                "/paper/execute",
                json={"event_group_id": "eg-nocap", "outcome_ids": ["n-yes", "n-no"]},
            ).status_code == 200
        positions = client.get("/paper/default").json()["positions"]
        assert Decimal(positions["n-yes"]) == Decimal("800")


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
        # 409 rather than 404: the group exists, the edge just isn't there.
        r = client.post(
            "/paper/execute",
            json={"event_group_id": "eg-alpha", "outcome_ids": ["a-yes", "nope"]},
        )
        assert r.status_code == 409

        # Unknown group entirely.
        r = client.post("/paper/execute", json={"event_group_id": "eg-missing"})
        assert r.status_code == 409

        # Neither attempt may fill anything.
        assert client.get("/paper/default").json()["positions"] == {}


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
                    {"outcome_id": "p-yes", "venue_id": "polymarket_us", "is_yes_side": True},
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


def test_monitored_reports_net_figures():
    """/monitored's net fields describe the best *tradeable pair*, ranked by
    highest net_edge * qty -- not the cheapest unit cost, and not derived from
    best_yes_ask/best_no_ask (which can both come from the same venue).

    Four legs give four (yes, no) combinations. K-Yes(0.32) + P-No(0.66) is
    the pair with the least-negative net profit once fees and the p:SHORT
    depth of 9 are applied -- see task-7-report.md for the full arithmetic.
    """
    with TestClient(create_app()) as client:
        r = client.post(
            "/event-groups",
            json={
                "id": "eg-net",
                "title": "A vs B",
                "legs": [
                    {"outcome_id": "k:YES", "venue_id": "kalshi", "is_yes_side": True},
                    {"outcome_id": "k:NO", "venue_id": "kalshi", "is_yes_side": False},
                    {"outcome_id": "p:LONG", "venue_id": "polymarket_us", "is_yes_side": True},
                    {"outcome_id": "p:SHORT", "venue_id": "polymarket_us", "is_yes_side": False},
                ],
            },
        )
        assert r.status_code == 201

        for oid, bid, ask, size in [
            ("k:YES", "0.30", "0.32", "412"),
            ("k:NO", "0.66", "0.69", "1156"),
            ("p:LONG", "0.33", "0.35", "2616"),
            ("p:SHORT", "0.62", "0.66", "9"),
        ]:
            assert client.post(
                "/quotes",
                json={"outcome_id": oid, "bid": bid, "ask": ask, "ask_size": size},
            ).status_code == 204

        r = client.get("/monitored")
        assert r.status_code == 200
        group = next(g for g in r.json() if g["id"] == "eg-net")

        # Best pair is K-Yes 0.32 + P-No 0.66 = 0.98 gross, negative after fees.
        assert Decimal(group["net_edge"]) < 0
        assert group["best_pair_yes_outcome_id"] == "k:YES"
        assert group["best_pair_no_outcome_id"] == "p:SHORT"
        # Thinnest leg of that pair is p:SHORT at 9.
        assert Decimal(group["max_tradeable_qty"]) == Decimal("9")
        assert Decimal(group["net_max_profit"]) == (
            Decimal(group["net_edge"]) * Decimal("9")
        )
        assert Decimal(group["capital_required"]) > 0


def test_monitored_net_fields_null_without_quotes():
    with TestClient(create_app()) as client:
        r = client.post(
            "/event-groups",
            json={
                "id": "eg-empty",
                "title": "C vs D",
                "legs": [
                    {"outcome_id": "k2:YES", "venue_id": "kalshi", "is_yes_side": True},
                    {"outcome_id": "p2:SHORT", "venue_id": "polymarket_us", "is_yes_side": False},
                ],
            },
        )
        assert r.status_code == 201

        r = client.get("/monitored")
        assert r.status_code == 200
        group = next(g for g in r.json() if g["id"] == "eg-empty")
        assert group["net_edge"] is None
        assert group["max_tradeable_qty"] is None
        assert group["net_max_profit"] is None
        assert group["capital_required"] is None
        assert group["best_pair_yes_outcome_id"] is None
        assert group["best_pair_no_outcome_id"] is None

