"""The landing page must not be shadowed by the "static missing" placeholder.

Regression test for a bug where `nova serve` mounted the real Astro site at
`/` *after* `create_app` had already registered a `@app.get("/")` route
saying the site was missing. Starlette matches routes in registration order,
so the placeholder won permanently: the home page was unreachable and `/`
told the operator to run a build that had already been run.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from novafabric.serve.app import create_app


def _app(tmp_path: Path, **kwargs: object):
    return create_app(
        token="t0ken",
        capsule_dir=tmp_path / "capsules",
        db_path=tmp_path / "registry.db",
        **kwargs,  # type: ignore[arg-type]
    )


def test_placeholder_served_when_nobody_mounts_static(tmp_path: Path) -> None:
    """Default behaviour is unchanged: no static anywhere -> honest placeholder."""
    client = TestClient(_app(tmp_path), base_url="http://localhost")
    resp = client.get("/", params={"token": "t0ken"})
    assert resp.status_code == 200
    assert resp.json()["dashboard_static"].startswith("missing")


def test_placeholder_suppressed_when_caller_will_mount_static(tmp_path: Path) -> None:
    """The bug: the placeholder must not pre-empt a caller-mounted site."""
    app = _app(tmp_path, static_mounted_by_caller=True)

    # No route may claim "/" before the caller mounts there.
    root_routes = [
        r for r in app.routes if getattr(r, "path", None) == "/" and hasattr(r, "endpoint")
    ]
    assert not root_routes, (
        "create_app registered a '/' route despite static_mounted_by_caller=True; "
        "it would shadow the landing page the caller mounts afterwards"
    )


def test_caller_mounted_static_actually_serves_the_landing_page(tmp_path: Path) -> None:
    """End-to-end: mount static after create_app, exactly as nova serve does."""
    from fastapi.staticfiles import StaticFiles

    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!DOCTYPE html><h1>NovaFabric</h1>", encoding="utf-8")

    app = _app(tmp_path, static_mounted_by_caller=True)
    app.mount("/", StaticFiles(directory=str(static), html=True, check_dir=False), name="site")

    resp = TestClient(app, base_url="http://localhost").get("/", params={"token": "t0ken"})
    assert resp.status_code == 200
    assert "NovaFabric" in resp.text
    assert "dashboard_static" not in resp.text  # the placeholder never wins


def test_api_routes_still_win_over_the_static_mount(tmp_path: Path) -> None:
    """The reason static is mounted last — API routes must not be shadowed."""
    from fastapi.staticfiles import StaticFiles

    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<h1>site</h1>", encoding="utf-8")

    app = _app(tmp_path, static_mounted_by_caller=True)
    app.mount("/", StaticFiles(directory=str(static), html=True, check_dir=False), name="site")

    resp = TestClient(app, base_url="http://localhost").get("/api/runs", params={"token": "t0ken"})
    assert resp.status_code == 200
    assert "<h1>" not in resp.text  # served by the API, not the static catch-all
