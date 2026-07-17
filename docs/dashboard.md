# Local Dashboard (`nova serve`) — User Reference

**Status:** Experimental. Read this entire page before relying on the dashboard for anything that matters.
**Per:** [ADR-0027](../design/adr/0027-nova-serve-experimental-dashboard.md), [Non-goals](../design/strategy/non-goals.md)
**First shipped:** v0.7

The dashboard is an **opt-in local-only HTTP server** (`nova serve --experimental`) that renders an interactive view over the same registry SQLite, lineage SQLite, and capsule directories the CLI reads. The CLI is the canonical interface; the dashboard is a satellite that surfaces the equivalent CLI command for every action.

---

## Dashboard tabs (complete inventory)

The dashboard ships **26 tabs**, grouped by workflow. (Source of truth:
`web/src/components/dashboard/Sidebar.tsx`.)

| Tab | Group | Since | What it does |
|---|---|---|---|
| Home | Overview | v0.8 | Journey cards, status bar, resume session |
| Analytics | Overview | Unreleased | Time-bucketed run analytics from the runs index: volume + failure stacked bars, duration p50/p95 lines, stat tiles, 7/30/90-day ranges, chart/table toggle (`/api/analytics/summary`) |
| Runs | Debug & investigate | v0.7 | Run list, search/filter, inspect capsule, validate, replay, verify; capsule tree, run lineage edges, secret scan (v0.46.0) |
| Diff | Debug & investigate | v0.7 | N-run comparison (2–5), word-level diff, mutation badges |
| Registry | Govern & promote | v0.7 | Asset lifecycle: eval, promote, rollback, register, suggest-register, unregister |
| Governance | Govern & promote | v0.16.0 | Classify (EU AI Act/NIST/OMB), audit (6 profiles), export-examiner, policy sign |
| Eval | Govern & promote | Unreleased | Eval suites (`eval list`), run a suite (`eval run`), regression compare (`eval compare`) |
| Risk | Govern & promote | Unreleased | OWASP LLM assurance (`assure`), secret scan, failure attribution (`diagnose`), risk-tier classify, MCP scan |
| Lineage | Audit & verify | v0.7 | Provenance / blast-radius / replay-chain DAG, interactive query, OpenLineage/PROV export |
| KG | Audit & verify | v0.17.0 | Capsule knowledge graph: query, audit, entity queue, alias mgmt, ingest |
| Cost | Audit & verify | v0.17.0 | Cost report, pricing, burn analysis (ClickHouse-backed) |
| Schema | Audit & verify | v0.17.0 | Schema registry (JSON Schema + proto3) |
| Evidence | Audit & verify | v0.9 | Bundle list, DSSE/TSR/Merkle verification, download, in-browser ed25519 verify |
| Audit | Audit & verify | v0.7 | Dashboard mutation audit log, action-type filter |
| Holds | Audit & verify | v0.11 | Place/release legal holds, sidebar badge count |
| Policy | Audit & verify | v0.11 | Interactive OPA/Rego check, ALLOW/DENY badge, explain trace |
| Seal | Audit & verify | v0.13.0 | NovaSeal verify, Sigstore sign/verify, linked-envelope proposals, bypass |
| Compliance | Audit & verify | v0.15.2 | GDPR RoPA, AI-SBOM, NIST RMF, CRA coverage, erasure, audit bundle/verify (8+ panels) |
| Incidents | Audit & verify | Unreleased | EU AI Act Art. 73 incident records with a live reporting-deadline clock; open/list/transition/export (AIM, NIS2) |
| Capture | Audit & verify | v0.7 | Capture matrix (4 runners + distributed + MCP/API proxies), adapters, recent capsules |
| Infra | Infrastructure | v0.12.6 | Phase 0–6 component status cards (NovaSeal, Collector, OCS, MetaDB, Lineage, …) |
| Storage | Infrastructure | Unreleased | WORM object-store stats/validate/inspect, manifest chain, collector status, gated DB upgrade + rebuild-metadata-db, system-card export |
| Ops | Infrastructure | Unreleased | Installation diagnostics (`doctor`), warm-daemon liveness (read-only), JWKS flush |
| Admin | Admin | v0.7 | Token management, role management (API v0.14.3; UI partial), JWKS flush, new-run-id, DB ops |
| Commands | CLI | v0.8 | Full-CLI command builders (every `nova` command, 227 today) with live preview, filter box, and copy |
| Reports | Reports | v0.17.0+ | 9 report templates (see below) |

**Reports tab — 9 templates:** run-history, eval-regression, capsule-compare, cost-burn,
throughput, evidence-inventory, policy-audit, seal-verification, release-comparison.

**Compliance tab panels:** GDPR Art.30 RoPA, AI-SBOM (CycloneDX ML-BOM), NIST AI RMF, AI-SBOM
coverage/CRA deadline, GDPR erasure status, audit coverage/bundle/verify, C2PA, RO-Crate, EU AI
Act Art.12.

**Governance tab panels:** risk classification, audit-profile coverage, examiner export, policy
signing, regulatory vocabularies + manual classification + eval suites/run (v0.46.0).

**v0.46.0 parity additions (12 panels across 7 tabs):** eval suites list + eval run
(Governance), policy inventory + policy sign (Policy), regulatory vocabularies + manual
classification (Governance), generate AI-SBOM (Compliance), reindex capsules (Admin),
distributed capsule tree + run lineage edges + secret scan with `--fail-on` gate (Runs),
lineage-store deployment profile (Infra).

**Unreleased coverage additions:** 5 new tabs (Eval, Risk, Storage, Incidents, Ops) plus
**Seal → Ratchet** (`seal ratchet init/rotate/status`) and **Evidence → Assertions**
(`evidence completeness/bind`) panels, surfacing ~30 previously CLI-only commands. Destructive /
process-control actions (DB rebuild, ratchet rotate) are confirmation-gated and daemon control is
read-only over HTTP ("safe mutations only"). New cross-cutting UX: app-wide toast notifications, a
unified action/confirm pattern, a virtualized data table, per-tab help, deep-linkable tabs
(`?tab=`), and global entity search (runs/assets/incidents) in the ⌘K palette.

> The dashboard server exposes **170+ REST endpoints** — the full machine list is in
> [`api-reference.md`](api-reference.md) (and `GET /openapi.json` at runtime).

---

## Install + run

```bash
# 1. Install the optional [serve] extra (FastAPI + uvicorn, both Tier-A licenses)
pip install 'novafabric[serve]'

# 2. Start the server (--experimental is mandatory)
nova serve --experimental

# 3. Open the URL the panel prints (it embeds a one-shot session token).
# Or paste the token manually if you prefer to type the URL.
```

