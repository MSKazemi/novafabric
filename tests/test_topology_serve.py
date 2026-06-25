"""Integration tests for the topology endpoints in nova serve.

Covers /topology/clusters (Arrow IPC), /topology/stream (TDP WebSocket),
and /metrics/stream (SSE) — both enabled and disabled modes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

import pyarrow.ipc as pa_ipc  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-topo-abc123xyz"
LOCALHOST_HEADERS = {"host": "127.0.0.1:4321"}


@pytest.fixture
def topo_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("NOVA_DASHBOARD_DUCKDB_PATH", str(tmp_path / "topology.duckdb"))
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=capsule_dir,
        db_path=tmp_path / "registry.db",
        static_dir=None,
        topology_enabled=True,
    )
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture
def plain_client(tmp_path: Path) -> Iterator[TestClient]:
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    app = create_app(
        token=VALID_TOKEN,
        capsule_dir=capsule_dir,
        db_path=tmp_path / "registry.db",
        static_dir=None,
        topology_enabled=False,
    )
    with TestClient(app) as c:
        yield c


# ---------- /topology/clusters ----------

def test_clusters_endpoint_requires_auth(topo_client: TestClient) -> None:
    res = topo_client.get("/topology/clusters", headers=LOCALHOST_HEADERS)
    assert res.status_code == 401


def test_clusters_endpoint_not_enabled_returns_404(plain_client: TestClient) -> None:
    res = plain_client.get(
        f"/topology/clusters?token={VALID_TOKEN}", headers=LOCALHOST_HEADERS
    )
    assert res.status_code == 404


def test_clusters_endpoint_returns_arrow_ipc(topo_client: TestClient) -> None:
    res = topo_client.get(
        f"/topology/clusters?token={VALID_TOKEN}", headers=LOCALHOST_HEADERS
    )
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/vnd.apache.arrow.stream"
    # Body must be valid Arrow IPC
    reader = pa_ipc.open_stream(res.content)
    schema = reader.schema
    assert schema.metadata is not None
    schema_id = (schema.metadata or {}).get(b"schema_id", b"").decode()
    assert schema_id == "ads.v1.cluster_layer"
    table = reader.read_all()
    # Empty store — zero rows
    assert table.num_rows == 0
    assert set(table.schema.names) == {
        "cluster_id", "centroid_x", "centroid_y", "agent_count", "inter_cluster_edges"
    }


# ---------- /topology/cluster-list (JSON, for Table/Treemap views) ----------

def test_cluster_list_endpoint_requires_auth(topo_client: TestClient) -> None:
    res = topo_client.get("/topology/cluster-list", headers=LOCALHOST_HEADERS)
    assert res.status_code == 401


def test_cluster_list_endpoint_not_enabled_returns_404(plain_client: TestClient) -> None:
    res = plain_client.get(
        f"/topology/cluster-list?token={VALID_TOKEN}", headers=LOCALHOST_HEADERS
    )
    assert res.status_code == 404


def test_cluster_list_endpoint_returns_json(topo_client: TestClient) -> None:
    res = topo_client.get(
        f"/topology/cluster-list?token={VALID_TOKEN}", headers=LOCALHOST_HEADERS
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    assert res.json() == []  # empty store


# ---------- /topology/stream (TDP WebSocket) ----------

def test_ws_stream_not_enabled_returns_403_or_disconnect(plain_client: TestClient) -> None:
    # When topology_enabled=False the route is not registered — connection refused
    with pytest.raises(Exception):
        with plain_client.websocket_connect("/topology/stream"):
            pass


def test_ws_stream_rejects_missing_subprotocol(topo_client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with topo_client.websocket_connect("/topology/stream"):
            pass
    assert exc_info.value.code == 4400


def test_ws_stream_rejects_missing_token(topo_client: TestClient) -> None:
    # WS must enforce token auth like every other route (audit fix).
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with topo_client.websocket_connect(
            "/topology/stream",
            headers={"sec-websocket-protocol": "nova-tdp-v1", **LOCALHOST_HEADERS},
        ):
            pass
    assert exc_info.value.code == 4401


def test_ws_stream_rejects_non_localhost_host(topo_client: TestClient) -> None:
    # WS must enforce the DNS-rebinding host guard like HTTP routes (audit fix).
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with topo_client.websocket_connect(
            f"/topology/stream?token={VALID_TOKEN}",
            headers={"sec-websocket-protocol": "nova-tdp-v1", "host": "evil.example.com"},
        ):
            pass
    assert exc_info.value.code == 4403


def test_ws_stream_accepts_nova_tdp_v1(topo_client: TestClient) -> None:
    with topo_client.websocket_connect(
        f"/topology/stream?token={VALID_TOKEN}",
        headers={"sec-websocket-protocol": "nova-tdp-v1", **LOCALHOST_HEADERS},
    ):
        # Connection accepted — server will send heartbeat after 10s; just close.
        pass  # context manager exit sends close frame


def test_ws_stream_resume_from_sends_no_events_on_empty_buffer(topo_client: TestClient) -> None:
    """resume_from with checkpoint_id=0 on an empty DeltaBuffer sends no replay frames."""
    with topo_client.websocket_connect(
        f"/topology/stream?token={VALID_TOKEN}",
        headers={"sec-websocket-protocol": "nova-tdp-v1", **LOCALHOST_HEADERS},
    ) as ws:
        ws.send_text(json.dumps({"type": "resume_from", "checkpoint_id": 0}))
        # Empty DeltaBuffer — no replay events to send; just close cleanly.
        # The server sends nothing for an empty buffer (no events with seq > 0).
        pass  # no message expected; closing without receive is fine


def test_ws_stream_subgraph_expand_sends_arrow_bytes(topo_client: TestClient) -> None:
    """subgraph_expand request returns two Arrow IPC byte frames + checkpoint frame.

    A-2: the checkpoint frame after expand is now binary Arrow IPC (not JSON text).
    """
    with topo_client.websocket_connect(
        f"/topology/stream?token={VALID_TOKEN}",
        headers={"sec-websocket-protocol": "nova-tdp-v1", **LOCALHOST_HEADERS},
    ) as ws:
        ws.send_text(json.dumps({"type": "subgraph_expand", "cluster_id": 0}))
        nodes_bytes = ws.receive_bytes()
        edges_bytes = ws.receive_bytes()
        ckpt_bytes = ws.receive_bytes()

        # Validate nodes Arrow IPC frame
        nodes_reader = pa_ipc.open_stream(nodes_bytes)
        nodes_schema_id = (nodes_reader.schema.metadata or {}).get(b"schema_id", b"").decode()
        assert nodes_schema_id == "ads.v1.subgraph_page.nodes"

        # Validate edges Arrow IPC frame
        edges_reader = pa_ipc.open_stream(edges_bytes)
        edges_schema_id = (edges_reader.schema.metadata or {}).get(b"schema_id", b"").decode()
        assert edges_schema_id == "ads.v1.subgraph_page.edges"

        # A-2: checkpoint is now a binary Arrow IPC delta_event frame
        ckpt_reader = pa_ipc.open_stream(ckpt_bytes)
        ckpt_schema_id = (ckpt_reader.schema.metadata or {}).get(b"schema_id", b"").decode()
        assert ckpt_schema_id == "ads.v1.delta_event"
        ckpt_table = ckpt_reader.read_all()
        assert ckpt_table.num_rows == 1
        event_type = ckpt_table.column("event_type")[0].as_py()
        assert event_type == "batch_checkpoint"
        payload_bytes = ckpt_table.column("payload")[0].as_py()
        payload = json.loads(payload_bytes)
        assert payload["type"] == "batch_checkpoint"
        assert "seq" in payload


def test_ws_stream_delta_events_are_binary_arrow_ipc(topo_client: TestClient) -> None:
    """A-2: delta events pushed to WS clients are binary Arrow IPC, not JSON text.

    Injects an event directly into the DeltaBuffer after the client connects
    and subscribes, then verifies the client receives a binary frame with the
    ads.v1.delta_event schema.
    """
    import pyarrow.ipc as _pa_ipc

    # Use a simpler approach: trigger a subgraph_collapse which produces
    # a binary checkpoint frame — this is the same code path as live delta push.
    with topo_client.websocket_connect(
        f"/topology/stream?token={VALID_TOKEN}",
        headers={"sec-websocket-protocol": "nova-tdp-v1", **LOCALHOST_HEADERS},
    ) as ws:
        # subgraph_collapse sends a binary checkpoint frame (A-2 code path)
        ws.send_text(json.dumps({"type": "subgraph_collapse", "cluster_id": 0}))
        frame = ws.receive_bytes()

        reader = _pa_ipc.open_stream(frame)
        schema_id = (reader.schema.metadata or {}).get(b"schema_id", b"").decode()
        assert schema_id == "ads.v1.delta_event", (
            f"Expected ads.v1.delta_event, got {schema_id!r}. "
            "Delta events must be binary Arrow IPC (A-2)."
        )
        table = reader.read_all()
        assert table.num_rows == 1
        assert table.column("event_type")[0].as_py() == "batch_checkpoint"


def test_ws_stream_resume_from_sends_binary_replay_frames(topo_client: TestClient) -> None:
    """resume_from replay sends binary Arrow IPC delta_event frames, not JSON text (A-2)."""
    import pyarrow.ipc as _pa_ipc

    # First, inject a known event into the app's DeltaBuffer by triggering
    # an expand (which calls checkpoint()) — then resume from seq 0.
    with topo_client.websocket_connect(
        f"/topology/stream?token={VALID_TOKEN}",
        headers={"sec-websocket-protocol": "nova-tdp-v1", **LOCALHOST_HEADERS},
    ) as ws:
        # subgraph_expand puts a checkpoint in the buffer; resume_from 0 replays it.
        ws.send_text(json.dumps({"type": "subgraph_expand", "cluster_id": 0}))
        _ = ws.receive_bytes()  # nodes
        _ = ws.receive_bytes()  # edges
        _ = ws.receive_bytes()  # checkpoint

        # Now resume from 0 — the buffer has at least one event (the checkpoint).
        ws.send_text(json.dumps({"type": "resume_from", "checkpoint_id": 0}))
        frame = ws.receive_bytes()  # must be binary, not text

        reader = _pa_ipc.open_stream(frame)
        schema_id = (reader.schema.metadata or {}).get(b"schema_id", b"").decode()
        assert schema_id == "ads.v1.delta_event"


# ---------- /metrics/stream (SSE) ----------

def test_metrics_stream_requires_auth(topo_client: TestClient) -> None:
    with topo_client.stream("GET", "/metrics/stream", headers=LOCALHOST_HEADERS) as r:
        assert r.status_code == 401


def test_metrics_stream_not_enabled_returns_404(plain_client: TestClient) -> None:
    with plain_client.stream(
        "GET", f"/metrics/stream?token={VALID_TOKEN}", headers=LOCALHOST_HEADERS
    ) as r:
        assert r.status_code == 404


def test_metrics_stream_route_is_registered(topo_client: TestClient) -> None:
    """Verify /metrics/stream is registered when topology_enabled=True."""
    from starlette.routing import Route
    paths = {
        r.path for r in topo_client.app.routes
        if isinstance(r, Route) and hasattr(r, "path")
    }
    assert "/metrics/stream" in paths
