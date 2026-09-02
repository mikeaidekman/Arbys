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
    # `dropped_write_stats()` is a process-wide counter, not per-engine — an
    # earlier file's retry tests (tests/db/test_write_reliability.py) leave it
    # non-zero, which would make test_health_reports_dropped_writes flaky
    # depending on run order rather than on anything this test does.
    db_session.reset_dropped_writes()
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
    # $250 of cost basis. Each ticket is 100 contracts at 0.40 + 0.50 = $90,
    # so two fit and the third would take the game to $270.
    monkeypatch.setenv("ARBYS_MAX_OUTCOME_STAKE", "250")
    with TestClient(create_app()) as client:
        _register(client, "eg-cap", "c-yes", "c-no")
        # 100 contracts resting on each ask, which is thinner than the stake
        # budget allows, so every ticket is exactly 100 units.
        for oid, px in (("c-yes", "0.40"), ("c-no", "0.50")):
            client.post(
                "/quotes",
                json={"outcome_id": oid, "bid": px, "ask": px, "ask_size": "100"},
            )

        # Each ticket commits $90, so the third would exceed the $250 cap.
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
    monkeypatch.setenv("ARBYS_MAX_OUTCOME_STAKE", "0")
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


def test_execute_by_event_group_picks_that_group(tmp_path, monkeypatch):
    """Executing by descriptor must fill the named group, not a list position.

    The frontend merges websocket-pushed opportunities ahead of REST ones, so
    its array order differs from the server's. Passing a position from that
    array as opportunity_index can fill a different arb than the one shown.
    """
    # Pinned rather than inherited: this test's worked numbers are
    # arithmetic on a $200 budget, and it exists to pin *ranking*, not
    # whatever the shipped default happens to be.
    monkeypatch.setenv("ARBYS_MAX_TICKET_STAKE", "200")
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


def test_execute_records_a_missed_ticket_when_the_edge_is_gone():
    """The endpoint keeps returning 409 — the UI's "failed" button is
    unchanged — but a row now exists for the attempt."""
    with TestClient(create_app()) as client:
        _register(client, "eg-miss", "p-yes-ms", "k-no-ms")
        client.post("/quotes", json={"outcome_id": "p-yes-ms", "bid": "0.60", "ask": "0.60"})
        client.post("/quotes", json={"outcome_id": "k-no-ms", "bid": "0.60", "ask": "0.60"})

        r = client.post(
            "/paper/execute",
            json={"event_group_id": "eg-miss", "outcome_ids": ["p-yes-ms", "k-no-ms"]},
        )
        assert r.status_code == 409

        page = client.get("/paper/default/tickets").json()
        assert page["total"] == 1
        assert page["next_cursor"] is None
        tickets = page["items"]
        assert len(tickets) == 1
        assert tickets[0]["status"] == "missed"
        assert tickets[0]["legs"] == []


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



def _register_four_leg_group(client, group_id: str, quotes: list[tuple[str, str, str, str]]):
    """Register a 4-leg (Kalshi YES/NO + Polymarket US LONG/SHORT) group.

    Four legs give four (yes, no) combinations, including the two same-venue
    ones -- which is what makes the ranking in `/monitored` non-trivial.
    """
    prefix = group_id
    legs = [
        {"outcome_id": f"{prefix}:k:YES", "venue_id": "kalshi", "is_yes_side": True},
        {"outcome_id": f"{prefix}:k:NO", "venue_id": "kalshi", "is_yes_side": False},
        {"outcome_id": f"{prefix}:p:LONG", "venue_id": "polymarket_us", "is_yes_side": True},
        {"outcome_id": f"{prefix}:p:SHORT", "venue_id": "polymarket_us", "is_yes_side": False},
    ]
    r = client.post(
        "/event-groups",
        json={"id": group_id, "title": "A vs B", "legs": legs},
    )
    assert r.status_code == 201
    for suffix, bid, ask, size in quotes:
        r = client.post(
            "/quotes",
            json={
                "outcome_id": f"{prefix}:{suffix}",
                "bid": bid,
                "ask": ask,
                "ask_size": size,
            },
        )
        assert r.status_code == 204
    r = client.get("/monitored")
    assert r.status_code == 200
    return next(g for g in r.json() if g["id"] == group_id)


