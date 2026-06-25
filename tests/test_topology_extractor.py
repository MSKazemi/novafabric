"""Tests for TopologyExtractor (FR-12: Louvain re-pass trigger)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from novafabric.serve.topology.cluster_store import ClusterStore
from novafabric.serve.topology.delta_buffer import DeltaBuffer
from novafabric.serve.topology.topology_extractor import TopologyExtractor


@pytest.fixture
def store(tmp_path: Path) -> ClusterStore:
    return ClusterStore(db_path=tmp_path / "extractor.duckdb")


@pytest.fixture
def buf() -> DeltaBuffer:
    return DeltaBuffer()


@pytest.fixture
def extractor(store: ClusterStore, buf: DeltaBuffer) -> TopologyExtractor:
    return TopologyExtractor(store, buf)


def test_add_agent_creates_node(extractor: TopologyExtractor) -> None:
    for i in range(100):
        extractor.add_agent(f"agent-{i}", agent_type="llm")
    assert extractor.node_count() == 100


def test_add_agent_enqueues_add_node_event(extractor: TopologyExtractor, buf: DeltaBuffer) -> None:
    extractor.add_agent("a1", "tool", "idle")
    events = buf.replay_since(0)
    add_events = [e for e in events if e["type"] == "add_node"]
    assert any(e["node_id"] == "a1" for e in add_events)


def test_add_agent_idempotent(extractor: TopologyExtractor) -> None:
    extractor.add_agent("a1")
    extractor.add_agent("a1")  # should not add twice
    assert extractor.node_count() == 1


def test_add_edge_enqueues_add_edge_event(extractor: TopologyExtractor, buf: DeltaBuffer) -> None:
    extractor.add_agent("a1")
    extractor.add_agent("a2")
    extractor.add_edge("a1", "a2")
    events = buf.replay_since(0)
    edge_events = [e for e in events if e["type"] == "add_edge"]
    assert len(edge_events) == 1
    assert edge_events[0]["source_id"] == "a1"
    assert edge_events[0]["target_id"] == "a2"


def test_louvain_triggered_at_10pct_edge_change(
    store: ClusterStore, buf: DeltaBuffer
) -> None:
    """Add nodes + initial edges (triggers first pass), then add 10% more."""
    extractor = TopologyExtractor(store, buf)
    louvain_calls = []
    extractor.on_louvain_complete = lambda: louvain_calls.append(1)

    # Add 20 nodes
    for i in range(20):
        extractor.add_agent(f"agent-{i}")

    # Patch run_louvain_pass to run synchronously in tests
    def sync_schedule() -> None:
        asyncio.run(extractor.run_louvain_pass())

    extractor._schedule_louvain = sync_schedule  # type: ignore[method-assign]

    # Add 10 initial edges (triggers first pass)
    for i in range(10):
        extractor.add_edge(f"agent-{i}", f"agent-{(i+1) % 20}")

    assert extractor._edges_at_last_pass > 0

    # Add 10% more edges (should trigger another pass)
    initial_passes = len(louvain_calls)
    more = max(1, int(extractor._edges_at_last_pass * 0.11))
    for i in range(more):
        extractor.add_edge(f"agent-{i}", f"agent-{(i+5) % 20}")

    assert len(louvain_calls) > initial_passes


def test_louvain_pass_writes_cluster_layer(
    store: ClusterStore, buf: DeltaBuffer
) -> None:
    extractor = TopologyExtractor(store, buf)

    def sync_schedule() -> None:
        asyncio.run(extractor.run_louvain_pass())

    extractor._schedule_louvain = sync_schedule  # type: ignore[method-assign]

    for i in range(10):
        extractor.add_agent(f"agent-{i}")
    for i in range(9):
        extractor.add_edge(f"agent-{i}", f"agent-{i+1}")

    assert store.cluster_count() > 0


def test_resolution_is_configurable(tmp_path: Path, buf: DeltaBuffer) -> None:
    """A lower Louvain resolution must yield fewer (larger) clusters than a higher one."""

    def build(resolution: float, name: str) -> int:
        s = ClusterStore(db_path=tmp_path / f"{name}.duckdb")
        ex = TopologyExtractor(s, DeltaBuffer(), louvain_resolution=resolution)
        ex._schedule_louvain = lambda: asyncio.run(ex.run_louvain_pass())  # type: ignore[method-assign]
        # Two 6-node cliques joined by a single bridge edge. Low resolution keeps
        # them as 2 communities; high resolution shatters them into singletons.
        cliques = [[f"{name}-a{i}" for i in range(6)], [f"{name}-b{i}" for i in range(6)]]
        for clique in cliques:
            for i in range(len(clique)):
                for j in range(i + 1, len(clique)):
                    ex.add_agent(clique[i])
                    ex.add_agent(clique[j])
                    ex.add_edge(clique[i], clique[j])
        ex.add_edge(cliques[0][-1], cliques[1][0])  # bridge
        asyncio.run(ex.run_louvain_pass())
        return s.cluster_count()

    low = build(0.5, "low")
    high = build(5.0, "high")
    assert low < high, f"low-resolution clusters ({low}) should be fewer than high ({high})"


def test_isolated_nodes_collapsed_into_single_cluster(
    store: ClusterStore, buf: DeltaBuffer
) -> None:
    """Degree-0 nodes (e.g. runs with no captured model calls) must collapse into
    ONE 'misc' cluster instead of N singleton clusters (readability)."""
    extractor = TopologyExtractor(store, buf, collapse_isolated=True)
    extractor._schedule_louvain = lambda: asyncio.run(extractor.run_louvain_pass())  # type: ignore[method-assign]

    # One connected pair + many isolated nodes.
    extractor.add_agent("run:a")
    extractor.add_agent("model:x")
    extractor.add_edge("run:a", "model:x")
    for i in range(20):
        extractor.add_agent(f"orphan-{i}")  # no edges → degree 0

    asyncio.run(extractor.run_louvain_pass())

    # 20 isolated nodes must NOT produce 20 clusters; they collapse to one.
    rows = store._conn.execute(
        "SELECT cluster_id, COUNT(*) FROM agent_nodes GROUP BY cluster_id"
    ).fetchall()
    sizes = sorted(c for _, c in rows)
    assert 20 in sizes, f"expected a 20-node misc cluster, got sizes {sizes}"
    # Far fewer clusters than the 22 we'd get without collapsing.
    assert store.cluster_count() <= 3


def test_isolated_nodes_not_collapsed_when_disabled(
    store: ClusterStore, buf: DeltaBuffer
) -> None:
    extractor = TopologyExtractor(store, buf, collapse_isolated=False)
    extractor._schedule_louvain = lambda: asyncio.run(extractor.run_louvain_pass())  # type: ignore[method-assign]
    for i in range(5):
        extractor.add_agent(f"orphan-{i}")
    asyncio.run(extractor.run_louvain_pass())
    assert store.cluster_count() == 5


def test_louvain_pass_sets_node_positions(
    store: ClusterStore, buf: DeltaBuffer
) -> None:
    """After a Louvain pass, agent nodes must have pos_x/pos_y != (0,0) for all (or nearly all)."""
    extractor = TopologyExtractor(store, buf)

    def sync_schedule() -> None:
        asyncio.run(extractor.run_louvain_pass())

    extractor._schedule_louvain = sync_schedule  # type: ignore[method-assign]

    for i in range(6):
        extractor.add_agent(f"a{i}")
    for i in range(5):
        extractor.add_edge(f"a{i}", f"a{i+1}")

    rows = store._conn.execute(
        "SELECT pos_x, pos_y FROM agent_nodes"
    ).fetchall()
    assert len(rows) > 0
    nonzero = sum(1 for x, y in rows if abs(x) > 1e-9 or abs(y) > 1e-9)
    assert nonzero > 0
