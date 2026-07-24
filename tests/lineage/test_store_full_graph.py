"""ADR-0212 prep slice — whole-graph read accessors on ``LineageStore``.

``all_nodes`` / ``all_edges`` are the bounded, canonically-ordered read surface
the analytics layer builds on. Silent truncation is forbidden: oversize graphs
raise ``LineageGraphTooLargeError``.
"""
from __future__ import annotations

import pytest

from novafabric.lineage._store import LineageGraphTooLargeError, LineageStore

_SEEDED_NODE_COUNT = 16  # 13 runs + 2 assets + 1 external orphan
_SEEDED_EDGE_COUNT = 15


def test_empty_store_returns_empty_lists(tmp_path):
    store = LineageStore(db_path=tmp_path / "empty.db")
    assert store.all_nodes() == []
    assert store.all_edges() == []


def test_all_nodes_ordered_and_parsed(seeded_lineage_store):
    nodes = seeded_lineage_store.all_nodes()
    assert len(nodes) == _SEEDED_NODE_COUNT
    ids = [n["node_id"] for n in nodes]
    assert ids == sorted(ids)
    assert all(isinstance(n["payload"], dict) for n in nodes)
    # Stable across calls (canonical ordering, not insertion order).
    assert nodes == seeded_lineage_store.all_nodes()


def test_all_edges_ordered_with_endpoint_ids(seeded_lineage_store):
    edges = seeded_lineage_store.all_edges()
    assert len(edges) == _SEEDED_EDGE_COUNT
    keys = [
        (e["source_id"], e["target_id"], e["edge_type"], e["edge_id"])
        for e in edges
    ]
    assert keys == sorted(keys)
    node_ids = {n["node_id"] for n in seeded_lineage_store.all_nodes()}
    assert all(e["source_id"] in node_ids and e["target_id"] in node_ids for e in edges)


def test_all_edges_round_trips_facets(seeded_lineage_store):
    edges = seeded_lineage_store.all_edges()
    faceted = [e for e in edges if e["payload"].get("facets")]
    assert len(faceted) == 1
    assert faceted[0]["payload"]["facets"] == {"demo": {"note": "seeded facet"}}
    assert faceted[0]["confidence"] == "inferred"


def test_limit_exceeded_raises_named_error(seeded_lineage_store):
    with pytest.raises(LineageGraphTooLargeError):
        seeded_lineage_store.all_nodes(limit=3)
    with pytest.raises(LineageGraphTooLargeError):
        seeded_lineage_store.all_edges(limit=3)
