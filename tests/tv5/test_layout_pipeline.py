"""Tests for TV-5 layout pipeline."""
from __future__ import annotations

import asyncio


def test_compute_layout_3d_basic():
    """Basic layout computation returns positions for all nodes."""
    from novafabric.serve.topology.layout_pipeline_3d import _compute_layout_3d

    edges = [("agent-1", "model-gpt4"), ("agent-1", "tool-search"), ("agent-2", "model-gpt4")]
    node_types = {
        "agent-1": "agent",
        "agent-2": "agent",
        "model-gpt4": "model",
        "tool-search": "tool",
    }
    positions = _compute_layout_3d(edges, node_types)
    assert "agent-1" in positions
    assert len(positions["agent-1"]) == 3  # [x, y, z]
    assert "model-gpt4" in positions


def test_compute_layout_3d_empty_graph():
    from novafabric.serve.topology.layout_pipeline_3d import _compute_layout_3d

    positions = _compute_layout_3d([], {})
    assert positions == {}


def test_compute_layout_3d_single_node():
    from novafabric.serve.topology.layout_pipeline_3d import _compute_layout_3d

    # A graph with a single node (no edges) can't be laid out by networkx
    # without nodes being added explicitly; verify empty result for edge-less input
    positions = _compute_layout_3d([], {"lone": "agent"})
    assert positions == {}


def test_compute_layout_3d_returns_floats():
    from novafabric.serve.topology.layout_pipeline_3d import _compute_layout_3d

    edges = [("a", "b")]
    positions = _compute_layout_3d(edges, {"a": "agent", "b": "model"})
    for node, pos in positions.items():
        assert isinstance(pos[0], float)
        assert isinstance(pos[1], float)
        assert isinstance(pos[2], float)


def test_snapshot_saved_after_compute(tmp_path):
    from novafabric.serve.topology.layout_pipeline_3d import LayoutPipeline3D
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

    store = SnapshotStore3D(tmp_path / "snapshots")
    pipeline = LayoutPipeline3D(store)

    edges = [("agent-1", "gpt-4o")]
    node_types = {"agent-1": "agent", "gpt-4o": "model"}

    snapshot = asyncio.run(pipeline.compute_snapshot(edges, node_types, "test001"))
    assert snapshot["window_id"] == "test001"
    assert store.get("test001") is not None


def test_layout_pipeline_callback(tmp_path):
    from novafabric.serve.topology.layout_pipeline_3d import LayoutPipeline3D
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

    store = SnapshotStore3D(tmp_path / "snapshots")
    pipeline = LayoutPipeline3D(store)

    received: list[str] = []

    async def callback(window_id: str) -> None:
        received.append(window_id)

    pipeline.add_snapshot_callback(callback)
    asyncio.run(pipeline.compute_snapshot([("a", "b")], {}, "cb_test"))
    assert "cb_test" in received


def test_layout_pipeline_tracks_latest_window(tmp_path):
    from novafabric.serve.topology.layout_pipeline_3d import LayoutPipeline3D
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

    store = SnapshotStore3D(tmp_path / "snapshots")
    pipeline = LayoutPipeline3D(store)

    assert pipeline.get_latest_window_id() is None
    asyncio.run(pipeline.compute_snapshot([("a", "b")], {}, "wid_first"))
    assert pipeline.get_latest_window_id() == "wid_first"


def test_layout_pipeline_snapshot_has_elapsed_ms(tmp_path):
    from novafabric.serve.topology.layout_pipeline_3d import LayoutPipeline3D
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

    store = SnapshotStore3D(tmp_path / "snapshots")
    pipeline = LayoutPipeline3D(store)
    snap = asyncio.run(pipeline.compute_snapshot([("x", "y")], {}, "elap_test"))
    assert "layout_elapsed_ms" in snap
    assert snap["layout_elapsed_ms"] >= 0


def test_layout_pipeline_snapshot_node_count(tmp_path):
    from novafabric.serve.topology.layout_pipeline_3d import LayoutPipeline3D
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

    store = SnapshotStore3D(tmp_path / "snapshots")
    pipeline = LayoutPipeline3D(store)
    edges = [("agent-1", "model-gpt4"), ("agent-2", "model-gpt4")]
    snap = asyncio.run(pipeline.compute_snapshot(edges, {}, "nc_test"))
    # 3 unique nodes: agent-1, agent-2, model-gpt4
    assert snap["node_count"] == 3
    assert snap["edge_count"] == 2