def test_monitored_all_negative_pairs_rank_by_price_not_thinnest_book(monkeypatch):
    """With every pair net-negative, the best pair is the best *priced* one.

    That is the live-market regime, not an edge case: 0 of 175 monitored rows
    were net-positive when this was measured. Ranking by net_edge * qty
    inverts there -- the product is negative, so its maximum is the *thinnest*
    book. Arithmetic for these four quotes (Kalshi 0.07*p*(1-p), Polymarket US
    0.06*p*(1-p), $200 ticket cap, 0.01 tick):

        k:YES + k:NO     edge -0.053292  qty   3.00  profit  -0.1599  <- old
        k:YES + p:SHORT  edge -0.031200  qty 193.94  profit  -6.0509  <- new
        p:LONG + p:SHORT edge -0.079250  qty   3.00  profit  -0.2378
        p:LONG + k:NO    edge -0.101342  qty   3.00  profit  -0.3040

    Maximising profit picked the 3-deep same-venue pair priced 2.2c worse.
    """
    # Pinned rather than inherited: this test's worked numbers are
    # arithmetic on a $200 budget, and it exists to pin *ranking*, not
    # whatever the shipped default happens to be.
    monkeypatch.setenv("ARBYS_MAX_TICKET_STAKE", "200")
    with TestClient(create_app()) as client:
        group = _register_four_leg_group(
            client,
            "eg-neg",
            [
                ("k:YES", "0.38", "0.40", "5000"),
                ("k:NO", "0.60", "0.62", "3"),
                ("p:LONG", "0.43", "0.45", "3"),
                ("p:SHORT", "0.58", "0.60", "5000"),
            ],
        )

        assert Decimal(group["net_edge"]) < 0
        assert group["best_pair_yes_outcome_id"] == "eg-neg:k:YES"
        assert group["best_pair_no_outcome_id"] == "eg-neg:p:SHORT"
        # The best price of the four, not the least-negative product.
        assert Decimal(group["net_edge"]) == Decimal("-0.031200")
        # Depth of the pair that was chosen, capped by the $200 ticket budget
        # -- decisively not the 3-contract book the old ranking preferred.
        assert Decimal(group["max_tradeable_qty"]) == Decimal("193.94")
        assert Decimal(group["net_max_profit"]) == (
            Decimal(group["net_edge"]) * Decimal(group["max_tradeable_qty"])
        )


def test_monitored_known_empty_pair_never_outranks_real_depth(monkeypatch):
    """A qty == 0 pair must lose to any pair with real depth.

    Reachable in production: a one-sided book keeps its live side and
    synthesises the missing side at size 0 (see CLAUDE.md), so `ask_size = 0`
    here is a real shape, not a contrivance. Under net_edge * qty such a pair
    scores exactly 0 and therefore beats every genuinely negative pair, and
    the row then renders "no size" while another pair has 189 contracts.

    p:SHORT is deliberately the *best-priced* leg, so pure price ranking would
    still pick it -- the known-empty demotion is what has to save this.

    Only cross-venue pairs are candidates, so the two same-venue combinations
    below are struck out. That leaves exactly one pair with real depth, and
    the known-empty one still has to lose to it:

        k:YES + p:SHORT  edge -0.021314  qty      0  profit  0.0000  <- best price, no size
        p:LONG + k:NO    edge -0.101342  qty 181.59  profit -18.403  <- must win
        k:YES + k:NO     -- same venue, not a candidate
        p:LONG + p:SHORT -- same venue, not a candidate
    """
    # Pinned rather than inherited: this test's worked numbers are
    # arithmetic on a $200 budget, and it exists to pin *ranking*, not
    # whatever the shipped default happens to be.
    monkeypatch.setenv("ARBYS_MAX_TICKET_STAKE", "200")
    with TestClient(create_app()) as client:
        group = _register_four_leg_group(
            client,
            "eg-zero",
            [
                ("k:YES", "0.38", "0.40", "5000"),
                ("k:NO", "0.60", "0.62", "5000"),
                ("p:LONG", "0.43", "0.45", "5000"),
                ("p:SHORT", "0.57", "0.59", "0"),
            ],
        )

        assert group["best_pair_no_outcome_id"] != "eg-zero:p:SHORT"
        assert group["best_pair_yes_outcome_id"] == "eg-zero:p:LONG"
        assert group["best_pair_no_outcome_id"] == "eg-zero:k:NO"
        assert Decimal(group["max_tradeable_qty"]) > 0
        assert Decimal(group["max_tradeable_qty"]) == Decimal("181.59")
        assert Decimal(group["net_edge"]) == Decimal("-0.101342")


