"""Ed25519 request signing for the authenticated Polymarket US API.

Market data comes from the **public** gateway (``gateway.polymarket.us``) and
needs none of this. These credentials are for ``api.polymarket.us``: the market
WebSocket, portfolio, and order placement. Obtaining them requires completed
identity verification, then a key generated at ``polymarket.us/developer``.

The portal shows the secret **once**. There is no recovery — a lost key means
generating a new pair.

Signing, per the venue's documentation::

    message   = f"{timestamp_ms}{METHOD}{path}"      # no separators
    signature = base64(ed25519_sign(message))
    headers   = X-PM-Access-Key / X-PM-Timestamp / X-PM-Signature

The timestamp must be within 30 seconds of server time, so a badly skewed
system clock fails every request with what looks like an auth error.

Credentials are optional everywhere. ``creds_from_env`` returns ``None`` when
they are absent or unreadable rather than raising, because the public gateway
keeps working without them — the same shape as ``kalshi_ws_creds_from_env``.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ed25519

AUTH_BASE = "https://api.polymarket.us"

log = logging.getLogger(__name__)

_ED25519_SEED_BYTES = 32


@dataclass(frozen=True)
class PolymarketUsCredentials:
    key_id: str
    secret_key: ed25519.Ed25519PrivateKey


def load_secret_key(secret_b64: str) -> ed25519.Ed25519PrivateKey:
    """Decode the base64 secret the developer portal issues.

    Takes the first 32 bytes: an Ed25519 private key is a 32-byte seed, but
    some tooling emits seed+public concatenated, and the documented decode
    slices regardless.

    Raises ``ValueError`` on anything that is not a usable key, so callers can
    distinguish "no credentials configured" from "credentials are wrong".
    """
    try:
        raw = base64.b64decode(secret_b64.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"secret key is not valid base64: {exc}") from exc
    if len(raw) < _ED25519_SEED_BYTES:
        raise ValueError(
            f"secret key decodes to {len(raw)} bytes; need at least {_ED25519_SEED_BYTES}"
        )
    return ed25519.Ed25519PrivateKey.from_private_bytes(raw[:_ED25519_SEED_BYTES])


def signing_message(timestamp_ms: str, method: str, path: str) -> str:
    """``f"{timestamp}{METHOD}{path}"`` — no separators, method uppercased.

    A stray separator or a lowercase verb produces a well-formed signature
    over the wrong string, which the venue rejects as an auth failure with no
    hint that the message construction is at fault.
    """
    return f"{timestamp_ms}{method.upper()}{path}"


def auth_headers(
    creds: PolymarketUsCredentials,
    method: str,
    path: str,
    *,
    timestamp_ms: int | None = None,
) -> dict[str, str]:
    """Signed headers for one request.

    ``path`` excludes the query string, matching the venue's examples.
    ``timestamp_ms`` is injectable so signing is testable without a clock.
    """
    ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    message = signing_message(ts, method, path)
    signature = base64.b64encode(creds.secret_key.sign(message.encode())).decode()
    return {
        "X-PM-Access-Key": creds.key_id,
        "X-PM-Timestamp": ts,
        "X-PM-Signature": signature,
        "Content-Type": "application/json",
    }


def creds_from_env() -> PolymarketUsCredentials | None:
    """Load credentials from the environment, or ``None`` if unusable.

    Needs both ``POLYMARKET_US_API_KEY_ID`` and
    ``POLYMARKET_US_PRIVATE_KEY_PATH``. The key file holds the base64 secret
    exactly as the portal displayed it; a trailing newline is fine.

    The key lives in a file rather than an env var on purpose, matching the
    Kalshi ``.pem`` convention: keep it **outside this repo**.
    """
    key_id = os.environ.get("POLYMARKET_US_API_KEY_ID")
    key_path = os.environ.get("POLYMARKET_US_PRIVATE_KEY_PATH")
    if not key_id or not key_path:
        return None
    try:
        secret_b64 = Path(key_path).read_text(encoding="utf-8")
        return PolymarketUsCredentials(key_id=key_id, secret_key=load_secret_key(secret_b64))
    except (OSError, ValueError) as exc:
        log.error("failed to load Polymarket US secret key from %s: %s", key_path, exc)
        return None
