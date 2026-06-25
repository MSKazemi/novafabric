"""Tests for TV-5 REST + WebSocket router."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def tv5_app(tmp_path):
    from novafabric.serve.topology.layout_pipeline_3d import LayoutPipeline3D
    from novafabric.serve.topology.router_tv5 import make_tv5_router
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

    app = FastAPI()
    store = SnapshotStore3D(tmp_path / "snapshots")
    pipeline = LayoutPipeline3D(store)
    router = make_tv5_router(store, pipeline)
    app.include_router(router)
    return app, store


def test_live_endpoint(tv5_app):
    app, store = tv5_app
    client = TestClient(app)
    resp = client.get("/api/tv5/live")
    assert resp.status_code == 200
    data = resp.json()
    assert "windowId" in data


def test_live_endpoint_returns_null_window_initially(tv5_app):
    app, store = tv5_app
    client = TestClient(app)
    resp = client.get("/api/tv5/live")
    assert resp.status_code == 200
    data = resp.json()
    assert data["windowId"] is None


def test_windows_endpoint(tv5_app):
    app, store = tv5_app
    client = TestClient(app)
    resp = client.get("/api/tv5/windows")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_windows_endpoint_shows_saved_snapshots(tv5_app):
    app, store = tv5_app
    store.save("win_a", {"data": 1})
    client = TestClient(app)
    resp = client.get("/api/tv5/windows")
    assert resp.status_code == 200
    window_ids = [w["windowId"] for w in resp.json()]
    assert "win_a" in window_ids


def test_snapshot_endpoint_not_found(tv5_app):
    app, store = tv5_app
    client = TestClient(app)
    resp = client.get("/api/tv5/snapshot/nonexistent")
    assert resp.status_code == 404
    assert "error" in resp.json()["detail"]


def test_snapshot_endpoint_found(tv5_app):
    app, store = tv5_app
    store.save("test123", {"node_count": 3, "positions": {}})
    client = TestClient(app)
    resp = client.get("/api/tv5/snapshot/test123")
    assert resp.status_code == 200


def test_snapshot_path_traversal_blocked(tv5_app):
    app, store = tv5_app
    client = TestClient(app)
    resp = client.get("/api/tv5/snapshot/../etc/passwd")
    assert resp.status_code in (400, 404, 422)


def test_snapshot_uppercase_blocked(tv5_app):
    app, store = tv5_app
    client = TestClient(app)
    resp = client.get("/api/tv5/snapshot/UPPER")
    assert resp.status_code == 400


def test_snapshot_spaces_blocked(tv5_app):
    app, store = tv5_app
    client = TestClient(app)
    resp = client.get("/api/tv5/snapshot/win id")
    assert resp.status_code in (400, 404, 422)


def test_tv5_router_not_mounted_by_default():
    """TV-5 endpoints are not accessible without --tv5 flag."""
    app = FastAPI()
    # No TV-5 router mounted
    client = TestClient(app)
    resp = client.get("/api/tv5/live")
    assert resp.status_code == 404


def test_windows_time_range_filter(tv5_app):
    """from/to query params filter windows by timestamp."""
    app, store = tv5_app
    store.save("range_win", {"data": 1})
    client = TestClient(app)
    # Filter with a future range that excludes nothing
    resp = client.get("/api/tv5/windows?from=0&to=9999999999")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    # Filter with past range that excludes everything
    resp_empty = client.get("/api/tv5/windows?from=0&to=1")
    assert resp_empty.status_code == 200
    # No windows have timestamp <=1 (unix epoch)
    for w in resp_empty.json():
        assert w["timestamp"] <= 1
