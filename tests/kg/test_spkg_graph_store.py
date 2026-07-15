"""SP-1 tests: SPKG KùzuDB graph store + attack-path latency (ADR-0111, BQ-SPKG-01).

Requires the optional ``spkg`` extra (installs kuzu); skipped otherwise.
"""
from __future__ import annotations

import pytest

pytest.importorskip("kuzu")

from novafabric.kg.spkg.bench import benchmark, build_synthetic_edges  # noqa: E402
from novafabric.kg.spkg.graph_store import SpkgGraphStore  # noqa: E402
from novafabric.lineage._types import node_id_for  # noqa: E402


def test_ingest_and_attack_path_hop_counts() -> None:
    store = SpkgGraphStore()
    edges = [
        {"source": {"kind": "run", "ref": r}, "target": {"kind": "run", "ref": s},
         "edge_type": "produces", "capsule_run_id": r, "created_at": "t"}
        for r, s in [("A", "B"), ("B", "C"), ("C", "D"), ("A", "X")]
    ]
    assert store.ingest_edges(edges) == 4
    assert store.node_count() == 5  # A B C D X
    assert store.edge_count() == 4

    a, b, d = (node_id_for("run", x) for x in ("A", "B", "D"))
    assert store.attack_path(a, b) == 1
    assert store.attack_path(a, d) == 3        # A->B->C->D
    assert store.attack_path(d, a) is None     # no upstream path
    store.close()


def test_build_synthetic_edges_shape() -> None:
    edges = build_synthetic_edges(n_edges=600, layers=4, fanout=3, seed=1)
    assert 0 < len(edges) <= 600
    # first edge goes from layer 0 to layer 1
    assert edges[0]["source"]["ref"].startswith("L0_")
    assert edges[0]["target"]["ref"].startswith("L1_")


def test_sp1_attack_path_latency_budget() -> None:
    """SP-1 (CI scale): attack-path p99 well under the 500 ms budget on a moderate graph.

    The 1M-edge SP-1 acceptance runs the same `benchmark()` on a real host (BQ-SPKG-01).
    """
    result = benchmark(n_edges=4000, n_queries=60, layers=6, fanout=3, seed=1)
    assert result["n_edges"] > 0
    assert result["n_nodes"] > 0
    # Generous bound: proves the query path is fast; the real gate is the 1M host run.
    assert result["p99_ms"] < 500.0, result
