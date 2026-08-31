"""Cloudflare Access, verified rather than merely fronted.

Every token here is minted locally against a throwaway RSA key and the JWKS
fetch is replaced, so the suite never contacts Cloudflare — which also means
these tests keep working if the account is torn down.

The case that matters most is `test_a_valid_signature_for_another_audience_is_rejected`:
a token signed by the same team but issued for a *different* Access
application is cryptographically perfect and must still fail. Skipping the
`aud` check is the classic way this integration is wrong while appearing to
work.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from arbys.backend import access
from arbys.backend.app import create_app

TEAM = "arbys-test.cloudflareaccess.com"
AUD = "aud-tag-under-test"
KID = "test-key-1"


@pytest.fixture
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def access_on(monkeypatch, signing_key):
    """Configure Access and serve our own key set instead of Cloudflare's."""
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
    monkeypatch.setattr(access, "_fetch_jwks", lambda _team: {"keys": [jwk]})
    access.reset_jwks_cache()
    yield
    access.reset_jwks_cache()


def _token(key, *, aud=AUD, iss=f"https://{TEAM}", kid=KID, exp_delta=3600):
    now = int(time.time())
    return jwt.encode(
        {"aud": aud, "iss": iss, "iat": now, "exp": now + exp_delta, "email": "x@y.z"},
        key,
        algorithm="RS256",
        headers={"kid": kid},
    )


def test_a_request_with_no_assertion_is_rejected(access_on):
    """A direct hit on the platform hostname carries no assertion. This is the
    assertion that proves the origin is not reachable around Access."""
    with TestClient(create_app()) as client:
        assert client.get("/monitored").status_code == 403


def test_a_valid_assertion_is_accepted(access_on, signing_key):
    with TestClient(create_app()) as client:
        r = client.get(
            "/monitored", headers={"Cf-Access-Jwt-Assertion": _token(signing_key)}
        )
        assert r.status_code == 200


def test_a_valid_signature_for_another_audience_is_rejected(access_on, signing_key):
    """Signed by the same team, issued for a different Access application.

    Cryptographically perfect and must still fail: without the `aud` check any
    application in the account could mint a token that opens this one.
    """
    with TestClient(create_app()) as client:
        token = _token(signing_key, aud="some-other-application")
        r = client.get("/monitored", headers={"Cf-Access-Jwt-Assertion": token})
        assert r.status_code == 403


def test_a_token_from_another_issuer_is_rejected(access_on, signing_key):
    with TestClient(create_app()) as client:
        token = _token(signing_key, iss="https://someone-else.cloudflareaccess.com")
        r = client.get("/monitored", headers={"Cf-Access-Jwt-Assertion": token})
        assert r.status_code == 403


def test_an_expired_assertion_is_rejected(access_on, signing_key):
    with TestClient(create_app()) as client:
        token = _token(signing_key, exp_delta=-60)
        r = client.get("/monitored", headers={"Cf-Access-Jwt-Assertion": token})
        assert r.status_code == 403


def test_a_token_signed_by_the_wrong_key_is_rejected(access_on):
    """The forgery case: right shape, right claims, not Cloudflare's key."""
    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with TestClient(create_app()) as client:
        r = client.get(
            "/monitored", headers={"Cf-Access-Jwt-Assertion": _token(impostor)}
        )
        assert r.status_code == 403


def test_an_unknown_kid_is_rejected(access_on, signing_key):
    with TestClient(create_app()) as client:
        token = _token(signing_key, kid="a-key-we-have-never-seen")
        r = client.get("/monitored", headers={"Cf-Access-Jwt-Assertion": token})
        assert r.status_code == 403


def test_health_is_exempt(access_on):
    """Fly's checks reach the machine internally and cannot present an
    assertion. It is also how a human finds out the app is up when Access
    itself is what is misbehaving."""
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200


def test_the_cookie_form_is_accepted(access_on, signing_key):
    """A browser following the Access login flow carries CF_Authorization."""
    with TestClient(create_app()) as client:
        client.cookies.set("CF_Authorization", _token(signing_key))
        assert client.get("/monitored").status_code == 200


def test_unconfigured_is_a_no_op(monkeypatch):
    """Local dev and the rest of the suite must be unaffected."""
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/monitored").status_code == 200


def test_half_configured_is_also_a_no_op(monkeypatch):
    """One half cannot verify anything. Failing open here is deliberate and
    narrow: `enabled()` is false, so the app behaves exactly as an
    unconfigured one — which is loud at deploy time, because every request
    succeeds and the operator notices immediately."""
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/monitored").status_code == 200


# --- websockets, which resolve dependencies down a different path -----------

def test_the_websocket_is_refused_without_an_assertion(access_on):
    """The terminal's live feed must be behind Access too. A websocket cannot
    carry a 403, so this closes with 1008 (policy violation) instead -- and the
    test asserts only that the connection is refused, since what surfaces to
    the client differs between a rejected handshake and a closed socket."""
    from starlette.websockets import WebSocketDisconnect

    with (
        TestClient(create_app()) as client,
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/ws/opportunities"),
    ):
        pass
    # Asserted precisely, and not as a bare "it raised": the bug this guards
    # against raised TypeError from the same call, which a loose
    # `pytest.raises(Exception)` would have accepted as a pass.
    assert excinfo.value.code == 1008


def test_the_websocket_is_accepted_with_a_valid_assertion(access_on, signing_key):
    with TestClient(create_app()) as client:
        client.cookies.set("CF_Authorization", _token(signing_key))
        with client.websocket_connect("/ws/opportunities") as ws:
            assert ws is not None
