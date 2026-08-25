from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbys.discovery import service as service_mod
from arbys.discovery.kalshi_sports import VenueGame
from arbys.discovery.service import DiscoveryService
from arbys.discovery.teams import MLB_RESOLVER
from arbys.shared.types import EventGroup, EventGroupLeg


def _stub_group(group_id: str) -> EventGroup:
    return EventGroup(
        id=group_id,
        title=f"stub {group_id}",
        legs=(
            EventGroupLeg(outcome_id=f"{group_id}-a", venue_id="kalshi", is_yes_side=True),
            EventGroupLeg(
                outcome_id=f"{group_id}-b", venue_id="polymarket_us", is_yes_side=False
            ),
        ),
    )


async def _committing_run_write(_context, work):
    """Stand-in for `run_write` that always commits, against a fake session."""
    await work(MagicMock())
    return True


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
        venue_id="polymarket_us",
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
    monkeypatch.setattr(service_mod, "fetch_polymarket_us_games", fake_poly)

    async def _empty(**_):
        return []

    monkeypatch.setattr(service_mod, "fetch_kalshi_tennis_matches", _empty)
    monkeypatch.setattr(service_mod, "fetch_polymarket_us_tennis", _empty)
    monkeypatch.setattr(service_mod, "fetch_kalshi_totals", _empty)
    monkeypatch.setattr(service_mod, "fetch_polymarket_us_totals", _empty)

    # Bypass DB.
    monkeypatch.setattr(service_mod, "run_write", _committing_run_write)
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
        venue_id="polymarket_us",
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
    monkeypatch.setattr(service_mod, "fetch_polymarket_us_games", fake_poly)

    async def _empty(**_):
        return []

    monkeypatch.setattr(service_mod, "fetch_kalshi_tennis_matches", _empty)
    monkeypatch.setattr(service_mod, "fetch_polymarket_us_tennis", _empty)
    monkeypatch.setattr(service_mod, "fetch_kalshi_totals", _empty)
    monkeypatch.setattr(service_mod, "fetch_polymarket_us_totals", _empty)
    monkeypatch.setattr(service_mod, "run_write", _committing_run_write)
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

    for name in ("fetch_kalshi_team_games", "fetch_polymarket_us_games",
                 "fetch_kalshi_tennis_matches", "fetch_polymarket_us_tennis",
                 "fetch_kalshi_totals", "fetch_polymarket_us_totals"):
        monkeypatch.setattr(service_mod, name, _empty)

    monkeypatch.setattr(service_mod, "run_write", _committing_run_write)
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
async def test_dropped_retire_leaves_app_state_holding_the_group(monkeypatch):
    """A retire whose DB delete fails to commit must not let AppState un-know
    a group the database still has.

    Mirrors `test_dropped_batch_leaves_app_state_untouched` for the retire
    half of `run_once`: `_retire_missing` used to unregister from the engine,
    clear opportunities, and pop from `AppState.event_groups` *before* the raw
    DB delete, so a delete that failed left memory ahead of the database --
    and the next `bootstrap()` rehydrated the "retired" group from the DB,
    resurrecting the exact delisted-market phantom retirement exists to kill.
    """
    stale = EventGroup(
        id="mlb-STALE-GONE-2026-08-11",
        title="should have been retired",
        source="discovery",
        legs=(EventGroupLeg(outcome_id="x", venue_id="kalshi", is_yes_side=True),),
    )

    async def fake_discover_all():
        return [], True  # nothing found this pass -> `stale` looks gone

    monkeypatch.setattr(service_mod, "discover_all_event_groups", fake_discover_all)

    async def _always_dropped(_context, _work):
        return False

    monkeypatch.setattr(service_mod, "run_write", _always_dropped)

    state = MagicMock()
    state.event_groups = {stale.id: stale}
    state.engine = MagicMock()
    state.restart_ingest = AsyncMock()

    count = await DiscoveryService(state).run_once()

    assert count == 0, "discovery still reports what it found"
    assert stale.id in state.event_groups, "dropped retire must not un-know a live group"
    state.engine.unregister_group.assert_not_called()
    state.clear_group_opportunities.assert_not_called()
    state.restart_ingest.assert_not_awaited()


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

    for name in ("fetch_polymarket_us_games", "fetch_kalshi_tennis_matches",
                 "fetch_polymarket_us_tennis", "fetch_kalshi_totals",
                 "fetch_polymarket_us_totals"):
        monkeypatch.setattr(service_mod, name, _empty)
    monkeypatch.setattr(service_mod, "fetch_kalshi_team_games", _boom)

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


