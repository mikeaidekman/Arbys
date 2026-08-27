from decimal import Decimal

import pytest

from arbys.backend import state as state_module
from arbys.backend.state import DEFAULT_MAX_TICKET_STAKE, max_ticket_stake


def test_default_is_200(monkeypatch):
    monkeypatch.delenv("ARBYS_MAX_TICKET_STAKE", raising=False)
    assert max_ticket_stake() == Decimal("200")
    assert Decimal("200") == DEFAULT_MAX_TICKET_STAKE


def test_explicit_value_is_honoured(monkeypatch):
    monkeypatch.setenv("ARBYS_MAX_TICKET_STAKE", "50")
    assert max_ticket_stake() == Decimal("50")


def test_zero_disables_the_cap(monkeypatch):
    monkeypatch.setenv("ARBYS_MAX_TICKET_STAKE", "0")
    assert max_ticket_stake() is None


@pytest.mark.parametrize("bad", ["", "abc", "1.2.3"])
def test_garbage_falls_back_to_default(monkeypatch, bad):
    monkeypatch.setenv("ARBYS_MAX_TICKET_STAKE", bad)
    assert max_ticket_stake() == Decimal("200")


def test_auto_trade_is_off_by_default(monkeypatch):
    monkeypatch.delenv("ARBYS_ENABLE_AUTO_TRADE", raising=False)
    assert state_module._auto_trade_enabled() is False


def test_auto_trade_enabled_only_by_exactly_one(monkeypatch):
    monkeypatch.setenv("ARBYS_ENABLE_AUTO_TRADE", "1")
    assert state_module._auto_trade_enabled() is True
    for value in ("0", "true", "yes", "", "2"):
        monkeypatch.setenv("ARBYS_ENABLE_AUTO_TRADE", value)
        assert state_module._auto_trade_enabled() is False


def test_auto_trade_cooldown_defaults_to_60(monkeypatch):
    monkeypatch.delenv("ARBYS_AUTO_TRADE_COOLDOWN_S", raising=False)
    assert state_module._auto_trade_cooldown_s() == 60.0


def test_auto_trade_cooldown_reads_the_env(monkeypatch):
    monkeypatch.setenv("ARBYS_AUTO_TRADE_COOLDOWN_S", "5.5")
    assert state_module._auto_trade_cooldown_s() == 5.5


def test_auto_trade_cooldown_survives_garbage_and_refuses_negatives(monkeypatch):
    monkeypatch.setenv("ARBYS_AUTO_TRADE_COOLDOWN_S", "not-a-number")
    assert state_module._auto_trade_cooldown_s() == 60.0
    # 0 is a legitimate "no cooldown"; negative is nonsense and clamps to it.
    monkeypatch.setenv("ARBYS_AUTO_TRADE_COOLDOWN_S", "0")
    assert state_module._auto_trade_cooldown_s() == 0.0
    monkeypatch.setenv("ARBYS_AUTO_TRADE_COOLDOWN_S", "-30")
    assert state_module._auto_trade_cooldown_s() == 0.0
