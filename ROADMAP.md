# NovaFabric Roadmap

> **Label glossary:** `experimental` — ships and works, interface may change before v1.0 schema freeze; `prototype` — implemented but not validated at target scale or runtime; `planned` — not yet implemented; `research` — design/research only. No item is listed as shipped until tests pass and the feature ships in a release.
>
> **Compliance note:** NovaFabric produces evidence that *supports* compliance workflows. It does not guarantee compliance with any regulation or standard.

---

## Shipped

| Version | Feature | Status |
|---|---|---|
| v0.58.0 | **Container image + Helm chart publishing** — NovaFabric now ships to three install channels from the public repo, each cut from a single `v*` tag: a multi-arch (`amd64`+`arm64`) runtime image to **GHCR** (`ghcr.io/novafabric/novafabric`, OIDC — no stored secret) with an optional Docker Hub mirror (`kazemi/novafabric`, gated on `DOCKERHUB_TOKEN`), and a first-party **Helm chart** (`deploy/helm/novafabric/`, `nova serve` + Postgres, non-root defaults) packaged as an OCI artifact to `oci://ghcr.io/novafabric/charts`. All publish jobs guarded to the public repo. No package/CLI/schema/API change. | **works today** |
| v0.57.0 | **Web — search & AI-crawler visibility pass** — the marketing site (`novafabric.dev`) gains the discoverability surface it lacked: a build-time `sitemap` (`@astrojs/sitemap`), `SoftwareApplication`/`Organization`/`Offer` JSON-LD structured data on the landing page (only page-visible facts asserted), a `robots.txt` with an explicit answer-engine crawler allowlist (`OAI-SearchBot`, `ChatGPT-User`, `Claude-SearchBot`, `Claude-User`, `PerplexityBot`, …), and an `llms.txt`. Website-only — no package/CLI/schema/API change. | **works today** |
| v0.56.3 | **App-wide bug audit — 10 confirmed defects fixed** — replay `semantic`/`exact` read the wrong record keys (similarity always 1.0 / never exact-eligible); lineage `blast_radius`/`provenance`/`replay_chain` recursive CTEs lacked cycle guards (hang on cyclic graphs); Merkle `verify_inclusion_proof` ignored `tree_size` (phantom-index soundness gap); `/topology/stream` WebSocket bypassed token + host auth; 5 KG endpoints leaked SQLite connections; `scan-secrets` 500'd on an unknown severity; diff crashed on an `outputs/` subdirectory; NIST-RMF + GDPR-RoPA read wrong (underscored) filenames/schema so present evidence read as missing. Regression tests added. Documented-but-unchanged v0.1 limits (timestamp degrade-tolerance, SoD bypass allowlist, daemon fork races) noted in CHANGELOG. | **experimental** |
| v0.56.2 | **Dashboard fix — hide always-empty "Children" tab on non-distributed runs** — the parent/child detail tab rendered on every run and always read "No child runs" for ordinary single-process captures; it now appears only for real distributed parent/worker capsules. | **experimental** |
| v0.56.1 | **Dashboard fixes — clean console + correct Art.73 clock** — `GET /api/seal/policy` (serve) returns 200 `configured:false` instead of 404 when no policy is signed (no more per-load console error); the Incidents tab shows a static `✓ filed` marker (not a live ticking countdown) for `reported`/`closed` incidents. | **experimental** |
| v0.56.0 | **Accountability Spine deepening** — court-admissibility evidence binding, measured-energy path, EU AI Act Annex IV, and a dashboard Accountability-Spine tab + energy/ledger/safety-case serve endpoints (ADRs 0093/0094/0095). | **experimental** |
| v0.55.0 | **Accountability Spine cores + dashboard CLI-coverage + slice-C spool forwarder** — three threads consolidated onto `main`. (1) **Accountability Spine** (ADRs 0093/0094/0095, experimental): `nova energy` Energy-Anchored Action Receipts (measured-or-declared-unknown joules/carbon → Seal; RAPL/NVML/Slurm-sacct; ADR-0093), `nova ledger` + replay-attestation Adversary-Anchored Ledger (per-stream sidecar hash chains, DSSE checkpoints, determinism certs + Rego gate; ADR-0094), `nova safety-case` Evidence-Grounded CAE compiler (no naked claims, κ/Wilson-driven backing; ADR-0095). (2) **Dashboard CLI-coverage + 10x**: 5 new tabs (Eval, Risk, Storage, Incidents w/ live Art.73 clock, Ops) + Seal-Ratchet + Evidence-Assertions panels surfacing ~30 previously CLI-only commands via ~14 new serve routes (safe-mutations-gated); shared toast/useMutation/DataTable/TabShell/URL-tab/palette-search frontend foundation. (3) **Slice-C spool forwarder** (ADR-0092): `SpoolSink` + `nova capture --emit-spool` + Go `novafabric-spool-forwarder` (drain→JetStream, exactly-once). | **experimental** |
| v0.54.0 | **`nova capture --fast-emit` — import-deferred hook install (ADR-0092 slice B)** — the default path imports every present SDK (`openai`, `mcp`, `requests`, …) at workload startup purely to patch it (~717 ms openai / ~340 ms mcp, paid even when unused). `--fast-emit` (also `CaptureOrchestrator(fast_emit=True)` / `NOVAFABRIC_FAST_EMIT=1`) registers one-shot `sys.meta_path` post-import callbacks so each SDK is patched only if/when the workload imports it; unused SDKs are never imported by capture, and the `EventRecorder`/pydantic models load lazily on first event. Measured (warm-fs, orchestrator, 4-run median): compute-only workload 2068 → 464 ms (−78 %); `import openai` workload 2223 → 1509 ms (−32 %) — win scales inversely with SDK usage. Fidelity unchanged (real-subprocess test); pure stdlib; fail-open; excluded from daemon delegation. 12 tests. | **experimental** |
| v0.53.0 | **Capture-side `record_*` API for the extended event taxonomy (ADR-0082 wiring)** — v0.49 landed the `CapsuleEventType` members + Pydantic models but deferred capture-side emit; this adds the public `EventRecorder` methods agents call to emit them: `record_state_transition`, `record_memory_operation`, `record_guardrail`, `record_evaluator`, `record_reranker`, `record_vector_retrieval` (last selects `VectorRetrieval{Started,Completed,Failed}` by `phase`). Each writes a dedicated `event_type`-tagged JSONL stream via `_append_typed`; same fail-open contract as `record_file_event`/`record_network_event`/`record_human_approval`. 6 tests, no new deps, no CLI change. | **works today** |
| v0.52.0 | **Warm capture daemon — resident emitter, slice A (ADR-0092, extends ADR-0020, realizes SI-2)** — opt-in prefork `AF_UNIX` daemon (`nova daemon start\|stop\|status`) imports `novafabric` once and serves each run from an `os.fork()` worker ("one run = one process, one capsule = one writer"), removing the per-run orchestrator cold-start. Stdlib-only `novacap` thin client (SCM_RIGHTS stdio passing; auto-fallback to direct `nova capture --no-daemon`); `nova capture --daemon/--no-daemon` with a plain-only delegation guard. UID-checked socket (`SO_PEERCRED`, 0600), no network listener, no new deps, Linux only. Measured (warm-fs): `/bin/true` capture 593.9 ms → 209.6 ms (−64.7 %); daemon capsule structurally identical to direct. Honest boundary: removes orchestrator import (#1) only — the workload's own hook-install import (#2) is unchanged → slice B (spool-light client). | **experimental** |
| v0.51.0 | **Collector productization slice + SPK-COL spike closure (ADR-0020 Accepted)** — (1) `nova collector rebuild`: offset-replay rebuild of the run_id-keyed JetStream buffer (SI-1, proven by SPK-COL-1 PASS 3/3) with per-run digests + order checking. (2) `deploy/collector-arrow/` OTel-Arrow wire profile (SI-3, proven by SPK-COL-3: 31.5% egress reduction, bounded burst RSS). (3) SPK-COL-2 hot-path number: +0.36%/call at 100ms calls. (4) ADRs 0086–0090 + 0041 v0.2 Accepted (Wave-2 slices shipped v0.50.0). (5) 43-test serve coverage campaign. Full resident-emitter collector rework remains the Phase-2 feature branch. | **experimental** |
| v0.50.0 | **SOTA gap-closure Wave 2 — first slices (ADRs 0086–0091 + ADR-0041 v0.2)** — (1) Intervention (counterfactual) replay (gap-005, ADR-0086): 5th replay mode, `InterventionSpec` event substitution + check-functions, diffable hard-marked output capsule. (2) Evidence completeness assertion + criterion→evidence bindings + DSSE re-performance attestation (gap-008, ADR-0087): `nova evidence completeness/bind/attest-replay`, three new in-toto predicate types. (3) Incident record + EU AI Act Art. 73 deadline clock (15/10/2-day, anchored at awareness) + OECD AIM export (gap-010, ADR-0088): `nova incident open/list/status/export`. (4) Forward-secure per-node key ratchet (gap-015, ADR-0089): HKDF epoch chain, secure-erase rotation, epoch-pubkey registry with rollback detection, `nova seal ratchet`. (5) Merkle log consistency proofs (gap-001 slice, ADR-0041 v0.2): O(log n) offline verifier on SQLite+Postgres logs, `nova seal log verify --consistency`. (6) Column-level lineage facets (gap-003 slice 1, ADR-0090): stdlib SQL extractor, `--with-facets`. eBPF capture (gap-012, ADR-0091) recorded as future design; collector (gap-002) stays spike-gated. | **experimental** |
| v0.49.0 | **SOTA gap-closure Wave 1 (6 capabilities, ADRs 0081–0085 + 0080 Accepted)** — (1) `nova promote direct --significance-gate` (gap-004): opt-in Wald-SPRT regression gate; noise and inconclusive evidence never block. (2) CloudEvents v1.0 envelope interop (gap-009, ADR-0081): `to_cloudevents()`/`from_cloudevents()` route evidence events through any CloudEvents-aware broker; extension attributes byte-compatible with the Go collector. (3) Extended span taxonomy (gap-011, ADR-0082): 8 new `CapsuleEventType` members (25 → 33) with Pydantic models — state transitions, memory ops, guardrails, evaluators, rerankers, vector retrieval. (4) Hot in-memory lineage impact index (gap-013, ADR-0083). (5) `nova diagnose <run-id>` failure attribution over the lineage graph (gap-006, ADR-0084). (6) `nova export-system-card` sealed system/audit cards + eval version pinning (gap-014, ADR-0085). Plus salvaged e2e contract tests for ErrorBoundary + managed SSE reconnect. | **experimental** |
| v0.48.0 | **Dashboard 10x (performance · reliability · scale · UX)** — (1) All 20 dashboard tabs code-split (`React.lazy`/`Suspense`, per-tab chunks) with per-tab `ErrorBoundary` crash isolation. (2) `CommandPalette` (⌘K/Ctrl+K) fuzzy navigation. (3) `usePolling` visibility-aware refresh hook; `Skeleton` loading states. (4) Managed SSE reconnect (`openManagedRunStream`, capped 1s→30s backoff with live connection state). (5) Backend scale: `/api/assets` SQL `LIMIT`/`OFFSET` pagination with column projection (`list_assets_paginated`); composite `(status, created_at DESC)` index on `runs_cache`; `_StatsCache.get_or_compute()` double-checked locking; cache-first run-history/cost-burn reports with filesystem fallback. (6) Web typecheck gate restored: TS6 `baseUrl` config error had been masking 8 real type errors; all fixed, `tsc --noEmit` green. | **experimental** |
| v0.47.0 | **Topology readability + evidence-grade gating primitives** — (1) Topology dashboard 5-mode view switcher (Cluster 2D force default, Call-graph dagre, Treemap, Table, 3D experimental); label declutter, +/−/Fit zoom, degree-0 singletons collapsed into one "misc" super-node, deterministic Louvain. (2) `novafabric.eval.significance` (gap-004, ADR-0080): `wilson_interval()` + `sprt_bernoulli()` three-valued SPRT verdicts for noise-robust regression gates. (3) `env.lock` optional `hardware.inference` block (gap-007) from the `NOVAFABRIC_INFERENCE_*` env contract — separates environmental drift from behavioral regression in replay/diff. (4) SPK-COL-1/2/3 collector spike records (gap-002, ADR-0020 gate). (5) FIX: promote policy gate now receives `eval_score` + `asset_type` (real OPA denied all promotions; NoopEngine allowed all — neither enforced the gate); 0.90 threshold scoped to agents. Also v0.46.1 (same day): mypy strict 85→0, Sigstore signer ported to sigstore-python 4.x (real runtime bug), CI typecheck job vs --all-extras. | **experimental** |
| v0.46.0 | **Dashboard parity gap closure** — fresh audit of all ~80 `nova` commands against the serve route table; 12 CLI capabilities gained dashboard equivalents (12 new REST endpoints, 161 total; 12 panels across 7 tabs): `eval list`/`eval run`, `policy list`/`policy sign`, `classify list-vocabularies`/`classify run` (manual), `aibom generate [--all]`, `ingest-capsule`, `run show --with-children` (capsule tree), `run lineage` (edge-type filter), `lineage-store profile`, `scan-secrets [--fail-on]` (PASS/FAIL gate). 39 new serve tests. Also fixed stale `.venv` shebangs from the repo move (quality gates were silently running against the old environment). | **experimental** |
| v0.45.0 | **Capture-fidelity + regulated-deployment sprint** — (1) **Capture correctness fix**: `install_all()` now sets the `EventRecorder` singleton from the writer so the wire-level hooks actually write `network_events.jsonl`/`file_events.jsonl`/`human_approvals.jsonl` in real captures (previously the singleton was set only in the orchestrator's parent process, silently dropping every NetworkEvent/FileEvent in the child capture subprocess + SDK + adapter paths). Capsule manifest now references the event streams. (2) **C2PA live marking** (EU AI Act Art.50, ADR-0074 → Accepted): `nova capture --mark-provenance` writes the sealed `c2pa-manifest.json` disclosure during capture; exporter hardened to read OTel `gen_ai.*` model keys. (3) **AIBOM per-deployment automation** (EU CRA, ADR-0073): `nova aibom generate [--all] [--force]` batch-refreshes CycloneDX 1.7 AI-BOMs across a capsule store. (4) **ClickHouse schema auto-migration** on `nova serve` startup. | **experimental** |
| v0.1 | Asset Registry (SQLite, 8 CLI commands, eval-gated promotion) | **experimental** |
| v0.2 | Run Capsule MVP (`nova capture`, `nova validate`, capsule schema, OTel GenAI) | **experimental** |
| v0.3 | Replay (`forensic`, `mocked`, `semantic`, `exact`) + structural Diff | **experimental** |
| v0.4 | Trust Layer (signed Evidence Bundles, secret scanning, redaction) + Lineage graph (SQLite) + OpenLineage | **experimental** |
| v0.5 | Integrations: MCP capture, `nova mcp-proxy`, wire-level hooks (requests, httpx, plugin contract) | **experimental** |
| v0.6 | Wire-level expansion (aiohttp, urllib3, Bedrock) + full OTel GenAI semconv + Multi-target runners (Local, Docker, Kubernetes, Slurm) + `nova api-proxy` | **experimental** |
| v0.7 | Server mode (Postgres, REST API, OIDC, RBAC, offline tokens) + `nova serve --experimental` | **experimental** |
| v0.8 | Policy + Approval Gates (OPA/Rego, maker-checker) + WORM storage adapters (S3/Azure/GCS) + legal holds | **experimental** |
| v0.9 | Standard Eval Suites (GAIA, SWE-bench, AgentBench, MMLU, TruthfulQA, Smoke; OCI-pinned; Rego-gated promotion) | **experimental** |
| Phase 0 | NovaSeal v0.1 — DSSE signing, RFC 3161 timestamps, SQLite Merkle log, `nova verify` | **experimental** |
| Phase 1 | Event Envelope v1 — JSON Schema, proto3, SHA-256 pin, Pydantic model, 70 tests, 1000-event corpus CI gate | **experimental** |
| Phase 2 | Collector tier — Go binary (`novafabric-collector`), crash-safe spool (100-SIGKILL recovery tested), NovaSeal batch processor (295K events/sec, p99 4.7ms), HPC profile, cap-006 Go types. BQ-011: 4/5 acceptance criteria satisfied (Lustre hardware-gated) | **experimental** |
| Phase 3 | Parent/Child Capsule — PARENT + WORKER hierarchy, PARTIALLY_COMPLETE state, Slurm DDP support; all 4 BQ-012 acceptance criteria met (LangGraph edge types, orphan placeholder on crash, Phase 1 latency gate p99=0.26ms, `--edge-type` CLI filter) | **experimental** (BQ-012 ✅ 2026-05-17) |
| Phase 4 | Object Capsule Store — manifest chain, multi-backend router (local/S3/MinIO), WORM conformance 10/10; p99 ≤ 350 ms put measured in benchmark test (not a CI gate) | **experimental** |
| Phase 5 | Metadata DB — Postgres RLS, multi-tenant isolation, PgBouncer support, `nova db migrate-to-postgres`, 85% coverage gate; `query_runs` p99 ≤ 200 ms measured in benchmark (not a CI gate) | **experimental** |
| Phase 6 | Lineage at Scale — `AbstractLineageStore` ABC, KuzuDB v2 backend, benchmark harness, migration kit; cross-site federation protocol | **experimental** (KuzuDB — v2a gate cleared 2026-05-16; p99=45.5ms @ 10M edges; BQ-015 ✅); **prototype** (federation — OQ-04 sovereignty open) |
| v0.14.0 | NovaSeal linked-envelope chain maker-checker — `nova seal propose/approve/verify`, `nova policy sign`; five-check SoD verifier; JCS-bound proposal_digest; ADR-0059 | **experimental** |
| v0.14.1 | SealTab dashboard + `/api/seal/*` REST routes (GET policy, GET proposals, POST verify); `make seal-smoke-test` Makefile target | **experimental** |
| v0.14.2 | Coverage gate hardening + approval-branch tests for seal routes (2323 tests, 90% coverage) | **experimental** |
| v0.14.3 | RBAC API — role-management REST surface (`POST/DELETE/GET /v0/admin/roles`, `nova server revoke-role`, ADR-0060); BQ-011 collector: 4/5 criteria satisfied (295K events/sec, cross-language round-trip, HPC autonomy, Epilog flush; Lustre hardware-gated) | **experimental** |
| v0.14.4 | Security & CI hardening — 10 Dependabot alerts cleared (1 critical grpc, 4 high otel/devalue, 4 moderate, 1 low tmp); collector CI restored to green (Go 1.22 → 1.25, lint regex fix, race-mode latency-vs-throughput split, narrow MPL-2.0 allowlist for `hashicorp/go-version` via ADR-0024 amendment) | **experimental** |
| v0.15.0 | **BQ-012** — Parent/Child acceptance criteria: LangGraph edge-type tests, orphan-crash placeholder, p99=0.26ms commit latency gate, `--edge-type` filter on `nova lineage` blast-radius/provenance. **BQ-005** — Compliance evidence MVP: `ToolPermissionEvent` (cap-004), `PIIDetectionGate` LEGAL-HOLD DRAFT (cap-001), `AnnexIVExporter` JSON-LD (cap-002), `NIS2Exporter` Phases 1/2/3 (cap-005); `nova export-annex-iv`, `nova export-nis2`, `nova subject-proof` CLI commands; 3 new JSON Schemas. | **experimental** |
| v0.15.1 | Dashboard `ComplianceTab` (⚖ Reg) — four compliance panels: Tool Permission Events (cap-004), EU AI Act Annex IV (cap-002), NIS2 Incident Report (cap-005), GDPR Subject Proof (cap-001). Four backing API endpoints: `/api/runs/{run_id}/tool-permission-events`, `/api/compliance/annex-iv`, `/api/compliance/nis2`, `/api/compliance/subject-proof`. | **experimental** |
| v0.15.2 | Track B dashboard scale — RunsTab cursor pagination + SSE live feed (`api.searchRuns()`, `openRunStream()`, pulsing live indicator, `N of ~total` header); RegistryTab load-more pattern (append-only, `_loadPage` helper). | **experimental** |
| v0.16.0 | Governance (`nova classify`, EU AI Act/NIST/OMB risk tier, ADR-0056); Compliance audit (`nova audit`, 6 regulatory profiles); Examiner exporters (`nova export-examiner bagit/pccp/iso42001`); PBS + LSF HPC runners; LangGraph/AutoGen/CrewAI/DSPy adapters; extended capture events (File/Network/HumanApproval); Judge framework (Embedding/Numerical/LLM judges, OPA adapter, `judge_gate.rego`); NovaSeal SigningIntent enum + RFC 3161 nonce replay protection; GCS WORM complete; ADR-0061–0065. | **experimental** |
| v0.16.1 | **Track C — Live Topology Dashboard v0.1**: `nova serve --topology`; Python server-side (`serve/topology/` — ADS encoder, DeltaBuffer, ClusterStore, TopologyExtractor, Louvain + FA2 approximation); three endpoints (`GET /topology/clusters` Arrow IPC, `WS /topology/stream` TDP v1, `GET /metrics/stream` SSE); `packages/nova-dashboard/` browser SPA (React 19 + Sigma.js 3 + Graphology 0.26 + Apache Arrow 21 + FA2 Web Worker); 49 Python + 16 TypeScript tests. OQ-02/OQ-03/OQ-ADR-002 resolved. | **experimental** |
| v0.16.2 | Patch: six topology runtime deps added to `pyproject.toml`; architecture docs update; ruff cleanup. | **experimental** |
| v0.16.3 | Patch: `nova serve` bind-safety gate moved before `[serve]` import; `RunsTab` React hooks ordering fix; developer guide expanded (adapters, audit profiles, topology dev loops); release notes v0.16.0–v0.16.2. | **experimental** |
| v0.16.4 | Dashboard governance + compliance UI — `GovernanceTab` (EU AI Act / NIST / OMB risk-tier colour-coded badges); 4 new serve endpoints (`GET /api/governance/classify`, `GET /api/compliance/audit/map`, `POST /api/compliance/audit/report`, `POST /api/compliance/examiner/{format}`); extended `ComplianceTab` + `SealTab`; `commandRegistry` Governance track. | **experimental** |
| v0.16.5 | Patch: `SealTab` `inputClass` scoping crash fixed; `make bundle` / `copy-dashboard.mjs` no longer wipes `topology/`; bundle rebuilt (`DashboardApp.CNGsRY1B.js`). | **experimental** |
| v0.17.0 | **Three parallel tracks from nova-design** — (A) Evidence Fabric v1.0 core pipeline: cap-001/002/003/004/006/009 (`CapsuleEventType` enum, `CostInterceptor`, `DualObjectStore` with `NOVA_CAP003_ENABLED` flag, `CaptureLevelPolicy`, `LineageConsumer` stub, `NovaObjectStore`); `nova schema/cost/storage/erasure/policy capture-level` CLI; `[scale]` extra; **ADR-0066**. (B) **Capsule Knowledge Graph v1** — second KuzuDB instance separate from lineage; `KGStore`, `EntityNormaliser`, `GCounter`/`CRDTAccumulator`, `KGIngestionPipeline`; `nova kg init/status/ingest/query`; `[scale-kg]` extra; **ADR-0067**. (C) **TV-5 3D Topology View** (`nova serve --tv5`) — `SnapshotStore3D`, `LayoutPipeline3D` (networkx spring_layout, resolves OQ-030), TV-5 REST+WS router, `TV5Panel.tsx` Three.js component; **ADR-0068**. 157 new tests; 0 regressions on 2875-test main suite. | **experimental** |
| v0.18.0 | **Dashboard parity for v0.17.0** — DB-KG-1 (new `KGTab` + `/api/kg/status` + `/api/kg/agents/{id}/edges`); DB-CAP-1 (Capture-level panel in `PolicyTab` + `/api/policy/capture-level` GET/POST); DB-ERA-1 (GDPR erasure panel in `ComplianceTab` + `/api/compliance/erasure/request` POST + `/status` GET); DB-STG-1 (Storage operations card in `InfraTab` + `/api/storage/validate` + `/api/storage/inspect/{run_id}`). 17 new backend tests. Restores v0.11 completeness principle (every CLI capability has a dashboard equivalent). Also: Makefile `bundle` target now uses `npm run build:dashboard` so topology/ is no longer at risk of being wiped. | **experimental** |
| v0.19.0 | **Full dashboard parity + run utilities** — DB-COST-1 (`CostTab` + `/api/cost/pricing|report`); DB-SCH-1 (`SchemaTab` + `/api/schema/list`); KG init/ingest panels (`POST /api/kg/init|ingest`); Generate Run ID (`GET /api/admin/new-run-id` + `NewRunIdPanel`); Database Ops reference (`DatabaseOpsPanel`); `ValidateDistributedBlock` + parent/child API (`GET /api/runs/{id}/children`, `POST /api/runs/{id}/validate-distributed`). Tutorial sections for KG/capture-level/storage/erasure/TV-5. 26 new backend tests. **All CLI surfaces have dashboard equivalents.** | **experimental** |
| v0.19.1 | Patch: KG ingest `run_id` autocomplete; CostTab + HoldsTab `SuggestInput` wiring. | **experimental** |
| v0.19.2 | Patch: infinite API loop fix (useRef pattern on all count-reporting tabs); dagre LR edge routing (sourcePosition/targetPosition); cost price table updated (Claude 4.x + current OpenAI). | **experimental** |
| v0.20.0 | **Dashboard Tier 1 gaps** — 7 new serve endpoints + 7 new dashboard panels closing the last CLI-vs-UI gaps: `DELETE /api/assets/{n}/{v}` (unregister with status guard); `GET /api/doctor` (7-subsystem health); `POST /api/policy/test` (OPA test runner, stub-aware); `GET /api/policy/explain` (decision_id audit lookup); `GET /api/compliance/audit/coverage` (per-profile control coverage); `POST /api/compliance/audit/bundle` (ZIP export + browser download); `POST /api/compliance/audit/verify` (AuditReport schema validation). Panels added: RegistryTab delete, AdminTab diagnostics, PolicyTab test+explain, ComplianceTab coverage+bundle+verify. 91 serve tests green. | **experimental** |
| v0.22.0 | **Gap-closure sprint (nova-design audit 2026-05-19)** — closes all 7 Tier-A correctness gaps and all Tier-B partial implementations: **A-1** DeltaBuffer pub-sub (live delta push ≤1 s); **A-2** Arrow IPC binary transport for all 7 TDP delta event types; **A-3** Collector file-based dead-letter queue (`dlq.go`, `dlq_entries_total` metric); **A-4** Postgres v002 quarterly RANGE partitions (16 child tables 2024-Q1→2027-Q4); **A-5** lineage migration reads from OCS capsule zip (ADR-0022 derived-index); **A-6** `get_capsule(verify_chain=True)` walks `prev_commit_hash` chain, raises `ChainIntegrityError`; **A-7** Python NovaSeal verifier now accepts Ed25519 in addition to ECDSA P-256. **B-1** TV-5 3D completions: `LODController.tsx`, `TimeSlider.tsx`, `tv5Store.ts` (Zustand 5), msgpack browser, TTL retention, Prometheus metrics. **B-2** KG Tier 2/3: `AliasTableResolver` (asyncpg+TTL), `ReviewQueue` (SQLite), `CapsuleEventConsumer` (NATS), `GCounter`/`ORSet` WAL CRDT. **B-3** Evidence Fabric scale: ClickHouse `AggregatingMergeTree` MV (`cost_by_model_mv`), `DualObjectStore` S3 routing, `LineageConsumer` NATS JetStream pull + KuzuDB bulk COPY. 3330 tests green. | **experimental** |
| v0.23.0 | **OAS v1.0 spec track — V-0, V-1, V-2** — All 8 version-bearing JSON schemas locked to `schema_version ^1\.` (ADR-0034 §1); all 9 v1 spec docs promoted to "pre-freeze ready"; OAS umbrella doc updated with technical gate checklist. 41 new schema lock tests (8 schemas × 5 assertions + 1 migrate round-trip). `nova migrate` (V-3) already shipped in v0.22.0. 3373 tests green. Remaining gate: ≥3 design partner sign-offs (V-5, 1/3). | **experimental** |
| v0.24.0 | **B-tier feature completeness sprint** — 7 deferred implementation gaps closed: (B-5) OCS zstd dict compression (`ZstdDictRegistry`, `[ocs-compress]` extra); (B-6) maker-checker bypass notification dispatch (`BypassNotifier` protocol, File+Webhook notifiers, env-var config); (B-7) NovaSeal Cloud KMS signing (`AwsKmsSigningBackend`, `AzureKvSigningBackend`, `GcpKmsSigningBackend`, `[seal-aws/azure/gcp]` extras); (B-8) metadata DB 100K-row scale benchmark + 1% checksum assertion; (B-9) JanusGraph lineage backend with real Gremlin queries + LDBC SNB BI adaptations + Helm chart; (B-4) Python cffi spool wrapper (`NovaPySpool`) + OCB builder config; (B-10) 5 new TypeScript test files for dashboard (AdsValidator, FA2 worker, renderer, tc-001–tc-010 contracts, TDP client). 6 new optional extras. 160 dashboard TS tests; 3500+ Python tests. | **experimental** |
| v0.25.0 | **C-tier compliance documentation sprint** — research-backed audit of all C-tier compliance/standards gaps across 6 regulatory domains (GDPR, HIPAA/FDA, RFC 3161, RO-Crate/PROV-JSON, Sigstore, 10-year AI governance forecast). OQ-021 resolved: `schemas/lineage-edge.schema.json` updated to Phase 3 four-type edge vocabulary (contains/spawned/delegated_to/replayed_from). 9 new compliance ADRs (ADR-0069–0077): GDPR Art.17 crypto-shredding, RFC 3161 trust chain, Sigstore keyless, PQC migration, AIBOM/CycloneDX, C2PA content credentials, DID/VC identity, EU AI Act Art.12 compliance mode, multi-region log sovereignty. 12 new compliance docs in `design/compliance/`. | **experimental** |
| v0.25.1 | **Compliance exporters + OWASP assure + Evidence Fabric** — cap-007 `nova export-ropa` (GDPR Art.30 RoPA, JSON-LD); cap-008 `nova export-aibom` (CycloneDX 1.6 AI-SBOM, no SDK); cap-009 `nova export-nist-rmf` (NIST AI RMF 1.0, GOVERN/MAP/MEASURE/MANAGE scoring, 8 metrics); `nova assure` (OWASP LLM Top 10 2025, 10 checks, E-10); `nova mcp scan` (25 OWASP rules, E-9); `novafabric.evidence_fabric` (`DuckDBAccumulator`, `EventQueueConsumer`, `LocalPIITable`); OpenSSF Scorecard CI; PgBouncer deploy config; 6-phase cluster-scale migration guide. | **experimental** |
| v0.26.0 | **Dashboard scale hardening + KG Protocol fix** — BL-1 6 SQL indexes on `SQLiteMetadataStore` (runs/assets/lineage); BL-5 TanStack Virtual scroll in `RunsTab`; BL-6 `RegistryTab` cursor-pagination; SSE `/api/events/runs`; B-2 KG Protocol type fix. | **experimental** |
| v0.26.1 | **Ecosystem adapters + doc-sync** — (E-5..E-8, ADR-0078) four framework adapters: OpenAI Agents SDK `TracingProcessor`, Google ADK `BasePlugin`, Bedrock AgentCore EventStream wrapper, A2A `ClientCallInterceptor`; executable differentiation table (E-3, `scripts/verify_differentiation_table.py`, 10 checks, D-01..D-10). Full CLI reference gap-fill: `nova run`, `nova kg alias`, `nova kg entity-queue`, `nova storage`, `nova policy capture-level`, `nova mcp risk-report`, `nova lineage-store`, `nova lineage rebuild`. Release notes, CHANGELOG, architecture docs updated. | **experimental** |
| v0.26.2 | **Lint-only patch** — remove unused `importlib.util` import from `serve/app.py`. | **experimental** |
| v0.26.3 | **G-E Track 5 dashboard parity** — `AssurancePanel` (OWASP LLM assurance, E-10) + `MCPScanPanel` (MCP supply-chain scan, E-9) + `AdaptersPanel` (adapter registry, E-5..E-8) added to `ComplianceTab`; `api.ts` `assureRun`/`mcpScan`/`listAdapters`; static bundle rebuilt; CLI ref + architecture docs updated. Every CLI surface now has a dashboard panel. | **experimental** |
| v0.26.4 | **Test isolation patch** — `asyncio.run()` in 4 test files; hardcoded worktree path fix in `test_differentiation_table.py`. | **experimental** |
| v0.26.5 | **OPA policy-source evaluation + 3 dashboard UX fixes** — `OpaEngine` now evaluates user-supplied Rego source via `policy_source`; `HomeTab` cost 401 fix; `RegistryTab` sparkline re-fetch after eval run; `MCPScanPanel` repositioned above footer. | **experimental** |
| v0.27.0 | **Full CLI-to-dashboard parity** — 11 new backend endpoints + frontend panels: seal bypass (`BypassSodPanel`), admin role assign/revoke, JWKS flush, DB upgrade, capsule migrate, validate-spec (`ValidateSpecPanel`), asset report (`ReportPanel`), MCP risk report (`MCPRiskReportPanel`), capsule delete, lineage import (`LineageImportPanel`), eval compare (`EvalComparePanel`). 59 new tests. | **experimental** |
| v0.27.1 | **Two-tier Docker stack + deploy docs** — `make dev-up` (Postgres + dashboard) and `make prod-up` (full: + ClickHouse + NATS + Kafka + PgBouncer + JanusGraph); `novafabric-` container naming; comprehensive Data layer section in `design/architecture/cluster-scale.md`; `NOVA_JANUSGRAPH_URL` env var. | **experimental** |
| v0.28.0 | **G-A correctness gap closure** — G-A7: `ECDSAP256Signer` in Go collector aligns with Python NovaSeal DER format (15 new tests); G-A3: `NOVA_DLQ_DIR` wired into HPC leaf spool store (DLQ struct existed since v0.22.0 but was never instantiated); G-A6: `get_capsule()` defaults to `verify_chain=True` (OQ-027). G-A1/A2/A4/A5 + G-B1/B2/B3 confirmed already closed. | **experimental** |
| v0.29.0 | **KG multi-layer topology + Policy UX + Rekor + Evidence Fabric scale tier + Compliance + v1 criteria** — `MCPServer`/`SERVED_BY` auto-discovery; `GET /api/kg/topology`; Policy Explain autocomplete; `nova export-evidence --sigstore`; `NATSJetStreamConsumer` + `ClickHouseAccumulator` + `AvroSerializer` scale backends (`NOVA_NATS_URL`/`NOVA_CLICKHOUSE_URL` env routing; `pip install novafabric[nats\|clickhouse\|avro]`) (G-B3); RFC 3161 full trust chain + CRL/OCSP revocation; `export_ro_crate()` RO-Crate v1.1 + `export_prov_json()` W3C PROV-JSON library functions (CLI planned) (G-C); `nova migrate-schema` batch capsule migration + pgBouncer deploy config + cluster-scale migration runbook (G-F). | **experimental** |
| v0.29.1 | **Patch** — bundle sync, `nova migrate-schema` Rich markup fix, avro test skip guard. | **experimental** |
| v0.29.2 | **KG scalability + dashboard polish** — `IngestTracker` SQLite-backed persistent ingest state (survives restarts); `query_agent_mcp_servers()` two-hop Cypher query; `nova kg status` Rich table with per-type node counts; `TopologyLayerPanel` lazy-load; `resolve_merkle_db_path()` centralised NovaSeal path logic; docs fully synced (architecture/CLI-ref/ROADMAP). | **experimental** |
| v0.29.3 | **KG ingest completeness + bug fixes** — `nova kg ingest` reads `tool-calls.jsonl` alongside `model-calls.jsonl`; `query_agent_mcp_servers()` Decimal cast fix; `NOVAFABRIC_SEAL_DB_PATH` env-var now beats `novaseal.yaml`; dashboard `AgentQueryPanel` shows MCP servers table; `GET /api/kg/agents/{id}/edges` includes `mcp_servers`; `docs/developer-guide.md` MCPServer extension section. | **experimental** |
| v0.29.4 | **Dashboard `AgentQueryPanel` MCP servers table** — `GET /api/kg/agents/{id}/edges` includes `mcp_servers` 2-hop list; `AgentQueryPanel` renders third results table; lineage.md KG section fully synced (env-var table, `IngestTracker`, API surface). | **experimental** |
| v0.30.0 | **Dashboard CLI parity: `nova verify`, `nova suggest-register`, `nova lineage emit-openlineage` panels** — `CapsuleVerifyPanel` (SealTab), `SuggestRegisterPanel` (RegistryTab), `OpenLineageExportPanel` (LineageTab); `POST /api/runs/{id}/verify`; `GET /api/lineage/{id}/emit-openlineage`; `POST /api/assets/register-from-yaml`; bug fixes (TraceDiffGraph span matching, SealTab empty-log UX); `default_capsule_dir()` single-source-of-truth fix. | **experimental** |
| v0.30.3 | **Icon fix + UX improvements + Reports tab + SuggestInput 100% coverage** — Favicon N-path corrected (was M); hex frame + apple-touch-icon; brand mark in collapsed sidebar; collapsible sidebar groups (localStorage); breadcrumb top bar with connection pill; TABS order synced from NAV_GROUPS; dynamic keyboard help; EmptyState icon prop (padding p-12→p-8); badge colour fix (neutral/info-blue). New `ReportsTab` Catalog+Builder layout with 10 report types (Developer/Ops/Compliance/Management); 10 `/api/reports/*` endpoints; CSV+JSON+PDF export; `api.reports.fetch()`; 13 new tests; JSONL TraceSpanView in CapsuleInspector. `SuggestInput` now covers all 6 remaining bare ID inputs: `deploymentId`/`incidentId` (localStorage MRU), `subjectId` panels (localStorage MRU), `runId` in AssurancePanel + StorageOpsCard (live-fetched). `useLocalMru` hook. 3827 tests. | **experimental** |
| v0.44.0 | **6-track compliance + cryptography sprint** — OQ-01 resolved: `nova pii erase` GDPR Art.17 crypto-shredding (DEKStore, ErasureReceipt, AES-256-GCM, ADR-0069 Accepted); cap-001 graduates to active; `NOVA_CAP003_ENABLED` defaults true. RFC 3161 trust chain: `NonceStore` (63-bit nonce replay guard, offline_mode), `verify_tsa_cert_chain()` cert-chain depth ≤4 (ADR-0070 Accepted). Sigstore keyless: `nova seal sign --backend sigstore`, `nova verify --backend sigstore`, Sigstore Bundle v0.3, `novafabric[sigstore]` extra (ADR-0071 Accepted). `nova export-hipaa-proof` — all 18 Safe Harbor identifier categories, proof_digest. Postgres partition benchmark on n1 (24 vCPU / 62 GiB / PG 16.13): 10K×1M rows, p99 worst-case 16 ms (12× below FR-17 gate). pgBouncer citation gap resolved (ADR-0050/0052). 143 new tests. **Dashboard parity restored**: `PiiErasePanel` (ComplianceTab), `HIPAAProofPanel` (ComplianceTab), `SigstoreSignPanel` (SealTab), `SigstoreVerifyPanel` (SealTab) — all 20 dashboard tabs have complete CLI parity with v0.44.0. 10 new serve tests. | **experimental** |
| v0.43.0 | **CLI help overhaul** — all ~55 `nova` commands now have a `Scope:` line and `\b Examples:` block; 8 `str` Enum types (`RunnerName`, `ReplayMode`, `DiffOutputFormat`, `ReportFormat`, `LineageOutputFormat`, `AssureOutputFormat`, `ScanSeverity`, `ScanThreshold`) give shell tab-completion and typed validation; ADR refs removed from all user-facing help strings. | **stable** |
| v0.42.0 | **KG bulk ingest** — `nova kg ingest --all` + `--capsule-dir`; `POST /api/kg/ingest-all`; **Re-ingest All** dashboard button in KG tab. Scans all capsule subdirectories, tracks already-ingested dirs via `IngestTracker`, displays per-capsule progress. | **stable** |
| v0.41.0 | **Test coverage sprint** — 36 new tests across `cli/test_approve_cmd.py`, `cli/test_rebuild_cmd.py`, `cli/test_storage_scale_cmd.py`, `lineage/test_federation_shard_local.py` (token branch coverage), `trust/test_rfc3161.py` (chain-verification paths, DER long-form, OCSP/CRL URL extraction). 4114 tests total. | **stable** |
| v0.40.0 | **CLI gap closure: `nova eval list` + `nova policy list`** — `nova eval list` discovers all `novafabric.eval_suites` entry-point adapters and shows suite ID, version, OCI digest, and module path; `nova policy list` shows Rego bundle files + signed `PolicyStore` versions with `--namespace` filter; `PolicyStore.list_all()`; 11 new tests. | **stable** |
| v0.39.0 | **CycloneDX ML-BOM upgrade 1.6 → 1.7** — `AIBOMExporter` emits `specVersion: "1.7"` (ECMA-424 2nd Edition); `metadata.tools` object format; `metadata.lifecycles`; `modelCard.limitations` (CRA Art.9 disclosure); `type: "data"` dataset components from `lineage_datasets`; dynamic tool version. 7 new tests; 26 total. | **experimental** |
| v0.38.1 | **`nova init` — pip install first-run setup** — creates `NOVAFABRIC_HOME` directory structure (`capsules/`, `keys/`, `replays/`); generates Ed25519 signing keypair (mode 600); idempotent (`--force` to regenerate). 17 tests. | **stable** |
| v0.38.0 | **Scale-S4: Postgres Merkle log for NovaSeal** — `PostgresMerkleLog` (psycopg3); `open_merkle_log()` URI-dispatch factory; sampled `verify_consistency()` (p99 < 200 ms at 1M entries); `--full` audit flag; `[seal-postgres]` extras; `NOVAFABRIC_SEAL_DB_PATH` DSN support; CLI default DB honored at invocation time. | **experimental** |
| v0.37.0 | **Dashboard parity for compliance CLIs** — `ComplianceTab` gains 4 panels: GDPR Art.30 RoPA Export, AI-SBOM Export (CycloneDX 1.6), NIST AI RMF Report (GOVERN/MAP/MEASURE/MANAGE scores), AIBOM Coverage Status (CRA deadline 2026-09-11); 4 new `nova serve` endpoints; 8 new integration tests. | **experimental** |
| v0.36.0 | **Scale-S3: CapsuleWatcher + `nova ingest-capsule`** — background capsule indexer with `PollingBackend` (default) and `WatchdogBackend` (inotify/FSEvents/kqueue); `nova ingest-capsule` CLI (single / `--all` / `--watch`); `[watch]` extras (`watchdog>=4.0.0`); `NOVA_WATCHER_BACKEND` + `NOVA_WATCHER_INTERVAL` env vars; `nova serve` delegates startup and incremental indexing to `CapsuleWatcher`; `watchdog` in dev deps for full CI coverage; 29 tests. | **experimental** |
| v0.35.0 | **AIBOM / CRA compliance** — `nova aibom status` (CRA SBOM coverage tracker); `nova export-aibom --output` default to `<capsule_dir>/aibom.json`; ADR-0073 Accepted; 19 AIBOM tests. Security: sqlfluff 4.2.1, idna 3.15, ws 8.20.1. | **experimental** |
| v0.34.0 | **Dashboard parity for v0.32–v0.33 regulatory CLIs** — `RoCrateExportPanel` + `C2paExportPanel` (ComplianceTab), `ProvJsonExportPanel` (LineageTab), `EuAiActStatusPanel` + `EuAiActExportPanel` (GovernanceTab); 5 new REST endpoints; 22 new tests. | **experimental** |
| v0.33.0 | **Regulatory compliance exporters (EU AI Act Art.50 + Art.12)** — `nova export-c2pa` (C2PA v2.3 manifest, `c2pa.ai.generated: true`, TSP-signing-ready, ADR-0074); `nova euaiact export` (Art.12 log events, date-range filter, Art.74 authority access, ADR-0076); `nova euaiact status`; `is_within_retention()` for GDPR Art.17(3)(b) deferral gate. 35 new tests. | **experimental** |
| v0.32.0 | **Compliance CLI wiring** — `nova export-rocrate <capsule_dir>` (RO-Crate v1.1 ZIP; library shipped v0.29.0, CLI wired v0.32.0); `nova lineage export-prov <capsule_dir>` (W3C PROV-JSON; library shipped v0.29.0, CLI wired v0.32.0). 19 new tests. | **experimental** |
| v0.31.1 | **KGStore Prometheus metrics** — optional `prometheus_client` instrumentation; 4 counters/gauges (`novafabric_kg_node_merge_total`, `_edge_upsert_total`, `_crdt_merge_total`, `_node_count`); graceful fallback when library absent. | **experimental** |
| v0.31.0 | **Dashboard CLI parity sprint — 7 gaps closed** — KGQueryPanel (`nova kg query`), KGAuditPanel (`nova kg audit`), EntityQueuePanel (`nova kg entity-queue list/approve/reject/stats`), KGAliasPanel (`nova kg alias list/register`); GdprErasurePanel erasure-status section (`nova erasure status`); AuditMapPanel (`nova audit map --profile`); RunsTab Children view (`nova run show --with-children`). 2 new backend endpoints (GET/POST `/api/kg/aliases`); 8 new `api.ts` methods; 9 new backend tests. 3837 tests. | **experimental** |

---

## Planned — v0.27.x (Compliance Implementation Sprint)

Goal: implement the compliance ADRs written in v0.25.0. All items depend on their corresponding ADR being accepted.

| Feature | ADR | Deadline driver |
|---|---|---|
| ~~GDPR Art.17 crypto-shredding — `nova pii erase` + `nova pii status`; DEKStore; ErasureReceipt; cap-001 graduates from LEGAL-HOLD DRAFT~~ — **shipped v0.44.0** | ADR-0069 Accepted | GDPR Art.17 (existing obligation) |
| ~~RFC 3161 trust chain + nonce replay guard — air-gapped TSA verification; offline_mode for HPC; cert-chain depth ≤4~~ — **shipped v0.44.0** | ADR-0070 Accepted | eIDAS / QTSP trust chain |
| RO-Crate v1.1 FAIR export — `nova export-rocrate`; machine-readable research object metadata | — | FAIR data mandates | **shipped v0.32.0** |
| W3C PROV-JSON lineage export — `nova lineage export-prov`; standard provenance graph for interoperability | — | OpenLineage / W3C PROV | **shipped v0.32.0** |
| ~~HIPAA Safe Harbor cryptographic proof — `nova export-hipaa-proof`; 18-identifier de-identification~~ — **shipped v0.44.0** | — | HIPAA Safe Harbor §164.514(b) |
| ~~AIBOM upgrade CycloneDX 1.6 → 1.7~~ — **shipped v0.39.0** — `nova export-aibom` emits CycloneDX 1.7 (ECMA-424 2nd Edition); `metadata.tools` object format, `metadata.lifecycles`, `modelCard.limitations`, `type: "data"` dataset components; ADR-0073 Accepted | ADR-0073 | EU CRA SBOM deadline 2026-09-11 |
| C2PA content credentials — `nova export-c2pa` (post-hoc) + `nova capture --mark-provenance` (live, sealed) | ADR-0074 **Accepted** | EU AI Act Art.50 deadline 2026-08-02 | **shipped** — export v0.33.0, live in-capture marking v0.45.0; TSP hard-binding signature deferred |
| ~~Sigstore keyless signing — `nova seal sign --backend sigstore`; Sigstore Bundle v0.3; `novafabric[sigstore]` extra~~ — **shipped v0.44.0** (Fulcio OIDC + Rekor v2; `nova verify --backend sigstore`; full CRL/OCSP deferred to future) | ADR-0071 Accepted | ADR-0055 Sigstore path |
| EU AI Act Art.12 compliance mode — `nova euaiact export` | ADR-0076 | EU AI Act Art.12 binding 2026-08-02 | **shipped v0.33.0** (authority export; capture-time gate deferred) |

---

## Regulatory Deadline Calendar

Dates represent mandatory or significant regulatory milestones relevant to NovaFabric deployments. All items are documented as **planned** or **future design** — NovaFabric does not certify compliance.

| Date | Regulation / Standard | Obligation | ADR |
|---|---|---|---|
| **2026-08-02** | EU AI Act Art.12 | High-risk AI logging requirements binding for new-to-market systems; `nova euaiact export` required by regulated deployments | ADR-0076 |
| **2026-08-02** | EU AI Act Art.50 | C2PA content marking mandatory for AI-generated content; `nova export-c2pa` (post-hoc) + `nova capture --mark-provenance` (live, sealed) ✅ shipped v0.45.0 | ADR-0074 |
| **2026-09-11** | EU Cyber Resilience Act (CRA) | SBOM/AIBOM required for AI-enabled products; `nova export-aibom` + `nova aibom generate --all` (per-deployment automation) ✅ shipped v0.45.0 | ADR-0073 |
| **2027-12-11** | EU CRA | Full CRA application; all products with digital elements in scope | ADR-0073 |
| **2028-08-02** | EU AI Act Art.12 | Extended application to AI in regulated products (medical devices, machinery, vehicles) | ADR-0076 |
| **2029** | PQC migration window opens | 10-year retained artifacts signed with ECDSA today must be re-signed or co-signed with ML-DSA before ECDSA deprecation | ADR-0072 |
| **2030** | NIST IR 8547 | RSA and ECDSA deprecated for US federal systems; NovaSeal must offer ML-DSA as primary signing algorithm | ADR-0072 |
| **2031–2033** | EU binding DID/VC agent identity | EU digital identity framework expected to mandate verifiable credentials for autonomous AI agents | ADR-0075 |
| **2035** | NIST IR 8547 | RSA and ECDSA disallowed entirely for US federal systems; ECDSA-only capsule chains unverifiable | ADR-0072 |

---

## In progress — v1.0 (OAS spec stability)

| Track | Feature | Status |
|---|---|---|
| V-0 | ADR-0034 + OAS v1.0 umbrella spec | ✅ **complete** (v0.23.0) |
| V-1 | OAS extensions registry + 9 spec doc promotions | ✅ **complete** (v0.23.0) |
| V-2 | Schema promotions (8 schemas locked to `^1\.`) | ✅ **complete** (v0.23.0) |
| V-3 | `nova migrate` CLI — v0.1.x → v1.0.0 capsule conversion | ✅ **complete** (v0.22.0, 51 tests) |
| V-4 | LF AI & Data Sandbox application draft | **experimental** (submit when sponsor identified) |
| V-5 | Design partner recruitment (≥ 3 sign-offs for `Adopted` status) | **in progress** (1/3) |

---

## Cluster-scale phases — all shipped

All cluster-scale phases (Phase 0–6) are shipped; per-phase maturity labels are in
the Shipped table above. Research documentation lives in this repo under the private
`design/` directory.

---

## Shipped — v0.11 Dashboard Completeness

Goal: every CLI capability has a dashboard equivalent.

| Track | Feature | Status |
|---|---|---|
| ~~DC-1~~ | Evidence verification UI — inline `sig/tsr/log` badges (Ed25519 DSSE + RFC 3161 + NovaSeal Merkle) | **experimental** (2026-05-14) |
| ~~DC-2~~ | Legal holds tab — place / release / list holds; sidebar count badge | **experimental** (2026-05-14) |
| ~~DC-3~~ | Secrets viewer — redaction proof panel in RunsTab | **experimental** (2026-05-14) |
| ~~DC-4~~ | Semantic + exact replay UI — similarity gauge + determinism eligibility card | **experimental** (2026-05-14) |
| ~~DC-5~~ | Policy check UI — PolicyTab with OPA/Rego policy tester, ALLOW/DENY badge | **experimental** (2026-05-14); OPA explain trace viewer added v0.13.2 |
| ~~DC-6~~ | Asset spec diff — Compare… modal + diff table in Registry tab | **experimental** (2026-05-14) |
| ~~DC-7~~ | Capsule validation UI — Validate button + expandable error badge in RunsTab | **experimental** (2026-05-14) |
| ~~DC-8~~ | Diff URL persistence — `?run_a=&run_b=` (v0.11) → migrated to `?run_ids=…` in v0.13.3 | **experimental** (2026-05-13; format updated 2026-05-15) |

---

## Shipped — v0.12 Asset Intelligence

Goal: close all six asset lifecycle gaps; add dashboard compare shortcut.

| Track | Feature | Status |
|---|---|---|
| ~~AI-2~~ | `nova asset-diff agent@v1 agent@v2` — spec-level diff (unified + JSON output) | **experimental** (v0.11.1, 2026-05-14) |
| ~~AI-3~~ | `nova rollback my-agent` — atomic archive + restore of previous production version | **experimental** (v0.12.0, 2026-05-14) |
| ~~AI-4~~ | `nova list --stale [--stale-days N]` — surface assets inactive > N days | **experimental** (v0.12.0, 2026-05-14) |
| ~~AI-5~~ | Asset status at consumption — `status_at_consumption` written to `assets.jsonl` + lineage | **experimental** (v0.11.1, 2026-05-14) |
| ~~AI-6~~ | Declared dependency graph — `depends_on` edges written at `nova register` time | **experimental** (v0.11.1, 2026-05-14) |
| ~~C-4~~ | RunsTab multi-select → DiffTab compare shortcut — check 2 rows, "Compare selected ⊕" auto-jumps | **experimental** (v0.12.0, 2026-05-14) |
| ~~C-5~~ | `--require-asset-status` gate on `nova capture` — pre-capture status enforcement | **experimental** (v0.12.0, 2026-05-14) |
| ~~C-2 / AI-1~~ | `nova suggest-register` — asset suggestion engine; analyze capsules, draft YAMLs, 3 modes (interactive / draft-only / auto); dashboard smart empty state; post-capture hint | **experimental** (v0.12.1, 2026-05-14) |
| ~~AI-7~~ | `nova unregister <name@version>` — hard-delete with status guard + audit trail | **experimental** (v0.12.6, 2026-05-14) |
| ~~UX-2~~ | Dashboard coverage Phase 1 — 35-command Commands tab (4 journey tracks), Lineage QueryPanel, InfraTab (10 Phase 0–6 component cards), enriched CaptureTab | **experimental** (v0.12.7, 2026-05-14) |
| ~~UX-1~~ | Dashboard autocomplete suggestions — `SuggestInput` shared component; context-aware ref inputs across all tabs (Lineage, Diff, Holds, Policy, Compliance, Infra — 100% coverage v0.30.3); `useLocalMru` for free-text fields | **experimental** (v0.12.7→v0.30.3, 2026-05-20) |
| ~~EU-1/EU-2~~ | Eval results panel: null score → `—`, empty suite name → `(unknown suite)` | **experimental** (v0.12.8, 2026-05-14) |
| ~~Bug-1~~ | Promote dialog: invalid target statuses disabled (`archived → production` was wrongly allowed) | **experimental** (v0.12.9, 2026-05-14) |
| ~~Bug-2~~ | Promote dialog: inline error banner on 4xx responses (was toast-only, dialog stayed open) | **experimental** (v0.12.9, 2026-05-14) |
| ~~Bug-3~~ | Promote dialog: eval-gate copy now conditional on `asset_type === 'agent'` | **experimental** (v0.12.9, 2026-05-14) |

---

## Shipped — v0.12.x Dashboard UX Improvements

Goal: polish the 13-tab dashboard based on a full stress-test walkthrough.
Detailed plans: `.claude/plans/dashboard-ux-improvements-v012.md`.

| Track | Feature | Status |
|---|---|---|
| ~~DU-1~~ | Status filter pill-bar above RunsTab table (All / running / success / failure / error) | **experimental** (a612186, 2026-05-14) |
| ~~DU-2~~ | Copy-to-clipboard icon on run ID cells in RunsTab (hover-reveal) | **experimental** (a612186, 2026-05-14) |
| ~~DU-3~~ | Ancestry breadcrumb above selected node in LineageTab detail panel | **experimental** (76254b0, 2026-05-14) |
| ~~DU-4~~ | Persist last diff comparison (from/to/result) in `sessionStorage` across navigation | **experimental** (d8c2805, 2026-05-14) |
| ~~DU-5~~ | Bulk promote — checkbox column + floating action bar for batch promotion | **experimental** (8f233a5, 2026-05-14) |
| ~~DU-6~~ | Action-type filter dropdown on AuditTab (promote / eval / rollback / approve / register) | **experimental** (0cebd5d, 2026-05-14) |
| ~~DU-7~~ | Staleness indicator — amber border on HomeTab resume cards older than 24 h | **experimental** (0cebd5d, 2026-05-14) |
| ~~DU-8~~ | "Open folder" link in CaptureTab recent capsules when local path is present | **experimental** (4513cdd, 2026-05-14) |
| ~~DU-9~~ | Client-side Rego syntax feedback in PolicyTab textarea (missing `package`, unbalanced `{}`) | **experimental** (4513cdd, 2026-05-14) |
| ~~DU-10~~ | Shared `<EmptyState>` component — `bordered`/`fill`/`inline` variants; applied to HoldsTab, AuditTab, RunsTab, EvidenceList, RegistryBrowser, LineageGraph | **experimental** (v0.13.1, 2026-05-15) |
| ~~Hooks-1~~ | AuditTab blank page — `useMemo(availableActions)` moved before early returns; `(entries ?? [])` null guard | **experimental** (v0.12.10, 2026-05-14) |
| ~~Hooks-2~~ | LineageTab blank page — `useMemo(ancestors)` moved before early returns; rewrote to depend only on `selectedNode` + `edges` | **experimental** (v0.12.10, 2026-05-14) |
| ~~Paths-1~~ | `NOVAFABRIC_HOME` — single canonical data root; all internal paths derive from it (`registry.db`, `.serve-token`, `dashboard-audit.jsonl`); Docker container sets `NOVAFABRIC_HOME=/data/nova` | **experimental** (v0.12.11, 2026-05-14) |
| ~~Cap-6~~ | Lineage injection guard — `POST /capsules/upload` rejects children with a non-existent `parent_run_id` within the 24-hour orphan window; error code `parent_not_found` | **experimental** (v0.12.12, 2026-05-15) |
| ~~Cap-7~~ | Cyclic lineage detection — `CapsuleTreeAssembler` raises `CyclicLineageError` on A→B→A cycles instead of `RecursionError` | **experimental** (v0.12.12, 2026-05-15) |
| ~~Obs-1~~ | Orphan Prometheus counter — `novafabric_orphan_created_total{reason}` optional counter in `OrphanManager`; degrades silently if `prometheus_client` absent | **experimental** (v0.12.12, 2026-05-15) |
| ~~Sec-T1~~ | Security regression suite — 4 tests for Cap-6 (lineage injection), Cap-7 (cycle detection), and ADR-0045 fail-open window semantics; `pyproject.toml` version realigned | **experimental** (v0.12.13, 2026-05-15) |
| ~~Bundle-1~~ | Rebuild `src/novafabric/serve/static/` from v0.12.14 source — fixes lineage node click not updating detail panel in `nova serve`; hooks fix (v0.12.10) was in source but stale bundle still shipped | **experimental** (v0.12.14, 2026-05-15) |
| ~~Seal-1~~ | TSA signature verification — `verify_timestamp()` now validates CMS `SignerInfo.signature` against the embedded TSA certificate; degrade-safe (falls back to hash-only when CMS structure absent or unsupported) | **experimental** (v0.12.15, 2026-05-15) |
| ~~Seal-2~~ | `NovaSeal.seal()` p99 CI gate — 100-round `pytest-benchmark` harness asserts p99 < 200 ms; dedicated `seal-latency-gate` CI job with JSON artifact; `--benchmark-disable` on coverage step | **experimental** (v0.12.16, 2026-05-15) |

---

## Shipped — v0.13 Trust, Approval, and Multi-Run Diff

| Track | Feature | Status |
|---|---|---|
| ~~D-5 / ADR-0058~~ | Maker-checker dual-approval — `nova promote propose/approve`; Ed25519 keypair auto-generated per identity; SoD enforced at crypto level; opt-in `maker_checker_gate.rego`; ADR-0018 amended | **experimental** (v0.13.0, 2026-05-15) |
| ~~DU-10~~ | Shared `<EmptyState>` component — `bordered`/`fill`/`inline` variants; replaces 8 ad-hoc empty states across the dashboard | **experimental** (v0.13.1, 2026-05-15) |
| ~~DC-5 Explain~~ | OPA trace viewer in PolicyTab — "explain" checkbox fires `--explain full --format pretty`; `trace_text` in `PolicyDecision`; collapsible trace panel (max 256 px) in the decision result | **experimental** (v0.13.2, 2026-05-15) |
| ~~C-5 / N-run Diff~~ | N-run diff in dashboard — RunsTab checkbox cap 2→5; DiffTab baseline + N-1 parallel diffs via `runMultiDiff`; stacked collapsible `MultiDiffCard` panels; URL `?run_ids=a,b,c` | **experimental** (v0.13.3, 2026-05-15) |
| ~~LG-dbl~~ | LineageGraph double-click selects node instead of zooming (`zoomOnDoubleClick=false`); test coverage restored to 90% (OpaEngine trace + serve infra/admin tests) | **experimental** (v0.13.4, 2026-05-15) |
| ~~BQ-016~~ | Parent/child capsule security hardening — `POST /v0/capsules` rejects child with non-existent `parent_run_id` (HTTP 409); `CyclicLineageError` on A→B→A; `novafabric_orphan_created_total` counter; all items confirmed pre-existing | **experimental** (confirmed v0.13.5, 2026-05-15) |
| ~~BQ-017a~~ | Envelope version pre-filter in Go collector — `filterInvalidEnvelopeVersions()` drops records with `envelope_version != "1"` before signing; `nova_invalid_envelope_version_total{observed_version}` counter; 8 new Go processor tests | **experimental** (v0.13.5, 2026-05-15) |
| ~~BQ-017b~~ | `ObjectCapsuleStore.get_capsule()` — reads SHA-256 pin from manifest chain, verifies bytes from WORM backend, raises `CapsuleIntegrityError` on mismatch; 3 protocol tests | **experimental** (v0.13.5, 2026-05-15) |
| ~~BQ-017c~~ | `tests/envelope/test_backwards_compat.py` — 7 contract tests: forward-compat (unknown fields tolerated) + version gating (`envelope_version != "1"` rejected) | **experimental** (v0.13.5, 2026-05-15) |

---

## Shipped — Regulated-Industries ADR Formalizations

Goal: formalize the seven regulated-industries design decisions from the `design/`
SoA2Prod study as first-class ADRs in the main series. Status `Proposed` = design frozen,
code not yet implemented unless noted.

| ADR | Title | Status |
|---|---|---|
| ADR-0054 | DSSE Signing Envelope — PAE encoding, ECDSA P-256/SHA-256, `.seal/` layout | **Proposed** (x509 path ships as **experimental** in NovaSeal v0.12; 2026-05-14) |
| ADR-0055 | Dual-Mode Signing Identity — `profile: sigstore` (Fulcio+Rekor) vs `profile: x509` (PKCS#11 HSM / PKCS#12) | **Proposed** (x509 path ships as **experimental**; Sigstore path design-frozen, deferred; 2026-05-14) |
| ADR-0056 | Rules-Based Risk-Tier Classifier — deterministic YAML vocabularies, EU AI Act / NIST RMF / OMB M-24-10 tiers | **planned** (design frozen; `novafabric/governance/` implementation deferred to v0.13+; 2026-05-14) |
| ADR-0057 | Per-Tenant Merkle Log — Trillian-compatible append-only log (SQLite dev / Postgres prod); RFC 6962 tree; signed leaf entries | **Proposed** (SQLite backend ships as **experimental**; Postgres deferred to v0.13+; 2026-05-14) |
| ~~ADR-0058~~ | Maker-Checker Dual-Approval for `nova promote` — `propose`/`approve` sub-commands, Ed25519 keyring, SoD at crypto level, opt-in Rego gate | **experimental** (v0.13.0, 2026-05-15) |

ADR-004 (WormAdapter) and ADR-007 (RFC 3161) were already formalized as ADR-0031 and
ADR-0030 respectively; ADR-001 as ADR-0054.

---

## Planned — Dashboard UI Completeness Phase 2

Goal: every shipped CLI capability and cluster-scale component has a dashboard UI or
a clear, interactive placeholder with CLI guidance. Phase 1 shipped 2026-05-14
(13→35 commands, Lineage query panel, InfraTab, enriched CaptureTab).
Detailed plans: `.claude/plans/dashboard-capability-coverage.md`.

| Track | Feature | Needs |
|---|---|---|
| DD-1 | Parent/child hierarchy tree in Runs tab — `nova run show --with-children` UI; PARENT + WORKER tree, PARTIALLY_COMPLETE badge | New tree-view component; no new API needed (`/api/runs/{id}` already returns parent/child fields) |
| DD-2 | Collector status card in Infra tab — live `nova_*` Prometheus metric scrape; spool lag, signing p99, event/sec gauge | New `/api/infra/collector` endpoint in serve; Prometheus pull from collector sidecar |
| DD-3 | Rollback button in Registry tab — one-click `nova rollback` from asset detail panel; confirm dialog + audit log entry | New `POST /api/assets/{name}/rollback` endpoint; mirrors existing promote route |
| DD-4 | Approval workflow in Registry tab — pending approvals list, role-tagged approve button, approval count badge on promote gate | New `GET /api/assets/{id}/approvals` + `POST /api/assets/{id}/approve` endpoints |
| DD-5 | `nova doctor` health card in Home tab — backend type, key store, OPA binary, schema version shown on connect | Extend `/api/health` to return storage type, key path, OPA availability |
| DD-6 | Lineage time-travel query in Lineage tab — `at` timestamp picker extends QueryPanel; uses existing `nova lineage time-travel` logic | New `GET /api/lineage/time-travel/{ref}?at=` endpoint in serve |
| DD-7 | Object Store browser in Infra tab — manifest chain list, chunk stats, WORM conformance score | New `GET /api/storage/manifest-chain` endpoint; requires Object Capsule Store wired into serve |
| DD-8 | Server admin panel — issue/revoke tokens, assign RBAC roles from dashboard (server mode only) | New `POST /api/admin/tokens`, `POST /api/admin/roles` endpoints; gated on `NOVA_SERVER_MODE=true` |

---

## Planned — v0.13 Live Dashboard + Scale

Goal: dashboard works correctly under sustained write load and shows live experiment
progress. **All items need state-of-the-art engineering research before implementation.**
See `.claude/plans/dashboard-scale-v013.md` (to be written after research).

| Track | Feature | Research needed |
|---|---|---|
| SC-1 | SSE / WebSocket live feed — replace 5-second polling for run status transitions | FastAPI SSE patterns, event schema design, client reconnect strategy |
| SC-2 | Virtual scrolling for RunsTab — handle 100K+ rows without DOM collapse | TanStack Virtual vs AG Grid Community (both MIT); windowing strategy |
| SC-3 | Default time-window on `/api/runs` — no unbounded queries | Index strategy, UI date-range picker |
| SC-4 | Node/rack topology filter — filter by hostname, SLURM job ID, K8s node | Requires SC-3 index work first |
| SC-5 | Pre-aggregated Home tab counts — `COUNT(*)` at 1B rows needs materialized views | Postgres materialized view refresh strategy vs ClickHouse |
| SC-6 | Approximate query mode — HLL cardinality, reservoir sampling, t-digest percentiles | `postgresql-hll` vs ClickHouse native; DDSketch MIT library |

---

## Planned — v1.1 AI Topology Visualization

Goal: after collecting many capsules, show an interactive map of agents, tools, models,
compute nodes, and their communication patterns. Phase 5 + Phase 6 blockers now cleared.
**Needs research:** WebGL rendering strategy, graph layout algorithms, LOD at 100K+ nodes.
See `.claude/plans/topology-visualization-investigation.md`.

| Track | Feature | Research needed |
|---|---|---|
| TV-1 | Cross-capsule aggregation layer — merge N capsules into a topology knowledge graph | Incremental graph construction, deduplication strategy |
| TV-2 | Stable node pseudonym — HMAC-based `node_id` for hostname deduplication across capsules | Privacy model, opt-in design |
| TV-3 | Topology node/edge kinds — `inference_server`, `tool_server`, `compute_node`, `runs_on`, `infers_via`, `calls_tool` | Extend lineage schema |
| TV-4 | 2D interactive graph — Sigma.js v3 (MIT, WebGL, 100K nodes) | Force-directed layout algorithms, LOD strategy |
| TV-5 | 3D optional view — `3d-force-graph` (MIT, Three.js) | Performance at 10K+ nodes, interaction model |
| TV-6 | Edge weight metrics — call frequency, latency p99, cost per edge | Aggregation from OTel spans in capsules |

---

## The Accountability Spine (research-grounded, ADRs 0093–0095)

**Status: ADRs Accepted; all three feature cores shipped (released v0.55.0, experimental).
Follow-up slices implemented (2026-06-19, experimental, branch `feat/spine-followups`, not yet
merged).** Three linked features grounded in the 2026 "accountable autonomy" research corpus
(D3 ex-post evidence is the load-bearing moat). Integrating overview:
`design/architecture/accountability-spine.md`; research→feature provenance:
`design/research/accountability-spine-traceability.md`. None is a third top-level format
(ADR-0034); all are additive/opt-in and reuse the shipped NovaSeal/Evidence machinery.
The follow-up slices add: the energy sampler + measured time-share attribution + Slurm `sacct`
capture + the signed `PREDICATE_ENERGY` bundle attestation (Spine-A); the FRE-902(14)
court-admissibility binding now auto-embedded via `nova export-evidence --with-custody`,
the EU AI Act Annex IV + NIST RMF safety-case renderers (`nova safety-case export --format
annex-iv|nist-rmf`), and the three token-gated read-only serve endpoints
(`/api/runs/{id}/energy|ledger|safety-case`) (Spine-C). The React dashboard panels and the
live n1 `sacct` round-trip remain the only deferred pieces.

| Track | Feature | Research wedge | ADR | Status |
|---|---|---|---|---|
| Spine-A | **Energy-Anchored Action Receipts** — measured-or-declared-unknown per-action joules + provenanced carbon, sealed into Seal/Evidence; honest-degradation default; Slurm `sacct` measured class (`nova energy probe/attest/verify/report`) | D3 × D7 (#1 wedge, no competitor) | ADR-0093 | **experimental** (core + follow-up: `EnergySampler` daemon, measured time-share attribution, Slurm `sacct` capture, signed `PREDICATE_ENERGY` bundle attestation; 80 tests) — *deferred:* live n1 sacct round-trip, React EnergyTab |
| Spine-B1 | **Adversary-Anchored Accountability Ledger** — per-stream sidecar hash-chains + signed multi-stream checkpoint; tamper-evident against a compromised agent *and* a malicious operator (`nova ledger anchor/verify/status`) | D3 × D10 × D1 | ADR-0094 (A half) | **experimental** (core; 65 tests) — *deferred:* live-capture wiring, hub anchoring cadence |
| Spine-B2 | **Deterministic Replay Attestation** — signed `BIT_EXACT`/`BOUNDED_EQUIVALENT`/`NON_DETERMINISTIC` certificate + `replay_attestation.rego` release gate; back-links to the ledger via `ledger_ref` | D3-core (no deterministic agent replay exists) | ADR-0094 (B half) | **experimental** (`replay_attestation.py` + gate; 10 OPA tests) — *deferred:* `nova evidence attest-replay --certify/--anchor` flags |
| Spine-C | **Safety-Case Compiler + Court-Admissible Evidence Binding** — CAE tree from real sealed artifacts (energy receipts + replay attestations compose in as `evidence_kind` leaves at zero schema cost); in-schema honesty / κ + CI backing states (`nova safety-case build/verify/export`) | D4 × D9 × D3 | ADR-0095 | **experimental** (compiler core + follow-up: FRE-902(14) admissibility binding auto-embedded via `nova export-evidence --with-custody`, Annex IV + NIST RMF renderers via `nova safety-case export --format annex-iv|nist-rmf`, three read-only serve endpoints `/api/runs/{id}/energy|ledger|safety-case`; 99+ tests) — *deferred:* React dashboard panels |

---

## Known implementation gaps (2026-05-19 audit)

Comprehensive audit of all 593 nova-design documents vs. v0.20.x. Full report in `.claude/plans/nova-design-gap-audit-2026-05-19.md`. Summary of actionable items:

### Tier A — Correctness ✅ ALL CLOSED (v0.22.0)

| # | Gap | Status |
|---|---|---|
| ~~A-1~~ | Dashboard: live delta events NOT pushed to WS clients — spawn-to-canvas ~10s, spec ≤5s | **fixed** v0.22.0 |
| ~~A-2~~ | Dashboard: TDP delta events sent as JSON text, not binary Arrow IPC | **fixed** v0.22.0 |
| ~~A-3~~ | Collector: no dead-letter queue; events silently dropped on backpressure (OQ-025) | **fixed** v0.22.0 |
| ~~A-4~~ | Postgres v002 partition DDL raises `NotImplementedError`; ADR-0051 still Proposed | **fixed** v0.22.0 |
| ~~A-5~~ | Lineage migration kit reads from Parquet, not ObjectCapsuleStore manifest chain | **fixed** v0.22.0 |
| ~~A-6~~ | `get_capsule()` does not walk prev_commit_hash chain on read (OQ-027) | **fixed** v0.22.0 |
| ~~A-7~~ | Collector NovaSeal signing uses Ed25519; Python NovaSeal uses ECDSA P-256 — no cross-algorithm path | **fixed** v0.22.0 |

### Tier B — Feature completeness ✅ ALL CLOSED (v0.22.0)

| Track | Status |
|---|---|
| ~~TV-5 3D view~~ | **shipped** v0.22.0 — LODController, TimeSlider, tv5Store (Zustand 5), msgpack, TTL retention, Prometheus |
| ~~Capsule KG v1~~ | **shipped** v0.22.0 — AliasTableResolver (asyncpg), ReviewQueue (SQLite), CapsuleEventConsumer (NATS), WAL CRDT |
| ~~Evidence Fabric scale~~ | **shipped** v0.22.0 — ClickHouse AggMergeTree MV, DualObjectStore S3, NATS JetStream, KuzuDB bulk COPY |
| ~~Collector~~ | **shipped** v0.24.0 — `NovaPySpool` (cffi+pure-Python), OCB builder config, 11 tests. NATS-on-Lustre still hardware-gated. |
| ~~Dashboard~~ | **shipped** v0.24.0 — 5 new TS test files; tc-001–tc-010 contracts; AdsValidator, FA2, renderer, TDP client (160 total) |
| ~~Metadata DB~~ | **shipped** v0.24.0 — SQLite 100K row test + 1% checksum; Postgres gate behind `NOVA_INTEGRATION=1` |
| ~~Lineage bench~~ | **shipped** v0.24.0 — real Gremlin queries (insert/provenance/blast_radius/replay_chain); 5 LDBC SNB BI adaptations; JanusGraph Helm chart (`deploy/helm/janusgraph/`). 4h smoke hardware-gated. |
| NovaSeal bypass dispatch | **shipped** v0.24.0 — `BypassNotifier` protocol, File+Webhook+Multi notifiers, env-var config |
| NovaSeal Cloud KMS | **shipped** v0.24.0 — AWS KMS, Azure KV, GCP KMS backends; `[seal-aws/azure/gcp]` optional extras |
| OCS zstd dict | **shipped** v0.24.0 — `ZstdDictRegistry`; `[ocs-compress]` extra; `compression_dict_id` in put/get |

### Tier C — Compliance / standards

Comprehensive research-backed audit completed in v0.25.0. Status per item after audit:

| Item | Status after v0.25.0 audit |
|---|---|
| FDA §11.50 signing intent | **implemented** — `SigningIntent` enum in `envelope.py`, v0.12.15+ |
| OQ-021 four-type edge schema | **resolved** — `schemas/lineage-edge.schema.json` updated v0.25.0 |
| RFC 3161 TSA signature verification | **partial** — signature check done; trust chain + CRL pending ADR-0070 |
| cap-001 PIIDetectionGate | **partial** — LEGAL-HOLD DRAFT; crypto-shredding strategy designed in ADR-0069 |
| Sigstore Rekor push | **partial** — Rekor integration exists; keyless signing pending ADR-0071 |
| HIPAA Safe Harbor cryptographic proof | **shipped v0.44.0** — `nova export-hipaa-proof`; all 18 Safe Harbor identifier categories; SHA-256 `proof_digest`; mandatory legal disclaimer |
| RO-Crate v1.1 FAIR export | **shipped v0.32.0** — `nova export-rocrate <capsule_dir>` |
| W3C PROV-JSON lineage export | **shipped v0.32.0** — `nova lineage export-prov <capsule_dir>` |
| AIBOM / CycloneDX ML-BOM v1.7 (cap-008) | **works today** — shipped v0.39.0; ADR-0073 Accepted; CRA deadline 2026-09-11 |
| C2PA content credentials | **shipped v0.33.0** — `nova export-c2pa <capsule_dir>` (TSP signing deferred) |
| EU AI Act Art.12 compliance mode | **shipped v0.33.0** — `nova euaiact export` + `nova euaiact status` |
| Post-quantum cryptography migration | **future design** — ADR-0072 accepted; migration must begin 2029 |
| W3C DID/VC agent identity | **future design** — ADR-0075 accepted; EU binding 2031–2033 |
| Multi-region log sovereignty | **future design** — ADR-0077 accepted; v1.x |
| X.509 CA chain validation | **future work** — planned v0.26.x |
| cap-006/007 (NIS2 Phase 2+3, GDPR RoPA) | **future work** — cap-005 (Phase 1) shipped; later phases planned |

### v1.0 trigger criteria status

| Criterion | Status |
|---|---|
| ≥ 3 design partner sign-offs | 1/3 |
| OAS v1.0 `open-agent-spec-v1.md` + `oas-extensions-registry.md` | Not started |
| `nova migrate` v0.x → v1.0 capsule conversion | Not implemented |
| LF AI & Data Sandbox application | Draft only; no sponsor yet |
| OpenSSF Scorecard ≥ 4 | Not configured |

---

## Storage scaling — action items (planned v1.x)

These are concrete engineering tasks that unblock production deployments at > 10K capsules.
None block v1.0; all are prerequisites for v1.1+ cluster-scale use.

| ID | Action item | Unblocks | Acceptance criteria |
|---|---|---|---|
| **Scale-S1** | ✅ **Shipped v0.32.0** — `runs_cache` SQLite table in `registry.db`; `build_runs_index()` populates on startup + 2s incremental refresh; `/api/runs`, `/api/runs/search`, `/api/stats` all query the index; disk-scan fallback when cache is empty. 17 unit tests (`tests/test_runs_cache.py`). | `/api/runs` stays < 200 ms at 100K capsules | `GET /api/runs?limit=50` p99 < 200 ms with 100K capsule directories on disk; benchmark in `tests/bench/` |
| **Scale-S2** | ✅ `v002_partition_ddl.py` Postgres partition DDL for MetadataStore — `_create_partitioned_runs()` implemented with `upgrade()`/`downgrade()` | 10K tenant × 1M run benchmark; ADR-0051 Accepted | `nova db upgrade` runs cleanly on Postgres; `tests/metadata_store/test_partition_ddl.py` green |
| **Scale-S3** | ✅ **Shipped v0.36.0** — `CapsuleWatcher` with pluggable `PollingBackend` (default) and `WatchdogBackend` (inotify/FSEvents; `pip install novafabric[watch]`); `nova ingest-capsule` CLI (single / `--all` / `--watch` / background in `nova serve`); `NOVA_WATCHER_BACKEND` + `NOVA_WATCHER_INTERVAL` env vars; `nova serve` delegates startup indexing to `CapsuleWatcher`. | Scale-S1 | `nova ingest-capsule <run_id>` indexes one capsule; background watcher indexes new capsules within 3 s SLA; `WatchdogBackend` SLA test in `tests/test_capsule_watcher.py` |
| **Scale-S4** | ✅ **Shipped v0.38.0** — `PostgresMerkleLog` (psycopg3); `open_merkle_log()` URI-dispatch factory; sampled `verify_consistency()` fast path; `[seal-postgres]` extra; CLI `--db` default now honors `NOVAFABRIC_SEAL_DB_PATH` at invocation time. | NovaSeal at > 1M log entries | `nova seal log verify` p99 < 200 ms at 1M log entries on Postgres; `NOVAFABRIC_SEAL_DB_PATH=postgresql://...` selects backend |

See [Storage Architecture](design/architecture/cluster-scale.md#production-storage-stack-polyglot-persistence) and [Dashboard Scale Characteristics](docs/dashboard.md#scale-characteristics-and-known-limits) for the full technical context.

---

## Future design — v1.x and beyond

These are design intent only. No implementation scheduled.

| Feature | Notes |
|---|---|
| Federation — multi-cluster evidence fabric | ADR-0021 §Federation |
| Distributed identity — SPIFFE/SPIRE per-node identity | ADR-0035 |
| AIBOM (AI Bill of Materials) | OAS v1.x extension |
| SLSA-for-AI attestations | OAS v1.x extension |
| eval-result schema freeze | After v1.0 Adopted status |
| Compliance bundles (SOC 2 Type II, ISO 42001) | v1.x post-certification |
| Redis / Kafka managed cloud hosting | Operator concern; NovaFabric is deploy-target agnostic |
| Dashboard at planetary scale (1M workers / 100K nodes) | ClickHouse OLAP, graph sharding (JanusGraph/NebulaGraph), distributed evidence store (Ceph RGW) — see `.claude/memory/project_dashboard_planetary_scale_investigation.md` |
| Agent lifecycle state machine (hot/warm/cold states, checkpoint_sequence) | `architecture/cluster-scale.md` in `design/` |
| Cache architecture L1–L4 (node-local, Valkey distributed, cold archive) | `architecture/cluster-scale.md` in `design/` |
| Cell-based fabric (cell scheduler, placement policy, nova rollout) | `architecture/cluster-scale.md` in `design/` |
| Human impact ledger (`human_impact` capsule field, sampling policy) | `architecture/governance.md` in `design/` |
| Jurisdiction policy (`jurisdiction` metadata, export_allowed enforcement) | `architecture/governance.md` in `design/` |

---

## Out of scope (forever)

See `design/strategy/non-goals.md` for the full list. Key exclusions:
- NovaFabric does not train, fine-tune, or serve models.
- NovaFabric does not replace your observability stack (Grafana, Prometheus, Jaeger).
- NovaFabric does not manage secrets at rest (use Vault, AWS Secrets Manager, etc.).
- NovaFabric does not run your CI/CD pipeline.