@pytest.mark.asyncio
async def test_polymarket_us_outage_does_not_retire_anything(monkeypatch):
    """Symmetric to the Kalshi case above, for the venue that just changed.

    A gateway.polymarket.us outage makes every cross-venue group stop
    matching. Retiring on that would wipe the board on a transient error.
    """
    existing = EventGroup(
        id="mlb-AAA-BBB-2026-08-11",
        title="still real",
        source="discovery",
        legs=(EventGroupLeg(outcome_id="x", venue_id="kalshi", is_yes_side=True),),
    )

    async def _empty(**_):
        return []

    async def _boom(**_):
        raise RuntimeError("gateway.polymarket.us is down")

    for name in ("fetch_kalshi_team_games", "fetch_kalshi_tennis_matches",
                 "fetch_polymarket_us_tennis", "fetch_kalshi_totals",
                 "fetch_polymarket_us_totals"):
        monkeypatch.setattr(service_mod, name, _empty)
    monkeypatch.setattr(service_mod, "fetch_polymarket_us_games", _boom)

    monkeypatch.setattr(service_mod.repo, "upsert_event_group", AsyncMock())
    monkeypatch.setattr(service_mod.repo, "delete_event_group", AsyncMock())

    state = MagicMock()
    state.event_groups = {existing.id: existing}
    state.engine = MagicMock()
    state.restart_ingest = AsyncMock()

    await DiscoveryService(state).run_once()

    assert existing.id in state.event_groups, "retired on a Polymarket US outage"


async def test_run_once_batches_group_writes():
    """One transaction per group starved the other writers.

    The first pass after a restart rewrites every group — 567 of them live —
    and each took the single SQLite write lock in turn while the PnL
    snapshotter and the broker's sink tried to interleave. Batching cuts the
    lock acquisitions by the batch size.

    A single transaction for all of them would be the wrong end of the
    trade-off: it holds the write lock for the whole pass and turns one
    failure into every group lost, so the batch size is asserted here too.
    """
    groups = [_stub_group(f"eg-{i}") for i in range(120)]
    # 120 groups in batches of 5 -> 24 transactions, not 120.
    written = service_mod._batch(groups, service_mod.GROUP_WRITE_BATCH)
    assert [len(b) for b in written] == [5] * 24


@pytest.mark.asyncio
async def test_dropped_batch_leaves_app_state_untouched(monkeypatch):
    """A batch that fails to commit must not let AppState claim a group the
    database has never seen — that divergence is worse than the write burst
    this batching exists to fix.
    """
    group = _stub_group("eg-dropped")

    async def fake_discover_all():
        return [group], True

    monkeypatch.setattr(service_mod, "discover_all_event_groups", fake_discover_all)

    async def _always_dropped(_context, _work):
        return False

    monkeypatch.setattr(service_mod, "run_write", _always_dropped)

    state = MagicMock()
    state.event_groups = {}
    state.engine = MagicMock()
    state.restart_ingest = AsyncMock()

    count = await DiscoveryService(state).run_once()

    assert count == 1, "discovery still reports what it found"
    assert group.id not in state.event_groups, "dropped batch must not be applied"
    state.engine.register_group.assert_not_called()
    state.restart_ingest.assert_not_awaited()
