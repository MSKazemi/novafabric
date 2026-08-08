"""ADR-0183 second slice: the legal-holds router mounts on the serve app.

Behavior preservation is proven by the pre-existing tests in
``tests/test_serve_holds.py`` passing unchanged; this module only asserts
the migration mechanics — the router's routes are present on the app and
the injected token dependency still guards them.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from novafabric.serve.app import create_app
from novafabric.serve.introspect import route_methods

LOCALHOST_HEADERS = {"host": "127.0.0.1:4321"}
TOKEN = "test-token-holds-router"


def _make_app(tmp_path: Path):  # noqa: ANN202
    return create_app(
        token=TOKEN, capsule_dir=tmp_path / ".novafabric" / "runs", db_path=None
    )


def test_holds_router_routes_are_mounted(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    mounted = route_methods(app)
    assert ("/api/holds", "GET") in mounted
    assert ("/api/holds", "POST") in mounted
    assert ("/api/holds/{hold_id}/release", "POST") in mounted


def test_migrated_holds_route_still_requires_token(tmp_path: Path) -> None:
    tc = TestClient(_make_app(tmp_path))
    assert tc.get("/api/holds", headers=LOCALHOST_HEADERS).status_code == 401
    assert (
        tc.get(
            "/api/holds", params={"token": TOKEN}, headers=LOCALHOST_HEADERS
        ).status_code
        == 200
    )