Notable flags:

| Flag | Default | Purpose |
|---|---|---|
| `--experimental` | (required) | Acknowledges the experimental gate. |
| `--port` | `4321` | TCP port. |
| `--host` | `127.0.0.1` | Bind address. Refuses non-localhost without `--insecure`. |
| `--capsule-dir` | `./.novafabric/runs/` | Where to look for capsules. |
| `--db-path` | `~/.novafabric/registry.db` | Registry/lineage SQLite path. |
| `--no-browser` | off | Don't auto-open a browser tab. |
| `--topology` | off | Enable the live topology dashboard (`/topology/`). |
| `--tv5` | off | Enable the experimental 3D topology view (implied by `--topology`). |
| `--topology-louvain-resolution` | `1.0` (or `NOVA_TOPOLOGY_LOUVAIN_RESOLUTION`) | Louvain clustering resolution. Lower values merge into fewer/larger clusters; higher values split further. |

Stop with `Ctrl+C`. The session token is rotated on every start; the previous token is invalidated.

### Topology view modes

The topology dashboard (`/topology/`) offers a **view-mode switcher** over five
complementary lenses on the same data — pick the right one for the task:

| Mode | What it shows | Best for |
|---|---|---|
| **Cluster** (default) | 2D force layout of cluster super-nodes, sized by agent count; click a cluster to expand its agents | Structure overview + drill-in |
| **Call-graph** | 2D layered (dagre) layout following the directed run → model → tool flow | Reading call direction/hierarchy |
| **Treemap** | Rectangles with area proportional to agent count | Spotting the biggest clusters at a glance |
| **Table** | Sortable, searchable exact list of clusters | Precise lookup; the anti-hairball |
| **3D (experimental)** | Three.js orbit view (TV-5); labels appear on hover/selection | Spatial exploration (not default) |

Graph modes provide on-screen **+ / − / Fit** zoom controls. Labels are
decluttered — only large, hovered, or selected nodes are labeled — so the
full-graph view stays legible at scale. Clusters whose only member is a single
agent (e.g. runs with no captured model calls) are collapsed into one "misc"
super-node rather than scattered as visual noise.

---

## Capability matrix: dashboard vs CLI

The dashboard exposes the **majority** of the read-side and many of the write-side capabilities of the CLI, but a number of operations are intentionally CLI-only. Every dashboard action surfaces its equivalent `nova` command in the confirm dialog.

### Read operations (Layer A — fully covered)

| Capability | CLI | Dashboard | Notes |
|---|---|---|---|
| List capsules under `.novafabric/runs/` | `nova` *(no direct command — discovers them)* | ✅ Runs tab | Search by run_id or command, filter by status, sort by time/duration, opt-in 5s auto-refresh. **v0.12.9 (DU-1):** status filter pill-bar (All / running / success / failure / error) above the table. **v0.12.9 (DU-2):** hover-reveal copy icon on each run ID cell. |
| Inspect a single capsule | `nova inspect <run-id>` | ✅ Runs tab → click row | File-tree + YAML pane + expandable JSONL rows + in-browser Ajv validate. |
| Validate a capsule against its schema | `nova validate <capsule>` | ✅ Capsule view → "Validate" button | Same `run-capsule.schema.json` compiled in-browser via Ajv. |
| List registered assets | `nova list [--type] [--status]` | ✅ Registry tab | Filters not yet exposed in UI; visible via the live API. |
| Get a single asset | `nova inspect <name@version>` | ✅ Registry tab → click row | Eval results for the selected asset are lazy-fetched. |
| Lineage queries | `nova lineage provenance / blast-radius / replay-chain` | ✅ Lineage tab | Full DAG render via React Flow + dagre. Click (or double-click) a node to see incoming/outgoing edges. **v0.12.6:** interactive QueryPanel — type a ref, pick mode (provenance / blast-radius / replay-chain), hit Run; results appear as a sortable table; CLI equivalent preview updates live. **v0.12.7:** ref input autocompletes `name@version` (provenance/blast-radius) or `run_id` (replay-chain) from loaded data. **v0.12.9 (DU-3):** when a node is selected, a breadcrumb row above the graph shows the full ancestry chain (root → … → parent → **selected**); each ancestor is clickable. **v0.13.4:** `zoomOnDoubleClick` disabled; double-clicking a node selects it (identical to single-click) instead of triggering zoom-to-fit. |
| Cluster-scale infrastructure status | *(no single command — consult each Phase 0–6 component separately)* | ✅ Infra tab (v0.12.6) | 10 component cards: NovaSeal, Collector, Object Store, Metadata DB, Lineage at Scale, Parent/Child Capsule, Server Mode, Eval Suites, Policy Gates, Run Capsule — each with a `shipped / partial / placeholder / planned` status badge plus the relevant CLI commands. Lets operators confirm which Phase 0–6 components are active without leaving the dashboard. |
| CLI command reference with live preview | *(all `nova` sub-commands)* | ✅ Commands tab | **v0.8:** 13 command builders for core capture, registry, and replay. **v0.12.6:** expanded to 35 hand-curated builders across 4 journey tracks. **Unreleased:** now mirrors the **complete** CLI — every `nova` command (227 today) has a fillable form. Hand-curated builders still provide richer copy for the most-used commands; the rest are auto-generated from the live Typer app (`web/scripts/gen-command-registry.py` → `generatedCommands.ts`), so the tab stays in sync as commands are added/removed (guarded by `tests/serve/test_command_registry_coverage.py`). Fields, choices, defaults and flags come from the CLI's own parameter metadata. Each builder renders a live command preview with a copy button; a filter box and per-group counts navigate the full surface. Copy-only (Layer C, ADR-0027) — the dashboard never executes commands. |
| Structural diff | `nova diff <run-a> <run-b>` | ✅ Diff tab | Aligned with `diff-report.schema.json`; renders environment / model_calls / tool_calls / outputs sections. **v0.9 additions:** 'Compare against…' in CapsuleInspector, word-level diff for model responses, mutation badge on tool call pairs. **v0.12 addition (C-4):** Multi-select checkboxes in RunsTab — check any 2 rows, click "Compare selected ⊕", jumps directly to Diff tab with both IDs pre-filled and diff auto-triggered; no copy-paste. See [ADR-0036](../design/adr/0036-cross-run-comparison-ux.md). **v0.12.7:** both run A and run B inputs autocomplete from the loaded runs list. **v0.12.9 (DU-4):** last comparison (from/to/result) persisted in `sessionStorage` — navigating away and back restores the previous comparison without re-fetching. **v0.13.3 (C-5):** Checkbox cap lifted 2→5; selecting 3–5 runs triggers N-run mode — first run is the baseline, N-1 parallel diffs fire via `runMultiDiff`, results shown as stacked collapsible `MultiDiffCard` panels with change-count badges. URL format migrated from `?run_a=&run_b=` to `?run_ids=a,b,c` (old params still parsed for backward compat). |
| Read evidence bundle | *(no read-only command — open the ZIP)* | ✅ Evidence tab (v0.9) | Lists all bundles from `~/.novafabric/evidence/`, shows DSSE statement, in-browser ed25519 verify via SubtleCrypto, ZIP download. See [ADR-0037](../design/adr/0037-evidence-tab-native-dashboard.md). |
| Verify evidence bundle cryptographic integrity | `nova verify <capsule>` (NovaSeal) | ✅ Evidence tab → Verify button (v0.11) | `POST /api/evidence/{id}/verify` — Ed25519 DSSE signature + RFC 3161 TSR + NovaSeal Merkle log inclusion. Inline `sig ✓`, `tsr ✓`, `log –` badges. `log –` shown when NovaSeal is not configured locally. |
| Validate a capsule's schema and file structure | `nova validate <capsule>` | ✅ Runs tab → Validate button (v0.11) | `POST /api/runs/{id}/validate` — checks required files, YAML parse, JSONL presence. Green `✓ valid` or red expandable error list. |
| View secret scan results | `nova scan-secrets <capsule>` | ✅ Runs tab → **Secrets** button on card (quick-access, v0.11) or → Secrets tab in detail panel | `GET /api/runs/{id}/redaction-proof` — shows scanner+packs, targets (hash before/after), per-finding severity badges. Includes re-scan trigger. |
| Report generation | `nova report [--format markdown\|json]` | ❌ — | Markdown / JSON report generation is CLI-only. Use `nova report` and pipe to your tooling. |
| Lineage time-travel queries | `nova lineage time-travel <ref> --asof <ts>` | ❌ — | Not yet wired into the dashboard; CLI-only in v0.7. |
| Lineage OpenLineage emission | `nova lineage emit-openlineage` | ❌ — | Integration target; CLI-only. |

