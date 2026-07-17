# NovaFabric REST API Reference

**Maturity:** experimental (v0.61.0). This is the HTTP surface of the local, read-only
`nova serve --experimental` dashboard. It works today, but its routes, request/response
shapes, and behaviour may change before the v1.0
schema freeze. The CLI remains the canonical interface; every endpoint below mirrors, or
is derived from, a `nova` command you can run without a server.

> **Generated, not hand-maintained.** The endpoint tables in this document are produced
> by reading the live FastAPI route table in `src/novafabric/serve/app.py`. Re-run the
> generator after any route change rather than editing endpoints by hand:
>
> ```bash
> uv run python design/scripts/gen_api_reference.py
> ```
>
> The prose sections outside the tables (below and at the end) are hand-authored — edit
> them directly; the generator only rewrites the tables.

## What you will learn

- **What this API is** — and, just as importantly, what it is *not* (§ "Two different APIs").
- **How to start the server** and obtain the session token that authorizes requests.
- **The authentication and network model** — why it binds to localhost only and how the
  one-shot token works.
- **How the endpoints map to the five NovaFabric primitives** and to `nova` CLI commands.
- **The full endpoint catalogue** — ~190 routes across 25 functional domains (regenerate the tables with `design/scripts/gen_api_reference.py` for the exact live count).
- **Honesty notes** on routes that surface PLANNED / experimental capabilities.

## Two different APIs — do not confuse them

NovaFabric ships two distinct HTTP surfaces. **This document describes the first.**

| | `nova serve --experimental` (this doc) | `nova server` (multi-tenant) |
|---|---|---|
| Purpose | Local, read-first dashboard over your own capsules | Shared, multi-user server mode |
| Audience | A single operator on one machine | Teams, with OIDC / RBAC |
| Binds to | `127.0.0.1` only (localhost) | A network interface |
| Auth | One-shot session `?token=…` query parameter | OIDC / RBAC, offline CI tokens, SCIM provisioning (experimental) |
| Backend | SQLite (default), no network required | Postgres 16 |
| Spec | This file (generated from `serve/app.py`) | `api/openapi.yaml` (OpenAPI) |
| Maturity | experimental (Layer A, read-only) | experimental (v0.7+) |

The multi-tenant server (row 2) is specified separately in `api/openapi.yaml` and is **not**
covered here. Everything below is the local dashboard surface.

## Starting the server

The dashboard lives in the optional `serve` extra and is gated behind a mandatory
`--experimental` flag (per ADR-0027):

```bash
pip install 'novafabric[serve]'
nova serve --experimental
```

On startup the server:

- binds to **`127.0.0.1:4321`** by default (change with `--host` / `--port`);
- generates a **session token**, prints it, and writes it to `~/.novafabric/.serve-token`
  (file mode `0600`);
- opens a browser at `/dashboard?token=…` unless you pass `--no-browser`.

Verified `nova serve --experimental` flags:

| Flag | Effect |
|---|---|
| `--experimental` | **Required.** Acknowledges this is an experimental subcommand. |
| `--host TEXT` | Interface to bind to. Localhost only by default (`127.0.0.1`). |
| `--port INTEGER` | TCP port for the HTTP server (default `4321`). |
| `--capsule-dir PATH` | Directory of capsules to browse. Defaults to `$NOVAFABRIC_HOME/capsules`. |
| `--db-path PATH` | Registry / lineage SQLite path. Defaults to `~/.novafabric/registry.db`. |
| `--no-browser` | Don't auto-open a browser. |
| `--topology` | Enable the live topology dashboard (Louvain cluster view + TDP WebSocket). |
| `--tv5` | Enable the experimental 3D topology view (TV-5). Implied by `--topology`. |
| `--topology-louvain-resolution FLOAT` | Louvain clustering resolution for the topology view (default `1.0`, or `NOVA_TOPOLOGY_LOUVAIN_RESOLUTION`). |

## Authentication & network model

The dashboard is designed for single-machine use and defends itself accordingly:

