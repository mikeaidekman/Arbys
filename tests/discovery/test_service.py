from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbys.discovery import service as service_mod
from arbys.discovery.kalshi_sports import VenueGame
from arbys.discovery.service import DiscoveryService
from arbys.discovery.teams import MLB_RESOLVER


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
