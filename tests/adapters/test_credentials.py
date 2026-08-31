"""Absent credentials are a configuration; broken ones are a bug.

Today both take the same path — log, return None — and the factories then build
the un-credentialed REST adapter. That is *correct* when nothing is configured:
the no-KYC REST path is documented and must keep working.

It is wrong when a key id is set and the key itself is missing, unreadable or
malformed, and it matters more than it looks. The entire hosting argument rests
on the credentialed WebSocket path having cheap restarts — measured at +31s to
a fully-served shard. A broken key silently selects REST, where restarts are
expensive, so the premise dissolves while the app still reports healthy and the
only evidence is one log line on a box nobody is tailing.

These also cover the container problem: both loaders read a file path, and no
platform secret store hands you a file.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from arbys.adapters.kalshi_ws import kalshi_ws_creds_from_env
from arbys.adapters.polymarket_us_auth import creds_from_env


@pytest.fixture
def pem(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    data = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    path = tmp_path / "kalshi.pem"
    path.write_text(data, encoding="utf-8")
    return data, str(path)


@pytest.fixture
def secret(tmp_path):
    raw = ed25519.Ed25519PrivateKey.generate().private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    b64 = base64.b64encode(raw).decode()
    path = tmp_path / "polymarket.key"
    path.write_text(b64, encoding="utf-8")
    return b64, str(path)


# --- neither set: the documented no-KYC path, unchanged ---------------------

def test_kalshi_absent_is_not_an_error(monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY", raising=False)
    assert kalshi_ws_creds_from_env() is None


def test_polymarket_absent_is_not_an_error(monkeypatch):
    monkeypatch.delenv("POLYMARKET_US_API_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_US_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.delenv("POLYMARKET_US_PRIVATE_KEY", raising=False)
    assert creds_from_env() is None


# --- the path form still works: local dev must not regress ------------------

def test_kalshi_path_form_still_works(monkeypatch, pem):
    _data, path = pem
    monkeypatch.setenv("KALSHI_API_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", path)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY", raising=False)
    got = kalshi_ws_creds_from_env()
    assert got is not None and got[0] == "kid"


def test_polymarket_path_form_still_works(monkeypatch, secret):
    _b64, path = secret
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", "kid")
    monkeypatch.setenv("POLYMARKET_US_PRIVATE_KEY_PATH", path)
    monkeypatch.delenv("POLYMARKET_US_PRIVATE_KEY", raising=False)
    assert creds_from_env() is not None


# --- inline, for a container that has no repo and no .env -------------------

def test_kalshi_inline_key_wins_over_the_path(monkeypatch, pem, tmp_path):
    data, _path = pem
    junk = tmp_path / "junk.pem"
    junk.write_text("not a key", encoding="utf-8")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", data)
    # Deliberately unusable: if the path were consulted this would raise.
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(junk))
    assert kalshi_ws_creds_from_env() is not None


def test_polymarket_inline_key_wins_over_the_path(monkeypatch, secret, tmp_path):
    b64, _path = secret
    junk = tmp_path / "junk.key"
    junk.write_text("!!!not base64!!!", encoding="utf-8")
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", "kid")
    monkeypatch.setenv("POLYMARKET_US_PRIVATE_KEY", b64)
    monkeypatch.setenv("POLYMARKET_US_PRIVATE_KEY_PATH", str(junk))
    assert creds_from_env() is not None


def test_a_flattened_pem_still_loads(monkeypatch, pem):
    r"""Kalshi's key is a multi-line PEM, and multi-line values are the classic
    thing a secret store or a shell mangles into literal \n sequences. Failing
    as "malformed key" would send someone hunting for a bad key rather than a
    bad newline."""
    data, _path = pem
    monkeypatch.setenv("KALSHI_API_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", data.replace("\n", "\\n"))
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    assert kalshi_ws_creds_from_env() is not None


# --- set but unusable: the pair that matters --------------------------------

def test_kalshi_id_set_with_an_unloadable_key_raises(monkeypatch, tmp_path):
    """Not None — a raise. Returning None here downgrades to REST silently and
    the app still reports healthy."""
    monkeypatch.setenv("KALSHI_API_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(tmp_path / "nope.pem"))
    monkeypatch.delenv("KALSHI_PRIVATE_KEY", raising=False)
    with pytest.raises(RuntimeError, match="KALSHI_PRIVATE_KEY"):
        kalshi_ws_creds_from_env()


def test_kalshi_id_set_with_a_malformed_inline_key_raises(monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nnope\n")
    with pytest.raises(RuntimeError, match="KALSHI_PRIVATE_KEY"):
        kalshi_ws_creds_from_env()


def test_kalshi_id_set_with_no_key_at_all_raises(monkeypatch):
    """Half-configured is a mistake, not a choice to use REST."""
    monkeypatch.setenv("KALSHI_API_KEY_ID", "kid")
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY", raising=False)
    with pytest.raises(RuntimeError, match="KALSHI_PRIVATE_KEY"):
        kalshi_ws_creds_from_env()


def test_polymarket_id_set_with_an_unloadable_key_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", "kid")
    monkeypatch.setenv("POLYMARKET_US_PRIVATE_KEY_PATH", str(tmp_path / "nope.key"))
    monkeypatch.delenv("POLYMARKET_US_PRIVATE_KEY", raising=False)
    with pytest.raises(RuntimeError, match="POLYMARKET_US_PRIVATE_KEY"):
        creds_from_env()


def test_polymarket_id_set_with_a_malformed_inline_key_raises(monkeypatch):
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", "kid")
    monkeypatch.setenv("POLYMARKET_US_PRIVATE_KEY", "!!!not base64!!!")
    with pytest.raises(RuntimeError, match="POLYMARKET_US_PRIVATE_KEY"):
        creds_from_env()
