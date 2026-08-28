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
| `capture.absolute-overhead.mean` — Absolute wall-clock cost of full capture, independent of workload length | 4.56 s | measured | 7-point compute sweep 3M-400M (61.8x baseline range), n=30 reps/point, ascending; sd 0.29 s, CV 0.064. Reverse-order control over the same 7 points agrees at 4.34 s (4.8% apart) with a tighter spread, so run order does not produce it. | measured 2026-08-28 on Azure Standard_D8s_v5 (8 vCPU), westeurope, nova-runner; revalidate by 2027-03-01 — c1_asc.json + c1_desc.json from the 2026-08-28 campaign (private research record) |
| `collector.marginal-per-event` — Marginal cost added to one instrumented call in a warm process | 0.55 ms | measured | median marginal overhead across three call durations: +0.412 ms on an 11.5 ms call (3.58%), +0.514 ms on 52.2 ms (0.99%), +0.547 ms on 102.2 ms (0.54%). Roughly constant in absolute terms, so the percentage falls as calls get longer. | measured 2026-08-28 on Azure Standard_D8s_v5 (8 vCPU), westeurope, nova-bench4; revalidate by 2027-03-01 — c4.log, SPK-COL-2, from the 2026-08-28 campaign (private research record) |
| `collector.seal-batch.throughput` — Go collector NovaSeal batch processing throughput | 295000 events/s | measured | batch signing pipeline, p99 4.7 ms per batch (Phase 2 acceptance run) | measured 2026-05-10 on n1 experiment server; revalidate by 2026-11-01 — ROADMAP Phase 2 row (BQ-011) |
| `lineage.bulk-copy.throughput` — Lineage bulk-copy sustained edge rate -- MISSES its own service objective 7.5x | 1340 edges/s | measured | bulk-copy path against a 10000 edges/s objective set by our own SLO -- a 7.5x miss. Recorded because a catalog that carries only the objectives we meet is not a catalog. | measured 2026-08-28 on Azure Standard_D8s_v5 (8 vCPU), westeurope, nova-bench3; revalidate by 2027-03-01 — lineage_bulk_copy.json from the 2026-08-28 campaign (private research record) |
| `lineage.kuzu-blast-radius.p99` — KuzuDB lineage blast-radius query at 10M edges, p99 | 45.5 ms | measured | 10M-edge synthetic graph, lineage bench harness (BQ-015 promotion gate) | measured 2026-05-16 on n1 experiment server; revalidate by 2026-11-01 — ROADMAP Phase 6 row; rationale in ADR-0053 (private design record) |
| `metadata.rls-overhead.p50` — Postgres row-level-security cost on tenant-scoped reads, p50, at concurrency >= 4 | 7.4 % | measured | worst p50 cost vs a BYPASSRLS baseline over 100K/1000-tenant and 1M/5000-tenant tables at concurrency 4-32 (range 2.9-7.4%). At concurrency 1 the cost is 13.7-19.2% -- the fixed per-query policy evaluation is not amortised there. Tenant isolation verified exact in both runs. | measured 2026-08-28 on Azure Standard_D8s_v5 (8 vCPU), westeurope, nova-bench3; revalidate by 2027-03-01 — e4_rls_100k.json + e4_rls_1m.json from the 2026-08-28 campaign (private research record) |
| `policy.decision.p99` — OPA policy decision latency, p99, worst of four policies | 29 ms | measured | 4 policies x 500 iterations (promote allow/deny, replay-mutating allow/deny); p50 20.3-21.0 ms, p99 23.8-29.3 ms, all four decisions correct. Correctness only holds after the harness defect that omitted `asset_type` was fixed -- before that the bench measured the wrong decision. | measured 2026-08-28 on Azure Standard_D8s_v5 (8 vCPU), westeurope, nova-bench2; revalidate by 2027-03-01 — f9_opa.json + f9_sod.json from the 2026-08-28 campaign (private research record) |
| `query.persistent-index.speedup` — `nova query` persistent-index speedup vs full scan at 2K/10K capsules | 5.1× | measured | 2K capsules 5.1x, 10K capsules 4.9x (ADR-0225 A1 measurement — not the projected 13x) | measured 2026-08-06 on n1 experiment server; revalidate by 2027-02-01 — BL-040; rationale in ADR-0225 (private design record) |
| `topology.cluster-load.p99` — Topology read path, p99, worst observed across the node-count range | 2 ms | measured | worst p99 over 3 repetitions of a 100-4000-node sweep (0.98-1.99 ms); a separate single 100-8000-node sweep (80x range) stayed within 1.05-1.74 ms. Flat against node count while recluster is linear in it. | measured 2026-08-28 on Azure Standard_D8s_v5 (8 vCPU), westeurope, nova-bench2; revalidate by 2027-03-01 — f12_rep{1,2,3}.json + f12_topology.json from the 2026-08-28 campaign (private research record) |
| `topology.recluster.per-node` — Cluster-topology recluster cost per node (linear regime) | 0.0177 s/node | measured | 3 repetitions of a 100-4000-node sweep; R2=0.994, per-size CV 0.018-0.097. Crosses a 30 s service objective at ~1700 nodes -- reported as a limit, not omitted. | measured 2026-08-28 on Azure Standard_D8s_v5 (8 vCPU), westeurope, nova-bench2; revalidate by 2027-03-01 — f12_rep{1,2,3}.json from the 2026-08-28 campaign (private research record) |

Workload shapes are named, not adjectival: a number is only comparable to
the workload it states. Sizing guidance derived from these entries is
planned (ADR-0248 D4) and will inherit the weakest label in its input chain.