def test_monitored_positive_pairs_still_rank_by_absolute_profit(monkeypatch):
    """When a pair clears fees, ranking stays net_edge * qty.

    This is the invariant the fix must not break: `detect_cross_venue_two_leg`
    is depth-scaled, so a deep 1.2c pair beats a 2-contract 12.2c pair there,
    and the frontend joins a displayed pair to a published opportunity by leg
    outcome_id. Naming the fatter *per-contract* edge instead would leave a
    live arb's Fill button disabled.

        k:YES + p:SHORT  edge 0.011836  qty 202.39  profit 2.3955  <- chosen
        p:LONG + k:NO    edge 0.121950  qty   2.00  profit 0.2439  (best price)
        k:YES + k:NO     edge 0.068500  qty   2.00  profit 0.1370
        p:LONG + p:SHORT edge 0.065286  qty   2.00  profit 0.1306
    """
    # Pinned rather than inherited: this test's worked numbers are
    # arithmetic on a $200 budget, and it exists to pin *ranking*, not
    # whatever the shipped default happens to be.
    monkeypatch.setenv("ARBYS_MAX_TICKET_STAKE", "200")
    with TestClient(create_app()) as client:
        group = _register_four_leg_group(
            client,
            "eg-pos",
            [
                ("k:YES", "0.28", "0.30", "5000"),
                ("k:NO", "0.58", "0.60", "2"),
                ("p:LONG", "0.23", "0.25", "2"),
                ("p:SHORT", "0.64", "0.66", "5000"),
            ],
        )

        assert group["best_pair_yes_outcome_id"] == "eg-pos:k:YES"
        assert group["best_pair_no_outcome_id"] == "eg-pos:p:SHORT"
        # The thinnest edge of the four, chosen on depth.
        assert Decimal(group["net_edge"]) == Decimal("0.011836")
        assert Decimal(group["max_tradeable_qty"]) == Decimal("202.39")
        assert Decimal(group["net_max_profit"]) == Decimal("2.39548804")


def test_summary_reports_live_equity_without_waiting_for_a_snapshot():
    """PnlSnapshotService writes every 30s; the strip cannot wait for it, and
    after a restart there is no snapshot at all."""
    with TestClient(create_app()) as client:
        _register(client, "eg-eq", "p-yes", "k-no")
        client.post("/quotes", json={"outcome_id": "p-yes", "bid": "0.40", "ask": "0.40"})
        client.post("/quotes", json={"outcome_id": "k-no", "bid": "0.50", "ask": "0.50"})
        assert client.post(
            "/paper/execute",
            json={"event_group_id": "eg-eq", "outcome_ids": ["p-yes", "k-no"]},
        ).status_code == 200

        body = client.get("/paper/default").json()
        assert Decimal(body["position_value"]) > 0
        assert Decimal(body["equity"]) == Decimal(body["cash"]) + Decimal(
            body["position_value"]
        )
        assert "unrealized_pnl" in body
        # The ticket that was just filled has no settlement row yet. Per the
        # design spec ("Tickets with any unsettled leg report realized: null
        # and show as open" / "Missing settlement rows are the normal state
        # for open tickets and render as open"), that is exactly what "open"
        # means, so the just-filled ticket counts as one open ticket here.
        assert body["open_ticket_count"] == 1


def test_tickets_endpoint_groups_legs_and_names_the_event():
    with TestClient(create_app()) as client:
        _register(client, "eg-tk", "p-yes", "k-no")
        client.post("/quotes", json={"outcome_id": "p-yes", "bid": "0.40", "ask": "0.40"})
        client.post("/quotes", json={"outcome_id": "k-no", "bid": "0.50", "ask": "0.50"})
        client.post(
            "/paper/execute",
            json={"event_group_id": "eg-tk", "outcome_ids": ["p-yes", "k-no"]},
        )
        tickets = client.get("/paper/default/tickets").json()["items"]
        assert len(tickets) == 1
        assert tickets[0]["status"] == "filled"
        assert tickets[0]["source"] == "manual"
        assert len(tickets[0]["legs"]) == 2
        # Not just truthiness: `_title`'s fallback is `event_group_id`, which
        # is also truthy, so this must pin the actual registered title.
        assert tickets[0]["title_snapshot"] == "Group eg-tk"
        assert tickets[0]["legs"][0]["fill_price"] is not None


def test_positions_endpoint_returns_readable_titles():
    with TestClient(create_app()) as client:
        _register(client, "eg-pos", "p-yes", "k-no")
        client.post("/quotes", json={"outcome_id": "p-yes", "bid": "0.40", "ask": "0.40"})
        client.post("/quotes", json={"outcome_id": "k-no", "bid": "0.50", "ask": "0.50"})
        client.post(
            "/paper/execute",
            json={"event_group_id": "eg-pos", "outcome_ids": ["p-yes", "k-no"]},
        )
        positions = client.get("/paper/default/positions").json()
        assert len(positions) == 2
        # Not just truthiness: the fallback is the raw venue-native outcome
        # id, which is also truthy -- pin the actual registered title so a
        # regression to the fallback is caught.
        assert all(p["title"] == "Group eg-pos" for p in positions)
        assert all(p["mark"] is not None for p in positions)


