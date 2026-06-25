"""In-repo scale benchmark (smoke) for the KuzuDB lineage backend.

This is a CI-feasible benchmark that exercises the same ``blast_radius`` /
``provenance`` query paths as the authoritative 10M-edge benchmark in the
standalone ``nova-lineage-bench`` harness (BQ-015; see its ``MEASURED_CEILING.md``).

It is deliberately small so it runs in CI in a few seconds. It is **not** a
replacement for ``nova-lineage-bench`` and does **not** reproduce the 10M-edge
ceiling — it provides reproducible in-repo evidence that the query path works at
non-trivial scale and guards against gross latency regressions. The authoritative
"45.5 ms @ 10M edges, blast_radius depth-5" figure quoted in the docs comes from
``nova-lineage-bench``, not from this test (ADR-0053 gate: < 500 ms).
"""

from __future__ import annotations

import pathlib
import time

import pytest

kuzu = pytest.importorskip("kuzu")

from novafabric.lineage._types import LineageEdge  # noqa: E402
from novafabric.lineage.backends.kuzu import KuzuLineageStore  # noqa: E402

# ADR-0053 gate is blast_radius depth-5 p99 < 500 ms. At this in-repo scale the
# real margin is enormous; the assertion only guards against gross regressions.
GATE_MS = 500.0
# Kept small so the test stays CI-fast: KuzuLineageStore.insert() is ~20 edges/s
# (per-edge MERGE), so the insert phase dominates wall-clock. blast_radius is a
# bounded depth-5 traversal, so query latency is representative even at this size;
# the authoritative 10M-edge measurement lives in nova-lineage-bench.
N_CHAINS = 20
CHAIN_LEN = 5  # ~100 edges
N_QUERIES = 100
DEPTH = 5


def _edge(src: str, tgt: str) -> LineageEdge:
    return LineageEdge(
        edge_type="contains",
        source={"kind": "run", "run_id": src},
        target={"kind": "run", "run_id": tgt},
        confidence="high",
        capsule_run_id="cap-bench",
    )


def _p(samples_ms: list[float], q: float) -> float:
    s = sorted(samples_ms)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def test_kuzu_blast_radius_scale_smoke(tmp_path: pathlib.Path) -> None:
    store = KuzuLineageStore(db_path=tmp_path / "bench.kuzu")

    heads: list[str] = []
    for c in range(N_CHAINS):
        prev = f"r{c}_0"
        heads.append(prev)
        for i in range(1, CHAIN_LEN + 1):
            nxt = f"r{c}_{i}"
            store.insert(_edge(prev, nxt))
            prev = nxt

    # Warm-up (page cache / query plan), then measure.
    for h in heads[:20]:
        store.blast_radius(h, max_depth=DEPTH)

    latencies: list[float] = []
    for q in range(N_QUERIES):
        head = heads[q % len(heads)]
        t0 = time.perf_counter()
        rows = store.blast_radius(head, max_depth=DEPTH)
        latencies.append((time.perf_counter() - t0) * 1000.0)
        assert isinstance(rows, list)

    p50, p95, p99 = _p(latencies, 0.50), _p(latencies, 0.95), _p(latencies, 0.99)
    print(
        f"\n[kuzu in-repo smoke bench] edges={N_CHAINS * CHAIN_LEN} "
        f"queries={N_QUERIES} blast_radius depth-{DEPTH}: "
        f"p50={p50:.2f}ms p95={p95:.2f}ms p99={p99:.2f}ms "
        f"(ADR-0053 gate <{GATE_MS}ms; authoritative 10M p99=45.5ms in nova-lineage-bench)"
    )

    # Regression guard only — the real headroom at this scale is large.
    assert p99 < GATE_MS
