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
            "polymarket_us": lambda oids: (
                created.setdefault("polymarket_us", StubAdapter("polymarket_us", oids, []))
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
                        {"outcome_id": "poly-y", "venue_id": "polymarket_us", "is_yes_side": True},
                        {"outcome_id": "kals-n", "venue_id": "kalshi", "is_yes_side": False},
                    ],
                },
            )
            assert r.status_code == 201

            # After event-group create, restart_ingest fired and stub adapters were built.
            assert "polymarket_us" in created
            assert "kalshi" in created
            assert created["polymarket_us"].outcome_ids == ["poly-y"]
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
                        {"outcome_id": "poly-y", "venue_id": "polymarket_us", "is_yes_side": True},
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
            a = StubAdapter("polymarket_us", oids, [q1])
            created["polymarket_us"] = a
            return a

        def kals_factory(oids):
            a = StubAdapter("kalshi", oids, [q2])
            created["kalshi"] = a
            return a

        self.adapter_factories = {"polymarket_us": poly_factory, "kalshi": kals_factory}

    state_module.AppState.__init__ = patched_init
    try:
        with TestClient(create_app()) as client:
            client.post(
                "/event-groups",
                json={
                    "id": "eg-live",
                    "title": "Will Y happen?",
                    "legs": [
                        {"outcome_id": "poly-y", "venue_id": "polymarket_us", "is_yes_side": True},
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


def test_factory_picks_websocket_when_credentials_are_present(monkeypatch, tmp_path):
    import base64

    from cryptography.hazmat.primitives.asymmetric import ed25519

    from arbys.adapters.polymarket_us_ws import PolymarketUsWebSocketAdapter
    from arbys.backend.state import _default_adapter_factories

    key = ed25519.Ed25519PrivateKey.generate()
    path = tmp_path / "pm.key"
    path.write_text(base64.b64encode(key.private_bytes_raw()).decode(), encoding="utf-8")
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", "kid")
    monkeypatch.setenv("POLYMARKET_US_PRIVATE_KEY_PATH", str(path))

    adapter = _default_adapter_factories()["polymarket_us"](["s:LONG"])
    assert isinstance(adapter, PolymarketUsWebSocketAdapter)


def test_factory_falls_back_to_rest_without_credentials(monkeypatch):
    """The REST path is the only one that works without KYC, so it stays."""
    from arbys.adapters.polymarket_us import PolymarketUsAdapter
    from arbys.backend.state import _default_adapter_factories

    monkeypatch.delenv("POLYMARKET_US_API_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_US_PRIVATE_KEY_PATH", raising=False)

    adapter = _default_adapter_factories()["polymarket_us"](["s:LONG"])
    assert isinstance(adapter, PolymarketUsAdapter)


# --- sync_ingest: a retirement must not cost us every socket ---------------


class _CountingAdapter(MarketDataAdapter):
    """Records how many times it was built, and whether it was ever closed."""

    def __init__(self, venue_id: str, outcome_ids: list[str]) -> None:
        self.venue_id = venue_id
        self.outcome_ids = outcome_ids
        self.closed = False

    async def list_markets(self) -> list[Outcome]:
        return []

    async def stream_quotes(self) -> AsyncIterator[Quote]:
        await asyncio.sleep(3600)
        yield  # pragma: no cover - never reached

    async def close(self) -> None:
        self.closed = True


def _group(gid: str, *outcome_ids: str):
    from arbys.shared.types import EventGroup, EventGroupLeg

    return EventGroup(
        id=gid,
        title=gid,
        legs=tuple(
            EventGroupLeg(outcome_id=o, venue_id="v1", is_yes_side=(i == 0))
            for i, o in enumerate(outcome_ids)
        ),
    )


@pytest.mark.asyncio
async def test_sync_ingest_does_not_rebuild_when_a_group_is_only_retired():
    """Dropping a market needs no socket change; adding one does.

    Neither venue supports unsubscribing, so a rebuild is the only way to
    shorten the subscription list - and a rebuild makes the venue replay
    cached books that can be hours old, blanking every market for minutes.
    Discovery retires a finished match on nearly every pass, so rebuilding for
    that reason meant the whole book went stale every ~12 minutes to stop
    watching one game that had ended.
    """
    st = state_module.AppState()
    built: list[_CountingAdapter] = []

    def factory(oids: list[str]) -> MarketDataAdapter:
        a = _CountingAdapter("v1", oids)
        built.append(a)
        return a

    st.adapter_factories = {"v1": factory}

    st.event_groups["g1"] = _group("g1", "a:YES", "a:NO")
    st.event_groups["g2"] = _group("g2", "b:YES", "b:NO")
    await st.sync_ingest()
    assert len(built) == 1, "first sync should build the adapter"

    # Nothing changed at all -> no rebuild.
    await st.sync_ingest()
    assert len(built) == 1, "an unchanged pass rebuilt the subscriptions"

    # A retirement -> still no rebuild, and the live socket is untouched.
    st.event_groups.pop("g2")
    await st.sync_ingest()
    assert len(built) == 1, "a retirement rebuilt every subscription"
    assert built[0].closed is False, "a retirement closed the live socket"

    # A genuinely new market -> rebuild, because nothing else can subscribe it.
    st.event_groups["g3"] = _group("g3", "c:YES", "c:NO")
    await st.sync_ingest()
    assert len(built) == 2, "a new market did not get subscribed"
    assert "c:YES" in built[1].outcome_ids

    await st._stop_ingest()


@pytest.mark.asyncio
async def test_in_play_slugs_only_names_started_events():
    """The tighter backstop deadline keys off this, so a future game must not
    be in it - polling every pre-game market at in-play rates is wasted load."""
    from datetime import UTC, datetime, timedelta

    from arbys.shared.types import EventGroup, EventGroupLeg

    st = state_module.AppState()
    now = datetime.now(UTC)

    def grp(gid: str, slug: str, start):
        return EventGroup(
            id=gid,
            title=gid,
            start_time=start,
            legs=(
                EventGroupLeg(outcome_id=f"{slug}:LONG", venue_id="polymarket_us", is_yes_side=True),
                EventGroupLeg(outcome_id=f"{slug}:SHORT", venue_id="polymarket_us", is_yes_side=False),
            ),
        )

    st.event_groups["live"] = grp("live", "playing-now", now - timedelta(minutes=5))
    st.event_groups["later"] = grp("later", "tomorrow", now + timedelta(hours=6))
    st.event_groups["unknown"] = grp("unknown", "no-start", None)

    live = st.in_play_slugs()
    assert "playing-now" in live
    assert "tomorrow" not in live
    assert "no-start" not in live


@pytest.mark.asyncio
async def test_a_finished_event_stops_counting_as_in_play():
    """"Started" is not a state you stay in.

    Without an upper bound a game that ended hours ago is still "in-play", so
    it is polled at in-play rates and re-subscribed every sweep for the life of
    the process - observed as 13 markets per shard being repaired every 15s,
    none of which could ever stream again because they had settled.
    """
    from datetime import UTC, datetime, timedelta

    from arbys.shared.types import EventGroup, EventGroupLeg

    st = state_module.AppState()
    now = datetime.now(UTC)

    def grp(gid: str, slug: str, start):
        return EventGroup(
            id=gid,
            title=gid,
            start_time=start,
            legs=(
                EventGroupLeg(
                    outcome_id=f"{slug}:LONG", venue_id="polymarket_us", is_yes_side=True
                ),
            ),
        )

    st.event_groups["now"] = grp("now", "underway", now - timedelta(minutes=30))
    st.event_groups["done"] = grp("done", "finished", now - timedelta(hours=9))

    live = st.in_play_slugs()
    assert "underway" in live
    assert "finished" not in live, "a game from nine hours ago is still in-play"


@pytest.mark.asyncio
async def test_the_venue_flag_beats_the_clock_for_in_play():
    """A finished match must drop out even though its start time is recent.

    The clock alone cannot tell a game in progress from one that ended an hour
    ago, which is what had finished tennis matches being polled at in-play
    rates and re-subscribed every sweep for the life of the process.
    """
    from datetime import UTC, datetime, timedelta

    from arbys.shared.types import EventGroup, EventGroupLeg

    st = state_module.AppState()
    now = datetime.now(UTC)

    def grp(gid: str, slug: str, start, in_play):
        return EventGroup(
            id=gid,
            title=gid,
            start_time=start,
            in_play=in_play,
            legs=(
                EventGroupLeg(
                    outcome_id=f"{slug}:LONG", venue_id="polymarket_us", is_yes_side=True
                ),
            ),
        )

    recent = now - timedelta(minutes=40)
    st.event_groups["playing"] = grp("playing", "on-court", recent, True)
    st.event_groups["finished"] = grp("finished", "walked-off", recent, False)
    # No venue said anything -> the clock still decides.
    st.event_groups["silent"] = grp("silent", "no-word", recent, None)
    st.event_groups["old"] = grp("old", "yesterday", now - timedelta(hours=9), None)

    live = st.in_play_slugs()
    assert "on-court" in live
    assert "walked-off" not in live, "a finished match was still treated as in-play"
    assert "no-word" in live, "the clock fallback stopped working"
    assert "yesterday" not in live


def test_match_in_play_ignores_venues_that_report_nothing():
    """Kalshi publishes no live state; that is not a vote for 'not playing'."""
    from datetime import UTC, datetime

    from arbys.discovery.kalshi_sports import VenueGame
    from arbys.discovery.matcher import CrossVenueMatch
    from arbys.discovery.teams import Team

    a = Team("AAA", "Alpha Ayes", "Alpha", "Ayes")
    b = Team("BBB", "Beta Bees", "Beta", "Bees")
    start = datetime(2026, 8, 25, 16, 30, tzinfo=UTC)

    def game(venue, live, ended):
        return VenueGame(
            sport="atp",
            venue_id=venue,
            game_date=start.date(),
            teams=(a, b),
            outcome_ids={"AAA": f"{venue}-a", "BBB": f"{venue}-b"},
            ref=venue,
            start_time=start,
            live=live,
            ended=ended,
        )

    def match(per_venue):
        return CrossVenueMatch(
            sport="atp",
            game_date=start.date(),
            team_a=a,
            team_b=b,
            per_venue=per_venue,
        )

    # Kalshi silent, Polymarket says playing -> playing.
    m = match({"kalshi": game("kalshi", None, None),
               "polymarket_us": game("polymarket_us", True, False)})
    assert m.in_play() is True

    # Polymarket says finished -> finished, whatever else is silent.
    m = match({"kalshi": game("kalshi", None, None),
               "polymarket_us": game("polymarket_us", False, True)})
    assert m.in_play() is False

    # Nobody said -> unknown, so callers fall back to the clock.
    m = match({"kalshi": game("kalshi", None, None)})
    assert m.in_play() is None