def test_positions_endpoint_survives_event_group_retirement():
    """Discovery retires event groups routinely, deleting their legs -- which
    would blank the live join's title for exactly the outcomes worth
    auditing (finished games). The ticket's title_snapshot must survive it.
    """
    with TestClient(create_app()) as client:
        _register(client, "eg-retire", "p-yes-ret", "k-no-ret")
        client.post("/quotes", json={"outcome_id": "p-yes-ret", "bid": "0.40", "ask": "0.40"})
        client.post("/quotes", json={"outcome_id": "k-no-ret", "bid": "0.50", "ask": "0.50"})
        client.post(
            "/paper/execute",
            json={"event_group_id": "eg-retire", "outcome_ids": ["p-yes-ret", "k-no-ret"]},
        )
        assert client.delete("/event-groups/eg-retire").status_code == 204

        positions = client.get("/paper/default/positions").json()
        assert len(positions) == 2
        for p in positions:
            assert p["title"] == "Group eg-retire"


def test_positions_endpoint_prefers_ticket_snapshot_over_the_live_join():
    """The two title sources can disagree even before a group is retired: a
    later re-registration can rename the live event_group while an
    already-filled ticket's title_snapshot stays frozen at submit time. The
    snapshot must win -- it's what the ticket actually traded under.

    Unlike the retirement test above, both sources have a row for these
    outcome_ids here, so this is the one that actually exercises which
    `setdefault` loop runs first in `paper_position_titles`.
    """
    with TestClient(create_app()) as client:
        _register(client, "eg-rename", "p-yes-rn", "k-no-rn")
        client.post("/quotes", json={"outcome_id": "p-yes-rn", "bid": "0.40", "ask": "0.40"})
        client.post("/quotes", json={"outcome_id": "k-no-rn", "bid": "0.50", "ask": "0.50"})
        client.post(
            "/paper/execute",
            json={"event_group_id": "eg-rename", "outcome_ids": ["p-yes-rn", "k-no-rn"]},
        )
        r = client.post(
            "/event-groups",
            json={
                "id": "eg-rename",
                "title": "Renamed after the fact",
                "legs": [
                    {"outcome_id": "p-yes-rn", "venue_id": "polymarket_us", "is_yes_side": True},
                    {"outcome_id": "k-no-rn", "venue_id": "kalshi", "is_yes_side": False},
                ],
            },
        )
        assert r.status_code == 201

        positions = client.get("/paper/default/positions").json()
        assert len(positions) == 2
        for p in positions:
            assert p["title"] == "Group eg-rename"


def test_positions_carry_a_shared_event_group_id_for_grouping():
    """Both legs of one game must report the same event_group_id.

    The account page groups position rows per event. It keys on this id rather
    than on the title string precisely because the two title sources can
    disagree: after a re-registration, a filled ticket's frozen
    `title_snapshot` and the live `event_group.title` differ, and grouping by
    name would split one game across two rows. The id is stable across a
    rename, so the grouping survives it.
    """
    with TestClient(create_app()) as client:
        _register(client, "eg-group", "p-yes-gr", "k-no-gr")
        client.post("/quotes", json={"outcome_id": "p-yes-gr", "bid": "0.40", "ask": "0.40"})
        client.post("/quotes", json={"outcome_id": "k-no-gr", "bid": "0.50", "ask": "0.50"})
        assert client.post(
            "/paper/execute",
            json={"event_group_id": "eg-group", "outcome_ids": ["p-yes-gr", "k-no-gr"]},
        ).status_code == 200

        # Rename the live group so the two title sources now disagree.
        assert client.post(
            "/event-groups",
            json={
                "id": "eg-group",
                "title": "Renamed mid-flight",
                "legs": [
                    {"outcome_id": "p-yes-gr", "venue_id": "polymarket_us", "is_yes_side": True},
                    {"outcome_id": "k-no-gr", "venue_id": "kalshi", "is_yes_side": False},
                ],
            },
        ).status_code == 201

        positions = client.get("/paper/default/positions").json()
        assert len(positions) == 2
        assert {p["event_group_id"] for p in positions} == {"eg-group"}
        # One key for the pair is what collapses them into a single row.
        assert len({p["event_group_id"] for p in positions}) == 1


