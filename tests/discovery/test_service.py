from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbys.discovery import service as service_mod
from arbys.discovery.kalshi_sports import VenueGame
from arbys.discovery.service import DiscoveryService
from arbys.discovery.teams import MLB_RESOLVER
from arbys.shared.types import EventGroup, EventGroupLeg


@pytest.mark.asyncio
async def test_run_once_registers_new_groups_and_restarts_ingest(monkeypatch):
    lad = MLB_RESOLVER.by_code("LAD")
    chc = MLB_RESOLVER.by_code("CHC")
    assert lad and chc
    kalshi_game = VenueGame(
        sport="mlb",
        venue_id="kalshi",
        game_date=date(2026, 8, 5),
        teams=(lad, chc),
        outcome_ids={"LAD": "K-LAD:YES", "CHC": "K-CHC:YES"},
        ref="k",
    )
    poly_game = VenueGame(
        sport="mlb",
        venue_id="polymarket",
        game_date=date(2026, 8, 5),
        teams=(lad, chc),
        outcome_ids={"LAD": "P-LAD", "CHC": "P-CHC"},
        ref="p",
    )

    # Discovery now fans out over several team sports; only mlb has games here.
    async def fake_kalshi(*, sport="mlb", **_):
        return [kalshi_game] if sport == "mlb" else []

    async def fake_poly(*, sport="mlb", **_):
        return [poly_game] if sport == "mlb" else []

    monkeypatch.setattr(service_mod, "fetch_kalshi_team_games", fake_kalshi)
    monkeypatch.setattr(service_mod, "fetch_polymarket_sports_games", fake_poly)

    async def _empty(**_):
        return []

    monkeypatch.setattr(service_mod, "fetch_kalshi_tennis_matches", _empty)
    monkeypatch.setattr(service_mod, "fetch_polymarket_tennis_matches", _empty)
    monkeypatch.setattr(service_mod, "fetch_kalshi_totals", _empty)
    monkeypatch.setattr(service_mod, "fetch_polymarket_totals", _empty)

    # Bypass DB.
    fake_scope = MagicMock()
    fake_scope.__aenter__ = AsyncMock(return_value=MagicMock())
    fake_scope.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(service_mod, "session_scope", lambda: fake_scope)
    monkeypatch.setattr(service_mod.repo, "upsert_event_group", AsyncMock())

    state = MagicMock()
    state.event_groups = {}
    state.engine = MagicMock()
    state.restart_ingest = AsyncMock()

    svc = DiscoveryService(state)
    count = await svc.run_once()

    assert count == 1
    assert "mlb-CHC-LAD-2026-08-05" in state.event_groups
    state.engine.register_group.assert_called_once()
    state.restart_ingest.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_once_noop_when_group_unchanged(monkeypatch):
    """Second identical pass should not trigger restart_ingest."""
    lad = MLB_RESOLVER.by_code("LAD")
    chc = MLB_RESOLVER.by_code("CHC")
    assert lad and chc
    kalshi_game = VenueGame(
        sport="mlb",
        venue_id="kalshi",
        game_date=date(2026, 8, 5),
        teams=(lad, chc),
        outcome_ids={"LAD": "K-LAD:YES", "CHC": "K-CHC:YES"},
        ref="k",
    )
    poly_game = VenueGame(
        sport="mlb",
        venue_id="polymarket",
        game_date=date(2026, 8, 5),
        teams=(lad, chc),
        outcome_ids={"LAD": "P-LAD", "CHC": "P-CHC"},
        ref="p",
    )

    # Discovery now fans out over several team sports; only mlb has games here.
    async def fake_kalshi(*, sport="mlb", **_):
        return [kalshi_game] if sport == "mlb" else []

    async def fake_poly(*, sport="mlb", **_):
        return [poly_game] if sport == "mlb" else []

    monkeypatch.setattr(service_mod, "fetch_kalshi_team_games", fake_kalshi)
    monkeypatch.setattr(service_mod, "fetch_polymarket_sports_games", fake_poly)

    async def _empty(**_):
        return []

    monkeypatch.setattr(service_mod, "fetch_kalshi_tennis_matches", _empty)
    monkeypatch.setattr(service_mod, "fetch_polymarket_tennis_matches", _empty)
    monkeypatch.setattr(service_mod, "fetch_kalshi_totals", _empty)
    monkeypatch.setattr(service_mod, "fetch_polymarket_totals", _empty)
    fake_scope = MagicMock()
    fake_scope.__aenter__ = AsyncMock(return_value=MagicMock())
    fake_scope.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(service_mod, "session_scope", lambda: fake_scope)
    monkeypatch.setattr(service_mod.repo, "upsert_event_group", AsyncMock())

    state = MagicMock()
    state.event_groups = {}
    state.engine = MagicMock()
    state.restart_ingest = AsyncMock()

    svc = DiscoveryService(state)
    await svc.run_once()
    state.restart_ingest.reset_mock()

    # Second pass — should be a no-op.
    await svc.run_once()
    state.restart_ingest.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_once_retires_discovered_groups_that_vanish(monkeypatch):
    """A group the venues no longer offer must stop being tracked.

    Groups used to be upserted but never removed, so one whose markets were
    delisted (or whose venue tokens rotated) lived on through restart
    hydration -- still displayed, still priced off its last quotes.
    """
    gone = EventGroup(
        id="mlb-MIL-SD-2026-08-11",
        title="stale",
        source="discovery",
        legs=(EventGroupLeg(outcome_id="x", venue_id="kalshi", is_yes_side=True),),
    )
    kept = EventGroup(
        id="manual-thing",
        title="hand registered",
        source="manual",
        legs=(EventGroupLeg(outcome_id="y", venue_id="kalshi", is_yes_side=True),),
    )

    async def _empty(**_):
        return []

    for name in ("fetch_kalshi_team_games", "fetch_polymarket_sports_games",
                 "fetch_kalshi_tennis_matches", "fetch_polymarket_tennis_matches",
                 "fetch_kalshi_totals", "fetch_polymarket_totals"):
        monkeypatch.setattr(service_mod, name, _empty)

    fake_scope = MagicMock()
    fake_scope.__aenter__ = AsyncMock(return_value=MagicMock())
    fake_scope.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(service_mod, "session_scope", lambda: fake_scope)
    monkeypatch.setattr(service_mod.repo, "upsert_event_group", AsyncMock())
    deleted = AsyncMock()
    monkeypatch.setattr(service_mod.repo, "delete_event_group", deleted)

    state = MagicMock()
    state.event_groups = {gone.id: gone, kept.id: kept}
    state.engine = MagicMock()
    state.restart_ingest = AsyncMock()

    svc = DiscoveryService(state)
    await svc.run_once()

    assert gone.id not in state.event_groups, "vanished discovery group was not retired"
    assert kept.id in state.event_groups, "hand-registered group must never be retired"
    state.engine.unregister_group.assert_called_once_with(gone.id)
    assert deleted.await_count == 1
    state.restart_ingest.assert_awaited()
    # Its opportunities must go too: once unregistered the engine never
    # re-evaluates it, so nothing else would ever empty the set.
    state.clear_group_opportunities.assert_called_once_with(gone.id)


