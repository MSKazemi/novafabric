"""Tests for TV-5 topology visualization Pydantic models."""
from __future__ import annotations

import pytest
from pydantic import ValidationError


def _import():
    from novafabric.serve.topology.visualization.models import (
        ClusterRecord,
        NodeRecord,
        TopologySnapshot,
        WindowRecord,
    )

    return NodeRecord, ClusterRecord, WindowRecord, TopologySnapshot


# ---------------------------------------------------------------------------
# NodeRecord
# ---------------------------------------------------------------------------


def test_node_record_valid():
    NodeRecord, _, _, _ = _import()
    node = NodeRecord(
        node_id="agent-1",
        node_type="agent",
        label="Agent 1",
        cluster_id="cluster-a",
        health=0.9,
        x=1.0,
        y=2.0,
        z=3.0,
    )
    assert node.node_id == "agent-1"
    assert node.node_type == "agent"
    assert node.health == pytest.approx(0.9)


def test_node_record_all_types():
    NodeRecord, _, _, _ = _import()
    for t in ("agent", "model", "tool", "compute"):
        node = NodeRecord(node_id="n", node_type=t, label="l", cluster_id="c")
        assert node.node_type == t


def test_node_record_invalid_type():
    NodeRecord, _, _, _ = _import()
    with pytest.raises(ValidationError):
        NodeRecord(node_id="n", node_type="unknown_type", label="l", cluster_id="c")


def test_node_record_health_out_of_range():
    NodeRecord, _, _, _ = _import()
    with pytest.raises(ValidationError):
        NodeRecord(
            node_id="n", node_type="agent", label="l", cluster_id="c", health=1.5
        )
    with pytest.raises(ValidationError):
        NodeRecord(
            node_id="n", node_type="agent", label="l", cluster_id="c", health=-0.1
        )


def test_node_record_default_position():
    NodeRecord, _, _, _ = _import()
    node = NodeRecord(node_id="n", node_type="agent", label="l", cluster_id="c")
    assert node.x == 0.0
    assert node.y == 0.0
    assert node.z == 0.0


# ---------------------------------------------------------------------------
# ClusterRecord
# ---------------------------------------------------------------------------


def test_cluster_record_valid():
    _, ClusterRecord, _, _ = _import()
    cluster = ClusterRecord(
        cluster_id="cl-1",
        label="Cluster 1",
        centroid=(10.0, 20.0, 30.0),
        node_count=5,
    )
    assert cluster.cluster_id == "cl-1"
    assert cluster.node_count == 5
    assert cluster.centroid == (10.0, 20.0, 30.0)


def test_cluster_record_defaults():
    _, ClusterRecord, _, _ = _import()
    cluster = ClusterRecord(cluster_id="cl", label="C")
    assert cluster.centroid == (0.0, 0.0, 0.0)
    assert cluster.node_count == 0


def test_cluster_record_negative_node_count_rejected():
    _, ClusterRecord, _, _ = _import()
    with pytest.raises(ValidationError):
        ClusterRecord(cluster_id="cl", label="C", node_count=-1)


# ---------------------------------------------------------------------------
# WindowRecord
# ---------------------------------------------------------------------------


def test_window_record_fine():
    _, _, WindowRecord, _ = _import()
    w = WindowRecord(window_id="win-001", tier="fine", captured_at=1716000000.0)
    assert w.tier == "fine"


def test_window_record_coarse():
    _, _, WindowRecord, _ = _import()
    w = WindowRecord(window_id="win-001", tier="coarse", captured_at=1716000000.0)
    assert w.tier == "coarse"


def test_window_record_invalid_tier():
    _, _, WindowRecord, _ = _import()
    with pytest.raises(ValidationError):
        WindowRecord(window_id="w", tier="hourly", captured_at=0.0)


# ---------------------------------------------------------------------------
# TopologySnapshot
# ---------------------------------------------------------------------------


def test_topology_snapshot_valid():
    NodeRecord, ClusterRecord, _, TopologySnapshot = _import()
    snap = TopologySnapshot(
        window_id="snap-1",
        tier="fine",
        captured_at=1716000000.0,
        nodes=[
            NodeRecord(
                node_id="a1",
                node_type="agent",
                label="A1",
                cluster_id="cl",
                x=1.0,
                y=2.0,
                z=3.0,
            )
        ],
        clusters=[
            ClusterRecord(cluster_id="cl", label="Cluster", node_count=1)
        ],
    )
    assert snap.window_id == "snap-1"
    assert len(snap.nodes) == 1
    assert snap.nodes[0].node_id == "a1"


def test_topology_snapshot_empty():
    _, _, _, TopologySnapshot = _import()
    snap = TopologySnapshot(window_id="empty", tier="coarse", captured_at=1.0)
    assert snap.nodes == []
    assert snap.clusters == []


def test_topology_snapshot_from_raw():
    """TopologySnapshot.from_raw() converts layout pipeline raw dicts."""
    _, _, _, TopologySnapshot = _import()
    raw = {
        "window_id": "raw-1",
        "timestamp": 1716000000,
        "positions": {
            "agent-1": [1.0, 2.0, 3.0],
            "model-gpt4": [-1.0, 0.0, 4.0],
        },
        "node_types": {
            "agent-1": "agent",
            "model-gpt4": "model",
        },
    }
    snap = TopologySnapshot.from_raw(raw)
    assert snap.window_id == "raw-1"
    assert len(snap.nodes) == 2
    node_ids = {n.node_id for n in snap.nodes}
    assert "agent-1" in node_ids
    assert "model-gpt4" in node_ids


def test_topology_snapshot_from_raw_compute_node_mapping():
    """from_raw() maps 'compute_node' → 'compute'."""
    _, _, _, TopologySnapshot = _import()
    raw = {
        "window_id": "cn-test",
        "timestamp": 0,
        "positions": {"cn-1": [0.0, 0.0, 0.0]},
        "node_types": {"cn-1": "compute_node"},
    }
    snap = TopologySnapshot.from_raw(raw)
    assert snap.nodes[0].node_type == "compute"


def test_topology_snapshot_from_raw_unknown_type_defaults_agent():
    """from_raw() maps unknown node types to 'agent' as fallback."""
    _, _, _, TopologySnapshot = _import()
    raw = {
        "window_id": "unk-test",
        "timestamp": 0,
        "positions": {"x": [0.0, 0.0, 0.0]},
        "node_types": {"x": "totally_unknown"},
    }
    snap = TopologySnapshot.from_raw(raw)
    assert snap.nodes[0].node_type == "agent"


def test_topology_snapshot_from_raw_empty():
    """from_raw() with no positions → empty node list."""
    _, _, _, TopologySnapshot = _import()
    raw = {"window_id": "empty-raw", "timestamp": 0, "positions": {}, "node_types": {}}
    snap = TopologySnapshot.from_raw(raw)
    assert snap.nodes == []
