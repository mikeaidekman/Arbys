"""Cloudflare Access verification.

This app has no authentication of its own and five mutating endpoints, two of
which are dangerous in a way that is not obvious: ``POST /quotes`` lets a
caller inject arbitrary prices and so manufacture an arbitrage, and
``POST /paper/{id}/reset`` wipes the ledger. On a laptop that is survivable
because nothing can reach it. On a platform the hostname is public by
construction, so this becomes load-bearing.

**Verify, do not merely front.** Putting Access in front of the hostname is
not enough on its own: `<app>.fly.dev` remains reachable directly, carries no
signed assertion, and would sail straight past a proxy-only arrangement. So
every request must present a `Cf-Access-Jwt-Assertion` that validates against
the team's public keys *and* names this application in `aud`. A direct hit on
the Fly hostname then fails, because Cloudflare is the only thing that can mint
one.

Off when unconfigured. With `CF_ACCESS_TEAM_DOMAIN` or `CF_ACCESS_AUD` unset
this is a no-op, so local dev and the test suite are unaffected — and so that
a misconfigured deploy fails visibly at the door rather than half-protecting
the app.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx
import jwt
from fastapi import HTTPException, Request

log = logging.getLogger(__name__)

# Probed by the platform, and it cannot present an assertion: Fly's health
# checks reach the machine internally without traversing Cloudflare at all.
# Exempting it is also what lets a human check `/health` from a terminal to
# find out whether the app is up when Access itself is the thing misbehaving.
EXEMPT_PATHS = frozenset({"/health"})

# Cloudflare rotates signing keys and publishes more than one at a time — the
# live endpoint served two when this was written — so a key is selected by the
# token's `kid` rather than by taking the first. Refetched when a `kid` is
# unknown, which is what makes a rotation invisible instead of an outage.
_JWKS_TTL_S = 3600.0

_jwks_cache: dict[str, Any] = {}
_jwks_fetched_at = 0.0


def team_domain() -> str | None:
    return os.environ.get("CF_ACCESS_TEAM_DOMAIN") or None


def audience() -> str | None:
    return os.environ.get("CF_ACCESS_AUD") or None


def enabled() -> bool:
    """Both halves or neither. One alone cannot verify anything."""
    return bool(team_domain() and audience())


def certs_url(team: str) -> str:
    return f"https://{team}/cdn-cgi/access/certs"


def issuer(team: str) -> str:
    return f"https://{team}"


def _fetch_jwks(team: str) -> dict[str, Any]:
    """Fetch the team's public keys. Separate so tests can replace it — the
    suite must never reach out to Cloudflare."""
    resp = httpx.get(certs_url(team), timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def _jwks(team: str, *, force: bool = False) -> dict[str, Any]:
    global _jwks_cache, _jwks_fetched_at
    stale = (time.monotonic() - _jwks_fetched_at) > _JWKS_TTL_S
    if force or stale or not _jwks_cache:
        _jwks_cache = _fetch_jwks(team)
        _jwks_fetched_at = time.monotonic()
    return _jwks_cache


def reset_jwks_cache() -> None:
    """Drop the cached keys. For tests, and after a configuration change."""
    global _jwks_cache, _jwks_fetched_at
    _jwks_cache = {}
    _jwks_fetched_at = 0.0


def _key_for(team: str, token: str) -> Any:
    kid = jwt.get_unverified_header(token).get("kid")
    if not kid:
        raise HTTPException(status_code=403, detail="access assertion has no kid")
    for force in (False, True):
        for entry in _jwks(team, force=force).get("keys", []):
            if entry.get("kid") == kid:
                return jwt.PyJWK(entry).key
        # Unknown kid on the cached set: Cloudflare may have rotated. One
        # forced refetch, then give up rather than hammering the endpoint on
        # every request with a bad token.
    raise HTTPException(status_code=403, detail="access assertion signed by an unknown key")


async def require_access(request: Request) -> None:
    """Global dependency. Rejects anything without a valid Access assertion."""
    if not enabled():
        return
    if request.url.path in EXEMPT_PATHS:
        return

    token = request.headers.get("Cf-Access-Jwt-Assertion") or request.cookies.get(
        "CF_Authorization"
    )
    if not token:
        # A direct request to the platform hostname lands here, which is the
        # whole point: the origin is not quietly reachable around Access.
        raise HTTPException(status_code=403, detail="no Cloudflare Access assertion")

    team, aud = team_domain(), audience()
    assert team is not None and aud is not None  # enabled() checked both
    try:
        jwt.decode(
            token,
            key=_key_for(team, token),
            algorithms=["RS256"],  # never from the token's own header
            audience=aud,
            issuer=issuer(team),
        )
    except HTTPException:
        raise
    except jwt.InvalidTokenError as exc:
        # Deliberately terse to the caller and detailed to the log: the reason
        # a token failed is useful to an operator and useful to an attacker.
        log.warning("rejected Access assertion for %s: %s", request.url.path, exc)
        raise HTTPException(status_code=403, detail="invalid Cloudflare Access assertion") from exc
