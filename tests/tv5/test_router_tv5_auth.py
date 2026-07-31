"""TV-5 router auth — token required on HTTP routes, token + localhost on WS.

Before this fix the /api/tv5 surface (HTTP + WebSocket) was mounted with no
auth at all, and the WS scope bypassed the app-level host_header_guard.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

TEST_TOKEN = "tv5-auth-test-token"
LOCALHOST_HEADERS = {"host": "127.0.0.1:4321"}


@pytest.fixture
def tv5_app(tmp_path):
    from novafabric.serve.topology.layout_pipeline_3d import LayoutPipeline3D
    from novafabric.serve.topology.router_tv5 import make_tv5_router
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

    app = FastAPI()
    store = SnapshotStore3D(tmp_path / "snapshots")
    pipeline = LayoutPipeline3D(store)
    router = make_tv5_router(store, pipeline, token=TEST_TOKEN)
    app.include_router(router)
    return app, store


class TestHttpAuth:
    def test_no_token_is_401(self, tv5_app) -> None:
        app, _ = tv5_app
        client = TestClient(app)
        for path in ("/api/tv5/live", "/api/tv5/windows", "/api/tv5/snapshot/abc"):
            assert client.get(path).status_code == 401

    def test_wrong_token_is_401(self, tv5_app) -> None:
        app, _ = tv5_app
        client = TestClient(app)
        assert client.get("/api/tv5/live?token=wrong").status_code == 401
        assert (
            client.get(
                "/api/tv5/live", headers={"Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )

    def test_query_token_accepted(self, tv5_app) -> None:
        app, _ = tv5_app
        client = TestClient(app)
        assert client.get(f"/api/tv5/live?token={TEST_TOKEN}").status_code == 200

    def test_bearer_token_accepted(self, tv5_app) -> None:
        app, _ = tv5_app
        client = TestClient(app)
        resp = client.get(
            "/api/tv5/windows", headers={"Authorization": f"Bearer {TEST_TOKEN}"}
        )
        assert resp.status_code == 200

    def test_token_required_on_snapshot(self, tv5_app) -> None:
        app, store = tv5_app
        store.save("win1", {"data": 1})
        client = TestClient(app)
        assert client.get("/api/tv5/snapshot/win1").status_code == 401
        assert (
            client.get(f"/api/tv5/snapshot/win1?token={TEST_TOKEN}").status_code == 200
        )


class TestWebSocketAuth:
    def test_ws_rejects_non_localhost_host(self, tv5_app) -> None:
        app, _ = tv5_app
        client = TestClient(app)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                f"/api/tv5/ws?token={TEST_TOKEN}",
                headers={"host": "evil.example.com"},
            ):
                pass
        assert exc_info.value.code == 4403

    def test_ws_rejects_missing_token(self, tv5_app) -> None:
        app, _ = tv5_app
        client = TestClient(app)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/api/tv5/ws", headers=LOCALHOST_HEADERS):
                pass
        assert exc_info.value.code == 4401

    def test_ws_rejects_wrong_token(self, tv5_app) -> None:
        app, _ = tv5_app
        client = TestClient(app)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/api/tv5/ws?token=wrong", headers=LOCALHOST_HEADERS
            ):
                pass
        assert exc_info.value.code == 4401

    def test_ws_accepts_valid_token_on_localhost(self, tv5_app) -> None:
        app, _ = tv5_app
        client = TestClient(app)
        import json

        with client.websocket_connect(
            f"/api/tv5/ws?token={TEST_TOKEN}", headers=LOCALHOST_HEADERS
        ) as ws:
            ws.send_text(json.dumps({"type": "subscribe", "topologyId": "t1"}))
            msg = json.loads(ws.receive_text())
            assert msg == {"type": "subscribed", "topologyId": "t1"}
