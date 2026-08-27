"""Test-wide isolation from the developer's ``.env``.

``arbys/backend/app.py`` calls ``load_dotenv()`` at import, so importing the
app inside a test pulls in whatever the working ``.env`` happens to say. On a
machine configured for live trading that means ``ARBYS_ENABLE_INGEST=1`` and
``ARBYS_ENABLE_DISCOVERY=1``, and the suite quietly starts contacting Kalshi
and Polymarket for real.

That breaks the project's own rule that tests never hit a real venue, and it
fails in the worst way: not with an error, but by hanging. Observed 2026-08-12
— ``tests/test_backend_e2e.py`` ran indefinitely because Kalshi was
rate-limiting the machine and the adapter's 429 backoff never gave up. The
same file passes in 20 seconds with ingest off. It had passed earlier the same
day, which is what makes this so easy to misdiagnose as "the change I just
made": the test is only as reliable as an external venue's mood.

The auto-trader submits tickets the moment it is on, so a suite run on a
live-configured machine would otherwise write real paper history.

Credentials are cleared for the same reason. With them present an adapter
factory selects the authenticated WebSocket, and a test that merely bootstraps
``AppState`` would open a real socket to the venue.

Anything needing these on must set them explicitly, inside the test, where the
intent is visible.
"""

from __future__ import annotations

import os

import pytest

# Env vars that would let a test reach the outside world, and the value that
# keeps it inside. Sorted for readability; order is irrelevant.
_OFFLINE_ENV: dict[str, str] = {
    "ARBYS_ENABLE_INGEST": "0",
    "ARBYS_ENABLE_DISCOVERY": "0",
    "ARBYS_ENABLE_DRAFTKINGS": "0",
    "ARBYS_ENABLE_AUTO_TRADE": "0",
}

# Credential vars are removed rather than blanked: the loaders treat empty
# strings as absent already, but deleting makes "no credentials" unambiguous.
_CREDENTIAL_ENV: tuple[str, ...] = (
    "KALSHI_API_KEY_ID",
    "KALSHI_PRIVATE_KEY_PATH",
    "POLYMARKET_US_API_KEY_ID",
    "POLYMARKET_US_PRIVATE_KEY_PATH",
)


@pytest.fixture(autouse=True, scope="session")
def _force_offline_test_environment() -> None:
    """Pin the venue-facing switches off for the whole session.

    Autouse and session-scoped: a test that forgets to opt out of live ingest
    should still be offline, and the guarantee should not depend on import
    order or on which test happens to run first.
    """
    for key, value in _OFFLINE_ENV.items():
        os.environ[key] = value
    for key in _CREDENTIAL_ENV:
        os.environ.pop(key, None)
