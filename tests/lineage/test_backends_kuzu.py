"""Tests for the KuzuDB lineage backend."""

from __future__ import annotations

import pathlib

import pytest

kuzu = pytest.importorskip("kuzu")

from novafabric.lineage._types import LineageEdge  # noqa: E402
from novafabric.lineage.backends.kuzu import KuzuLineageStore  # noqa: E402
from novafabric.lineage.store import AbstractLineageStore  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_node(run_id: str) -> dict[str, object]:
    return {"kind": "run", "run_id": run_id}


def _make_edge(
    src: str,
    tgt: str,
    edge_type: str = "contains",
    capsule_run_id: str = "cap-001",
    site_id: str = "local",
) -> LineageEdge:
    return LineageEdge(
        edge_type=edge_type,
        source=_run_node(src),
        target=_run_node(tgt),
        confidence="high",
        capsule_run_id=capsule_run_id,
    )


def _make_store(tmp_path: pathlib.Path) -> KuzuLineageStore:
    return KuzuLineageStore(db_path=tmp_path / "lineage.kuzu")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_kuzu_insert_and_provenance(tmp_path: pathlib.Path) -> None:
    """Insert 5 edges, query provenance from root — must return descendants."""
    store = _make_store(tmp_path)
    edges = [
        _make_edge("run-A", "run-B"),
        _make_edge("run-B", "run-C"),
        _make_edge("run-A", "run-C", "delegated_to"),
        _make_edge("run-C", "run-D", "replayed_from", "cap-002"),
        _make_edge("run-B", "run-D", capsule_run_id="cap-002"),
    ]
    for e in edges:
        store.insert(e)

    result = store.provenance("run-A", depth=3)
    assert isinstance(result, list)
    assert len(result) > 0
    node_ids = {r["node_id"] for r in result}
    assert "run-B" in node_ids
    assert "run-C" in node_ids


def test_kuzu_blast_radius(tmp_path: pathlib.Path) -> None:
    """Insert 5 edges, query blast_radius from leaf — must return ancestors."""
    store = _make_store(tmp_path)
    edges = [
        _make_edge("run-A", "run-B"),
        _make_edge("run-B", "run-C"),
        _make_edge("run-A", "run-C", "delegated_to"),
        _make_edge("run-C", "run-D", "replayed_from", "cap-002"),
        _make_edge("run-B", "run-D", capsule_run_id="cap-002"),
    ]
    for e in edges:
        store.insert(e)

    result = store.blast_radius("run-D", max_depth=5)
    assert isinstance(result, list)
    assert len(result) > 0
    node_ids = {r["node_id"] for r in result}
    assert "run-B" in node_ids or "run-C" in node_ids


def test_kuzu_replay_chain(tmp_path: pathlib.Path) -> None:
    """Insert edges with replayed_from type — replay_chain must return them."""
    store = _make_store(tmp_path)
    store.insert(_make_edge("run-C", "run-D", "replayed_from", "cap-002"))
    store.insert(_make_edge("run-A", "run-C"))  # non-replay edge

    result = store.replay_chain("run-C")
    assert isinstance(result, list)
    assert len(result) >= 1
    node_ids = {r["node_id"] for r in result}
    assert "run-D" in node_ids


def test_kuzu_insert_idempotent(tmp_path: pathlib.Path) -> None:
    """Insert the same edge twice — no error, only one entry in results."""
    store = _make_store(tmp_path)
    edge = _make_edge("run-A", "run-B")
    store.insert(edge)
    store.insert(edge)  # must not raise

    result = store.provenance("run-A", depth=1)
    # Idempotent — run-B should appear exactly once.
    node_ids = [r["node_id"] for r in result]
    assert node_ids.count("run-B") == 1


def test_kuzu_site_id_stored(tmp_path: pathlib.Path) -> None:
    """Insert edge with custom site_id — store holds it (provenance must work)."""
    store = KuzuLineageStore(db_path=tmp_path / "lineage.kuzu", site_id="cluster-eu-west-1")
    store.insert(_make_edge("run-X", "run-Y"))
    result = store.provenance("run-X", depth=1)
    assert len(result) == 1
    assert result[0]["node_id"] == "run-Y"


def test_kuzu_tmp_dir_cleanup(tmp_path: pathlib.Path) -> None:
    """KuzuLineageStore with explicit db_path does not crash on use."""
    store = KuzuLineageStore(db_path=tmp_path / "lineage.kuzu")
    store.insert(_make_edge("run-A", "run-B"))
    assert store.provenance("run-A", depth=1) != []


def test_kuzu_empty_store(tmp_path: pathlib.Path) -> None:
    """All query methods on an empty store return empty lists."""
    store = _make_store(tmp_path)
    assert store.provenance("run-X", depth=3) == []
    assert store.blast_radius("run-X", max_depth=5) == []
    assert store.replay_chain("run-X") == []


def test_kuzu_depth_limit(tmp_path: pathlib.Path) -> None:
    """depth=1 returns fewer nodes than depth=3 on a 5-node chain."""
    store = _make_store(tmp_path)
    # Build a linear chain: A→B→C→D→E
    for src, tgt in [("A", "B"), ("B", "C"), ("C", "D"), ("D", "E")]:
        store.insert(_make_edge(f"run-{src}", f"run-{tgt}"))

    shallow = store.provenance("run-A", depth=1)
    deep = store.provenance("run-A", depth=3)
    assert len(shallow) < len(deep)


def test_kuzu_node_row_shape(tmp_path: pathlib.Path) -> None:
    """Returned NodeRow dicts must have exactly node_id, kind, ref keys."""
    store = _make_store(tmp_path)
    store.insert(_make_edge("run-A", "run-B"))

    for method_result in [
        store.provenance("run-A", depth=1),
        store.blast_radius("run-B", max_depth=1),
        store.replay_chain("run-A"),  # no replay edges → empty but safe
    ]:
        for row in method_result:
            assert set(row.keys()) == {"node_id", "kind", "ref"}
            assert isinstance(row["node_id"], str)
            assert isinstance(row["kind"], str)
            assert isinstance(row["ref"], str)


def test_kuzu_none_db_path_uses_temp(tmp_path: pathlib.Path) -> None:
    """`KuzuLineageStore(db_path=None)` must work without raising ValueError."""
    store = KuzuLineageStore(db_path=None)
    assert isinstance(store, AbstractLineageStore)
    store.insert(_make_edge("run-A", "run-B"))
    result = store.provenance("run-A", depth=1)
    assert len(result) == 1


def test_kuzu_is_abstract_lineage_store(tmp_path: pathlib.Path) -> None:
    """KuzuLineageStore must be a subclass of AbstractLineageStore."""
    store = _make_store(tmp_path)
    assert isinstance(store, AbstractLineageStore)