- **Localhost-only bind.** By default it listens on `127.0.0.1` and refuses to serve
  requests whose `Host` header is not localhost — a DNS-rebinding defence. A non-localhost
  bind is possible but deliberately hidden and discouraged.
- **Session token on every `/api/*` route.** The token is passed as a **query parameter**,
  `?token=<value>`, and compared in constant time. Missing or wrong tokens return
  `401 Unauthorized`.
- **Token sources**, in precedence order: the `NOVAFABRIC_SERVE_TOKEN` environment variable
  (useful for pinning a stable token in Docker / CI), an existing `~/.novafabric/.serve-token`
  file (survives restarts without breaking open browser sessions), or a freshly generated
  cryptographically random token.
- **One unauthenticated route:** `GET /api/health` is intentionally open (it returns nothing
  sensitive) so a browser or probe can check liveness without a token.

Example authenticated request:

```bash
TOKEN=$(cat ~/.novafabric/.serve-token)
curl "http://127.0.0.1:4321/api/stats?token=$TOKEN"
```

## How the endpoints map to the five primitives

The route domains below line up with NovaFabric's five primitives plus the trust and
governance layers built on them:

| Primitive / layer | Domains below |
|---|---|
| **Asset Registry** | Assets & registry |
| **Run Capsule** | Runs & capsules; Schema; Cost & metrics; ingest-capsule |
| **Replay & Diff** | Runs & capsules (`/replay/*`); Assets & registry (`/api/diff`) |
| **Lineage** | Lineage; lineage-store; Knowledge graph |
| **Evidence Bundle & trust** | Evidence & audit; Sealing & trust; Legal holds |
| Governance & compliance | Compliance & governance; Policy & approval; incidents; Assurance & MCP |
| Operations & UI | Health & meta; Reports; Topology (live); Storage & infrastructure; Admin; ops |

Wherever a route mirrors a CLI command, its summary names it (for example,
`nova validate`, `nova eval run`, `nova verify`). When in doubt, the `nova` command is
authoritative and works offline without the server.

## Honesty notes on the endpoint catalogue

A handful of domains expose routes for capabilities that are **PLANNED / FUTURE DESIGN**,
not yet shipped as a general service. The routes exist in the dashboard, but treat the
underlying capability as design intent — never as a shipped guarantee:

- **Sealing & trust** — the NovaSeal signing core (DSSE envelopes, Merkle log, RFC 3161
  timestamps, signing-epoch ratchets, Sigstore keyless via the `sigstore` extra) is
  **implemented and tested** (`tests/seal/`, ADR-0041/0054); these routes surface it.
  Routes are still `experimental` — response shapes may change before the v1.0 freeze.
