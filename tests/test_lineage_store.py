# tests/test_lineage_store.py
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from novafabric.lineage._store import LineageStore
from novafabric.lineage._types import LineageEdge, LineageNode, node_id_for


def _make_store(tmp_path: Path) -> LineageStore:
    return LineageStore(db_path=tmp_path / "test.db")


def _make_node(kind: str, ref: str, capsule_run_id: str = "01RUN") -> LineageNode:
    return LineageNode(
        node_id=node_id_for(kind, ref),
        kind=kind,
        ref=ref,
        first_seen_capsule_run_id=capsule_run_id,
        payload={"kind": kind, "ref": ref},
    )


def _make_edge(edge_type: str, source_run_id: str,
               target_asset_ref: str = "model:foo@1.0.0",
               capsule_run_id: str = "01RUN") -> LineageEdge:
    # Use human refs (run_id, asset_ref) NOT node_ids.
    # _resolve_node_id() recomputes node_id from the source/target dicts.
    return LineageEdge(
        edge_type=edge_type,
        source={"kind": "run", "run_id": source_run_id},
        target={"kind": "asset", "asset_ref": target_asset_ref, "registry": "local"},
        confidence="observed",
        capsule_run_id=capsule_run_id,
    )


# --- Migration tests ---

def test_store_creates_lineage_nodes_table(tmp_path: Path) -> None:
    _make_store(tmp_path)
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "lineage_nodes" in tables
    assert "lineage_edges" in tables


def test_migration_is_idempotent(tmp_path: Path) -> None:
    _make_store(tmp_path)
    _make_store(tmp_path)  # second init must not raise


def test_lineage_nodes_has_unique_kind_ref_constraint(tmp_path: Path) -> None:
    _make_store(tmp_path)
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    node_id = node_id_for("run", "01RUN")
    conn.execute(
        "INSERT INTO lineage_nodes(node_id, kind, ref, payload) VALUES (?,?,?,?)",
        (node_id, "run", "01RUN", "{}")
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lineage_nodes(node_id, kind, ref, payload) VALUES (?,?,?,?)",
            (node_id_for("run", "01RUN_DIFF"), "run", "01RUN", "{}")
        )
        conn.commit()


# --- replace_capsule_lineage tests ---

