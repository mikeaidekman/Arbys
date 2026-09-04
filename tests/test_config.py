from decimal import Decimal

import pytest

from arbys.backend import state as state_module
from arbys.backend.state import (
    DEFAULT_MAX_DAYS_TO_START,
    DEFAULT_MAX_TICKET_STAKE,
    max_days_to_start,
    max_ticket_stake,
)


def test_default_is_250(monkeypatch):
    monkeypatch.delenv("ARBYS_MAX_TICKET_STAKE", raising=False)
    assert max_ticket_stake() == Decimal("250")
    assert Decimal("250") == DEFAULT_MAX_TICKET_STAKE


def test_explicit_value_is_honoured(monkeypatch):
    monkeypatch.setenv("ARBYS_MAX_TICKET_STAKE", "50")
    assert max_ticket_stake() == Decimal("50")


def test_zero_disables_the_cap(monkeypatch):
    monkeypatch.setenv("ARBYS_MAX_TICKET_STAKE", "0")
    assert max_ticket_stake() is None


@pytest.mark.parametrize("bad", ["", "abc", "1.2.3"])
def test_garbage_falls_back_to_default(monkeypatch, bad):
    monkeypatch.setenv("ARBYS_MAX_TICKET_STAKE", bad)
    assert max_ticket_stake() == Decimal("250")


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


def test_asyncpg_url_normalisation():
    """Managed Postgres hands you a libpq-flavoured URL and asyncpg rejects
    parts of it at connect time — on the first real deploy, against the
    production database. Translating `sslmode` and dropping `channel_binding`
    here is cheaper than asking whoever sets ARBYS_DB_URL to hand-edit a
    connection string correctly every time."""
    from arbys.db.session import normalise_asyncpg_url as n

    got = n("postgresql+asyncpg://u:secret@h/db?sslmode=require&channel_binding=require")
    assert "channel_binding" not in got
    assert "sslmode" not in got
    assert "ssl=require" in got
    # `str(URL)` masks the password as `***`. Round-tripping through it would
    # ship a URL that fails authentication, with an error blaming the
    # credentials rather than this function.
    assert "secret" in got

    # Untouched: not asyncpg, so libpq's spelling is the correct one.
    psycopg = "postgresql+psycopg://u:p@h/db?sslmode=require&channel_binding=require"
    assert n(psycopg) == psycopg
    sqlite = "sqlite+aiosqlite:///./arbys-local.db"
    assert n(sqlite) == sqlite

    # No query string at all is the common local case and must not gain one.
    assert n("postgresql+asyncpg://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"


def test_min_contract_qty_defaults_to_five(monkeypatch):
    monkeypatch.delenv("ARBYS_MIN_CONTRACT_QTY", raising=False)
    assert state_module.min_contract_qty() == Decimal("5")


def test_min_contract_qty_zero_disables_the_floor(monkeypatch):
    monkeypatch.setenv("ARBYS_MIN_CONTRACT_QTY", "0")
    assert state_module.min_contract_qty() == Decimal("0")


@pytest.mark.parametrize("bad", ["", "abc", "1.2.3"])
def test_min_contract_qty_garbage_falls_back(monkeypatch, bad):
    monkeypatch.setenv("ARBYS_MIN_CONTRACT_QTY", bad)
    assert state_module.min_contract_qty() == Decimal("5")


def test_max_days_to_start_defaults_to_seven(monkeypatch):
    monkeypatch.delenv("ARBYS_MAX_DAYS_TO_START", raising=False)
    assert max_days_to_start() == 7.0
    assert DEFAULT_MAX_DAYS_TO_START == 7.0


def test_max_days_to_start_reads_the_env(monkeypatch):
    monkeypatch.setenv("ARBYS_MAX_DAYS_TO_START", "3.5")
    assert max_days_to_start() == 3.5


def test_max_days_to_start_zero_disables_the_rule(monkeypatch):
    monkeypatch.setenv("ARBYS_MAX_DAYS_TO_START", "0")
    assert max_days_to_start() is None
    monkeypatch.setenv("ARBYS_MAX_DAYS_TO_START", "-2")
    assert max_days_to_start() is None


@pytest.mark.parametrize("bad", ["", "abc", "1.2.3"])
def test_max_days_to_start_garbage_falls_back(monkeypatch, bad):
    monkeypatch.setenv("ARBYS_MAX_DAYS_TO_START", bad)
    assert max_days_to_start() == 7.0


@pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_max_days_to_start_non_finite_falls_back(monkeypatch, bad):
    """`float()` parses these without raising, so a bare `float()` conversion
    accepts them silently. `nan` makes every `days_ahead <= nan` comparison
    false, refusing every ticket with no error anywhere; `inf` disables the
    rule the same way `0` does but without saying so. Both must fall back to
    the default like any other unparseable input."""
    monkeypatch.setenv("ARBYS_MAX_DAYS_TO_START", bad)
    assert max_days_to_start() == 7.0
