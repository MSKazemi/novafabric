"""SP-1 benchmark: attack-path latency on the SPKG KùzuDB store (ADR-0111).

Builds a synthetic layered provenance graph and measures shortest-attack-path query
latency. The unit test runs this at a moderate scale (fast, CI-safe); the SP-1 acceptance
(1M edges, p99 < 500 ms) runs the same function with ``n_edges=1_000_000`` on a real host
— see BUILD_QUEUE BQ-SPKG-01.

Requires the optional ``spkg`` extra (installs ``kuzu``).
"""
from __future__ import annotations

import random
import time
from typing import Any

from .graph_store import SpkgGraphStore


def build_synthetic_edges(
    n_edges: int, layers: int = 6, fanout: int = 3, seed: int = 1
) -> list[dict[str, Any]]:
    """Return lineage-shaped edge dicts forming a layered DAG.

    A layered graph keeps shortest-path queries bounded (<= ``layers`` hops) and paths
    likely to exist, so latency — not path explosion — is what is measured.
    """
    rng = random.Random(seed)
    layers = max(2, layers)
    per_layer = max(2, n_edges // ((layers - 1) * max(1, fanout)))
    edges: list[dict[str, Any]] = []

    def ref(layer: int, idx: int) -> str:
        return f"L{layer}_N{idx}"

    for layer in range(layers - 1):
        for i in range(per_layer):
            for _ in range(fanout):
                j = rng.randrange(per_layer)
                edges.append(
                    {
                        "source": {"kind": "run", "ref": ref(layer, i)},
                        "target": {"kind": "run", "ref": ref(layer + 1, j)},
                        "edge_type": "produces",
                        "capsule_run_id": ref(layer, i),
                        "created_at": "2026-07-02T00:00:00.000000Z",
                    }
                )
                if len(edges) >= n_edges:
                    return edges
    return edges


def benchmark(
    n_edges: int = 5000,
    n_queries: int = 100,
    layers: int = 6,
    fanout: int = 3,
    seed: int = 1,
) -> dict[str, Any]:
    """Ingest a synthetic graph and measure attack-path query latency (ms)."""
    from novafabric.lineage._types import node_id_for

    rng = random.Random(seed + 7)
    edges = build_synthetic_edges(n_edges, layers=layers, fanout=fanout, seed=seed)
    per_layer = max(2, n_edges // ((max(2, layers) - 1) * max(1, fanout)))

    store = SpkgGraphStore()
    store.ingest_edges(edges)

    latencies: list[float] = []
    for _ in range(n_queries):
        s_ref = f"L0_N{rng.randrange(per_layer)}"
        t_ref = f"L{layers - 1}_N{rng.randrange(per_layer)}"
        s_id = node_id_for("run", s_ref)
        t_id = node_id_for("run", t_ref)
        t0 = time.perf_counter()
        store.attack_path(s_id, t_id, max_depth=layers)
        latencies.append((time.perf_counter() - t0) * 1000.0)

    latencies.sort()

    def pct(p: float) -> float:
        if not latencies:
            return 0.0
        k = min(len(latencies) - 1, int(round(p * (len(latencies) - 1))))
        return latencies[k]

    result = {
        "n_edges": store.edge_count(),
        "n_nodes": store.node_count(),
        "n_queries": n_queries,
        "p50_ms": round(pct(0.50), 3),
        "p99_ms": round(pct(0.99), 3),
        "max_ms": round(max(latencies), 3) if latencies else 0.0,
    }
    store.close()
    return result