def test_replace_capsule_lineage_inserts_nodes_and_edges(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    run_node = _make_node("run", "01RUN")
    asset_node = _make_node("asset", "local:model:foo@1.0.0")
    edge = _make_edge("consumed", "01RUN", "model:foo@1.0.0")
    store.replace_capsule_lineage(
        nodes=[run_node, asset_node],
        edges=[edge],
        capsule_run_id="01RUN",
    )
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    node_count = conn.execute("SELECT COUNT(*) FROM lineage_nodes").fetchone()[0]
    edge_count = conn.execute("SELECT COUNT(*) FROM lineage_edges").fetchone()[0]
    assert node_count == 2
    assert edge_count == 1


def test_replace_capsule_lineage_is_idempotent(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    run_node = _make_node("run", "01RUN")
    asset_node = _make_node("asset", "local:model:foo@1.0.0")
    edge = _make_edge("consumed", "01RUN", "model:foo@1.0.0")
    store.replace_capsule_lineage([run_node, asset_node], [edge], "01RUN")
    store.replace_capsule_lineage([run_node, asset_node], [edge], "01RUN")
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    edge_count = conn.execute("SELECT COUNT(*) FROM lineage_edges").fetchone()[0]
    assert edge_count == 1


def test_shared_node_survives_when_other_capsule_reimported(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    # Two capsules share the same asset node
    asset_node = _make_node("asset", "local:model:foo@1.0.0", "01RUNA")
    run_a = _make_node("run", "01RUNA", "01RUNA")
    run_b = _make_node("run", "01RUNB", "01RUNB")
    edge_a = _make_edge("consumed", "01RUNA", "model:foo@1.0.0", "01RUNA")
    edge_b = _make_edge("consumed", "01RUNB", "model:foo@1.0.0", "01RUNB")
    store.replace_capsule_lineage([run_a, asset_node], [edge_a], "01RUNA")
    store.replace_capsule_lineage([run_b, asset_node], [edge_b], "01RUNB")
    # Re-import capsule A — should not delete asset_node
    store.replace_capsule_lineage([run_a, asset_node], [edge_a], "01RUNA")
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    # Both edges should still exist
    edge_count = conn.execute("SELECT COUNT(*) FROM lineage_edges").fetchone()[0]
    assert edge_count == 2


# --- Query tests ---

def _build_fixture_graph(tmp_path: Path) -> LineageStore:
    """
    Graph structure:
      run_a (run) --consumed--> asset_x (asset)
      artifact_y (artifact) --produced_by--> run_a (run)
      run_c (run) --replayed_from--> run_b (run)
      run_d (run) --replayed_from--> run_c (run)  (chain: d→c→b)

    node refs:
      asset_x: "local:model:foo@1.0.0"
      run_a:   "01RUNA"
      artifact_y: "artifact:01RUNA:outputs/result.txt"
      run_b:   "01RUNB"
      run_c:   "01RUNC"
      run_d:   "01RUND"
    """
    store = _make_store(tmp_path)

    asset_x  = _make_node("asset",    "local:model:foo@1.0.0",               "01RUNA")
    run_a    = _make_node("run",       "01RUNA",                               "01RUNA")
    artifact = _make_node("artifact",  "artifact:01RUNA:outputs/result.txt",  "01RUNA")
    run_b    = _make_node("run",       "01RUNB",                               "01RUNB")
    run_c    = _make_node("run",       "01RUNC",                               "01RUNC")
    run_d    = _make_node("run",       "01RUND",                               "01RUND")

    consumed_edge = LineageEdge(
        edge_type="consumed",
        source={"kind": "run",  "run_id": "01RUNA"},
        target={"kind": "asset", "asset_ref": "model:foo@1.0.0", "registry": "local"},
        confidence="observed", capsule_run_id="01RUNA",
    )
    produced_edge = LineageEdge(
        edge_type="produced_by",
        source={"kind": "artifact", "artifact_ref": {
            "capsule_run_id": "01RUNA", "path": "outputs/result.txt"}},
        target={"kind": "run", "run_id": "01RUNA"},
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

    store.replace_capsule_lineage(
        [run_a, asset_x, artifact], [consumed_edge, produced_edge], "01RUNA"
    )
    store.replace_capsule_lineage([run_b], [], "01RUNB")
    store.replace_capsule_lineage([run_c, run_b], [replay_bc], "01RUNC")
    store.replace_capsule_lineage([run_d, run_c], [replay_dc], "01RUND")
    return store


def test_blast_radius_of_asset_finds_consuming_run(tmp_path: Path) -> None:
    store = _build_fixture_graph(tmp_path)
    results = store.blast_radius(ref="local:model:foo@1.0.0", kind="asset")
    refs = {r["ref"] for r in results}
    assert "01RUNA" in refs


def test_blast_radius_depth_limits_results(tmp_path: Path) -> None:
    store = _build_fixture_graph(tmp_path)
    results_d1 = store.blast_radius(ref="local:model:foo@1.0.0", kind="asset", depth=1)
    results_d2 = store.blast_radius(ref="local:model:foo@1.0.0", kind="asset", depth=2)
    # depth=2 should reach more nodes
    assert len(results_d2) >= len(results_d1)


def test_provenance_of_run_finds_consumed_assets(tmp_path: Path) -> None:
    store = _build_fixture_graph(tmp_path)
    results = store.provenance(ref="01RUNA", kind="run")
    refs = {r["ref"] for r in results}
    assert "local:model:foo@1.0.0" in refs


def test_replay_chain_walks_back_to_original(tmp_path: Path) -> None:
    store = _build_fixture_graph(tmp_path)
    results = store.replay_chain(run_id="01RUND")
    # chain: D→C→B
    refs = {r["ref"] for r in results}
    assert "01RUNC" in refs
    assert "01RUNB" in refs


def test_replay_chain_empty_for_non_replay(tmp_path: Path) -> None:
    store = _build_fixture_graph(tmp_path)
    results = store.replay_chain(run_id="01RUNA")
    assert results == []


def test_time_travel_filters_by_asof(tmp_path: Path) -> None:
    store = _build_fixture_graph(tmp_path)
    future = "2099-01-01T00:00:00.000Z"
    results = store.time_travel(ref="01RUNA", kind="run", asof=future)
    assert len(results) >= 1  # at least the consumed edge


def test_time_travel_empty_before_all_edges(tmp_path: Path) -> None:
    store = _build_fixture_graph(tmp_path)
    past = "2000-01-01T00:00:00.000Z"
    results = store.time_travel(ref="01RUNA", kind="run", asof=past)
    assert results == []


def test_unknown_ref_returns_empty(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert store.blast_radius(ref="nonexistent") == []
    assert store.provenance(ref="nonexistent") == []
    assert store.replay_chain(run_id="nonexistent") == []


# --- Coverage for edge-case branches ---

def test_resolve_node_id_else_branch_for_unknown_kind(tmp_path: Path) -> None:
    """_resolve_node_id: 'else' branch executes for unknown kind (line 117)."""
    store = _make_store(tmp_path)
    # Insert a node with an unknown kind directly via SQL, then insert an edge
    # whose source carries that unknown kind so _resolve_node_id hits the else.
    # We do this by calling replace_capsule_lineage with a hand-crafted edge.
    unknown_node = LineageNode(
        node_id=node_id_for("custom", "my-custom-ref"),
        kind="custom",
        ref="my-custom-ref",
        first_seen_capsule_run_id="01RUN",
        payload={"kind": "custom", "ref": "my-custom-ref"},
    )
    run_node = _make_node("run", "01RUN")
    edge = LineageEdge(
        edge_type="consumed",
        source={"kind": "custom", "ref": "my-custom-ref"},
        target={"kind": "run", "run_id": "01RUN"},
        confidence="observed",
        capsule_run_id="01RUN",
    )
    # Should not raise — the else branch falls back to node_dict["ref"]
    store.replace_capsule_lineage([unknown_node, run_node], [edge], "01RUN")
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    edge_count = conn.execute("SELECT COUNT(*) FROM lineage_edges").fetchone()[0]
    assert edge_count == 1


def test_time_travel_returns_empty_for_unknown_ref(tmp_path: Path) -> None:
    """time_travel with an unknown ref returns [] (line 210 branch)."""
    store = _make_store(tmp_path)
    results = store.time_travel(ref="no-such-ref", asof="2099-01-01T00:00:00.000Z")
    assert results == []