def test_position_event_group_id_survives_group_deletion():
    """The grouping key outlives the event group itself.

    Discovery retires groups routinely, so a finished game's legs would lose
    their shared key if it were read from the live join. It comes from the
    ticket's frozen snapshot instead, which is what keeps a settled game's
    rows collapsed into one row rather than scattering into per-leg rows the
    moment the group is retired.

    (An outcome never traded through a ticket *and* whose group is gone has no
    key at all and reports null; the UI falls back to the outcome id there, so
    unrelated legs are never merged under a shared null.)
    """
    with TestClient(create_app()) as client:
        _register(client, "eg-orphan", "p-yes-or", "k-no-or")
        client.post("/quotes", json={"outcome_id": "p-yes-or", "bid": "0.40", "ask": "0.40"})
        client.post("/quotes", json={"outcome_id": "k-no-or", "bid": "0.50", "ask": "0.50"})
        assert client.post(
            "/paper/execute",
            json={"event_group_id": "eg-orphan", "outcome_ids": ["p-yes-or", "k-no-or"]},
        ).status_code == 200
        # Traded through a ticket, so the snapshot keeps the id alive even
        # after the group is deleted.
        assert client.delete("/event-groups/eg-orphan").status_code == 204
        positions = client.get("/paper/default/positions").json()
        assert {p["event_group_id"] for p in positions} == {"eg-orphan"}


def test_health_reports_dropped_writes():
    """A non-zero count means the ledger on screen is incomplete. Surfacing it
    is what turns silent data loss into something observable."""
    with TestClient(create_app()) as client:
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["dropped_writes"] == 0
        assert body["last_dropped_write"] is None


def test_a_dust_sized_edge_is_never_published():
    """`ARBYS_MIN_CONTRACT_QTY` gates at detection, so no dust reaches the tape.

    The book here carries a fat 10c gross edge but only 2 contracts a side.
    Refusing it is a floor on *size*, not on edge — the distinction matters,
    because an edge floor is an explicit non-goal. Measured on the hosted
    account, 158 open positions on future games held ~$713 between them,
    averaging $4.50 each, many under a tenth of a contract.
    """
    with TestClient(create_app()) as client:
        _register(client, "eg-dust", "p-yes", "k-no")
        for oid, px in (("p-yes", "0.40"), ("k-no", "0.50")):
            client.post(
                "/quotes",
                json={"outcome_id": oid, "bid": px, "ask": px, "ask_size": "2"},
            )
        assert client.get("/opportunities").json() == []

        # The same book, deep enough to clear the floor, still trades.
        for oid, px in (("p-yes", "0.40"), ("k-no", "0.50")):
            client.post(
                "/quotes",
                json={"outcome_id": oid, "bid": px, "ask": px, "ask_size": "50"},
            )
        opps = client.get("/opportunities").json()
        assert [o["event_group_id"] for o in opps] == ["eg-dust"]
        assert Decimal(opps[0]["legs"][0]["qty"]) == Decimal("50")


def test_the_position_cap_counts_both_legs_of_the_game_in_dollars(monkeypatch):
    """Cost basis across the whole group, not units on one outcome.

    Each ticket here is 100 contracts at 0.40 + 0.50, so it commits $90 across
    the two venues. With a $130 cap the second is refused because the game
    already holds $90 -- per-outcome accounting would see only the $40 on
    `c-yes` and wave it through. The rejection states the figure it used, so
    the number in the config means the number on screen.
    """
    monkeypatch.setenv("ARBYS_MAX_OUTCOME_STAKE", "130")
    with TestClient(create_app()) as client:
        _register(client, "eg-scope", "c-yes", "c-no")
        for oid, px in (("c-yes", "0.40"), ("c-no", "0.50")):
            client.post(
                "/quotes",
                json={"outcome_id": oid, "bid": px, "ask": px, "ask_size": "100"},
            )
        first = client.post(
            "/paper/execute",
            json={"event_group_id": "eg-scope", "outcome_ids": ["c-yes", "c-no"]},
        )
        assert first.status_code == 200, first.text

        second = client.post(
            "/paper/execute",
            json={"event_group_id": "eg-scope", "outcome_ids": ["c-yes", "c-no"]},
        )
        assert second.status_code == 409, second.text
        detail = second.json()["detail"]
        assert "position cap" in detail
        assert "$130" in detail
        # Both legs, not the $40.00 sitting on c-yes alone.
        assert "holds $90.00" in detail