@pytest.mark.asyncio
async def test_failed_subpass_does_not_retire_anything(monkeypatch):
    """A venue outage must not be mistaken for 'these games no longer exist'."""
    existing = EventGroup(
        id="mlb-AAA-BBB-2026-08-11",
        title="still real",
        source="discovery",
        legs=(EventGroupLeg(outcome_id="x", venue_id="kalshi", is_yes_side=True),),
    )

    async def _empty(**_):
        return []

    async def _boom(**_):
        raise RuntimeError("kalshi is down")

    for name in ("fetch_polymarket_sports_games", "fetch_kalshi_tennis_matches",
                 "fetch_polymarket_tennis_matches", "fetch_kalshi_totals",
                 "fetch_polymarket_totals"):
        monkeypatch.setattr(service_mod, name, _empty)
    monkeypatch.setattr(service_mod, "fetch_kalshi_team_games", _boom)

    fake_scope = MagicMock()
    fake_scope.__aenter__ = AsyncMock(return_value=MagicMock())
    fake_scope.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(service_mod, "session_scope", lambda: fake_scope)
    monkeypatch.setattr(service_mod.repo, "upsert_event_group", AsyncMock())
    deleted = AsyncMock()
    monkeypatch.setattr(service_mod.repo, "delete_event_group", deleted)

    state = MagicMock()
    state.event_groups = {existing.id: existing}
    state.engine = MagicMock()
    state.restart_ingest = AsyncMock()

    await DiscoveryService(state).run_once()

    assert existing.id in state.event_groups, "retired on an incomplete pass"
    assert deleted.await_count == 0