### Write operations (Layer B — confirm-gated, audit-logged)

| Capability | CLI | Dashboard | Notes |
|---|---|---|---|
| Register an asset | `nova register <spec.yaml>` | ✅ Registry tab → "+ Register asset" | Paste YAML into the dialog; spec validated by the same `validate_spec` the CLI uses. |
| Run eval suites | `nova eval <name@version>` | ✅ Registry tab → row "EVAL" button | Runs the same `run_evals` entry point as the CLI. **v0.9 addition:** Eval history sparkline per asset (last 10 results, green/red bars) and full eval history panel in the eval dialog. See [ADR-0038](../design/adr/0038-eval-trend-dashboard.md). |
| Promote an asset | `nova promote direct <name@version> --to <status> [--force]` | ✅ Registry tab → row "PROMOTE →" button | Eval gate enforced for agents (use `--force` only with intent). Force is visually marked as destructive. **v0.12.9:** invalid target buttons disabled client-side (e.g. `archived → production` is dimmed); inline error banner shown on server rejection; eval-gate copy conditional on `asset_type === 'agent'`. **v0.13.0 (D-5):** `nova promote` is now a sub-group (`direct` / `propose` / `approve`). The dashboard Promote button maps to `direct`. For maker-checker SoD flow, use `nova promote propose` + `nova promote approve` from the CLI. |
| Bulk-promote multiple assets | *(run `nova promote` for each)* | ✅ Registry tab → checkbox column + floating action bar (v0.12.9) | Select one or more assets, click "Promote N" in the floating bar; each is promoted to its next valid status in sequence. Select-all master checkbox in header. Floating bar appears only when ≥ 1 asset is checked. |
| Forensic replay | `nova replay <capsule> --mode forensic` | ✅ Runs tab → row "REPLAY" button | Read-only inspection, no subprocess, safe. |
| Semantic replay analysis | `nova replay <capsule> --mode semantic` | ✅ Runs tab → row "SEMANTIC" button (v0.11) | Compute pairwise text similarity across model call responses. Returns `similarity_score` (0–100%) and a gauge. Read-only, no subprocess. |
| Exact replay eligibility | `nova replay <capsule> --mode exact` | ✅ Runs tab → row "EXACT" button (v0.11) | Check whether the capsule satisfies exact replay requirements: `env.lock.lock_mode=deterministic` and `seed` present on every model call. Returns `exact_eligible` (bool) + blocker list. |
| Re-scan & redact | `nova redact <capsule>` | ✅ Runs tab → row "REDACT" button | Re-runs `SecretScannerV0` and overwrites `redaction-proof.json`. **Limitation:** strategy overrides and unsafe-skip marking (`--strategy-override`, `--mark-unsafe-skip`, `--rationale`) are CLI-only. |
| Compare two asset spec versions | `nova diff <name@v1> <name@v2>` | ✅ Registry tab → "Compare…" (v0.11) | `GET /api/assets/{name}/diff?from_version=&to_version=` — flattened field diff; green `+`, red `−`, yellow `~` table rows. Available on assets with ≥ 2 registered versions. |
| Export signed evidence | `nova export-evidence <capsule> --output <zip> --key <pem>` | ✅ Runs tab → row "EXPORT ↗" button | Signing key auto-generated at `~/.novafabric/keys/local-key.pem` if missing (audit-logged). Output defaults to `~/.novafabric/evidence/<run_id>.zip`. **Limitation:** custom output paths and `--allow-unsafe-skips` are wired in the API but not surfaced as form controls in the UI yet. |
| Place / release a legal hold | `nova hold create <registry> --reason … [--duration-days N]` / `nova hold release <hold-id>` | ✅ Holds tab (v0.11) | `POST /api/holds` + `POST /api/holds/{id}/release`. Active holds grouped by registry; inline release button; place-hold form with optional duration. **v0.12.7:** registry name input autocompletes from the list of registries shown on-screen. |
| List active legal holds | `nova hold list <registry>` | ✅ Holds tab (v0.11) | `GET /api/holds` — discovers all registries under `.novafabric/registries/`; sidebar badge shows active count. |
| Interactive policy check | `nova policy check` | ✅ Policy tab (v0.11) | `POST /api/policy/check` — evaluates a `PolicyInput` (action, subject, resource) via the OPA engine and returns a `PolicyDecision`; large ALLOW/DENY badge + reason + metadata; yellow warning when OPA is not installed. **v0.12.7:** resource ref autocompletes `name@version` when kind = asset, or `run_id` when kind = capsule/replay; loads at mount from `GET /api/assets` and `GET /api/runs`. **v0.12.9 (DU-9):** Rego source textarea with 300 ms debounced client-side syntax check (missing `package`, unbalanced braces); advisory warning banner, does not block submission. **v0.13.2 (DC-5 Explain):** "explain" checkbox triggers `POST /api/policy/check` with `explain: true`; backend runs a second OPA subprocess with `--explain full --format pretty`; `trace_text` returned in `PolicyDecision`; decision result shows "show trace ↓" toggle that reveals a `max-h-64` scrollable monospace trace panel. |
| Export GDPR Art.30 RoPA entry | `nova export-ropa <capsule> --output ropa.json` | ✅ Compliance tab → **GDPR Art.30 RoPA Export** (v0.37.0) | `POST /api/compliance/export/ropa` — optional `controller_name` / `controller_contact`; completeness badge + missing-fields list + JSON document display. |
| Export AI-SBOM (CycloneDX 1.7) | `nova export-aibom <capsule> [--output aibom.json]` | ✅ Compliance tab → **AI-SBOM Export** (v0.37.0) | `POST /api/compliance/export/aibom` — component list (type/name/version/description), `serial_number`, `bom_format`. CycloneDX ML-BOM 1.7 (ECMA-424 2nd Edition) since v0.39.0. |
| Export NIST AI RMF report | `nova export-nist-rmf <capsule> --output report.json` | ✅ Compliance tab → **NIST AI RMF Report** (v0.37.0) | `POST /api/compliance/export/nist-rmf` — GOVERN/MAP/MEASURE/MANAGE score bars, risk-level badge, missing-evidence list. |
| Check AIBOM coverage (CRA) | `nova aibom status [--capsules-dir <dir>]` | ✅ Compliance tab → **AI-SBOM Coverage Status** (v0.37.0) | `GET /api/aibom/status` — auto-loads on mount; total/covered/missing capsule counts, coverage progress bar, CRA deadline 2026-09-11. |
| Mocked replay (re-execute agent code, cache hits) | `nova replay <capsule> --mode mocked` | ❌ — | Mocked replay spawns the agent subprocess. That straddles Layer B/C; in v0.7 it is **CLI-only** until the sandbox model lands per ADR-0027. |
| Scan secrets | `nova scan-secrets <capsule>` | ❌ — | Use `redact` from the dashboard, or `nova scan-secrets` from the terminal. |

