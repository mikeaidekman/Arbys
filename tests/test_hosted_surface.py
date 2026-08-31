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


# --- the built SPA's static files -------------------------------------------

@pytest.fixture
def built_spa(tmp_path, monkeypatch):
    """A dist tree shaped like the real one: a hashed bundle, a root-level file,
    and a nested directory copied verbatim out of `frontend/public/`."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "design" / "industry").mkdir(parents=True)
    (dist / "index.html").write_text("<html>arbys</html>", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("//bundle", encoding="utf-8")
    (dist / "design" / "industry" / "styles.css").write_text(".btn{}", encoding="utf-8")
    monkeypatch.setenv("ARBYS_SPA_DIR", str(dist))
    with TestClient(create_app()) as c:
        yield c


def test_the_vendored_design_system_is_served(built_spa):
    """`_mount_spa` served root-level *files* and the `assets` directory only,
    so every other directory Vite copies out of `public/` was missing. That is
    where the vendored design system lives -- the whole basis of the UI -- and
    it 404'd in the container while `/favicon.svg` beside it worked. Nothing
    raised; the page just rendered with its component styles gone.
    """
    r = built_spa.get("/design/industry/styles.css")
    assert r.status_code == 200
    assert ".btn" in r.text


def test_the_hashed_bundle_and_root_files_still_serve(built_spa):
    assert built_spa.get("/assets/index-abc123.js").status_code == 200
    assert built_spa.get("/favicon.svg").status_code == 200
    assert built_spa.get("/").status_code == 200


def test_static_mounts_are_not_a_catch_all(built_spa):
    """Named routes and real directory names only -- an unknown path must 404
    rather than quietly returning index.html with a 200."""
    assert built_spa.get("/not-a-real-route").status_code == 404
    assert built_spa.get("/design/industry/nope.css").status_code == 404


# --- endpoints that block the event loop ------------------------------------

def test_expensive_endpoints_are_not_coroutines():
    """An `async def` handler with no `await` runs to completion on the event
    loop and freezes every other task for its whole duration.

    `/monitored` is ~158 lines of Decimal arithmetic over every registered
    group and awaits nothing. On the hosted machine that measured 5.3s per call
    across 864 groups, giving a loop-lag p95 of 4.4-6.0s while p50 stayed at
    1ms -- idle, then frozen, which is one blocking call rather than a busy box.

    Six seconds matters twice over: both venue websockets run
    `ping_timeout=20`, and `ARBYS_POLYMARKET_US_PRIORITY_DARK_AFTER_S` is 6s, so
    a stall this long marks live markets dark by itself and escalates into shard
    rebuilds that replay hours-old books.

    Declared `def`, FastAPI runs it in a threadpool and the loop keeps
    breathing. This asserts the property rather than the keyword, because the
    keyword is easy to reintroduce while tidying.
    """
    import inspect

    app = create_app()
    offenders = []
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        path = getattr(route, "path", "")
        if endpoint is None or path not in ("/monitored",):
            continue
        if inspect.iscoroutinefunction(endpoint):
            offenders.append(path)
    assert not offenders, (
        f"{offenders} are coroutines but never await -- they will block the "
        "event loop and stall both venue websockets"
    )
