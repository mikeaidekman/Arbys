"""Auth-helper tests. These need no real credentials.

A keypair is generated in-process, used to sign, and verified with the
matching public key — which proves the message construction and encoding are
right without anyone having completed KYC.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

from arbys.adapters.polymarket_us_auth import (
    PolymarketUsCredentials,
    auth_headers,
    creds_from_env,
    load_secret_key,
    signing_message,
)


def _keypair() -> tuple[ed25519.Ed25519PrivateKey, str]:
    """Return (private_key, base64_secret) the way the venue hands it over."""
    key = ed25519.Ed25519PrivateKey.generate()
    raw = key.private_bytes_raw()
    return key, base64.b64encode(raw).decode()


def test_signing_message_is_timestamp_method_path_with_no_separators():
    """Documented format: f"{timestamp}{METHOD}{path}". A stray separator or
    a lowercase method silently fails auth on every request."""
    assert signing_message("1754000000000", "GET", "/v1/portfolio/positions") == (
        "1754000000000GET/v1/portfolio/positions"
    )


def test_signing_message_uppercases_the_method():
    assert signing_message("1", "get", "/x") == "1GET/x"


def test_load_secret_key_accepts_the_base64_the_portal_gives_you():
    key, secret_b64 = _keypair()
    loaded = load_secret_key(secret_b64)
    assert isinstance(loaded, ed25519.Ed25519PrivateKey)
    assert loaded.private_bytes_raw() == key.private_bytes_raw()


def test_load_secret_key_truncates_to_32_bytes():
    """Some portals emit a 64-byte seed+public concatenation. Ed25519 private
    keys are 32 bytes; the documented decode takes the first 32."""
    key, _ = _keypair()
    raw = key.private_bytes_raw()
    fat = base64.b64encode(raw + key.public_key().public_bytes_raw()).decode()
    assert load_secret_key(fat).private_bytes_raw() == raw


def test_load_secret_key_rejects_garbage():
    with pytest.raises(ValueError):
        load_secret_key("not-base64-!!!")
    with pytest.raises(ValueError):
        load_secret_key(base64.b64encode(b"tooshort").decode())


def test_auth_headers_produce_a_signature_the_public_key_verifies():
    """The end-to-end check: what we send is what the venue will validate."""
    key, secret_b64 = _keypair()
    creds = PolymarketUsCredentials(key_id="kid-123", secret_key=load_secret_key(secret_b64))

    headers = auth_headers(creds, "GET", "/v1/portfolio/positions", timestamp_ms=1754000000000)

    assert headers["X-PM-Access-Key"] == "kid-123"
    assert headers["X-PM-Timestamp"] == "1754000000000"

    message = b"1754000000000GET/v1/portfolio/positions"
    signature = base64.b64decode(headers["X-PM-Signature"])
    key.public_key().verify(signature, message)  # raises if wrong


def test_a_tampered_message_fails_verification():
    """Guards against the signature being computed over the wrong string."""
    key, secret_b64 = _keypair()
    creds = PolymarketUsCredentials(key_id="k", secret_key=load_secret_key(secret_b64))
    headers = auth_headers(creds, "GET", "/v1/a", timestamp_ms=1)
    signature = base64.b64decode(headers["X-PM-Signature"])
    with pytest.raises(InvalidSignature):
        key.public_key().verify(signature, b"1GET/v1/b")


def test_creds_from_env_returns_none_when_unset(monkeypatch):
    """Absent credentials are the normal case, not an error — the public
    gateway needs none."""
    monkeypatch.delenv("POLYMARKET_US_API_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_US_PRIVATE_KEY_PATH", raising=False)
    assert creds_from_env() is None


def test_creds_from_env_raises_when_only_the_id_is_set(monkeypatch, tmp_path):
    """Half-configured credentials are a mistake, not a choice to use REST.

    This test previously asserted `is None`. That was right while the REST
    fallback was merely slower; it stopped being right once the hosting design
    made cheap restarts a load-bearing property of the credentialed WebSocket
    path (measured: +31s to a fully-served shard). Returning None here selects
    REST silently, so the premise dissolves while the app still reports healthy.
    """
    import pytest

    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", "kid")
    monkeypatch.delenv("POLYMARKET_US_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.delenv("POLYMARKET_US_PRIVATE_KEY", raising=False)
    with pytest.raises(RuntimeError, match="POLYMARKET_US_PRIVATE_KEY"):
        creds_from_env()


def test_creds_from_env_loads_a_key_file(monkeypatch, tmp_path):
    _key, secret_b64 = _keypair()
    path = tmp_path / "pm-us.key"
    path.write_text(secret_b64 + "\n", encoding="utf-8")  # trailing newline is normal

    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", "kid-abc")
    monkeypatch.setenv("POLYMARKET_US_PRIVATE_KEY_PATH", str(path))

    creds = creds_from_env()
    assert creds is not None
    assert creds.key_id == "kid-abc"


def test_creds_from_env_raises_on_an_unreadable_key(monkeypatch, tmp_path):
    """A bad key must now crash startup — deliberately reversing this test.

    Its original rationale was "the public gateway still works", which is true
    and was the right call while REST was merely a slower equivalent. The
    hosting design changed what is at stake: it rests on the credentialed
    WebSocket path refilling the book in seconds, so a broken key that quietly
    selects REST leaves the app healthy-looking, slow to restart, and with one
    log line on a box nobody is tailing as the only evidence.

    Absent credentials still return None — that is the documented no-KYC path
    and it is covered by its own test. Only *set but unusable* raises.
    """
    import pytest

    path = tmp_path / "bad.key"
    path.write_text("garbage", encoding="utf-8")
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", "kid")
    monkeypatch.setenv("POLYMARKET_US_PRIVATE_KEY_PATH", str(path))
    monkeypatch.delenv("POLYMARKET_US_PRIVATE_KEY", raising=False)
    with pytest.raises(RuntimeError, match="POLYMARKET_US_PRIVATE_KEY_PATH"):
        creds_from_env()
