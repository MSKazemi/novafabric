"""Tests for ClusterStore (DuckDB, Arrow IPC round-trips)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pyarrow.ipc as pa_ipc
import pytest

from novafabric.serve.topology.ads_encoder import SCHEMA_IDS
from novafabric.serve.topology.cluster_store import ClusterStore


@pytest.fixture
def store(tmp_path: Path) -> ClusterStore:
    return ClusterStore(db_path=tmp_path / "test.duckdb")


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_write_and_fetch_cluster_layer(store: ClusterStore) -> None:
    clusters = [
        {"cluster_id": i, "centroid_x": float(i), "centroid_y": float(i),
         "agent_count": 10, "inter_cluster_edges": 2}
        for i in range(5)
    ]
    _run(store.write_cluster_layer(clusters))
    ipc = store.fetch_cluster_layer_arrow()
    reader = pa_ipc.open_stream(ipc)
    table = reader.read_all()
    assert table.num_rows == 5
    schema_id = (reader.schema.metadata or {}).get(b"schema_id", b"").decode()
    assert schema_id == SCHEMA_IDS["cluster_layer"]


def test_fetch_empty_cluster_layer_returns_valid_ipc(store: ClusterStore) -> None:
    ipc = store.fetch_cluster_layer_arrow()
    reader = pa_ipc.open_stream(ipc)
    table = reader.read_all()
    assert table.num_rows == 0


def test_write_and_fetch_subgraph(store: ClusterStore) -> None:
    _run(store.write_cluster_layer([
        {"cluster_id": 0, "centroid_x": 0.0, "centroid_y": 0.0, "agent_count": 3, "inter_cluster_edges": 0}
    ]))
    nodes = [
        {"node_id": f"n{i}", "agent_type": "llm", "status": "idle", "cluster_id": 0, "pos_x": float(i), "pos_y": 0.0}
        for i in range(3)
    ]
    edges = [
        {"source_id": "n0", "target_id": "n1", "edge_type": "calls", "cluster_id": 0},
        {"source_id": "n1", "target_id": "n2", "edge_type": "calls", "cluster_id": 0},
    ]
    _run(store.write_agent_nodes(nodes))
    _run(store.write_agent_edges(edges))

    nodes_ipc, edges_ipc = store.fetch_subgraph_arrow(0)

    node_reader = pa_ipc.open_stream(nodes_ipc)
    node_table = node_reader.read_all()
    assert node_table.num_rows == 3

    edge_reader = pa_ipc.open_stream(edges_ipc)
    edge_table = edge_reader.read_all()
    assert edge_table.num_rows == 2


def test_schema_id_present_in_subgraph_nodes(store: ClusterStore) -> None:
    _run(store.write_agent_nodes([
        {"node_id": "x", "agent_type": "tool", "status": "running", "cluster_id": 1, "pos_x": 0.0, "pos_y": 0.0}
    ]))
    ipc, _ = store.fetch_subgraph_arrow(1)
    reader = pa_ipc.open_stream(ipc)
    schema_id = (reader.schema.metadata or {}).get(b"schema_id", b"").decode()
    assert schema_id == SCHEMA_IDS["subgraph_page_nodes"]


def test_cluster_count(store: ClusterStore) -> None:
    assert store.cluster_count() == 0
    _run(store.write_cluster_layer([
        {"cluster_id": 0, "centroid_x": 0.0, "centroid_y": 0.0, "agent_count": 1, "inter_cluster_edges": 0}
    ]))
    assert store.cluster_count() == 1


def test_list_clusters_returns_json_rows_largest_first(store: ClusterStore) -> None:
    assert store.list_clusters() == []
    _run(store.write_cluster_layer([
        {"cluster_id": 0, "centroid_x": 0.0, "centroid_y": 0.0, "agent_count": 3, "inter_cluster_edges": 1},
        {"cluster_id": 1, "centroid_x": 1.0, "centroid_y": 1.0, "agent_count": 9, "inter_cluster_edges": 4},
        {"cluster_id": 2, "centroid_x": 2.0, "centroid_y": 2.0, "agent_count": 1, "inter_cluster_edges": 0},
    ]))
    rows = store.list_clusters()
    assert [r["cluster_id"] for r in rows] == [1, 0, 2]  # largest agent_count first
    assert rows[0] == {"cluster_id": 1, "agent_count": 9, "inter_cluster_edges": 4}
    assert all(set(r) == {"cluster_id", "agent_count", "inter_cluster_edges"} for r in rows)


def test_concurrent_writes_do_not_deadlock(store: ClusterStore) -> None:
    async def _concurrent() -> None:
        clusters_a = [{"cluster_id": 0, "centroid_x": 0.0, "centroid_y": 0.0, "agent_count": 1, "inter_cluster_edges": 0}]
        clusters_b = [{"cluster_id": 1, "centroid_x": 1.0, "centroid_y": 1.0, "agent_count": 2, "inter_cluster_edges": 0}]
        await asyncio.gather(
            store.write_cluster_layer(clusters_a),
            store.write_cluster_layer(clusters_b),
        )

    asyncio.run(_concurrent())
    # No exception means success
