# tests/test_lineage_index.py
"""Tests for the hot in-memory lineage impact index (ADR-0083, gap-013).

The primary invariant (I-2) is *equivalence*: the in-memory index must return the
same blast-radius node set as the recursive-CTE path in ``LineageStore``.
"""
from __future__ import annotations

from pathlib import Path

from novafabric.lineage._index import HotLineageIndex
from novafabric.lineage._store import LineageStore
from novafabric.lineage._types import LineageEdge


def _make_store(tmp_path: Path) -> LineageStore:
    return LineageStore(db_path=tmp_path / "test.db")


def _build_fixture_graph(tmp_path: Path) -> LineageStore:
    """A multi-hop graph mixing edge types, mirroring test_lineage_store fixtures.

    Edges (source -> target):
        run 01RUNA  --consumed-->  asset local:model:foo@1.0.0
        run 01RUNA  --produced-->  artifact:01RUNA:outputs/result.txt
        run 01RUNC  --replayed_from--> run 01RUNB
        run 01RUND  --replayed_from--> run 01RUNC
    """
    store = _make_store(tmp_path)
    consumed = LineageEdge(
        edge_type="consumed",
        source={"kind": "run", "run_id": "01RUNA"},
        target={"kind": "asset", "asset_ref": "model:foo@1.0.0", "registry": "local"},
        confidence="observed", capsule_run_id="01RUNA",
    )
    produced = LineageEdge(
        edge_type="produced",
        source={"kind": "run", "run_id": "01RUNA"},
        target={"kind": "artifact", "artifact_ref": {
            "capsule_run_id": "01RUNA", "path": "outputs/result.txt"}},
        confidence="inferred", capsule_run_id="01RUNA",
    )
    replay_bc = LineageEdge(
        edge_type="replayed_from",
        source={"kind": "run", "run_id": "01RUNC"},
        target={"kind": "run", "run_id": "01RUNB"},
        confidence="declared", capsule_run_id="01RUNC",
    )
    replay_dc = LineageEdge(
        edge_type="replayed_from",
        source={"kind": "run", "run_id": "01RUND"},
        target={"kind": "run", "run_id": "01RUNC"},
        confidence="declared", capsule_run_id="01RUND",
    )
    store.insert_edge(consumed)
    store.insert_edge(produced)
    store.insert_edge(replay_bc)
    store.insert_edge(replay_dc)
    return store


def _ids(rows: list[dict]) -> set[str]:
    return {r["node_id"] for r in rows}


# --- I-3: rebuild from store ---

def test_rebuild_from_store_loads_all_nodes_and_edges(tmp_path: Path) -> None:
    store = _build_fixture_graph(tmp_path)
    index = HotLineageIndex()
    index.rebuild_from_store(store)
    # 6 distinct nodes: A, B, C, D, asset, artifact
    assert index.node_count == 6
    assert index.edge_count == 4


def test_rebuild_is_idempotent(tmp_path: Path) -> None:
    store = _build_fixture_graph(tmp_path)
    index = HotLineageIndex()
    index.rebuild_from_store(store)
    index.rebuild_from_store(store)
    assert index.node_count == 6
    assert index.edge_count == 4


# --- I-2: equivalence with the recursive-CTE blast_radius ---

def test_blast_radius_equivalent_to_store_asset(tmp_path: Path) -> None:
    store = _build_fixture_graph(tmp_path)
    index = HotLineageIndex()
    index.rebuild_from_store(store)
    for depth in (1, 2, 3, 5):
        store_rows = store.blast_radius(
            ref="local:model:foo@1.0.0", kind="asset", depth=depth)
        index_rows = index.query_blast_radius(
            ref="local:model:foo@1.0.0", kind="asset", depth=depth)
        assert _ids(index_rows) == _ids(store_rows), f"mismatch at depth={depth}"


def test_blast_radius_equivalent_to_store_run_multihop(tmp_path: Path) -> None:
    store = _build_fixture_graph(tmp_path)
    index = HotLineageIndex()
    index.rebuild_from_store(store)
    # blast radius of 01RUNB walks back through the replay chain B<-C<-D
    for depth in (1, 2, 3):
        store_rows = store.blast_radius(ref="01RUNB", kind="run", depth=depth)
        index_rows = index.query_blast_radius(ref="01RUNB", kind="run", depth=depth)
        assert _ids(index_rows) == _ids(store_rows), f"mismatch at depth={depth}"


def test_blast_radius_equivalent_with_edge_type_filter(tmp_path: Path) -> None:
    store = _build_fixture_graph(tmp_path)
    index = HotLineageIndex()
    index.rebuild_from_store(store)
    store_rows = store.blast_radius(
        ref="01RUNB", kind="run", depth=5, edge_type="replayed_from")
    index_rows = index.query_blast_radius(
        ref="01RUNB", kind="run", depth=5, edge_type="replayed_from")
    assert _ids(index_rows) == _ids(store_rows)
    # 'consumed' filter must exclude the replay chain entirely
    store_consumed = store.blast_radius(
        ref="01RUNB", kind="run", depth=5, edge_type="consumed")
    index_consumed = index.query_blast_radius(
        ref="01RUNB", kind="run", depth=5, edge_type="consumed")
    assert _ids(index_consumed) == _ids(store_consumed) == set()


def test_blast_radius_unknown_ref_returns_empty(tmp_path: Path) -> None:
    store = _build_fixture_graph(tmp_path)
    index = HotLineageIndex()
    index.rebuild_from_store(store)
    assert index.query_blast_radius(ref="nonexistent", kind="run") == []


def test_blast_radius_kindless_lookup(tmp_path: Path) -> None:
    store = _build_fixture_graph(tmp_path)
    index = HotLineageIndex()
    index.rebuild_from_store(store)
    store_rows = store.blast_radius(ref="01RUNB")
    index_rows = index.query_blast_radius(ref="01RUNB")
    assert _ids(index_rows) == _ids(store_rows)


# --- I-4: bounded eviction ---

def test_index_respects_max_nodes_bound(tmp_path: Path) -> None:
    store = _build_fixture_graph(tmp_path)
    index = HotLineageIndex(max_nodes=3)
    index.rebuild_from_store(store)
    assert index.node_count <= 3


def test_eviction_is_lru(tmp_path: Path) -> None:
    index = HotLineageIndex(max_nodes=2)
    # add three nodes via direct edge upserts; oldest must be evicted
    index.add_edge("n1", "n2", "consumed")  # touches n1, n2
    index.add_edge("n2", "n3", "consumed")  # touches n2, n3 -> n1 is LRU, evicted
    assert index.node_count == 2
    assert not index.has_node("n1")
    assert index.has_node("n2")
    assert index.has_node("n3")


def test_query_after_full_eviction_falls_back_signal(tmp_path: Path) -> None:
    # A node never loaded yields the same empty answer as an unknown ref.
    index = HotLineageIndex(max_nodes=1)
    index.add_edge("a", "b", "consumed")  # only one of these survives
    # query for a node id that was evicted -> empty (cache miss, caller falls back)
    rows = index.query_blast_radius_by_node_id("evicted-id")
    assert rows == []


# --- constructor hook on the store (additive, opt-in) ---

def test_store_build_hot_index_returns_equivalent_index(tmp_path: Path) -> None:
    store = _build_fixture_graph(tmp_path)
    index = store.build_hot_index()
    store_rows = store.blast_radius(ref="local:model:foo@1.0.0", kind="asset", depth=5)
    index_rows = index.query_blast_radius(
        ref="local:model:foo@1.0.0", kind="asset", depth=5)
    assert _ids(index_rows) == _ids(store_rows)