- **Runs & capsules** — parent/child and `validate-distributed` routes surface the
  **experimental** parent/child capsule model (ADR-0039, shipped; Slurm DDP runs produce
  PARENT + WORKER capsules). `POST /api/otlp/v1/traces` (NF-034) is **experimental**: it
  accepts OTLP/HTTP **JSON** (default) or OTLP/**protobuf**
  (`Content-Type: application/x-protobuf`, ADR-0177; the server needs the `otlp` extra) —
  both converge on the same events — and seals GenAI spans into a lower-fidelity capsule
  labeled `capture_level: ingested-otlp`.
- **Storage & infrastructure** — object-store and manifest-chain routes surface the
  **experimental** object capsule store (CAS/WAL/WORM adapters, shipped); collector routes
  surface the **experimental** cluster-scale collector. SQLite + filesystem remains the
  shipped local default; the at-scale Postgres/AGE lineage tier is still **planned**.
- **Topology (live)** — the live topology dashboard (`--topology`) is experimental and
  gated behind prototype spikes.

NovaFabric provides primitives that *support* compliance workflows; it certifies no
regulation and vouches only that a signed capsule is unmodified since signing.

## Endpoint catalogue

~190 routes across 25 domains. Each row shows the HTTP method, the path, and a one-line
summary. Path parameters use `{name}` notation; `{ref:path}` and `{filepath:path}` accept
slash-containing values.

## Health & meta  (9)

| Method | Path | Summary |
|---|---|---|
| `GET` | `/` | serve no dashboard |
| `GET` | `/api/adapters` | List registered Nova framework adapters with availability flags. |
| `GET` | `/api/docs` | swagger ui html |
| `GET` | `/api/doctor` | Run diagnostic checks on the NovaFabric installation (nova doctor). |
| `GET` | `/api/health` | health |
| `GET` | `/api/openapi.json` | openapi |
| `GET` | `/api/stats` | Aggregate counts for the HomeTab. |
| `POST` | `/api/validate-spec` | Validate an asset YAML spec without registering — mirrors `nova validate <spec>`. |
| `GET` | `/docs/oauth2-redirect` | swagger ui redirect |

## Runs & capsules  (30)

| Method | Path | Summary |
|---|---|---|
| `POST` | `/api/capsule-migrate` | Migrate a v0.1.x capsule directory to v1.0.0 format (ADR-0034 §6). |
| `POST` | `/api/otlp/v1/traces` | Ingest OTLP OTel GenAI spans (JSON or protobuf via Content-Type) into a run capsule (NF-034, experimental). |
| `GET` | `/api/runs` | list runs |
| `GET` | `/api/runs/cost-summary` | Return per-run token and cost totals from ClickHouse. |
| `GET` | `/api/runs/search` | Cursor-based run listing. |
| `GET` | `/api/runs/stream` | Server-Sent Events stream of new run summaries. |
| `GET` | `/api/runs/suggest-register` | Return asset registration suggestions derived from recent capsules. |
| `DELETE` | `/api/runs/{run_id}` | Delete a capsule directory, subject to legal holds and retention policy. |
| `GET` | `/api/runs/{run_id}` | get run |
| `GET` | `/api/runs/{run_id}/children` | Return parent capsule metadata + list of child capsule summaries. |
| `GET` | `/api/runs/{run_id}/diagnose` | Attribute a failed run to its most likely responsible step. |
| `GET` | `/api/runs/{run_id}/energy` | Energy-Anchored Action Receipts + conservation for a run (ADR-0093). |
| `POST` | `/api/runs/{run_id}/export-system-card` | Generate and SEAL an auto-generated system/audit card. |
| `GET` | `/api/runs/{run_id}/file/{filepath:path}` | get run file |
| `GET` | `/api/runs/{run_id}/ledger` | Adversary-anchored ledger verification status (ADR-0094). |
| `POST` | `/api/runs/{run_id}/redact` | redact endpoint |
| `GET` | `/api/runs/{run_id}/redaction-proof` | get redaction proof endpoint |
| `POST` | `/api/runs/{run_id}/replay/dry-run` | dry run replay endpoint |
| `POST` | `/api/runs/{run_id}/replay/exact` | exact replay endpoint |
| `POST` | `/api/runs/{run_id}/replay/forensic` | forensic replay endpoint |
| `POST` | `/api/runs/{run_id}/replay/semantic` | semantic replay endpoint |
| `GET` | `/api/runs/{run_id}/run-lineage` | List spool lineage edges for a distributed run — nova run lineage. |
| `GET` | `/api/runs/{run_id}/safety-case` | Compile an evidence-grounded safety case for a run (ADR-0095). |
| `GET` | `/api/runs/{run_id}/scan-secrets` | Report secret/PII findings from the redaction log — nova scan-secrets. |
| `POST` | `/api/runs/{run_id}/scores` | Append one externally-computed score to the run's scores.jsonl (ADR-0119, experimental) — nova score submit. |
| `GET` | `/api/runs/{run_id}/tool-permission-events` | Return ToolPermissionEvent records for a capsule. |
| `GET` | `/api/runs/{run_id}/tree` | Show the parent/child capsule tree — nova run show --with-children. |
| `POST` | `/api/runs/{run_id}/validate` | Validate a capsule's schema and required files. |
| `POST` | `/api/runs/{run_id}/validate-distributed` | Validate a distributed parent capsule + its workers (nova run validate-distributed). |
| `POST` | `/api/runs/{run_id}/verify` | Verify DSSE signature + RFC 3161 timestamp + Merkle log inclusion (nova verify). |

## Assets & registry  (19)

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/assets` | list assets endpoint |
| `POST` | `/api/assets` | register asset endpoint |
| `POST` | `/api/assets/register-from-yaml` | Register an asset from a YAML spec string (used by suggest-register panel). |
| `GET` | `/api/assets/{asset_id}` | get asset by id endpoint |
| `GET` | `/api/assets/{asset_id}/approvals` | Return approval records for an asset (by UUID). |
| `POST` | `/api/assets/{asset_id}/approve` | approve asset endpoint |
| `POST` | `/api/assets/{asset_id}/eval` | Eval by UUID — resolves name+version from the registry then delegates. |
| `GET` | `/api/assets/{asset_id}/eval-history` | eval history endpoint |
| `POST` | `/api/assets/{asset_id}/promote` | Promote by UUID — resolves name+version from the registry then delegates. |
| `GET` | `/api/assets/{name}/diff` | asset spec diff endpoint |
| `POST` | `/api/assets/{name}/rollback` | rollback asset endpoint |
| `DELETE` | `/api/assets/{name}/{version}` | Hard-delete an asset from the registry (nova unregister). |
| `GET` | `/api/assets/{name}/{version}` | get asset endpoint |
| `POST` | `/api/assets/{name}/{version}/eval` | eval asset endpoint |
| `POST` | `/api/assets/{name}/{version}/promote` | promote asset endpoint |
| `GET` | `/api/diff` | diff runs |
| `POST` | `/api/eval/compare` | Compare two EvalResult JSON objects for regression — mirrors `nova eval compare`. |
| `POST` | `/api/eval/run` | Run a standard eval suite against a capsule — nova eval run. |
| `GET` | `/api/eval/suites` | List registered eval suite adapters — nova eval list. |

## Lineage  (8)

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/lineage/blast-radius/{ref:path}` | lineage blast radius |
| `GET` | `/api/lineage/edges` | Return every lineage edge in the SQLite. Powers the dashboard's full-DAG view. |
| `POST` | `/api/lineage/export-prov` | Export W3C PROV-JSON lineage document for a capsule. |
| `POST` | `/api/lineage/import` | Import capsule lineage events into the store — mirrors `nova lineage import`. |
| `GET` | `/api/lineage/provenance/{ref:path}` | lineage provenance |
| `GET` | `/api/lineage/replay-chain/{run_id}` | lineage replay chain |
| `GET` | `/api/lineage/time-travel/{ref:path}` | lineage time travel |
| `GET` | `/api/lineage/{run_id}/emit-openlineage` | Return OpenLineage events for a capsule as JSON (nova lineage emit-openlineage). |

## Evidence & audit  (8)

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/audit` | list audit |
| `GET` | `/api/evidence` | list evidence endpoint |
| `GET` | `/api/evidence/completeness/{run_id}` | Compute the completeness assertion for a capsule. |
| `GET` | `/api/evidence/{bundle_id}` | get evidence detail endpoint |
| `GET` | `/api/evidence/{bundle_id}/download` | download evidence endpoint |
| `POST` | `/api/evidence/{bundle_id}/verify` | Verify the cryptographic integrity of an evidence bundle. |
| `POST` | `/api/evidence/{run_id}` | export evidence endpoint |
| `POST` | `/api/evidence/{run_id}/bind` | Build criterion-evidence bindings for a capsule against a profile. |

## Compliance & governance  (25)

| Method | Path | Summary |
|---|---|---|
| `POST` | `/api/aibom/generate` | Generate CycloneDX AI-BOM(s) for one or all capsules — nova aibom generate. |
| `GET` | `/api/aibom/status` | Show CRA SBOM compliance status — nova aibom status. |
| `GET` | `/api/compliance/annex-iv` | Build and return an EU AI Act Annex IV document as JSON-LD. |
| `POST` | `/api/compliance/audit/bundle` | Export a compliance evidence bundle as base64-encoded ZIP (nova audit bundle). |
| `GET` | `/api/compliance/audit/coverage` | Per-control coverage analysis for the local capsule store (nova audit coverage). |
| `GET` | `/api/compliance/audit/map` | List evidence checkers for a compliance profile. |
| `POST` | `/api/compliance/audit/report` | Run compliance audit against a capsule and return a coverage report. |
| `POST` | `/api/compliance/audit/verify` | Validate the structure of an audit report JSON-LD (nova audit verify). |
| `POST` | `/api/compliance/erasure/request` | Queue a GDPR erasure request (DB-ERA-1, cap-003). Active — OQ-01 resolved by ADR-0069. |
| `GET` | `/api/compliance/erasure/status` | List GDPR erasure request status (DB-ERA-1). Stub. |
| `POST` | `/api/compliance/euaiact/export` | Export EU AI Act Art.12 structured log records (ADR-0076). |
| `GET` | `/api/compliance/euaiact/status` | Return EU AI Act Art.12 compliance configuration (ADR-0076). |
| `POST` | `/api/compliance/examiner/{format}` | Export a capsule in examiner format (bagit / pccp / iso42001). |
| `POST` | `/api/compliance/export/aibom` | Export CycloneDX 1.7 AI-SBOM (ML-BOM) — nova export-aibom. |
| `POST` | `/api/compliance/export/c2pa` | Export C2PA v2.3 manifest for a capsule (ADR-0074 / EU AI Act Art.50). |
| `POST` | `/api/compliance/export/hipaa-proof` | Export HIPAA Safe Harbor de-identification proof — nova export-hipaa-proof. |
| `POST` | `/api/compliance/export/nist-rmf` | Export NIST AI RMF 1.0 quantitative risk report — nova export-nist-rmf. |
| `POST` | `/api/compliance/export/rocrate` | Export a capsule as W3C RO-Crate v1.1 ZIP (base64-encoded). |
| `POST` | `/api/compliance/export/ropa` | Export GDPR Art.30 Records of Processing Activities (RoPA) — nova export-ropa. |
| `GET` | `/api/compliance/nis2` | Build and return a NIS2 incident report as JSON. |
| `POST` | `/api/compliance/pii/erase` | Destroy a data subject's DEK (GDPR Art.17 crypto-shredding) — nova pii erase. |
| `GET` | `/api/compliance/subject-proof` | Return GDPR Art. 17 redaction proof for a data subject. |
| `GET` | `/api/governance/classify` | Classify an AI system risk tier inferred from a Run Capsule. |
| `POST` | `/api/governance/classify-manual` | Classify a manually described AI system — nova classify run. |
| `GET` | `/api/governance/vocabularies` | List vocabulary versions — nova classify list-vocabularies. |

## Policy & approval  (8)

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/policy/capture-level` | Return current capture level + tier descriptions (DB-CAP-1, cap-004). |
| `POST` | `/api/policy/capture-level` | Validate a capture-level value and return restart instructions (DB-CAP-1). |
| `POST` | `/api/policy/check` | Evaluate a policy check interactively. |
| `GET` | `/api/policy/explain` | Look up a past policy decision from the audit log (nova policy explain). |
| `GET` | `/api/policy/list` | List Rego bundle files and signed promotion policies — nova policy list. |
| `GET` | `/api/policy/recent-decisions` | Return recent decision IDs from the audit log for autocomplete. |
| `POST` | `/api/policy/sign` | Sign and store a new promotion policy — nova policy sign. |
| `POST` | `/api/policy/test` | Run the Rego test suite for the policy bundle (nova policy test). |

## Sealing & trust  (10)

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/seal/log/verify` | Verify internal consistency of the local Merkle log (ADR-0041). |
| `GET` | `/api/seal/policy` | Return the latest promotion policy predicate. |
| `POST` | `/api/seal/ratchet/init` | Provision epoch-0 ratchet state for a node. |
| `POST` | `/api/seal/ratchet/rotate` | Advance to the next signing epoch; erase the previous chain key. |
| `GET` | `/api/seal/ratchet/status` | Show a node's current signing epoch and registry history. |
| `POST` | `/api/seal/sigstore/sign` | Sign a capsule artifact using Sigstore keyless signing (ADR-0071). |
| `POST` | `/api/seal/sigstore/verify` | Verify a stored Sigstore bundle for a capsule (ADR-0071). |
| `POST` | `/api/seal/{capsule_id}/bypass` | Create a time-limited SoD bypass, DSSE-signed and permanently logged (ADR-0059). |
| `GET` | `/api/seal/{capsule_id}/proposals` | List all proposals for a capsule with their approval status. |
| `POST` | `/api/seal/{capsule_id}/verify` | Run the five-check SoD verifier for a capsule's promote bundles. |

## Knowledge graph  (18)

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/kg/agents/{agent_id}/edges` | Return models, tools, and MCP servers called by an agent (DB-KG-1). |
| `GET` | `/api/kg/aliases` | List all alias-table entries, optionally filtered by canonical entity id. |
| `POST` | `/api/kg/aliases` | Register (upsert) an alias into the Tier-2 alias table. |
| `POST` | `/api/kg/attack-path` | Shortest attack path between two entities — mirrors `nova kg attack-path` (UC2). |
| `POST` | `/api/kg/blast-radius` | Impact/blast-radius of an entity — mirrors `nova kg blast-radius` (UC3). |
| `POST` | `/api/kg/detect` | Unsupervised SPKG anomaly scan of a capsule — mirrors `nova kg detect` (ADR-0111). |
| `GET` | `/api/kg/entity-queue` | Return all pending ReviewItems from the Tier-3 human review queue. |
| `GET` | `/api/kg/entity-queue/stats` | Return pending/approved/rejected counts for the entity review queue. |
| `POST` | `/api/kg/entity-queue/{item_id}/approve` | Approve a review item.  Body: {canonical: str, resolved_by: str}. |
| `POST` | `/api/kg/entity-queue/{item_id}/reject` | Reject a review item.  Body: {resolved_by: str}. |
| `POST` | `/api/kg/ingest` | Ingest a capsule directory into the KG — mirrors `nova kg ingest`. |
| `POST` | `/api/kg/ingest-all` | Bulk-ingest all capsule directories into the KG. |
| `POST` | `/api/kg/init` | Initialise the KG schema (idempotent) — mirrors `nova kg init`. |
| `GET` | `/api/kg/status` | Return Capsule KG store health + entity counts (DB-KG-1, ADR-0067). |
| `GET` | `/api/kg/topology` | Return all KG nodes and edges for multi-layer topology visualization. |
| `GET` | `/v1/kg/audit` | KG health audit: node/edge counts, zero-call-count edges. |
| `GET` | `/v1/kg/query` | Query models and tools for an agent.  Mirrors `nova kg query`. |
| `GET` | `/v1/kg/status` | KG store health check (Tier 2+ aware).  Alias of /api/kg/status. |

## Assurance & MCP  (3)

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/assure/{run_id}` | Run OWASP Top 10 for LLM evidence checks against a capsule. |
| `POST` | `/api/mcp/risk-report` | Generate a structured OWASP LLM risk report for an MCP manifest. |
| `POST` | `/api/mcp/scan` | Scan an MCP server manifest for OWASP LLM supply-chain risks. |

## Storage & infrastructure  (6)

| Method | Path | Summary |
|---|---|---|
| `POST` | `/api/db/upgrade` | Run alembic upgrade to the specified revision (default: head). |
| `GET` | `/api/infra/collector` | Return collector health. Returns {detected: false} if not running. |
| `GET` | `/api/storage/inspect/{run_id}` | Show dual-object split for a run (DB-STG-1, cap-003). |
| `GET` | `/api/storage/manifest-chain` | Return the last N entries in the manifest chain. |
| `GET` | `/api/storage/stats` | Return object capsule store statistics. |
| `GET` | `/api/storage/validate` | Validate S3 backend supports Object Lock COMPLIANCE (DB-STG-1, cap-009). |

## Cost & metrics  (3)

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/cost/pricing` | Return the per-1k-token price table from CostInterceptor (DB-COST-1). |
| `GET` | `/api/cost/report` | Return aggregated LLM cost (DB-COST-1 / cap-002). |
| `GET` | `/metrics/stream` | SSE metrics stream (FR-21: Last-Event-ID reconnect support). |

## Analytics  (1)

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/analytics/summary` | Time-bucketed run aggregates (volume, failures, duration percentiles) from the runs index — powers the dashboard Analytics tab. |

## Reports  (11)

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/report` | Generate an asset inventory report — mirrors `nova report`. |
| `GET` | `/api/reports/capsule-compare` | report capsule compare |
| `GET` | `/api/reports/cost-burn` | report cost burn |
| `GET` | `/api/reports/eval-regression` | report eval regression |
| `GET` | `/api/reports/evidence-inventory` | report evidence inventory |
| `GET` | `/api/reports/executive-summary` | report executive summary |
| `GET` | `/api/reports/policy-audit` | report policy audit |
| `GET` | `/api/reports/release-comparison` | report release comparison |
| `GET` | `/api/reports/run-history` | report run history |
| `GET` | `/api/reports/seal-verification` | report seal verification |
| `GET` | `/api/reports/throughput` | report throughput |

## Legal holds  (3)

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/holds` | list holds |
| `POST` | `/api/holds` | create hold |
| `POST` | `/api/holds/{hold_id}/release` | release hold |

## Schema  (1)

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/schema/list` | List the CapsuleEventType values + meta (DB-SCH-1, cap-001 / ADR-0066; |

## Topology (live)  (5)

| Method | Path | Summary |
|---|---|---|
| `POST` | `/api/topology/seed` | Seed the live topology store from capsules already on disk. |
| `GET` | `/api/topology/snapshot` | Return current topology counts for the SPA status bar. |
| `GET` | `/topology/cluster-edges` | Return inter-cluster edge aggregates for drawing edges between super-nodes. |
| `GET` | `/topology/cluster-list` | Return clusters as plain JSON rows (largest first) for the Table/Treemap views. |
| `GET` | `/topology/clusters` | Return Arrow IPC cluster layer (ADS v1 cluster_layer schema). |

## Admin  (9)

| Method | Path | Summary |
|---|---|---|
| `POST` | `/api/admin/flush-jwks-cache` | Flush the JWKS cache, forcing a re-fetch from the OIDC provider. |
| `GET` | `/api/admin/new-run-id` | Generate a fresh ULID for use as NOVAFABRIC_GLOBAL_RUN_ID (cap-007 FR-27). |
| `POST` | `/api/admin/rebuild-metadata-db` | Disaster-recovery rebuild of the metadata DB from the chain log. |
| `GET` | `/api/admin/roles` | List role assignments (local mode). |
| `POST` | `/api/admin/roles` | Assign a role to a subject (idempotent). Local-mode admin shortcut. |
| `DELETE` | `/api/admin/roles/{subject}/{role}` | Revoke a role from a subject. 404 if not found, 409 if lockout would occur. |
| `GET` | `/api/admin/tokens` | List issued local tokens (stored in ~/.novafabric/tokens.jsonl). |
| `POST` | `/api/admin/tokens` | Issue a new local session token. |
| `DELETE` | `/api/admin/tokens/{fingerprint}` | Revoke a token by fingerprint. |

## incidents  (5)

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/incidents` | List all incidents with their nearest Art. 73 deadline. |
| `POST` | `/api/incidents` | Open a new incident (forward-only lifecycle; never deleted). |
| `GET` | `/api/incidents/{incident_id}` | Get one incident with computed Art. 73 deadlines. |
| `GET` | `/api/incidents/{incident_id}/export` | Export an incident as an OECD-AIM or NIS2 structured report. |
| `POST` | `/api/incidents/{incident_id}/transition` | Advance an incident's lifecycle (open → reported → closed). |

## ingest-capsule  (1)

| Method | Path | Summary |
|---|---|---|
| `POST` | `/api/ingest-capsule` | Index capsule(s) into the runs metadata store — nova ingest-capsule. |

## lineage-store  (1)

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/lineage-store/profile` | Generate a lineage-store deployment profile — nova lineage-store profile. |

## ops  (1)

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/ops/daemon-status` | Report whether the warm capture daemon socket is alive (read-only). |

## Deprecation register (ADR-0188)

**Maturity:** mechanism works today (experimental); the register is empty and the CI
drift gate is future design. Policy and machinery:
[ADR-0188](../design/adr/0188-api-deprecation-sunset-policy.md).

**Scope.** The lifecycle policy applies to the multi-user `nova server` API only
(`api/openapi.yaml`, `/v0` and later). The `nova serve` dashboard API documented in the
tables above remains **experimental/unversioned** — no lifecycle promises — until the
planned ADR-0183 consolidation.

**Breaking-change definition (normative).** Breaking: removing or renaming a path,
query parameter, or response field; narrowing a type or enum; tightening auth or
required scopes on an existing endpoint; changing error-envelope semantics (codes,
envelope shape). Non-breaking: adding optional fields, endpoints, enum values
documented as open, or response headers. If it is not on the breaking list, it may
ship in any release; if it is, the lifecycle below is mandatory.

**Lifecycle.** A deprecation is announced in `CHANGELOG.md`, the release notes, and
this register, all at once. From the deprecating release onward the endpoint emits
three headers on every response (implemented in
`src/novafabric/server/deprecation.py`):

| Header | Format | Meaning |
|---|---|---|
| `Deprecation:` | `@<unix-timestamp>` or `true` (RFC 9745) | The endpoint is deprecated (and since when, if known). |
| `Sunset:` | HTTP-date (RFC 8594) | Earliest date the endpoint may be removed. |
| `Link:` | `<url>; rel="deprecation"` | Points at this register's entry for the endpoint. |

**Minimum window.** A deprecated endpoint stays working for **at least two minor
releases**. Removal lands only in a **minor** version bump pre-1.0 and only in a
**major** bump post-1.0. No silent removals, ever. Every register row names the
deprecating release, an earliest-removal release at least two minors later, and a
replacement (or an explicit "none"); each row must match a `deprecated: true`
operation in `api/openapi.yaml` (drift gate: future CI).

### Register

*No endpoints are currently deprecated.*

| Endpoint | Deprecated in | Earliest removal | Sunset date | Replacement |
|---|---|---|---|---|
| — | — | — | — | — |

## Interactive API docs

When the server is running, the same route table is browsable interactively:

- Swagger UI: `http://127.0.0.1:4321/api/docs?token=<token>`
- OpenAPI JSON: `http://127.0.0.1:4321/api/openapi.json`

## Summary & next steps

- This document covers the **local, experimental** `nova serve --experimental` HTTP surface —
  a read-first (Layer A) dashboard over capsules on your own machine. The separate
  multi-tenant `nova server` API is specified in `api/openapi.yaml`.
- Requests to `/api/*` require the **session token** as a `?token=` query parameter; only
  `GET /api/health` is open. The server binds to `127.0.0.1` and rejects non-localhost
  `Host` headers.
- The endpoint tables are **generated** — after any route change, re-run
  `uv run python design/scripts/gen_api_reference.py` so this file stays truthful.
- Some routes surface **PLANNED** capabilities (NovaSeal sealing, parent/child capsules,
  object store, cluster collector). The route existing is not a claim that the capability
  is shipped — see "Honesty notes" above.

Where to go next:

- **CLI reference** (`docs/cli-reference.md`) — the canonical, offline interface; every
  dashboard view maps to a `nova` command.
- **Developer guide** (`docs/developer-guide.md`) — adding asset types, CLI commands, and
  report formats.
- **`nova serve --help`** — the authoritative, always-current list of server flags.
