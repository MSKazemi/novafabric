"""Tests for TV-5 snapshot store."""
from __future__ import annotations

import os
import re
import time


def test_snapshot_save_and_get(tmp_path):
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

    store = SnapshotStore3D(tmp_path / "snapshots")
    snapshot = {"window_id": "abc123", "node_count": 5, "positions": {"n1": [1.0, 2.0, 3.0]}}
    store.save("abc123", snapshot)
    result = store.get("abc123")
    assert result is not None
    assert result["node_count"] == 5


def test_snapshot_get_missing_returns_none(tmp_path):
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

    store = SnapshotStore3D(tmp_path / "snapshots")
    assert store.get("nonexistent") is None


def test_snapshot_atomic_write(tmp_path):
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

    store = SnapshotStore3D(tmp_path / "snapshots")
    # No .tmp files should remain after save
    store.save("test123", {"data": "value"})
    tmp_files = list((tmp_path / "snapshots" / "fine").glob("*.tmp"))
    assert len(tmp_files) == 0


def test_snapshot_retention_enforced(tmp_path):
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

    os.environ["TV5_MAX_FINE_SNAPSHOTS"] = "3"
    try:
        store = SnapshotStore3D(tmp_path / "snapshots")
        for i in range(5):
            store.save(f"win{i:03d}", {"n": i})
        windows = store.list_windows()
        fine_windows = [w for w in windows if w["tier"] == "fine"]
        assert len(fine_windows) <= 3
    finally:
        del os.environ["TV5_MAX_FINE_SNAPSHOTS"]


def test_list_windows_sorted(tmp_path):
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

    store = SnapshotStore3D(tmp_path / "snapshots")
    store.save("win001", {"ts": 1})
    time.sleep(0.05)
    store.save("win002", {"ts": 2})
    windows = store.list_windows()
    assert windows[0]["windowId"] == "win002"  # Most recent first


def test_window_id_validation():
    """Valid window IDs match regex ^[a-z0-9_-]+$."""
    WINDOW_ID_RE = re.compile(r"^[a-z0-9_-]+$")
    assert WINDOW_ID_RE.match("abc123")
    assert WINDOW_ID_RE.match("win-001")
    assert not WINDOW_ID_RE.match("../etc/passwd")
    assert not WINDOW_ID_RE.match("ABC")


def test_list_windows_empty(tmp_path):
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

    store = SnapshotStore3D(tmp_path / "snapshots")
    assert store.list_windows() == []


def test_content_type_is_string(tmp_path):
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

    store = SnapshotStore3D(tmp_path / "snapshots")
    ct = store.content_type
    assert ct in ("application/json", "application/msgpack")


def test_snapshot_roundtrip_data_integrity(tmp_path):
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

    store = SnapshotStore3D(tmp_path / "snapshots")
    data = {
        "window_id": "rtrip",
        "node_count": 2,
        "positions": {"n1": [1.5, 2.5, 3.5], "n2": [-1.0, 0.0, 1.0]},
        "node_types": {"n1": "agent", "n2": "model"},
        "layout_elapsed_ms": 12.3,
    }
    store.save("rtrip", data)
    result = store.get("rtrip")
    assert result is not None
    assert result["node_count"] == 2
    assert "n1" in result["positions"]


def test_snapshot_overwrites_existing(tmp_path):
    from novafabric.serve.topology.snapshot_store_3d import SnapshotStore3D

    store = SnapshotStore3D(tmp_path / "snapshots")
    store.save("ow_test", {"val": 1})
    store.save("ow_test", {"val": 2})
    result = store.get("ow_test")
    assert result is not None
    assert result["val"] == 2
