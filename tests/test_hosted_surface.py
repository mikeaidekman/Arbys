"""The two ways the hosted page was broken while every existing test passed.

Both are deployment-shaped: the suite drove the API directly at its real paths
over HTTP, which is neither of the things a browser does. It asks for `/api/...`
because that is what the dev proxy taught the frontend to send, and it opens a
websocket, which resolves dependencies down a different code path entirely.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from arbys.backend.app import create_app


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


# --- the websocket dependency ----------------------------------------------

def test_the_opportunity_websocket_accepts_a_connection(client):
    """`require_access` was annotated `Request`, which FastAPI cannot supply to
    a websocket route -- so the app-level dependency raised
    `TypeError: missing 1 required positional argument: 'request'` while
    resolving, and the connection was rejected with a 500.

    That happens before `enabled()` is consulted, so it broke the live feed
    with Access switched off, which is the configuration this test runs in and
    the one production was running in when it was found.
    """
    with client.websocket_connect("/ws/opportunities") as ws:
        assert ws is not None


def test_the_websocket_is_reachable_under_the_api_prefix(client):
    with client.websocket_connect("/api/ws/opportunities") as ws:
        assert ws is not None


# --- the /api prefix --------------------------------------------------------

@pytest.mark.parametrize("path", ["/monitored", "/opportunities", "/health"])
def test_the_api_answers_under_the_prefix_the_spa_actually_uses(client, path):
    """The built SPA requests `/api/...`; vite's proxy strips that in dev and
    nothing stripped it in the container, so every call 404'd against a page
    that rendered blank rather than erroring."""
    assert client.get(path).status_code == 200
    assert client.get(f"/api{path}").status_code == 200


def test_the_prefix_strip_does_not_invent_routes(client):
    """Stripping must not turn an unknown path into a 200."""
    assert client.get("/api/no-such-endpoint").status_code == 404


def test_a_bare_api_root_does_not_500(client):
    assert client.get("/api").status_code in (200, 404, 405)
