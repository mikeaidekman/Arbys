"""One origin: FastAPI serves the built SPA alongside the API.

That removes CORS, the `/api` prefix-strip, and the second deploy target — the
rewrite in `frontend/vite.config.ts` is dev-server only, so any plan that keeps
the frontend separate has to reimplement it somewhere and 404s completely if it
is missed.

The risk being tested against is a catch-all route swallowing the API. This
implementation avoids it structurally by serving three named client routes
rather than `/{path:any}`, and these tests pin that: an unknown path must 404
rather than quietly returning index.html, which is how a typo'd endpoint turns
into a frontend that renders and an API that appears to have vanished.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from arbys.backend.app import create_app

CLIENT_ROUTES = ("/", "/admin", "/account")


@pytest.fixture
def dist(tmp_path, monkeypatch):
    """A minimal built SPA, so these tests never depend on `npm run build`."""
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>Arbys</title>", encoding="utf-8")
    (d / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (d / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    monkeypatch.setenv("ARBYS_SPA_DIR", str(d))
    return d


@pytest.mark.parametrize("route", CLIENT_ROUTES)
def test_client_routes_return_index_html(dist, route):
    """`/account` and `/admin` are react-router paths with no server route.
    Without this they 404 on a hard refresh or a pasted link."""
    with TestClient(create_app()) as client:
        r = client.get(route)
        assert r.status_code == 200
        assert "<title>Arbys</title>" in r.text


def test_hashed_assets_are_served(dist):
    with TestClient(create_app()) as client:
        assert client.get("/assets/index-abc123.js").status_code == 200
        assert client.get("/favicon.svg").status_code == 200


def test_the_api_still_answers(dist):
    """The SPA mount must not shadow the API it shares an origin with."""
    with TestClient(create_app()) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


@pytest.mark.parametrize(
    "path",
    [
        "/nonsense",
        "/paper/default/nonsense",  # API-shaped, but no such endpoint
        "/monitored/extra",
        "/assets/missing.js",
    ],
)
def test_unknown_paths_still_404(dist, path):
    """The regression that a catch-all would cause.

    Returning index.html here would mean a mistyped endpoint renders the
    frontend with a 200 — so a broken API call looks like a working page, and
    the failure surfaces as confusing UI rather than an HTTP error.
    """
    with TestClient(create_app()) as client:
        assert client.get(path).status_code == 404


def test_a_missing_dist_does_not_break_the_api(tmp_path, monkeypatch):
    """`frontend/dist` is gitignored, so a dev who has never run
    `npm run build` must still get a working API rather than a boot failure."""
    monkeypatch.setenv("ARBYS_SPA_DIR", str(tmp_path / "does-not-exist"))
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 404


def test_health_reports_which_data_path_each_venue_is_on(dist):
    """"Am I actually on the fast path?" answerable from outside the box.

    A credentialed WebSocket refills the quote book in seconds after a restart;
    REST takes one poll per leg against a rate-limited public tier. The whole
    hosting argument rests on the first, so a silent downgrade to the second
    needs to be visible somewhere other than a log line.

    Empty here because ingest is off in tests — which is a configuration, not
    a fault, and the field says so by being absent rather than wrong.
    """
    with TestClient(create_app()) as client:
        body = client.get("/health").json()
        assert "adapters" in body
        assert body["adapters"] == {}