### Capture and orchestration (Layer C — deferred)

| Capability | CLI | Dashboard | Notes |
|---|---|---|---|
| Capture a command | `nova capture <command>` | ❌ Deferred to v0.9 | Per ADR-0027 §1 Layer C, launching arbitrary subprocesses from a browser endpoint requires a sandboxed exec model, per-action audit log entries with full command text, and a confirmation flow that shows the command verbatim. None of those primitives are committed yet. **Layer C may not ship at all** if the security review concludes the risk outweighs the value. The Capture tab in the dashboard documents this and lists the four CLI alternatives (`nova capture`, `--runner docker`, `nova mcp-proxy`, `nova api-proxy`). |
| MCP proxy | `nova mcp-proxy <upstream>` | ❌ — | Long-running stdio bridge; CLI-only. |
| LLM API proxy | `nova api-proxy --upstream-url <url>` | ❌ — | Long-running HTTP server; CLI-only. |
| Capture with custom runner | `nova capture --runner {docker,kubernetes,slurm} ...` | ❌ — | Same Layer C reasoning. |

### Operational / advanced (CLI-only by design)

These are intentionally not in the dashboard because the CLI is the right surface for them:

| Capability | CLI | Why not in the dashboard |
|---|---|---|
| Schema validation of arbitrary files | `nova validate <spec.yaml>` (asset specs) | Use the dashboard's Register dialog for assets; pipe through `nova validate` for batch jobs. |
| Asset diff between two versions | `nova diff <name@v1> <name@v2>` | ✅ Registry tab → Compare (v0.11) — moved to the [Write operations](#write-operations-layer-b--confirm-gated-audit-logged) table above. |
| Streaming real-time output during a long-running job | (the capture command itself) | Dashboards aren't the right surface for tailing a single subprocess; use `nova capture` directly. |
| CI / non-interactive scripted runs | All commands | Dashboards require a browser; CI uses the CLI. |
| Cluster runners (Docker / K8s / Slurm) | `nova capture --runner ...` | Driven from the orchestration host, not from a browser tab. |

---

## What the dashboard does that the CLI cannot

A short list, for completeness — the dashboard is not strictly a subset:

- **Live filtered runs view.** Search across `run_id` and command, filter by status, sort by time or duration. The CLI prints a single shell-friendly stream; the dashboard makes 200+ capsules navigable.
- **Interactive lineage DAG.** React Flow + dagre layout, three highlight modes (provenance / blast-radius / replay-chain) without re-querying. CLI returns lineage as JSON; the dashboard visualises the graph.
- **In-browser schema validation.** The Validate button compiles `run-capsule.schema.json` via Ajv in the browser and reports timing. Useful as a sanity check before sharing a capsule.
- **In-browser ed25519 verification.** Evidence-bundle Verify button uses `SubtleCrypto.verify('Ed25519', …)` against the bundled public key. Tamper toggle flips a byte and the verification fails — visible cryptographic evidence.
- **Server-side bundle verification (v0.11).** `POST /api/evidence/{id}/verify` runs a full three-stage check — Ed25519 DSSE signature, RFC 3161 TSR, and NovaSeal Merkle log inclusion — and returns structured per-check results. The in-browser check is convenience-only; the server-side check is authoritative.
- **Single-pane audit log.** Every dashboard mutation appends to `~/.novafabric/dashboard-audit.jsonl`; the Audit tab shows recent entries newest-first with the equivalent CLI command. The CLI does not have an equivalent unified audit log for its own invocations (each command logs its own outputs). **v0.12.9 (DU-6):** action-type filter dropdown — derived dynamically from loaded entries — narrows the list to a single action type (promote / eval / rollback / approve / register / …).
- **Context-aware autocomplete on all ref inputs (v0.12.7).** Every text input that accepts a database reference — lineage query ref, diff run A/B, holds registry name, policy resource ref — shows a live-filtered dropdown populated from data already on the page. Focus the field to browse the top eight items; type to filter. The CLI has shell-level completion (Tab) but no live database-backed suggestions.
- **Staleness indicator on Home tab resume cards (v0.12.9, DU-7).** Resume cards older than 24 hours show an amber border and a "Last updated X — may be stale" tooltip. Useful for detecting runs that started but never completed.
- **Open capsule folder from Capture tab (v0.12.9, DU-8).** The Capture tab's "Recent capsules" panel lists the 5 most recent runs; when `capsule_path` is present, an **Open folder** link opens the local directory in the system file manager via the `file://` protocol.

---

## Security model

- **Localhost only** by default. `--host 0.0.0.0` is rejected without `--insecure`. Even with `--insecure`, put TLS in front of it; the dashboard does not terminate TLS.
- **Token authentication.** A one-shot, cryptographically random URL-safe token (`secrets.token_urlsafe(32)`) is generated at start-up, written to `~/.novafabric/.serve-token` (mode 0600), and rotated on every restart. The token must be passed as `?token=…` on every `/api/*` request. `Bearer` headers are not accepted (so a stolen token can't be sent silently from a misconfigured tab).
- **DNS-rebinding defence.** `Host` header must be `127.0.0.1`, `localhost`, or `::1`. Other hosts return 403 even with a valid token.
- **CORS allowlist.** Only `http://localhost:*` and `http://127.0.0.1:*` origins are accepted. Defence in depth — the token is the authoritative gate.
- **Constant-time token compare.** `_consteq` compares bytes equally regardless of where the mismatch is.
- **No persistent server-side state of its own.** Stop the server and the only thing left behind is the audit log JSONL and any artifacts (evidence bundles, redaction proofs) the user explicitly created. The dashboard owns no separate database.

---

## Audit log

Every Layer B mutation appends a JSONL record to `~/.novafabric/dashboard-audit.jsonl` (override with `NOVAFABRIC_DASHBOARD_AUDIT_FILE`). Each record has:

- `audit_id` — UUID
- `ts` — ISO 8601 timestamp
- `action` — e.g. `register_asset`, `eval_asset`, `promote_asset`, `forensic_replay`, `redact`, `export_evidence`
- `args` — the action-specific arguments (no secrets — the YAML body of `register_asset` is recorded only as a length, not the content)
- `cli_equivalent` — the `nova …` command a human could re-run
- `actor_token_fp` — first 8 chars of the session token (NOT the full token)
- `result` — `ok` or `error`
- `error` — present only on failures
- `extra` — action-specific metadata (e.g. `asset_id`, `replay_id`, `size_bytes`, `key_autogenerated_at`)

The file is append-only with mode 0600. To read it: `cat ~/.novafabric/dashboard-audit.jsonl | jq` or use the dashboard's **Audit** tab.

---

## Trace viewer (v0.7.1)

The **Trace** view (click a run row → "Trace" button) shows three sub-tabs:

### Waterfall tab

Renders the `trace.jsonl` span tree as an indented Gantt chart.

- **Span tree** — indented rows; collapse/expand branches by clicking the arrow.
- **Agent label column** — shows the `nova.agent.id` of the span's owning agent
  (or the nearest non-root ancestor name when the attribute is absent).
- **Outcome coloring** — bar colour encodes outcome: green = success/ok, red = any
  error status. Span type is readable from the span name, not the bar colour.
- **Wall-clock concurrency** — each span's bar starts at its true wall-clock offset;
  concurrent spans overlap visually as they did at runtime.
- **Timeline ruler** — 6-tick ruler across the full run duration.

### Thread tab

Chronological sequence of every model call and tool call, styled as a conversation.

- **Agent badge** — coloured pill showing which agent issued the call. When
  `nova.agent.id` is absent the dashboard infers the agent name from the model
  call's system prompt (regex: `"You are the X"` → `x`; see ADR-0035).
- **Agent group dividers** — a coloured separator appears whenever the active agent
  changes, giving visual phase-boundary annotations without requiring `nova.phase`.
- **Outcome avatar border** — the avatar circle border is green for success, red
  for any error status.
- **LLM call detail** — model name, system (openai / anthropic / ollama / …),
  separate input↑ / output↓ token counts, cache-hit indicator, finish reason badge
  (stop / tool_calls / length / …).
- **Expandable rows** — click any event to expand its full arguments (tool) or last
  message + completion text (model). Collapsed: shows first 200 chars inline.
- **Timestamp** — ISO wall-clock time shown above each event (HH:MM:SS.mmm).

### Swimlane tab

Gantt chart with **one horizontal lane per agent** — designed to make parallel
evidence-gathering phases visible at a glance.

- **Lane per agent** — inferred or explicit via `nova.agent.id`.
- **Bar per call** — model calls and tool calls appear as bars; width encodes
  duration, x-position encodes wall-clock start offset.
- **Outcome colours** — green = success, red = error/failure. Hover for details.
- **Graceful degradation** — when no agent attribution is available, all calls
  appear in a single "root" lane with a hint linking to ADR-0035.

### Adding agent attribution to your agent code

```python
# When writing a model call record, add nova.agent.id:
capsule.append_model_call({
    "model_call_id": ...,
    "nova.agent.id": "kubernetes_sentinel",
    "nova.agent.type": "evidence",
    # ... rest of gen_ai.* fields
})

# Same for tool calls:
capsule.append_tool_call({
    "tool_call_id": ...,
    "nova.agent.id": "kubernetes_sentinel",
    # ... rest of fields
})
```

When `nova.agent.id` is present the swimlane and Thread views use it directly.
When it is absent the Thread view falls back to system-prompt inference; the
swimlane shows a single root lane.

---

## Evidence Tab (v0.9 + v0.11)

See [ADR-0037](../design/adr/0037-evidence-tab-native-dashboard.md).

The Evidence Tab is a fully operational view over real bundles on disk. It requires `nova export-evidence` to have been run at least once (bundles live in `~/.novafabric/evidence/`).

**Bundle list view** — table of all bundles found in `~/.novafabric/evidence/`, showing `run_id`, timestamp, file size, and a manifest-hash integrity badge. The badge is a shallow check (SHA-256 of `manifest.json` fields) that runs at list time; use the Verify button for cryptographic assurance.

**Detail pane** — selecting a bundle opens a detail pane showing the DSSE statement (pretty-printed JSON), attestation hashes for each artifact, and the signing key fingerprint (first 16 hex chars of the SHA-256 of the public key).

**Verify button (v0.11)** — calls `POST /api/evidence/{bundle_id}/verify` server-side. Returns three independent checks:

| Check | What is verified | When `null` |
|---|---|---|
| `sig ✓ / ✗` | Ed25519 DSSE signature on `attestations/run.intoto.json` against `signatures/run.cert` | Never — always attempted |
| `tsr ✓ / ✗ / –` | RFC 3161 TSR in `manifest.dsse.tsr` against the DSSE bytes | When timestamping was not requested at export time |
| `log ✓ / ✗ / –` | NovaSeal Merkle log inclusion for the associated capsule | When `novaseal.yaml` is not configured or the capsule dir is not locally available |

A bundle is `valid` when all non-null checks pass. The `sig` check must pass for the others to be meaningful.

**Download link** — direct link to download the raw ZIP for offline verification (`openssl ts -verify`) or sharing.

---

## Cross-run comparison (v0.9)

**Planned v0.9.** See [ADR-0036](../design/adr/0036-cross-run-comparison-ux.md).

The current diff workflow has six friction steps: find run A in the Runs tab, copy its `run_id`, navigate to the Diff tab, paste it, find run B, copy and paste its `run_id`. The v0.9 additions remove the copy-paste steps entirely.

**D2 — Two-click compare from RunsTab.** Select any two rows in the Runs tab (checkbox or Shift-click) and a "Compare" button appears in the toolbar. Clicking it navigates directly to the Diff tab with both `run_id` values pre-filled and the diff auto-triggered.

**D3 — Compare against… from CapsuleInspector.** A "Compare against…" dropdown inside the CapsuleInspector lets you pick a second run from a recency-sorted list, then jumps to the Diff tab pre-filled.

**D4 — Word-level LCS diff for model responses.** Model call response text is diffed at the word level using a Longest Common Subsequence algorithm, with insertions highlighted green and deletions struck through in red — making prompt-sensitivity changes legible at a glance.

**D5 — Mutation badge on tool call pairs.** When a matched tool call pair has different arguments or outputs, a ⚡ mutation badge appears on the pair header. Badge tooltip shows argument key(s) that changed.

---

## Infrastructure tab (v0.12.6)

The **Infra** sidebar entry (under the Infrastructure group) shows the operational status of all ten Phase 0–6 cluster-scale components as card tiles:

| Component | Status | CLI to verify |
|---|---|---|
| Run Capsule (Phase 0 / v0.1) | `shipped` | `nova inspect <run-id>` |
| NovaSeal v0.1 (Phase 0) | `shipped` | `nova verify <capsule>` |
| Event Envelope v1 (Phase 1) | `shipped` | `nova validate <capsule>` |
| Collector tier (Phase 2) | `shipped` | `novafabric-collector --help` |
| Parent/Child Capsule (Phase 3) | `shipped` | `nova run show <id> --with-children` |
| Object Capsule Store (Phase 4) | `shipped` | `nova store status` |
| Metadata DB (Phase 5) | `shipped` | `nova db status` |
| Lineage at Scale (Phase 6) | `shipped` | `nova lineage provenance <ref>` |
| Server Mode (v0.7) | `shipped` | `nova server start` |
| Eval Suites (v0.9) | `shipped` | `nova eval <name@version>` |

Badge colours: **shipped** (green), **partial** (accent), **placeholder** (amber), **planned** (muted). Each card shows the CLI command(s) for that component so operators can investigate from the terminal without navigating away.

---

## Eval trend chart (v0.9)

**Planned v0.9.** See [ADR-0038](../design/adr/0038-eval-trend-dashboard.md).

**Per-asset eval sparkline.** Every row in the Registry tab gains an 8×32px inline SVG bar chart showing the last 10 eval results for that asset — green bars for pass, red for fail. The sparkline is lazy-loaded via `IntersectionObserver` so it does not block initial table render.

**Eval history panel.** Clicking the "EVAL" button for an asset opens an expanded history panel (above the trigger form) showing the last 20 eval results in a table: suite name, score, pass/fail, and relative timestamp (e.g. "3 days ago"). The panel is fetched on demand and cached for the session.

**v0.12.8 display fixes:**
- **Null score** — suites that produce only a pass/fail signal (no numeric score) store `{"score": null}`. Previously rendered as `0.00`; now renders as a muted `—` so it is not confused with a zero-score failure.
- **Empty suite name** — if `suite_name` is blank or absent in the database, the eval row shows italic `(unknown suite)` instead of empty whitespace.

---

## UX conveniences (v0.7)

- **Theme toggle** in the header — light / dark / system. Persisted in `localStorage`. An inline `<script>` runs before hydration to prevent flash.
- **Token-in-URL flow.** Click the URL `nova serve` prints (it embeds the token); the SPA consumes it, saves to `localStorage`, and strips it from the visible URL so it doesn't sit in browser history.
- **Auto-bounce on 401.** Any in-flight request that returns 401 (e.g. server restart, token rotation) clears the cached token and shows the connect screen again.
- **Confirm dialogs for every mutation.** Each shows a one-line description, the action's arguments where editable (e.g. promote target dropdown), the equivalent CLI command, and the audit-log notice.
- **Toast notifications** for success and failure of mutations — visible bottom-right for ~4 seconds.
- **Auto-refresh on the runs tab.** Opt-in 5-second polling; off by default.

---

## Roadmap

- **v0.7** *(shipped)* — Layer A (read-only) + Layer B partial (register, eval, promote, forensic replay, redact, export evidence). All CLI-equivalent actions are confirm-gated and audit-logged.
- **v0.8** *(shipped 2026-05-12)* — Home tab (journey cards, status bar, resume session), Commands tab (11 CLI command builders with live preview + copy), sidebar reorganized into 5 journey groups.
- **v0.9** *(shipped 2026-05-12)* — Evidence Tab native (list + verify real bundles), cross-run comparison UX (2-click compare shortcut, word-level diff, mutation badges), eval trend sparklines in Registry. See [ADR-0037](../design/adr/0037-evidence-tab-native-dashboard.md), [ADR-0036](../design/adr/0036-cross-run-comparison-ux.md), [ADR-0038](../design/adr/0038-eval-trend-dashboard.md).
- **v0.11** *(shipped 2026-05-14)* — Dashboard Completeness: all eight gap-closing tracks shipped.
  - **DC-8** — Diff compare URL persistence (`?run_a=&run_b=`).
  - **DC-3** — Secret scan results viewer (Secrets view in RunsTab, `GET /api/runs/{id}/redaction-proof`).
  - **DC-1** — Evidence verification UI (`POST /api/evidence/{id}/verify`, three-stage DSSE + TSR + Merkle check, inline badges).
  - **DC-7** — Capsule validation UI (`POST /api/runs/{id}/validate`, expandable error list).
  - **DC-6** — Asset spec diff (`GET /api/assets/{name}/diff`, Registry tab Compare… modal).
  - **DC-2** — Legal holds tab (place/release holds, sidebar badge count).
  - **DC-4** — Semantic + exact replay UI (similarity gauge, eligibility card).
  - **DC-5** — Policy check tab (`POST /api/policy/check`, interactive OPA/Rego policy tester, ALLOW/DENY badge).
- **v0.12** *(shipped 2026-05-14)* — C-4 compare shortcut in RunsTab: multi-select checkboxes (max 2), "Compare selected ⊕" banner, auto-jump to DiffTab with both IDs pre-filled and diff auto-triggered. `nova unregister` command added.
- **v0.12.6** *(shipped 2026-05-14)* — Dashboard coverage Phase 1: expanded Commands tab (13 → 35 builders across 4 journey tracks), Lineage QueryPanel (interactive provenance/blast-radius/replay-chain query with live CLI preview), InfraTab (10 Phase 0–6 component status cards), enriched CaptureTab (all 4 runners, distributed-run commands, full capture matrix). DD-1..DD-8 implementation plans created.
- **v0.12.7** *(shipped 2026-05-14)* — Context-aware autocomplete in every ref input: Lineage QueryPanel, Diff run A/B, Holds registry name, Policy resource ref. Shared `SuggestInput` component. Full documentation update.
- **v0.12.8** *(shipped 2026-05-14)* — Eval results panel: null score → `—`, empty suite name → `(unknown suite)`. Makefile `bundle` + `serve-local` targets.
- **v0.12.9** *(shipped 2026-05-14)* — Three promote-dialog bug fixes (Bug-1/2/3: valid transition enforcement, inline error banner, conditional eval-gate copy) and nine UX improvements: DU-1 (RunsTab status filter), DU-2 (copy run ID), DU-3 (lineage ancestry breadcrumb), DU-4 (DiffTab sessionStorage), DU-5 (bulk promote), DU-6 (AuditTab action filter), DU-7 (HomeTab staleness indicator), DU-8 (CaptureTab folder link), DU-9 (PolicyTab Rego lint).
- **v0.13.0** *(shipped 2026-05-15)* — D-5 maker-checker dual-approval: `nova promote` restructured as a sub-group (`direct` / `propose` / `approve`). Ed25519 keypair auto-generated per identity. SoD enforced at the cryptographic level. Opt-in `maker_checker_gate.rego`. ADR-0058 + ADR-0018 amendment. 14 new tests.
- **v0.13.1** *(shipped 2026-05-15)* — DU-10 shared `<EmptyState>` component (`bordered` / `fill` / `inline` variants), applied to HoldsTab, AuditTab, RunsTab, EvidenceList, RegistryBrowser, LineageGraph. Eliminates 8 ad-hoc empty-state patterns.
- **v0.13.2** *(shipped 2026-05-15)* — DC-5 OPA trace viewer in PolicyTab. "explain" checkbox sends `explain: true` to `POST /api/policy/check`; backend fires `opa eval --explain full --format pretty`; `trace_text` returned in `PolicyDecision`; collapsible "show trace ↓" panel in the decision result card.
- **v0.13.3** *(shipped 2026-05-15)* — C-5 N-run diff. RunsTab checkbox cap lifted 2→5. Selecting 3–5 runs and clicking "Compare selected ⊕" enters DiffTab N-run mode: first run is baseline, N-1 parallel diffs fire, results shown as stacked collapsible cards with change-count badges. URL migrated from `?run_a=&run_b=` to `?run_ids=a,b,c`.
- **v0.13.4** *(shipped 2026-05-15)* — LineageGraph UX fix: double-clicking a node now selects it instead of triggering zoom-to-fit (`zoomOnDoubleClick=false`). Test coverage restored to 90%.
- **v0.14 P1** *(API only; UI in P2)* — Role-management REST surface. `POST/DELETE/GET /v0/admin/roles` (production) and `/api/admin/roles` (local). Last-admin lockout invariant at the store layer (returns 409). `nova server revoke-role` CLI added. ADR-0060. AdminTab "Roles" panel placeholder remains until P2 lands.
- **v0.15.2** *(shipped 2026-05-20)* — Track B dashboard scale: RunsTab cursor-pagination + SSE live feed, RegistryTab load-more, ComplianceTab with 4 panels for cap-001/002/004/005 evidence controls.
- **v0.16.0–v0.16.5** *(shipped 2026-05-20)* — GovernanceTab UI (4 panels: classify, audit, export-examiner, policy sign), SealTab inputClass crash fix, PBS/LSF runner dashboard support.
- **v0.17.0** *(shipped 2026-05-20)* — KGTab (6 panels: query, audit, entity queue, alias, init, ingest), full Evidence Fabric v1.0 coverage, TV-5 3D topology view.
- **v0.18.0** *(shipped 2026-05-20)* — DB-KG-1 new KGTab init/ingest panels; DB-CAP-1/ERA-1/STG-1 extensions in PolicyTab/ComplianceTab/InfraTab.
- **v0.19.0** *(shipped 2026-05-20)* — All CLI surfaces covered: ValidateDistributedBlock, KGInitPanel, NewRunIdPanel, DatabaseOpsPanel.
- **v0.20.0** *(shipped 2026-05-20)* — 7 new endpoints (unregister, doctor, policy test/explain, audit coverage/bundle/verify) + 4 tab panels.
- **v0.27.0** *(shipped 2026-05-20)* — Full CLI→dashboard parity: 11 new panels across SealTab, AdminTab×4, RegistryTab×2, InfraTab, RunsTab, LineageTab, GovernanceTab. 59 new tests.
- **v0.29.0** *(shipped 2026-05-20)* — KG MCPServer topology layer (SERVED_BY edge, `/api/kg/topology`, TopologyLayerPanel).
- **v0.30.0** *(shipped 2026-05-20)* — 3 new panels: CapsuleVerifyPanel (SealTab), OpenLineageExportPanel (LineageTab), SuggestRegisterPanel (RegistryTab).
- **v0.31.0** *(shipped 2026-05-20)* — KGQueryPanel, KGAuditPanel, EntityQueuePanel, KGAliasPanel; erasure-status + AuditMapPanel (ComplianceTab); children tree (RunsTab).
- **v0.34.0** *(shipped 2026-05-20)* — Dashboard parity for v0.32–v0.33 regulatory CLIs: RoCrateExportPanel, C2paExportPanel, ProvJsonExportPanel, EuAiActStatusPanel, EuAiActExportPanel.
- **v0.37.0** *(shipped 2026-05-20)* — ComplianceTab gains 4 new panels: **GDPR Art.30 RoPA Export** (optional controller fields, completeness badge), **AI-SBOM Export** (CycloneDX 1.7 component list, v0.39.0), **NIST AI RMF Report** (GOVERN/MAP/MEASURE/MANAGE score bars, risk-level badge), **AI-SBOM Coverage Status** (auto-loads, 3-stat grid, CRA deadline 2026-09-11). Four new `nova serve` endpoints: `POST /api/compliance/export/ropa|aibom|nist-rmf`, `GET /api/aibom/status`.
- **v1.0** — Graduation review. The `--experimental` flag is removed only when a follow-up ADR explicitly proposes it.

The dashboard is judged on whether it earns its keep. If at any point it becomes maintenance-heavy without commensurate user value, the entire `src/novafabric/serve/` package can be deleted in a single commit and ADR-0027 reverted, leaving no migration debt because the dashboard owns no persistent state of its own.

---

## Scale characteristics and known limits

Understanding these limits prevents surprises when you grow beyond a small local
setup. The limits are documented here so they are visible before they bite.

### What works at scale today

| Mechanism | Where it lives | What it handles |
|---|---|---|
| Cursor-based pagination on `/api/runs/search` | `serve/app.py:42–66` | Fetches large run lists in pages without re-scanning from the start. A **cursor** is a bookmark: the server encodes `(created_at, run_id)` of the last returned item into a short base64 token. Send it back as `?cursor=...` to get the next page. Nothing to do with the VS Code text cursor. |
| `LIMIT`/`OFFSET` on assets and runs | `serve/app.py` | Registry and runs list endpoints cap result sizes. |
| TanStack Virtual in RunsTab and RegistryTab | `web/src/components/dashboard/tabs/` | Renders only the rows visible on screen — the DOM stays small even with thousands of rows loaded in memory. |
| SSE live feed for new capsules | `/api/runs/stream` | New capsule directories appear in the RunsTab without a full page reload. |
| 5 SQL indexes on registry DB | `serve/app.py` (v0.14.6) | Keeps `WHERE status = ?` and `ORDER BY created_at` fast at tens of thousands of assets. |

### Known hard limits (action items for future versions)

| Limit | Root cause | Planned fix |
|---|---|---|
| **~10,000 capsules** before `/api/runs` slows noticeably | `list_run_summaries()` (`serve/capsule_loader.py:51`) re-reads every `capsule.yaml` from disk on every request — O(N disk). | **Scale-S1 (shipped v0.32.0) + Scale-S3 (shipped v0.36.0):** `runs_cache` SQLite index + `CapsuleWatcher` background indexer; `nova ingest-capsule` CLI for manual re-index. |
| **NovaSeal Merkle log verify** scans full SQLite table | `serve/app.py`, `nova seal log verify` | **Scale-S4** (planned v1.x): Postgres path for the NovaSeal policy and Merkle log store. |
| **SQLite registry** breaks at ~1M rows | No partitioning in SQLite | Flip `NOVAFABRIC_METADATA_BACKEND=postgres` — the Postgres tier (`PostgresMetadataStore`) is already implemented. |

### Storage backends — three separate systems

Each backend is upgraded independently. Flipping one does not affect the others.
See [Storage Architecture](../design/architecture/cluster-scale.md#production-storage-stack-polyglot-persistence) for the full reference table and env var names.

| Store | Default | Scalable tier |
|---|---|---|
| Capsule content | Flat files under `$NOVAFABRIC_HOME/capsules/` — always filesystem | Object Capsule Store (`NOVA_OCS_BACKEND`) |
| Registry + lineage | `~/.novafabric/registry.db` (SQLite) | `NOVAFABRIC_METADATA_BACKEND=postgres` + `NOVAFABRIC_METADATA_DSN=...` |
| NovaSeal policy + Merkle log | `~/.novafabric/novaseal-merkle.db` (SQLite) | No Postgres path yet (Scale-S4) |

---

## KG topology vs. Live Topology

The dashboard exposes two topology views that are easy to confuse:

| | KG tab → Multi-Layer Topology | Top-right "Topology" button |
|---|---|---|
| Backing store | KuzuDB on disk (persistent) | In-memory DuckDB + ring buffer (ephemeral) |
| Node types | Agent, Model, Tool, MCPServer, InferenceEndpoint | Agent only |
| Updated by | Re-ingest All / auto-poll (60 s) | Running agents in real time (< 5 s) |
| Survives restart | ✓ | ✗ (re-seeded from disk at startup) |
| Queryable | ✓ (`nova kg query`) | ✗ view only |
| Purpose | Historical audit: what called what | Operational: what is running now |

**Re-ingest All** only updates the KG topology. It has no effect on the live Topology view.
**Restarting `nova serve`** re-seeds the live Topology from disk. It does not change KuzuDB.

Full comparison with update-trigger matrix: [`design/architecture/lineage.md — KG topology vs. Live Topology`](../design/architecture/lineage.md#kg-topology-vs-live-topology).

---

## See also

- [ADR-0027](../design/adr/0027-nova-serve-experimental-dashboard.md) — full architectural decision record.
- [`design/strategy/non-goals.md`](../design/strategy/non-goals.md) — the original "no web UI through v1.0" stance and its narrow exception.
- [`docs/cli-reference.md`](cli-reference.md) — the canonical CLI reference. Every dashboard action maps to a command listed there.
- [`design/adr/0011-evidence-bundle.md`](../design/adr/0011-evidence-bundle.md) — the signing key model the dashboard reuses.
- `web/` (in the repo) — the showcase site that explains what NovaFabric does using baked-in fixture data. Different audience: the showcase is *marketing*; the dashboard is *operational*.
- [ADR-0036](../design/adr/0036-cross-run-comparison-ux.md) — cross-run comparison UX design (v0.9).
- [ADR-0037](../design/adr/0037-evidence-tab-native-dashboard.md) — Evidence Tab native implementation (v0.9).
- [ADR-0038](../design/adr/0038-eval-trend-dashboard.md) — Eval trend chart in Registry (v0.9).
