# Performance SLO catalog

> **Generated file — do not edit.** Source:
> [`tests/bench/slo_catalog.toml`](../tests/bench/slo_catalog.toml), rendered by
> `scripts/gen_slo_docs.py`. A drift test fails when this page is stale.

Every published NovaFabric performance number lives here, and every number
carries exactly one honesty status:

- **gated** — enforced in CI
- **measured** — observed on a stated date/hardware; expires
- **target** — aspiration, not a promise

A `gated` number cannot drift from its gate: the gate test reads its
threshold from the same catalog that generates this page. A `measured`
number older than its `revalidate_by` date fails the suite until re-measured
or demoted — stale claims age out visibly. An absent entry means **no
claim**, which is itself information.

| Metric | Value | Status | Workload | Enforcement / provenance |
|---|---|---|---|---|
| `capture.trivial-run.p95` — Full captured run of `python -c pass` (subprocess spawn, fast-emit hooks, capsule write), p95 | 2 s | gated | 30 rounds, fresh run dir per round, warm filesystem | `tests/bench/test_capture_overhead_gate.py::test_capture_overhead_p95_gate` (ci:capture-overhead-gate) |
| `dashboard.analytics-summary.p95` — Dashboard /api/analytics/summary at 100K runs, p95 | 250 ms | gated | 100K flat runs, in-process | `tests/bench/test_dashboard_scale_gate.py::analytics_summary` (nightly:dashboard-scale) |
| `dashboard.audit-tail.p95` — Dashboard audit tail over 100K audit lines, p95 | 100 ms | gated | 100K audit lines, in-process | `tests/bench/test_dashboard_scale_gate.py::audit_tail` (nightly:dashboard-scale) |
| `dashboard.report-executive-summary.p95` — Dashboard executive-summary report at 100K runs, p95 | 300 ms | gated | 100K flat runs, in-process | `tests/bench/test_dashboard_scale_gate.py::report_executive_summary` (nightly:dashboard-scale) |
| `dashboard.report-run-history-deep-keyset.p95` — Dashboard run-history report, deep keyset page at 100K runs, p95 | 300 ms | gated | 100K flat runs, in-process | `tests/bench/test_dashboard_scale_gate.py::report_run_history_deep_keyset` (nightly:dashboard-scale) |
| `dashboard.report-run-history-page.p95` — Dashboard run-history report, first page at 100K runs, p95 | 300 ms | gated | 100K flat runs, in-process | `tests/bench/test_dashboard_scale_gate.py::report_run_history_page` (nightly:dashboard-scale) |
| `dashboard.report-throughput.p95` — Dashboard throughput report at 100K runs, p95 | 300 ms | gated | 100K flat runs, in-process | `tests/bench/test_dashboard_scale_gate.py::report_throughput` (nightly:dashboard-scale) |
| `dashboard.runs-page.p95` — Dashboard /api/runs first page at 100K runs, p95 | 100 ms | gated | 100K flat runs, in-process | `tests/bench/test_dashboard_scale_gate.py::runs_page` (nightly:dashboard-scale) |
| `dashboard.runs-search-page.p95` — Dashboard /api/runs/search keyset page at 100K runs, p95 | 100 ms | gated | 100K flat runs, in-process | `tests/bench/test_dashboard_scale_gate.py::runs_search_page` (nightly:dashboard-scale) |
| `seal.seal-call.p99` — NovaSeal.seal() single-envelope latency, p99 | 200 ms | gated | 100 rounds, in-process, SQLite Merkle log | `tests/seal/test_benchmark.py::test_seal_p99_latency_gate` (ci:seal-latency-gate) |
| `collector.seal-batch.throughput` — Go collector NovaSeal batch processing throughput | 295000 events/s | measured | batch signing pipeline, p99 4.7 ms per batch (Phase 2 acceptance run) | measured 2026-05-10 on n1 experiment server; revalidate by 2026-11-01 — ROADMAP Phase 2 row (BQ-011) |
| `lineage.kuzu-blast-radius.p99` — KuzuDB lineage blast-radius query at 10M edges, p99 | 45.5 ms | measured | 10M-edge synthetic graph, lineage bench harness (BQ-015 promotion gate) | measured 2026-05-16 on n1 experiment server; revalidate by 2026-11-01 — ADR-0053 v2a gate clearance; ROADMAP Phase 6 row |
| `query.persistent-index.speedup` — `nova query` persistent-index speedup vs full scan at 2K/10K capsules | 5.1× | measured | 2K capsules 5.1x, 10K capsules 4.9x (ADR-0225 A1 measurement — not the projected 13x) | measured 2026-08-06 on n1 experiment server; revalidate by 2027-02-01 — ADR-0225 amended measurement, BL-040 |

Workload shapes are named, not adjectival: a number is only comparable to
the workload it states. Sizing guidance derived from these entries is
planned (ADR-0248 D4) and will inherit the weakest label in its input chain.
