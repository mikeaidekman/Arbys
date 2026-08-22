from decimal import Decimal

import pytest

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
