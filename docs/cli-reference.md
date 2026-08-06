# CLI Reference

Both `nova` and `novafabric` refer to the same binary. NovaFabric is a
self-hosted, Apache-2.0 command-line toolkit that **captures, replays, diffs,
and audits** AI-agent and model runs. Every command works locally, without a
server, without accounts, and without network access to any NovaFabric-operated
service.

## What you will learn

This page is the complete reference for the `nova` CLI: every command, every
flag, every exit code, and the environment variables that configure them. It is
organized around the five NovaFabric primitives, so you can find the command by
knowing what you are trying to do:

- **Capture** a run into a portable, secret-redacted **Run Capsule** — see
  [Capture commands](#capture-commands-v02).
- **Replay** that capsule in one of four honest modes, or **diff** two runs as a
  CI gate — see [Replay commands](#replay-commands-v03).
- Trace **Lineage** (provenance, blast-radius, replay-chain, time-travel) and
  emit OpenLineage — see [Lineage commands](#lineage-commands-v04).
- Build a signed **Evidence Bundle** an auditor can verify offline with only
  `sha256sum` and an ed25519 verifier — see
  [Trust layer commands](#trust-layer-commands-v04).
- Manage the **Asset Registry** and its eval-gated, maker-checker promotion
  lifecycle — see [Registry commands](#registry-commands-v01) and
  [Policy and governance](#policy-and-governance-v08).

### Getting the help you need at the terminal

Every command self-documents. When a detail here is ambiguous, the terminal is
the source of truth:

```bash
nova --help              # top-level command list
nova capture --help      # any sub-command's flags, defaults, and examples
nova <group> <cmd> --help
nova --version           # installed NovaFabric version
nova --install-completion  # shell tab-completion for commands, flags, and enums
```

### Maturity legend

NovaFabric has shipped v0.1–v0.9 (and point releases beyond). Most surfaces are
**experimental** in the sense that they work today and are covered by tests, but
their interfaces may change before the v1.0 on-disk schema freeze. This
reference labels each capability so you never mistake intent for implementation:

| Label | Meaning |
|---|---|
| **works today** | Implemented in `main`; tests pass; usable now |
| **experimental** | Implemented and tested, but the interface may change before v1.0 |
| **planned** / **future** | Documented design intent only — **not implemented**. Never invoke as if it exists. |

A handful of sections below describe **planned** cluster-scale surfaces (for
example, the NovaSeal signing service and the live topology dashboard). They are
included so the reference is complete about direction, and are marked
accordingly. Local capture, replay, diff, lineage, and Evidence Bundles never
require any of them.

## Command index

Commands grouped by primitive and task. Each entry links to its full section.

### Capture and proxies

| Command | Purpose |
|---|---|
| [`nova init`](#setup-v038) | Set up a local installation and signing keypair |
| [`nova capture`](#nova-capture-cmd) | Wrap any command; record it as a Run Capsule |
| [`nova session`](#nova-session-experimental-adr-0122) | Group N independent runs into one multi-turn session: new, add, list, show (experimental); replay them in order (experimental, ADR-0123) |
| [`nova validate`](#nova-validate-path) | Validate a capsule or an asset spec |
| [`nova api-proxy`](#nova-api-proxy-v064) | Transparent LLM API proxy for non-Python clients |
| [`nova mcp-proxy`](#nova-mcp-proxy-experimental-v05x) | Transparent MCP stdio capture proxy |
| [`nova suggest-register`](#nova-suggest-register-capsule-ref-options) | Suggest assets to register from captured evidence |

### Replay, diff, and diagnose

| Command | Purpose |
|---|---|
| [`nova replay`](#nova-replay-capsule) | Re-run a capsule (forensic / mocked / semantic / exact) |
| [`nova diff`](#nova-diff-capsule-a-capsule-b) | Structurally compare two capsules; CI regression gate |
| [`nova diagnose`](#nova-diagnose-run-id) | Attribute a failed run to its responsible step |
| [`nova query`](#nova-query-experimental-adr-0129) | Aggregate metrics across local capsules, offline (experimental) |
| [`nova search`](#nova-search-experimental-adr-0204) | Full-text search over redacted capsule content (experimental) |
| [`nova view`](#nova-view-experimental-adr-0130) | Saved views: save, list, show, run, delete named query definitions (experimental) |
| [`nova trend`](#nova-trend-experimental-adr-0131) | Score/cost/latency trend report over local capsules: JSON + optional static HTML (experimental) |
| [`nova cost estimate`](#nova-cost-estimate-experimental-adr-0133) | Offline per-capsule cost: recorded vs catalog-estimated (experimental) |
| [`nova cost attribute`](#nova-cost-attribute-experimental-adr-0146) | Attribute recorded spend to productive vs wasted outcomes — descriptive (experimental) |
| [`nova cost fairness`](#nova-cost-fairness-experimental-adr-0146) | Per-agent cost/energy/calls fairness ledger: share, Gini, max/mean — descriptive (experimental) |
| [`nova cost usage-breakdown`](#nova-cost-usage-breakdown-experimental-adr-0132) | Token usage-type composition of a capsule — descriptive (experimental) |
| [`nova pricing`](#nova-pricing-listshowadd-experimental-adr-0133) | Local model-pricing catalog: list, show, add (experimental) |
| [`nova drift`](#offline-drift-and-tool-schema-analysis-experimental-adr-01470148) | Offline drift, silent-failure, and root-cause detectors over sealed capsules (experimental) |
| [`nova toolschema impact`](#nova-toolschema-impact-experimental-adr-0148) | Which historical runs break under a new tool schema (experimental) |

### Lineage

| Command | Purpose |
|---|---|
| [`nova lineage provenance`](#nova-lineage-provenance) | What a node depends on (forward) |
| [`nova lineage blast-radius`](#nova-lineage-blast-radius) | What depends on a node (backward) |
| [`nova lineage replay-chain`](#nova-lineage-replay-chain) | Trace a run back to its original capture |
| [`nova lineage time-travel`](#nova-lineage-time-travel) | Lineage state as of a timestamp |
| [`nova lineage emit-openlineage`](#nova-lineage-emit-openlineage) | Emit OpenLineage 2.0.2 events |
| [`nova lineage metrics`](#nova-lineage-metrics) | Hubs + single points of failure (experimental, ADR-0212) |
| [`nova lineage root-cause`](#nova-lineage-root-cause) | Rank upstream root-cause suspects (experimental, ADR-0213) |
| [`nova lineage export-graph`](#nova-lineage-export-graph) | GraphML/GEXF/Cypher export (experimental, ADR-0214) |
| [`nova lineage consume`](#nova-lineage-consume-experimental-cluster-scale) | Run the NATS JetStream LineageConsumer daemon (experimental, cluster-scale, ADR-0061/0066/0219) |
| [`nova insights`](#nova-insights) | Synthesized graph-intelligence report (experimental, ADR-0215) |
| [`nova graph agent`](#nova-graph-agent-experimental-adr-0124) | Reconstruct one run's execution DAG from its capsule (experimental) |

### Trust layer and evidence

| Command | Purpose |
|---|---|
| [`nova scan-secrets`](#nova-scan-secrets-capsule) | Report secrets/PII in a capsule's redaction proof |
| [`nova redact`](#nova-redact-capsule) | Re-scan and update a capsule's redaction proof |
| [`nova export-evidence`](#nova-export-evidence-capsule---output-bundlezip) | Build a signed Evidence Bundle ZIP |
| [`nova export --html`](#nova-export---html-capsule-dir) | Shareable single-file offline HTML capsule viewer (experimental) |
| [`nova verify-envelope`](#nova-verify-envelope-envelopejson---key-pem) | Verify a DSSE envelope's Ed25519 signature |
| [`nova assure`](#nova-assure-capsule-path-v025-e-10) | OWASP LLM Top 10 evidence checks |
| [`nova verify`](#nova-verify-capsule) | Verify a capsule's NovaSeal seal (experimental) |
| [`nova export-blob`](#nova-export-blob---dest-uri) | Batch capsule export with a signed completeness manifest (experimental) |
| [`nova import`](#nova-import-source) | Verified batch import of a blob export — DR restore, air-gap transfer, migration (experimental) |
| [`nova comment`](#nova-comment-add--list-experimental-adr-0121) | Append-only human annotations on capsule evidence |
| [`nova annotate`](#nova-annotate-experimental-adr-0118) | Human annotation queues — route subjects to reviewers, emit HUMAN scores |
| [`nova score submit`](#nova-score-submit-experimental-adr-0119) | Submit an externally-computed score into a capsule's append-only scores.jsonl |
| [`nova merkle-tree`](#nova-merkle-tree-document-experimental-adr-0172) | Render an Evidence Provenance Merkle proof tree from a sealed capsule's hashes (experimental) |
| [`nova trust-radar`](#nova-trust-radar-verification-experimental-adr-0173) | Trust Attestation Radar over a capsule's verification output (experimental) |
| [`nova redaction-xray`](#nova-redaction-xray-document-experimental-adr-0174) | Redaction / secret-scan X-Ray of a capsule's protection metadata (experimental) |
| [`nova assure-case`](#nova-assure-case-document-experimental-adr-0166) | Inspect an assurance-case document: validity, currency, conformance, defeaters (experimental) |
| [`nova assure-coverage`](#nova-assure-coverage-document-experimental-adr-0166) | Structural coverage of an assurance case — counts and gaps, never a grade (experimental) |
| [`nova passport`](#nova-passport-issue--verify-experimental-adr-0149) | Portable agent passport: issue + offline verify (experimental) |

### Registry, promotion, and evaluation

| Command | Purpose |
|---|---|
| [`nova register`](#nova-register-specyaml) / [`nova list`](#nova-list---type-type---status-status---stale---stale-days-n) / [`nova inspect`](#nova-inspect-nameversion) | Register and browse assets |
| [`nova promote`](#nova-promote) | Direct or maker-checker promotion through the lifecycle |
| [`nova rollback`](#nova-rollback-name---actor-id) / [`nova unregister`](#nova-unregister-nameversion) | Roll back or remove asset versions |
| [`nova prompt`](#prompt-versioning-commands-experimental-adr-0112) | Immutable, content-addressed prompt versions: register, get, list, history, diff + composition: compose, tree (experimental) |
| [`nova label`](#deployment-label-commands-experimental-adr-0113) | Deployment labels: movable named pointers to immutable asset versions; protected labels move via maker-checker (experimental) |
| [`nova eval`](#nova-eval-agentversion) | Run and compare evaluation suites |
| [`nova experiment`](#nova-experiment-run--list--show--compare-experimental-adr-0120) | Dataset-experiment harness: per-item runs + A/B regression gate (experimental) |
| [`nova approve`](#nova-approve-nameversion) | Approve a `pending_approval` asset |

### Compliance and governance exports

| Command | Regulation |
|---|---|
| [`nova export-annex-iv`](#nova-export-annex-iv-capsule---output-dir-dir---deployment-id-id) | EU AI Act Annex IV |
| [`nova export-nis2`](#nova-export-nis2-capsule---output-file---incident-id-id) | NIS2 Directive Art. 23 |
| [`nova export-ropa`](#nova-export-ropa-capsule---output-file) | GDPR Art. 30 RoPA |
| [`nova export-nist-rmf`](#nova-export-nist-rmf-capsule---output-file) | NIST AI RMF 1.0 |
| [`nova classify run`](#nova-classify-run) | EU AI Act / NIST RMF / OMB risk tier |
| [`nova audit map`](#nova-audit-map) / [`nova audit report`](#nova-audit-report) | Multi-profile control coverage |
| [`nova retention`](#retention-scheduler-adr-0134) | WORM-aware, audited retention sweep (plan/apply/status/explain) |
| [`nova forensics timeline`](#nova-forensics-timeline-experimental-adr-0155) | Incident forensic timeline over sealed evidence (experimental) |
| [`nova dsar`](#nova-dsar-assemble-experimental-adr-0161) | Subject-rights (DSAR) package assembly + SLA clock (experimental) |
| [Sector & transparency exports](#sector-and-transparency-export-commands-experimental) | 13 `nova export-*` renderers (experimental): SR 11-7 model risk, 21 CFR Part 11, RAI scorecard, EU AI Act Annex VIII, algorithm registers, public-sector disclosure, FOIA, whistleblower, citizen explanation, public incident, election disclosure, accessibility claim, control attestation |

> The compliance surfaces above produce **evidence that supports** compliance
> workflows. They do not certify or guarantee compliance with any regulation.
> See the disclaimers on each command.

#### Provenance marking — `evidence_source` (ADR-0197, experimental)

Field-group–structured exporters (`export-annex-iv`, `export-nis2`, and the
incident-store AIM/DORA projections) tag every field-group with an
**`evidence_source`** marker so a regulated consumer can tell how NovaFabric
established each value — never blurring an operator assertion into a
capsule-verified fact:

| Value | Meaning |
|---|---|
| `operator_asserted` | The value came from operator-authored input; NovaFabric did not check it. |
| `capsule_verified` | NovaFabric resolved this from a capsule and re-performed the binding. Carries an `evidence_ref` (`capsule_id` + `content_digest` [+ optional `seal_envelope_path`]) a third party can re-check offline. |
| `unverifiable` | NovaFabric attempted verification and could not complete it (missing capsule, unresolvable digest, absent seal) — reported, never downgraded to an assertion. |

The marker is **additive and optional** on the wire (a pre-ADR-0197 document
deserializes with `evidence_source: null`). As of **v0.66.0 it is applied across
the whole compliance-export layer** — the field-group exporters (v0.65.0) plus
all thirteen pure-projection families (Part 11, SR 11-7 model risk, DSAR, FOIA,
whistleblower, transparency register, Annex VIII, public-sector disclosure, RAI
scorecard, control attestation, citizen explanation, accessibility claim,
election/public-incident disclosure). Each pure projection is `operator_asserted`
with checked gaps marked `unverifiable`; a supplied capsule ref is never reported
as `capsule_verified` until a collector actually re-performs the sealed binding.
Status: **experimental**.

### Lifecycle events (experimental)

| Command | Purpose |
|---|---|
| [`nova events tail`](#nova-events-tail) | Read the local append-only lifecycle-event log |
| [`nova events emit`](#nova-events-emit) | Manually emit a lifecycle event (test a wired CI hook) |

### Server mode and storage

| Command | Purpose |
|---|---|
| [`nova serve --experimental`](#nova-serve---experimental) | Read-only local dashboard (loopback, single-user) |
| [`nova server start`](#nova-server-start) | Multi-user REST API (Postgres/SQLite, OIDC, RBAC) |
| [`nova server saml-metadata`](#nova-server-saml-metadata) | Emit the SAML SP metadata XML for IdP registration (experimental) |
| [`nova server scim-map-group`](#nova-server-scim-map-group-group-role-experimental-adr-0139-d3) | Declare IdP-group → RBAC-role mappings for SCIM provisioning (experimental) |
| [`nova server list-scim-events`](#nova-server-list-scim-events-experimental-adr-0139-d5) | Read-only SCIM provisioning audit trail (experimental) |
| [`nova server api-key`](#nova-server-api-key-create-experimental-adr-0193) | First-class API keys: create, list, revoke, rotate (experimental) |
| [`nova login`](#nova-login) / [`nova logout`](#nova-logout) | Authenticate with a NovaFabric server |
| [`nova doctor`](#nova-doctor---check-storage---check-scheduler) | Installation, storage, and scheduler/env-var diagnostics |
| [`nova migrate-to-postgres`](#nova-migrate-to-postgres) | Migrate the local SQLite registry to Postgres |
| [`nova backup`](#nova-backup-create-experimental-adr-0181) / [`nova restore`](#nova-restore-set-path-experimental-adr-0181--adr-0211) | Evidence-grade backup sets: create, verify offline, restore (local + automated pg restore, experimental) |
| [`nova support-bundle`](#nova-support-bundle-experimental-adr-0187) | Secret-safe diagnostics tarball for support (experimental) |
| [`nova audit-log`](#nova-audit-log-export-experimental-adr-0191) | Export local audit logs for SIEM ingestion (OCSF / CEF / native JSONL, experimental) |

For everything else — the knowledge graph, energy receipts, the accountability
ledger, HPC collector binaries, and the full environment-variable table — use
your browser's find, or jump to
[Environment variables](#environment-variables).

---

## Setup (v0.38)

### nova init

Initialise a local NovaFabric installation (pip install path only — docker-compose is
self-initialising via its entrypoint).

Creates the directory structure under `NOVAFABRIC_HOME` and generates an Ed25519 signing
keypair for NovaSeal.  Safe to run multiple times — existing keys are never overwritten
unless `--force` is passed.

```bash
nova init                        # uses $NOVAFABRIC_HOME (default ~/.novafabric)
nova init --home /data/nova      # custom home
nova init --force                # regenerate signing keypair
```

**Options**

| Flag | Description |
|---|---|
| `--home PATH` | Override `NOVAFABRIC_HOME` for this run |
| `--force` | Regenerate the Ed25519 keypair even if one already exists |

**Created paths**

| Path | Purpose |
|---|---|
| `$NOVAFABRIC_HOME/capsules/` | Default capsule storage |
| `$NOVAFABRIC_HOME/keys/signing_key.pem` | Ed25519 private key (mode 600) |
| `$NOVAFABRIC_HOME/keys/signing_key.pub.pem` | Ed25519 public key |
| `$NOVAFABRIC_HOME/replays/` | Replay result storage |

> **Note:** docker-compose users do not need `nova init`.  The container entrypoint
> handles first-boot setup automatically.

---

## Capture commands (v0.2)

### nova capture \<cmd...\>

Wrap a command and record its execution as a replayable run capsule.

```bash
nova capture python train.py --lr 0.001
nova capture -- python -c "import sys; sys.exit(1)"
nova capture --output-dir /mnt/runs python agent.py
```

Options:
- `--output-dir, -o PATH` — base directory for capsule storage (default: `$NOVAFABRIC_HOME/capsules/`)
- `--timeout FLOAT` — wall-clock deadline in seconds for the captured command (default: 600). Increase for long-running agents: `nova capture --timeout 3600 python agent.py`
- `--runner {local,docker,kubernetes,slurm,lsf,pbs}` — execution backend (default: `local`). Tab-completion available via `nova --install-completion`.
- `--environment TEXT` — **experimental** ([ADR-0126](./decisions.md)). Deployment-environment tag recorded verbatim on the capsule as the additive optional `deployment_environment` field (with its provenance in `environment_source`): conventionally `production` | `staging` | `development` | `test`, or any custom string (e.g. `prod-eu`, `canary` — a value outside the conventional four warns but is accepted). Precedence: this flag > the `NOVAFABRIC_ENVIRONMENT` env var > the SDK `deployment_environment=` argument; if none is supplied both fields stay absent (read as `unknown`) and the manifest is byte-compatible with earlier capsules. Never inferred from host/branch/namespace. **Distinct from the `env.lock` technical environment** (ADR-0007) — this is a delivery-lifecycle label, not a reproducibility fingerprint. Example: `nova capture --environment production -- python agent.py`
- `--experiment TEXT`, `--variant TEXT`, `--variant-source TEXT` (+ optional `--variant-label TEXT`, `--variant-assigned-at RFC3339`) — **experimental** ([ADR-0116](./decisions.md)). Record which A/B experiment and variant an **external** allocator had active for this run, as the additive optional `variant` block on the capsule manifest (`experiment_id`, `variant_id`, `assignment_source`, plus optional `variant_label`, `assigned_at`). **Record-only:** every field is copied verbatim from what you supply — NovaFabric never assigns, splits, samples, or analyzes variants (that is LaunchDarkly/Statsig/GrowthBook territory, an explicit non-goal), so `--variant-source` (the external assigner, e.g. `launchdarkly`, `statsig`, `upstream-router`) is required with the other two and is never defaulted, and `--variant-assigned-at` is never substituted with the capture time. Precedence: these flags > the `NOVAFABRIC_VARIANT*` env vars > the SDK `variant=` mapping argument, resolved atomically per source (no cross-source mixing). If nothing supplies a block it stays absent and the manifest is byte-compatible with earlier capsules. An incomplete flag set fails before capture starts; incomplete ambient env vars warn and are ignored (never block the workload). Example: `nova capture --experiment exp1 --variant arm-b --variant-source statsig -- python agent.py`
- `--session-id ULID`, `--session-sequence INT` — **experimental** ([ADR-0122](./decisions.md)). Tag the run as one ordered turn of a multi-turn *session* (a conversation or workflow of N otherwise-independent runs), recorded as the additive optional `session_id` / `sequence` back-reference fields on the capsule manifest. `--session-id` must be a ULID (create one with `nova session new`); `--session-sequence` is the zero-based turn index and requires `--session-id`. Precedence: these flags > the `NOVAFABRIC_SESSION_ID` / `NOVAFABRIC_SESSION_SEQUENCE` env vars > the SDK `session_id=`/`session_sequence=` arguments, resolved atomically per source. Absent = a standalone run, byte-compatible with earlier capsules. The `session.json` manifest (`nova session`, below) stays the **authoritative** ordered index; the capsule-side fields are advisory. **Not** the parent/child distributed-run hierarchy (ADR-0039) — that groups the workers of *one* job; a session groups *separate* runs over time. Example: `nova capture --session-id 01HZ8S9K3M4YZ2K7N9DPBYK2W0 --session-sequence 2 -- python agent.py`
- `--mark-provenance` — write a C2PA synthetic-content provenance marker (`c2pa-manifest.json`, with the `c2pa.ai.generated: true` EU AI Act Art.50 disclosure) into the capsule when the run produces model output. The marker is written before NovaSeal so it is covered by the capsule signature (ADR-0074). Opt-in; non-blocking. Example: `nova capture --mark-provenance python agent.py`
- `--fast-emit` — install capture hooks **lazily** in the workload subprocess (ADR-0092 slice B). The default path imports every present SDK (`openai`, `mcp`, `requests`, …) at startup purely to patch it — measured at ~717 ms for `openai`, ~340 ms for `mcp`, paid even if the workload never calls them. `--fast-emit` patches each SDK only if/when the workload itself imports it, so unused SDKs are never imported by capture. **Measured (warm-fs, orchestrator):** a compute-only workload **2068 ms → 464 ms (−78 %)**; an `import openai` workload **2223 ms → 1509 ms (−32 %)** — the saving scales inversely with SDK usage. Fidelity is unchanged. Runs in-process (not delegated to the warm daemon). Example: `nova capture --fast-emit python agent.py`
- `--emit-spool` — **experimental** (ADR-0092 slice C). Also write run-boundary EventEnvelope v1 records (`run.start`, `capsule.finalize`) to the local event spool (`$NOVAFABRIC_SPOOL_DIR`, default `$NOVAFABRIC_HOME/spool`) so the resident `novafabric-spool-forwarder` can drain and forward them to the collector tier over NATS JetStream. Off by default; fail-open; **edge-keyless** — signing happens at the hub, not here (hub-sign default). Runs in-process (not delegated to the warm daemon). Example: `nova capture --emit-spool python agent.py`
- `--emit-otel-genai` — **experimental** (NF-032, [ADR-0098](./decisions.md)). After capture, emit the run outward as OTel GenAI `gen_ai.*` spans (OTLP-shaped JSON) to `<capsule>/otel-genai-spans.json`: a root `invoke_agent` span plus a `chat` client span per model call and an `execute_tool` span per tool call. Every span carries `novafabric.mapping_version` and an honest `novafabric.semconv_maturity` (`stable` on LLM client spans, `development` on agent/tool spans — OTel GenAI agent spans are Development-status). Additive; runs in-process. Example: `nova capture --emit-otel-genai python agent.py`
- `--capture-content` — **opt-in** (NF-033). With `--emit-otel-genai`, include request messages in the emitted spans, routed through the ADR-0009 secret-redaction gate and size-bounded (ADR-0021 span cap). Off by default — spans carry no message/choice content unless this is set.
- `--capture-media` — **experimental, opt-in** ([ADR-0125](./decisions.md)). Store the bytes of multimodal message parts on model calls (inline base64 images/audio/documents — Anthropic `source.type: base64` blocks, OpenAI `image_url` data-URLs and `input_audio`) **content-addressed** in the capsule blob store at `outputs/<sha256>.<ext>` (deduplicated; bounded per part, default 10 MiB, `NOVAFABRIC_MEDIA_MAX_BYTES` override) and list each blob as an `Artifact` in the sealed manifest. **Off by default** (ADR-0021 §4 privacy-by-default): the part is always rewritten to a `media` reference block — IANA `media_type`, `sha256:<hex>` `content_hash` over the raw bytes, `byte_size` — and without this flag the bytes are discarded after hashing (`blob_ref: null`, reference-only). Inline base64 never lands in `model-calls.jsonl` either way; URL-referenced media is never fetched. `nova validate` re-hashes every captured blob against its recorded `content_hash` (tamper ⇒ validation fails); read back with `nova media list`. Example: `nova capture --capture-media -- python vision_agent.py`
- `--masker NAME` — **experimental** ([ADR-0135](./decisions.md)). Enable a registered PII masker for this capture, by `novafabric.maskers` entry-point name or dotted import path (repeatable). Custom maskers run **after** the built-in ADR-0009 secret scanner — built-ins always run and can never be disabled by a plugin — and every mask is attributed in `redaction-proof.json` (`masker_findings[]`). Fail-closed: a crashing, hanging, or invalid masker redacts the field (recorded in `masker_errors[]`) and never blocks the workload; an unresolvable masker aborts capture *before* the workload runs. Example: `nova capture --masker novafabric-email python agent.py`
- `--masking-config PATH` — **experimental** (ADR-0135). Path to a `masking.yaml` describing the custom masking pipeline (masker order, per-masker `timeout_ms`, `max_input_bytes`, `on_error: redact|drop`, opaque `config`). Defaults to `.novafabric/masking.yaml` when that file exists. Absent config or `enabled: false` ⇒ capture behaves exactly as ADR-0009 today, byte-for-byte. Schema: `schemas/masking-config.schema.json`. See the [developer guide](developer-guide.md#writing-a-custom-pii-masker-adr-0135--experimental) for writing a masker.

Use `--` to separate `nova capture` options from commands that contain flags:

```bash
nova capture -- python script.py -v --config cfg.yaml
```

The capsule is written to `<output-dir>/<ulid>/`. On exit (success or failure),
all artifacts are finalized and secret-scanned before the process returns.

Output:
```
✓ Capsule written: .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX  (run_id=01HXAY7M5JZ8R7K4P9DPBYK2WX)
```

Exit code mirrors the wrapped command's exit code.

**Observation log levels (ADR-0127 — experimental).** Every record in
`model-calls.jsonl` / `tool-calls.jsonl` may carry an additive optional severity
trio: `log_level` (`debug | info | warn | error`, lower-case), a secret-scanned
one-line `status_message`, and a `log_level_source` provenance
(`framework | span-status | adapter | user`). The level is a **stored forensic
attribute** — written once at capture, never acted on (no alerting, paging,
retrying, or blocking). Producers normalize framework names before writing
(`WARNING` → `warn`, `CRITICAL`/`FATAL` → `error`, `TRACE` → `debug`); an
out-of-domain value is rejected at write, and a record without the fields is
byte-identical to today — old capsules stay valid and read identically, with a
missing `log_level` read as `info` by filters (absence is preserved, never
back-filled). The OTLP trace import (server `/api/otlp/v1/traces`) maps a span
that reported `STATUS_CODE_ERROR` to `log_level: error` with
`log_level_source: span-status`. Filter recorded levels offline with
`nova query --where 'log_level >= warn'` (severity-ordered, ADR-0129). Python
capture API: `novafabric.capture.log_level` (`normalize_log_level`,
`resolve_log_level` — most-severe source wins, provenance recorded).

**Automatic capture hooks.** During `nova capture`, the following SDK calls are
recorded automatically when the corresponding library is importable:

| SDK | What is captured | File |
|---|---|---|
| `openai` | chat completion calls | `model-calls.jsonl` |
| `anthropic` | messages calls | `model-calls.jsonl` |
| `httpx` | requests to URLs in the URL registry (default: OpenAI, Anthropic, Cohere, Together, Mistral, Replicate) | `model-calls.jsonl` |
| `requests` | same registry — covers LangChain, LlamaIndex REST adapters, `boto3`/Bedrock, and any SDK that ships over `requests` | `model-calls.jsonl` |
| `mcp` (Model Context Protocol) | every `ClientSession.call_tool` invocation | `tool-calls.jsonl` (transport=mcp, full envelope) |
| `requests`/`httpx`/`aiohttp`/`urllib3` | every outbound HTTP request (AI or not), as a `NetworkEvent` | `network_events.jsonl` |
| third-party plugins | any class registered under the `novafabric.hooks` entry-point group | per the plugin's contract |

If a library isn't installed, the corresponding hook is silently a no-op. No
configuration needed.

The capsule also carries event streams written by the in-capture `EventRecorder`
when the corresponding events occur: `network_events.jsonl` (HTTP),
`file_events.jsonl` (file operations), and `human_approvals.jsonl` (approval
gates). The capsule manifest references each stream (`network_events_ref` etc.)
only when it is non-empty.

**Third-party plugins (experimental, v0.5.x).** Any Python package that
declares a class under the `novafabric.hooks` entry-point group is auto-discovered
at capture time. A failing plugin is isolated and logged — it cannot break the
built-in hooks. See [`docs/integrations/writing-a-hook-plugin.md`](integrations/writing-a-hook-plugin.md)
for the contract; the strategic context is RFC-0001.

**Customizing the URL registry (v0.5.x).** The wire-level hooks (`httpx`, `requests`) classify outbound URLs against a YAML registry vendored at `src/novafabric/capture/hooks/url_registry.yaml`. The registry is extended automatically at call time by `OLLAMA_BASE_URL` (langchain_ollama) and `OLLAMA_HOST` (ollama SDK) — non-default Ollama ports are captured without any manual configuration. For other private endpoints or providers, drop a replacement file at `~/.novafabric/url_registry.yaml`:

```yaml
schema_version: "0.1.0"
patterns:
  - match: "internal-llm.example.com"
    gen_ai_system: "internal-prod"
    transport: "http"
  - match: "api.openai.com"
    gen_ai_system: "openai"
    transport: "http"
```

A user-override file **replaces** the vendored default — copy the entries you still want from the default before editing. To override per-invocation, set `NOVAFABRIC_URL_REGISTRY=/abs/path/to/registry.yaml`. Per RFC-0001 §"Adoption / migration plan," merge semantics for overrides are deferred to v0.6 with a dedicated ADR.

**MCP capture (v0.5).** Each `call_tool` invocation produces a `tool-calls.jsonl`
record with `transport: "mcp"` and a complete `mcp` sub-object:

```json
{
  "transport": "mcp",
  "tool_name": "read_file",
  "mcp": {
    "server_name": "filesystem",
    "server_version": "1.0.0",
    "method": "tools/call",
    "request_id": "01KR4...",
    "envelope": {"jsonrpc": "2.0", "id": "01KR4...", "method": "tools/call",
                 "params": {"name": "read_file", "arguments": {...}}},
    "response_envelope": {"jsonrpc": "2.0", "id": "01KR4...", "result": {...}}
  },
  "status": "success",
  ...
}
```

The hook captures at the `ClientSession.call_tool` boundary (`experimental`, v0.5;
tested in `test_mcp_proxy.py`). For uninstrumented agents (Claude Desktop, Cursor,
Continue, third-party SDKs that do not import the Python `mcp` package), use
`nova mcp-proxy` below.

---

### nova session (experimental, ADR-0122)

Group N otherwise-independent runs into one multi-turn **session** — a
conversation or workflow whose turns are separate `nova capture` invocations.
A session is a local, content-addressed `session.json` manifest
(`$NOVAFABRIC_SESSION_DIR`, default `$NOVAFABRIC_HOME/sessions/<session_id>/`)
that references each member capsule by relative path + sha256 of its
`capsule.yaml`. It copies no capsule data and **never writes a member capsule**
(one capsule = one writer). Schema: `schemas/session-manifest.schema.json`.

**Not the parent/child hierarchy** (ADR-0032/0039): parent/child groups the
WORKER capsules of *one* distributed job; a session groups *N separate runs*
performed in sequence. The two compose — a session member may itself be a
distributed-run PARENT capsule.

```bash
# Create a session, capture its turns, group them
SID=$(nova session new --kind conversation)
nova capture --session-id "$SID" --session-sequence 0 -- python agent.py "hello"
nova capture --session-id "$SID" --session-sequence 1 -- python agent.py "and then?"
nova session add "$SID" "$NOVAFABRIC_HOME/capsules/<run-id-turn-0>"
nova session add "$SID" "$NOVAFABRIC_HOME/capsules/<run-id-turn-1>"

nova session list                 # all sessions: kind, member count, created
nova session show "$SID"          # ordered turns + aggregate stats
nova session show "$SID" --json   # machine-readable: members + stats

nova session replay "$SID"        # replay every turn in order (mocked)
nova session replay "$SID" --mode forensic --json
```

Subcommands:

- `nova session new [--kind conversation|workflow|custom] [--user REF]` —
  create an empty session manifest; prints the new `session_id` (a ULID) on
  stdout, script-friendly. `--user` is an opaque, redaction-safe actor
  reference (use a hash or handle, never a raw identifier).
- `nova session add <session_id> <capsule>` — append a capsule (directory path,
  or a run_id under the default capsule directory) as the next ordered member;
  auto-assigns `sequence`, records the content-addressed `capsule_ref`, copies
  `started_at`. `--role TEXT` labels the member (e.g. `user-turn`). A finalized
  session refuses adds unless `--reopen` is passed. Adding the same run twice
  is rejected.
- `nova session list [--json]` — enumerate sessions (directory scan of the
  sessions root; the ADR-0122 P3 SQLite index is future design), newest first.
- `nova session show <session_id> [--json] [--capsule-dir PATH]` — the ordered
  turns with per-member integrity (`ok` / `missing` when the capsule was
  deleted or moved / `tampered` when `capsule.yaml` no longer matches the
  recorded hash — reported, never fatal, never silently repaired) plus
  aggregate stats: turns, resolved/missing/tampered counts, summed duration,
  summed `usage_totals` tokens, and summed **recorded** `nova.cost` amounts by
  currency (written at capture, never recomputed). `--capsule-dir` is an extra
  base searched as `<dir>/<run_id>` for members whose recorded path moved.
- `nova session replay <session_id> [--mode forensic|mocked|semantic|exact]
  [--on-divergence stop|continue] [--continue-past-refusal] [--json]
  [--output-dir PATH] [--capsule-dir PATH]` — **experimental,
  [ADR-0123](./decisions.md) P1.** Replay every member
  capsule in ascending `sequence` order by invoking the **existing**
  single-capsule replay engine (the four ADR-0005 modes; default `mocked`)
  once per turn — no new replay mode, no bypass of the inherited
  mutating-tool/secret-gate defaults (ADR-0012). Each turn produces its own
  replay capsule under `--output-dir` (default `./.novafabric/replays`), and
  the session gets one `SessionReplayResult` record
  (`schemas/session-replay-result.schema.json`): ordered per-turn verdicts
  (`reproduced` / `diverged` / `refused`) plus one `whole_session_verdict`.
  Honesty rules: a `missing` or `tampered` member (same resolution as
  `session show`) is a hard per-turn **refusal**, halting the session unless
  `--continue-past-refusal` (the override is logged into the result); a soft
  divergence (the re-executed turn exited non-zero, or an `exact`-mode
  precondition failed → refusal) halts under the default
  `--on-divergence stop` and may be continued past with
  `--on-divergence continue`; turns after a halt are absent from the record,
  never marked `skipped`; a session with sequence gaps, or an empty session,
  refuses outright. Exit code is 0 only when the whole-session verdict is
  `reproduced`. *Still future design (planned): content-addressed state-seam
  verification between turns (P2), the composed session attestation +
  `--attest` (P4), sub-range `--from/--to` + `--dry-run` (P5), and the
  session-wide cost ceiling.*

All commands are local-first (no server, no network for
`forensic`/`mocked` replay) and read-only over member capsules.
`--session-dir PATH` on every subcommand overrides the sessions root.

### nova media (experimental, ADR-0125)

Read surface over the **content-addressed media** recorded on a capsule's
model calls ([ADR-0125](./decisions.md)). When a
model call's messages carried inline media (base64 image/audio/document
blocks), capture rewrites each part to a `media` reference block — IANA
`media_type`, `sha256:<hex>` `content_hash` over the raw bytes, `byte_size`,
`redacted`, and (with `nova capture --capture-media`) a deduplicated
`blob_ref` into the capsule's `outputs/` blob store. Schema:
`schemas/media-part.schema.json`.

```bash
nova media list <capsule-dir>          # table: type, media_type, hash, bytes, blob_ref, redacted
nova media list --json <capsule-dir>   # machine-readable, one object per media part
```

Reference-only parts (the default — ADR-0021 privacy-by-default) show
`— (reference-only)` for `blob_ref`: identity is proven by hash without
holding the bytes. Integrity checking is `nova validate`'s job — it re-hashes
every captured blob against the recorded `content_hash` and fails on a
missing or tampered blob. Local-first, read-only, no server.

### nova api-proxy (v0.6.4)

Transparent HTTP proxy that sits between a non-Python LLM client and an
upstream provider, recording every request/response pair as a
`model-calls.jsonl` entry. Implements [ADR-0026](./decisions.md).

Use when the client cannot import Python hooks — Claude Code, Cursor,
Continue.dev, or any Node/Go/Rust agent that uses a `*_BASE_URL` env var.

```bash
# Terminal 1 — start the proxy
nova api-proxy \
  --upstream-url https://api.openai.com \
  --listen 127.0.0.1:8765 \
  --capsule-dir .novafabric/runs/<run-id>

# Terminal 2 — point the client at it
export OPENAI_BASE_URL=http://127.0.0.1:8765
claude ...   # or cursor, or any LLM client that respects BASE_URL
```

Options:
- `--upstream-url URL` (required) — upstream LLM API base URL (e.g. `https://api.openai.com`)
- `--listen host:port` — listener address (default: `127.0.0.1:8765`)
- `--capsule-dir PATH` — active capsule directory; falls back to `$NOVAFABRIC_CAPSULE_DIR`, then `$NOVAFABRIC_HOME/capsules/`; auto-allocates a fresh ULID sub-dir
- `--span-id HEX` — parent OTel span id (16 hex chars); falls back to `$NOVAFABRIC_SPAN_ID`
- `--upstream-timeout FLOAT` — per-request timeout in seconds (default: `60.0`)

The proxy performs base-URL-override only — no TLS MITM, no local CA. Both
streaming (`text/event-stream`) and non-streaming responses are captured.

---

### nova mcp-proxy (experimental, v0.5.x)

Transparent stdio proxy that sits between an MCP client and an upstream MCP
server, recording every `tools/call` request/response pair into the active
capsule. Implements ADR-0015 §Secondary.

```bash
NOVAFABRIC_CAPSULE_DIR=/path/to/.novafabric/runs/01HX...
nova mcp-proxy -- npx -y @modelcontextprotocol/server-filesystem /tmp
```

Or with an explicit flag:

```bash
nova mcp-proxy --capsule-dir .novafabric/runs/01HX.../ -- /usr/local/bin/my-mcp-server --flag
```

**Claude Desktop integration.** Replace the upstream server's entry in
`claude_desktop_config.json` with a proxy invocation:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "nova",
      "args": [
        "mcp-proxy",
        "--capsule-dir", "/Users/me/.novafabric/runs/01HX...",
        "--",
        "npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"
      ]
    }
  }
}
```

Records emitted by the proxy use the same `tool-calls.jsonl` schema as the
client-wrapper hook, plus `extensions["io.novafabric.capture_method"] = "proxy"`
and `extensions["io.novafabric.mcp_protocol_version"]` (sniffed from the
upstream's `initialize` response). Verbatim JSON-RPC envelopes — request and
response — are recorded; the proxy never rewrites bytes.

**Status: experimental.** Stdio transport only. HTTP/SSE transport,
`resources/read`, `prompts/get`, and `sampling/createMessage` capture are
deferred to v0.6+. Multi-upstream routing and config files are out of scope —
one proxy invocation per upstream server.

---

## Collector buffer operations (experimental, v0.51.0, ADR-0020)

### nova collector rebuild

Rebuild the downstream store by replaying the durable JetStream event buffer
from offset 0 — the SPK-COL-1-proven recovery path (byte-equal rebuild,
per-run order preserved, RF1 broker-restart safe). Requires
`pip install novafabric[scale]` (nats-py).

```bash
nova collector rebuild --target ./rebuilt
nova collector rebuild --target ./rebuilt --stream nova-evidence \
    --subject "nova.evidence.>" --report rebuild-report.json
```

Options:
- `--target DIR` — directory to materialize per-run JSONL partitions into (required)
- `--nats-url URL` — broker (default: `$NOVA_NATS_URL` or `nats://localhost:4222`)
- `--stream NAME` — JetStream stream (default: `$NOVA_NATS_STREAM` or `nova-evidence`)
- `--subject FILTER` — subject filter (default: `$NOVA_NATS_SUBJECT` or `nova.evidence.>`)
- `--report PATH` — write the `RebuildReport` JSON (per-run sha256 digests, order flags)

Exit codes: 0 — rebuilt, order preserved · 1 — broker unreachable / drain error ·
2 — rebuilt but a per-run `seq` order violation was detected.

Events without a parseable `run_id` are routed to the `_unattributed`
partition, never dropped. See also `deploy/collector-arrow/` for the
OTel-Arrow wire profile (31.5 % measured egress reduction).

---

## Warm capture daemon (experimental, ADR-0092, Linux only)

A long-lived per-node daemon that imports `novafabric` once and serves each run
from a fork, eliminating the per-run orchestrator cold-start at fleet scale
(realizes SI-2; extends [ADR-0020](./decisions.md)).
Strictly opt-in — with no daemon running, `nova capture` behaves exactly as before.

### nova daemon start | stop | status

```bash
nova daemon start                  # foreground; bind $NOVAFABRIC_HOME/run/capture.sock
nova daemon start --max-concurrency 128
nova daemon status                 # running / not running
nova daemon stop                   # SIGTERM the running daemon
```

Run `nova daemon start` under a process supervisor (systemd, a Slurm Prolog, or
a Kubernetes DaemonSet). The socket is created mode 0600 under a 0700 directory,
owned by the agent UID; connections from other UIDs are rejected
(`SO_PEERCRED`). There is no network listener.

### novacap \<cmd...\>

Stdlib-only thin client (no `novafabric` import, so ~tens of ms to start). It
forwards the command, cwd, and environment to the daemon and passes your
stdin/stdout/stderr through, so the workload's terminal behaves normally.

```bash
novacap python agent.py
```

If no daemon is reachable, `novacap` transparently falls back to
`nova capture --no-daemon` (it never blocks your workload). For fleet use,
invoke `novacap` as the per-run entry (e.g. the Slurm `srun` wrapper) — that is
where the cold-start saving is realized.

`nova capture` also gains `--daemon/--no-daemon` (default auto): a *plain*
capture delegates to the daemon when one is reachable, but any invocation using
`--runner`, `--runner-option`, `--timeout`, `--asset`, `--mark-provenance`,
`--capture-media`, or `--output-dir` runs in-process so those flags are honored.

**Scope honesty:** the daemon removes the orchestrator's own import cold-start
(measured: `/bin/true` capture 593.9 ms → 209.6 ms, −64.7 % warm-fs). A
nova-instrumented *Python* agent still pays a one-time `sitecustomize` import
inside its own process to install the wire hooks; reducing that is a later slice.
A capsule produced via the daemon is structurally identical to one from a direct
`nova capture`.

---

## MCP supply-chain risk scanner (v0.25.1, E-9)

OWASP LLM Top 10 supply-chain checks for MCP server manifests (ADR-0069).
Both commands parse a JSON or YAML manifest that describes an MCP server and
its exposed tools.

**Reference:** `src/novafabric/cli/mcp_.py`, `src/novafabric/mcp_scanner/`.

### nova mcp scan

Scan an MCP server manifest for OWASP LLM supply-chain risks.  Exits 0 if no
findings at or above `--threshold`; exits 1 on violations; exits 2 if the
manifest file is not found.

```
nova mcp scan MANIFEST [--threshold {HIGH,MEDIUM,LOW}]
```

| Argument / Flag | Default | Description |
|---|---|---|
| `MANIFEST` | _(required)_ | Path to MCP server manifest (JSON or YAML) |
| `--threshold {HIGH,MEDIUM,LOW}` | `HIGH` | Minimum severity that causes a non-zero exit. Tab-completion available via `nova --install-completion`. |

```bash
# Fail CI on HIGH or above (default)
nova mcp scan mcp-server.json

# Fail on any finding (even LOW)
nova mcp scan mcp-server.yaml --threshold LOW
```

Prints a Rich table with columns: Tool, Category, Severity, Message.
Also prints the overall risk level and total finding count.

### nova mcp risk-report

Generate a structured OWASP LLM risk report for an MCP server manifest.

```
nova mcp risk-report MANIFEST [--format rich|json]
```

| Argument / Flag | Default | Description |
|---|---|---|
| `MANIFEST` | _(required)_ | Path to MCP server manifest (JSON or YAML) |
| `--format` | `rich` | Output format: `rich` (human-readable) or `json` (machine-readable) |

```bash
# Human-readable summary
nova mcp risk-report mcp-server.json

# JSON output for downstream tooling
nova mcp risk-report mcp-server.json --format json
```

JSON output includes per-tool risk scores and per-finding evidence strings.
Exits 2 if the manifest file is not found.

### nova mcp card (experimental, NF-039 / SEP-1649)

Publish and validate the **MCP Server Card** — the SEP-1649 discovery document
an MCP registry or client fetches from `/.well-known/mcp.json` to learn an
endpoint's protocol version, capabilities and auth **without connecting**.

```bash
nova mcp card show                                   # what nova serve publishes
nova mcp card show --json --base-url https://nova.example
nova mcp card validate card.json
nova mcp card validate https://nova.example/.well-known/mcp.json
```

**Generated, never hand-written.** The card is built from the live server
configuration, so it cannot drift from what the server actually does — a
discovery document that has drifted is worse than none, because a client trusts
it precisely for being authoritative.

What it reports:
- `protocolVersion` — MCP `2026-07-28` (the "stateless" release).
- `capabilities` — `tools`, `elicitation`, and `tasks` marked
  `{"extension": true}`. Tasks moved out of core in 2026-07-28; NovaFabric
  *detects and captures* Tasks-bearing messages but does not execute them, and
  the card says so rather than implying execution support.
- `auth` — the auth **actually in force**: `oidc` (with issuer) when OIDC is
  configured, `bearer` for the ADR-0184 local-token default, or `none` when
  `--insecure-no-auth` is set. It reports `none` explicitly rather than omitting
  the block, since silence would invite a client to assume there is some.

`nova serve` publishes the card at `GET /.well-known/mcp.json`, **unauthenticated
by design** — gating a discovery document behind the auth it describes would make
it undiscoverable. It carries only non-secret facts.

Validation is strict about structure and permissive about unknown keys: SEP-1649
is an evolving format, so an unrecognised field is forward-compatibility, but a
missing required field means a client cannot rely on the document.

Exit codes: `0` (valid), `1` (invalid card), `2` (unreadable file/URL).

### nova mcp conformance (experimental, NF-038)

Replay MCP conformance vectors and assert wire compatibility with the
2026-07-28 release.

```bash
nova mcp conformance tests/mcp_conformance/vectors/
nova mcp conformance tests/mcp_conformance/vectors/ --json
```

Each vector pins a wire behaviour against the capture shape it must produce.
They exist because **MCP capture is evidence**: a spec drift that silently
changes what gets recorded fails no ordinary test — the code runs, the capsule
writes, and the damage only appears when someone tries to replay an exchange
months later and the turn structure is gone. On failure the vector's `why`
field is printed, so a reader learns what breaks in the product rather than
just which assertion tripped.

Shipped vectors cover: a two-round SEP-2322 elicitation (rounds 1,1,2,2 under
one exchange id), concurrent interleaved exchanges (grouping must key off
JSON-RPC id, never arrival order), Tasks-as-extension passthrough, and a leg
with no id (which must *not* be captured, since correlating it under a
fabricated id would invent a grouping).

Exit codes: `0` (all passed), `1` (a vector failed), `2` (no vectors found —
a suite with no vectors proves nothing).

The `mcp-conformance` CI lane runs this plus `nova mcp card validate` on every
PR touching `proxy/`, `capture/hooks/_mcp.py`, `mcp/`, or `tests/mcp_conformance/`.

---

## Phase 3 — Distributed run commands (v0.15.0, ADR-0044/0045/0046)

Commands for parent/child capsule hierarchies (distributed Slurm/K8s runs).

### nova run new-run-id

Print a fresh ULID for use as `NOVAFABRIC_GLOBAL_RUN_ID`. Also available as top-level `nova new-run-id`.

```bash
export NOVAFABRIC_GLOBAL_RUN_ID=$(nova run new-run-id)
nova run new-run-id   # prints e.g. 01HXAY7MZPQRSTUVWXYZ
```

### nova run validate-distributed \<capsule-dir\>

Validate a distributed (parent/child) run capsule tree.

```bash
nova run validate-distributed .novafabric/runs/01HXPARENT/
nova run validate-distributed .novafabric/runs/01HXPARENT/ --parent-run-id 01HXPARENT
```

Options:
- `--parent-run-id TEXT` — override parent run_id (default: read from `capsule.json`)

Exit codes: `0` = COMPLETE, `1` = FAILED, `2` = PARTIALLY_COMPLETE.

### nova run show \<capsule-dir\>

Show a parent capsule and optionally its children in a tree view.

```bash
nova run show .novafabric/runs/01HXPARENT/
nova run show .novafabric/runs/01HXPARENT/ --with-children
nova run show .novafabric/spool/ --run-id 01HXPARENT --with-children --output json
```

Options:
- `--run-id TEXT` — parent run_id (inferred from `capsule.json` if omitted)
- `--with-children` — render the full child tree
- `--output text|json` — output format (default: `text`)

### nova run lineage \<run-id\>

Query lineage edges for a distributed run.

```bash
nova run lineage 01HXPARENT
nova run lineage 01HXPARENT --edge-types contains,spawned --output json
nova run lineage 01HXPARENT --spool-dir .novafabric/spool/
```

Options:
- `--spool-dir PATH` — capsule spool directory (default: `.`)
- `--edge-types TEXT` — comma-separated filter: `contains,spawned,delegated_to,replayed_from,member_of_session,wrote_memory,read_memory`
- `--output, -o text|json` — output format (default: `text`)

---

## Memory commands (Unreleased)

Memory provenance (ADR-0143 P1). Answers the poisoned-read question: an agent
gave a bad answer from something it remembered — where did that value come
from, and who else read it?

These commands read `memory_operations.jsonl` from a capsule. A capsule
without that file is valid; the commands report no operations rather than
failing. **No memory values are shown** — provenance is by key only, so the
graph never becomes a second copy of the content (ADR-0021 §4).

### nova memory lineage \<capsule\>

List the memory provenance edges implied by a capsule: `wrote_memory`
(run → item) on each write or update, `read_memory` (item → run) on each
read. Deletes emit no edge.

```bash
nova memory lineage .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/
nova memory lineage .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/ --output json
```

Options:
- `--output, -o text|json` — output format (default: `text`)

### nova memory trace \<capsule\> --key \<memory-key\>

Back-trace one memory key: which runs wrote it (oldest first), and which
runs read it (the blast radius).

```bash
nova memory trace .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/ --key user_prefs
nova memory trace .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/ -k user_prefs -o json
```

Options:
- `--key, -k TEXT` — memory key to trace (required)
- `--output, -o text|json` — output format (default: `text`)

**Scope, honestly:** the trace covers operations recorded *in the capsule you
point at*. A value written by run A and read by run B shows both sides only if
both runs' operations are in that capsule. Cross-capsule memory provenance
needs the merged lineage store and is **planned**, not implemented.

A read may carry `origin_run_id` — what the reading agent believed it was
reading. That is recorded as a *claim* on the edge, not as the edge's source:
the answer to "who really wrote this" comes from the `wrote_memory` edges, not
from the reader's own assertion.

---

## Replay commands (v0.3)

### nova replay \<capsule\>

Replay a captured run from its capsule directory.

```bash
nova replay .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/ --mode forensic
nova replay .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/ --mode mocked
nova replay .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/ --mode semantic
nova replay .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/ --mode exact
nova replay .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/ --dry-run
nova replay .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/ --output-dir /mnt/replays
```

Options:
- `--mode {mocked,forensic,semantic,exact,intervention}` — replay mode (default: `mocked`). Tab-completion available via `nova --install-completion`.
- `--dry-run` — report what would execute without running; writes dry-run report and exits 0
- `--allow-readonly` — permit re-invocation of `read-only` tools
- `--allow-mutating` — permit re-invocation of `idempotent-write` and `non-idempotent-write` tools
- `--allow-external-side-effects` — permit re-invocation of `external-side-effect` tools
- `--allow-unknown-mutation` — permit re-invocation of tools with `unknown` mutation class
- `--output-dir, -o PATH` — base directory for replay output (default: `.novafabric/replays/`)
- `--intervention-file PATH` — InterventionSpec YAML for `--mode intervention` (experimental, ADR-0086): one target selector (`event_index` or `span_id`) + exactly one substitution (`replace_model_response` / `replace_tool_result` / `mutate_payload`) + optional named check-functions (`fatal: true` aborts). The output capsule is diffable against the baseline with `nova diff`.

**Mode contracts:**

| Mode | Re-executes command? | Re-executes models? | Re-executes tools? | Output |
|---|---|---|---|---|
| `forensic` | No | No | No | Inspection report |
| `mocked` | Yes | From cache | From cache | Replay result |
| `semantic` | No | No | No | Similarity score (0–1.0) across model call responses |
| `exact` | No | No | No | Eligibility check: deterministic env + seeded calls |
| `intervention` | Yes | Substituted + cached | From cache | Counterfactual capsule marked `replay_mode: intervention` (experimental, ADR-0086) |

Output is written to `.novafabric/replays/<replay-ulid>/replay_result.yaml`.

```
✓ Replay written: .novafabric/replays/01HXBM1Y3K2NGH9V0RD9P0ZDC4  (replay_id=01HXBM1Y3K2NGH9V0RD9P0ZDC4  mode=forensic)
```

---

### nova diff \<capsule-a\> \<capsule-b\>

Structurally compare two run capsules. Smart-routed: if both arguments contain `@`,
falls back to the asset registry diff (v0.1 behavior).

```bash
nova diff .novafabric/runs/01HX.../ .novafabric/runs/01HY.../
nova diff cap-a/ cap-b/ --output-format json
nova diff cap-a/ cap-b/ --output-format github-annotation
nova diff cap-a/ cap-b/ --assert-no-regressions
nova diff --group-by variant runs/arm-a/ runs/arm-b/
```

Options:
- `--output-format {text,json,github-annotation}` — output format (default: `text`). Tab-completion available via `nova --install-completion`.
- `--assert-no-regressions` — exit 1 if any structural changes detected; useful as CI gate
- `--group-by variant` — **experimental** ([ADR-0116](./decisions.md)). Group the two capsules by their **recorded** A/B-variant attribution — the `(experiment_id, variant_id)` of the optional `variant` block — and label the diff as cross-arm (different groups) or within-arm (same group). A capsule without a `variant` block groups under `(no variant)`. Read-only over recorded facts: this never assigns variants and never mutates a capsule. Capsule paths only; `text`/`json` output only (`json` wraps the report in `{variant_groups, cross_arm, diff}`).

Diff sections: environment (Python, OS), model calls (aligned by span_id), tool calls
(aligned by tool_name + arg hash), output files (by hash).

---

## Diagnose commands (gap-006, ADR-0084)

### nova diagnose \<run-id\>

Attribute a **failed** run to its most likely responsible step, and label the failure
with an `AgentErrorTaxonomy` category. Runs *over* the existing lineage / causal graph
plus the captured trace — it reads `capsule.yaml`, `trace.jsonl`, `tool-calls.jsonl`, and
`model-calls.jsonl`, and (when present) the lineage store's parent/child edges. It is
read-only and writes nothing.

```bash
# Diagnose a failed run in the default capsule directory
nova diagnose run-2026-06-11-abc123

# Diagnose a capsule stored elsewhere, as JSON for tooling
nova diagnose run-xyz --capsule-dir ./capsules --output json
```

Options:
- `--capsule-dir PATH` — capsule storage directory (defaults to `$NOVAFABRIC_CAPSULE_DIR`).
- `--output {text,json}` — output format (default: `text`).
- `--intervene` — **experimental (ADR-0101)**: verify the top hypothesis with a
  counterfactual intervention replay and record an evidence-based verdict (see below).
- `--search-root-cause` — **experimental (ADR-0101 §NF-018)**: search for the earliest
  step whose correction flips the outcome (see below).
- `--max-interventions N` — bound on how many causal-root candidates
  `--search-root-cause` will test (default: `8`, hard ceiling: `50`). Only with
  `--search-root-cause`.
- `--replay-dir PATH` — base directory for the intervention replay output
  (default: `.novafabric/replays`). Only with `--intervene` or `--search-root-cause`.

Algorithm (ADR-0084): decompose the run into ordered steps (src-411 module
decomposition); a **coarse pass** scores each erroring step by explicit error signal,
**earliest-root-cause bias** (src-411 — an earlier error outranks a later downstream
symptom), and **causal-depth bias** (src-413 — a downstream `delegated_to`/`spawned`
step outranks its coordinator ancestor); a **fine pass** picks the single responsible
step and labels it.

`AgentErrorTaxonomy` categories: `MEMORY`, `REFLECTION`, `PLANNING`, `ACTION`, `SYSTEM`,
`UNKNOWN`. Scores are **relative ranking weights, not calibrated probabilities**. A run
with no error signal yields `UNKNOWN` and no fabricated culprit (exit 0); an unknown run
id exits 1.

**Hypothesis verification — `--intervene` (experimental, ADR-0101).** For the **top
hypothesis only**, `nova diagnose` auto-synthesizes an `InterventionSpec` (ADR-0086),
replays the capsule counterfactually under mocked semantics (zero-token), and appends a
verification block — hypothesis, intervention applied, original vs counterfactual
outcome, and an **evidence-based verdict, never guessed**:

```bash
nova diagnose run-xyz --capsule-dir ./capsules --intervene --output json
```

- `CONFIRMED` — the intervention replay re-executed the capsule's command and the
  original failure flipped to success (exit code 0).
- `REFUTED` — the re-execution still failed after the intervention.
- `INCONCLUSIVE` — the flip is not measurable; the reason is always recorded (no
  hypothesis, the original run did not fail, the hypothesis class is not auto-mappable,
  the capsule has no re-executable command, or the replay aborted).

The auto-mappable subset in this slice is **model-call hypotheses only**: the corrective
edit clears the error signal at the implicated model call via a `mutate_payload`
substitution. Tool/span/run hypotheses report an honest
`cannot auto-intervene for this hypothesis class` reason. The intervened output capsule
is hard-marked `replay_mode: intervention` and referenced from the verdict block.
Fidelity bound (ADR-0086): the verdict tests control-flow and downstream handling of the
recorded run, not fresh model behavior. Without `--intervene`, `nova diagnose` remains
read-only and its output is unchanged (an unverified hypothesis is never presented as a
proven root cause).

**Counterfactual root-cause search — `--search-root-cause` (experimental, ADR-0101
§NF-018).** Widens `--intervene` from testing only the top hypothesis into a search: it
sweeps the §NF-019 causal-root candidates — already ranked shallowest/earliest-first,
which is exactly the pruning the ADR calls for over a naive linear sweep of every step —
running a bounded number of zero-token intervention replays (default `--max-interventions
8`, hard ceiling `50`) until one confirms an outcome flip. The first `CONFIRMED` candidate
is the decisive root cause; every attempt (confirmed, refuted, or honestly unmappable) is
recorded, so the search itself is auditable, not just its winner:

```bash
nova diagnose run-xyz --search-root-cause --max-interventions 5 --output json
```

Same auto-mappable subset as `--intervene` (model-call hypotheses only). When no
candidate flips the outcome within the bound, the result says so explicitly
(`bounded: true`) rather than silently looking exhaustive. Composes with `--intervene` —
both flags can be passed together.

---

## Offline metrics query (experimental, ADR-0129)

### nova query (experimental, ADR-0129)

Aggregate metrics across **many local Run Capsules** with a small, bounded,
declarative query — offline, read-only, no server, no network, no raw SQL. The
grammar is a closed allow-list (spec: `capsule-query-dsl-v0`): exactly four
clauses (`select`, `where`, `group_by`, time window); anything outside the
allow-list is a hard parse error. Metrics are the *already-recorded* facts —
cost from the capsule's recorded `nova.cost` (ADR-0066), tokens/latency from
`model-calls.jsonl`, eval scores from `scores.jsonl` — never recomputed.

```bash
# How many capsules are on disk?
nova query --select 'count()'

# Average cost and run count of one asset in production, per model, last 7 days
nova query \
  --select 'avg(cost) AS avg_cost, count() AS runs' \
  --where 'asset = summarizer AND deployment_environment = production' \
  --group-by model --since 7d --json

# p95 latency by run status
nova query --select 'p95(latency) AS p95_ms' --group-by status

# The same query as a saveable JSON/YAML object (flags override file fields)
nova query --query-file q.yaml --capsule-dir ./capsules
```

Options:
- `--select EXPR[, EXPR…]` — **required** (here or in the query file). Aggregates:
  `count()`, and `sum`/`avg`/`min`/`max`/`pXX` (percentile, e.g. `p95`) over
  `cost`, `total_tokens`, `prompt_tokens`, `completion_tokens`, `latency`, or
  `score[<name>]` (eval scores are per named metric — bare `score` is rejected).
  Optional `AS alias`.
- `--where 'FIELD OP VALUE [AND …]'` — filters over the allow-listed dimensions
  `asset`, `deployment_environment`, `variant`, `log_level`, `model`, `model_id`,
  `status`, `tag` with operators `=`, `!=`, `<`, `<=`, `>`, `>=`, `IN (...)`.
  `AND` only (no `OR`/nesting in v0). `log_level` comparisons use severity order
  (`debug < info < warn < error`) over the per-observation severity recorded on
  model-call records (ADR-0127); a record without `log_level` reads as `info`.
- `--group-by DIM[,DIM…]` — group by the same allow-listed dimensions.
- `--since 7d|24h|P30D|<RFC 3339>` / `--until <RFC 3339>` — time window over the
  capsule `created_at` (since inclusive, until exclusive; default: all history → now).
- `--limit N` — max result rows (default 100, hard ceiling 10 000; group
  cardinality is also capped at 10 000 — a pathological `group_by` is refused).
- `--order-by '<alias> [asc|desc]'` — sort before truncation (default: first
  select, descending), so a `--limit` top-N is stable.
- `--query-file q.json|q.yaml` — the equivalent query object; flags override its
  fields. This is the saveable form later features build on (ADR-0130, planned).
- `--json` — the canonical machine-readable result (the default table is a
  rendering of it): `columns`, `rows`, `row_count`, `truncated`, `time_window`,
  and an `index {engine, built_at, capsule_count}` provenance block.
- `--capsule-dir PATH` — capsule storage directory (defaults to `$NOVAFABRIC_CAPSULE_DIR`).
- `--rebuild-index` — discard the cached rows and rebuild them from a full scan.
- `--no-cache` — ignore the persistent index entirely and scan every capsule.
  The authoritative answer, and slower.

**The persistent index (ADR-0225).** Since the cache landed, a capsule is
re-parsed only when it has actually changed, which is worth roughly **5×** on the
scan (measured: 1841 ms → 375 ms at 10,000 capsules). Two things follow:

- `nova query` **writes to `$NOVAFABRIC_HOME/query-index.db`**. It is a command
  that previously wrote nothing at all, so this is called out rather than left to
  be discovered. **Your capsules are still never written to** — they are signed
  evidence, and the cache deliberately lives outside them.
- A cache that is missing, stale or damaged costs you time, never correctness:
  any problem falls back to the full scan. If you ever suspect it, `--no-cache`
  gives the authoritative answer and `--rebuild-index` replaces the stored rows.

Semantics: `count()` counts **distinct capsules**; other aggregates run over the
matched model-call records, skipping capsules where the metric is absent (SQL
null semantics); a dimension a capsule never recorded matches no filter. Empty
result is `rows: []`, exit 0 — not an error. Parse errors exit 2; execution
errors exit 1.

Aggregation runs on an in-process index, built fresh on each query. **The
default engine is stdlib SQLite**, even when DuckDB is installed. Measured
(ADR-0222 OQ-3, `bench/query/MEASURED_CEILING.md`), DuckDB reaches only
*parity* — 0.86× at 1,000 capsules, 1.00× at 20,000 — because the capsule
directory scan is 86-89% of total query time and the index build is ~3%. SQLite
needs no extra dependency and ties or wins everywhere measured, so it is the
default. Results are identical either way, and the engine actually used is
reported in `index.engine`.

> DuckDB's fast path needs `pyarrow`, which `[query]` deliberately does not
> install (~154 MB, for no measurable gain here). `[scale]` and `[serve]`
> include it. With DuckDB selected but pyarrow absent, the index build falls
> back to a row-by-row insert that is ~20× slower than SQLite, and says so once
> in the log.

To use DuckDB anyway (it needs `pip install 'novafabric[query]'`):

```bash
NOVAFABRIC_QUERY_ENGINE=duckdb nova query --select 'count()' --group-by model
```

If that variable is set but DuckDB is not installed, the query still runs on
SQLite and logs a warning naming the extra — a read-only query is never failed
over an engine preference.

### nova view (experimental, ADR-0130)

Saved views: name a `nova query` once, persist it as a small human-readable
file, and re-run it anywhere. A view is **data, not code** — a verbatim
ADR-0129 query object plus optional advisory display preferences, stored one
file per view under `.novafabric/views/<view_id>.yaml` (JSON equally valid;
override the directory with `--views-dir` or `NOVAFABRIC_VIEWS_DIR`). View
files are meant to be committed to the project repo: sharing a view is
committing a file, reviewing a view is reading a diff. No server, no network.
Wire contract: `schemas/saved-view.schema.json`; spec: `saved-views-v0`.

```bash
# Save a query under a name (validated fail-closed at save time)
nova view save failed-runs-7d \
  --select 'count() AS runs' --where 'status = error' --since 7d \
  --description 'Failed runs, last week' --tags 'triage'

# Re-run it — exactly `nova query` over the stored query
nova view run failed-runs-7d --json

# Manage views
nova view list
nova view show failed-runs-7d
nova view rm failed-runs-7d
```

Subcommands:
- `nova view save NAME` — persist the given query under `NAME`. Accepts the
  same query flags as `nova query` (`--select`, `--where`, `--group-by`,
  `--since`, `--until`, `--limit`, `--order-by`, `--query-file`); the query is
  compiled through the ADR-0129 parser **before** anything is written — an
  invalid query exits 2 and no file is created. The stored query is the
  normalized query object (the same one `nova query --json` echoes back).
  `--view-id` sets an explicit slug id (otherwise derived from `NAME`:
  lowercase, non-alphanumeric runs collapsed to `-`); `--description`,
  `--columns`, `--sort 'FIELD [asc|desc], …'`, `--format table|json|csv`, and
  `--tags` set optional metadata and advisory display preferences; `--created-by`
  records an author (never inferred); `--json` writes a `.json` file instead of
  YAML. An existing `view_id` is refused unless `--force` (which preserves
  `created_at` and sets `updated_at`). On success the command prints the file
  path and the view's **content hash** (`view_hash`, `sha256:` over the
  canonical definition — name, query, display, tags; timestamps and author
  excluded) so a report can record exactly which view version produced it.
- `nova view run NAME` — load the view (by id or name) and execute its stored
  query via the ADR-0129 engine; semantically identical to `nova query` with
  the same clauses (invariant I2). `--capsule-dir`, `--json`, and
  `--format table|json|csv` behave as on `nova query`; a command-line format
  always overrides the saved `display.format` (display preferences are
  advisory, invariant I3 — `display.columns`/`display.sort` shape the table/CSV
  rendering only, never the canonical JSON, and never which capsules match).
  The JSON result is the `nova query` result plus a
  `view {view_id, name, view_hash}` provenance block. A stale view whose query
  no longer parses fails loudly (exit 2), never silently.
- `nova view list` — list saved views (`view_id`, name, description, tags);
  `--json` adds `view_hash` per view. An empty or missing views directory
  prints `(no views)`, exit 0. Corrupt view files are skipped with a warning —
  a broken view never blocks any other operation.
- `nova view show NAME` — print the stored view without executing it (YAML,
  with `view_hash` and path as trailing comments; `--json` for machine output).
- `nova view rm NAME` — delete the view's file.

Exit codes: parse/validation errors (bad query, bad `--view-id`, bad
`--format`) exit 2; missing/corrupt view, existing view without `--force`, and
execution errors exit 1; an empty result set exits 0.

### nova trend (experimental, ADR-0131)

Compute an **offline trend report** — one metric (`cost`, `score:<name>`, or
`latency`) bucketed by `day` / `week` (UTC calendar buckets) or `asset`
(categorical) over the local capsule directory. A read-only **snapshot
artifact**, never a live monitor: no server, no network, no thresholds, no
notifications (the "act on a threshold" concern is the ADR-0136 budget gate).
Runs on the ADR-0129 extraction/filter path; spec: `trend-report-v0`
(`schemas/trend-report.schema.json`).

```bash
# Weekly cost over the last 60 days (TrendReport JSON to stdout)
nova trend --metric cost --group-by week --since 60d

# Daily GAIA score, written to a file
nova trend --metric score:gaia --since 14d --json trend.json

# p99 latency per asset, plus a single self-contained static HTML artifact
nova trend --metric latency --stat p99 --group-by asset --html trend.html

# Use a saved view (ADR-0130) as the capsule selector
nova trend --metric cost --view prod-summarizer
```

- `--metric cost|score:<name>|latency` — exactly one metric per report (v0).
  Cost sums the recorded `nova.cost` amounts (USD; a capsule with an
  unresolvable currency is skipped with a warning, never silently converted);
  `score:<name>` averages the named suite from `scores.jsonl`; latency takes a
  point statistic per bucket (`--stat p50|p95|p99|mean`, default `p95`,
  latency only).
- `--group-by day|week` emits **every** calendar bucket in the `--since`
  window — a bucket with no capsules appears as an explicit gap
  (`value: null`, `n: 0`), never dropped. `--group-by asset` is categorical
  (`bucket_start: null`, ordered by asset id; capsules without an asset fall
  under `(none)`).
- `--since` (default `30d`) / `--until` (default now) bound the input window.
- `--view NAME` runs a saved view's `where` clause as the capsule selector;
  `view` and `filters` are echoed into the report for provenance.
- Output: canonical `TrendReport` JSON to stdout by default; `--json FILE`
  writes it to a file; `--html FILE` also writes **one** self-contained static
  HTML file — inline CSS, a stdlib pre-rendered inline SVG chart (line for
  time, bars for asset; gaps render as visible breaks), the exact JSON
  embedded inline, **no JavaScript and zero external requests** (opens from
  `file://`).
- Unreadable capsules and capsules missing the metric are tallied in
  `skipped_count` with a warning — the report is never aborted by one bad
  capsule. An empty directory or window succeeds with an empty series.

Exit codes: usage errors (unknown metric/group-by, `--stat` on a non-latency
metric, unparsable or pathological window) exit 2; runtime errors (missing
capsule directory, unresolvable saved view) exit 1; an empty result exits 0.

---

## Capsule content search (experimental, ADR-0204)

### nova search (experimental, ADR-0204)

Full-text search over the **redacted** text of local run capsules — "which run
mentioned invoice INV-2291?", "where did the agent call `rm -rf`?". Local-first:
reads the registry DB (SQLite FTS5) and capsule directory directly, no server
needed. The index is populated automatically when capsules are ingested by
`nova serve` / `nova ingest-capsule`; pre-existing capsules need one backfill.

```bash
nova search "invoice INV-2291"                 # all terms must match (AND)
nova search "rm -rf"                           # operators match literally
nova search "inv*"                             # trailing * = prefix search
nova search "429" --status failure --json      # filter + machine output
nova search "timeout" --stream trace           # restrict to one stream
nova search --reindex                          # backfill/repair the index
nova search --reindex --all                    # force full re-extraction
```

Options:
- `--limit N` (default 20, max 200) — max runs returned.
- `--json` — emit results as JSON (same item shape as the API `matches`).
- `--since` / `--until` / `--status` — filter on the run metadata index.
- `--stream {model-call-messages|tool-call-arguments|trace|capsule-yaml}` —
  restrict to one source stream.
- `--reindex [--all]` — idempotent backfill/repair from the capsule directory;
  also garbage-collects rows for deleted capsules. `--all` forces
  re-extraction of already-indexed capsules.
- `--capsule-dir` / `--db-path` — override the standard locations.

Guarantees (ADR-0204): only **post-redaction** capsule bytes are indexed, and
the corpus is a strict subset of the secret scanner's targets
(`model-calls.jsonl`, `tool-calls.jsonl`, `trace.jsonl`, `capsule.yaml`);
query input is quoted before it reaches the FTS5 MATCH grammar, so `OR`,
`NEAR`, `-`, `:` match literally. `NOVA_CONTENT_INDEX=off` disables indexing
at ingest.

Exit codes: `0` (success, even with no matches), `1` (FTS5 unavailable in
this Python's SQLite, or runtime error), `2` (usage error / empty query).

---

## Offline drift and tool-schema analysis (experimental, ADR-0147/0148)

Read-only, offline detectors over sealed capsule evidence. Each command loads a
JSON document from a path, prints a terminal report (or a machine-readable one
with `--json`), and never mutates any capsule. A flagged drift, silent failure,
or breaking schema change is a **detector observation to investigate, never a
verdict or gate** — the commands exit `0` whether or not anything is flagged,
and `2` only on missing or malformed input. This first slice takes the
samples/runs directly in the document; a collector that reads them from sealed
capsules over a `--baseline`/`--window` range is a documented follow-on.

### nova drift detect (experimental, ADR-0147)

Compute an offline two-sample drift record from supplied samples — no model
re-invocation, zero token cost.

```bash
nova drift detect drift.json
nova drift detect drift.json --json
```

Options:
- `<document>` (positional, required) — JSON: `{kind: output|behavioral, ...samples..., threshold}`. `output` documents carry `{metric, statistic, baseline[], window[], window_meta, baseline_id?}`; `behavioral` documents carry `{dimension, distance, baseline, window}`
- `--json` — emit the drift record as JSON

Exit codes: `0` (record rendered, drifted or not), `2` (missing or malformed input).

---

### nova drift silent-failure (experimental, ADR-0147)

Flag runs that reported success but whose quality signal fell below a
threshold. A silent failure is surfaced for review — it is a detector
observation, not a determination that the run failed.

```bash
nova drift silent-failure runs.json
nova drift silent-failure runs.json --json
```

Options:
- `<document>` (positional, required) — JSON: `{runs: [{run_id, status, quality_signal}], threshold, success_statuses?}`
- `--json` — emit the silent-failure report as JSON

Exit codes: `0` (report rendered, whether or not any run is flagged), `2` (bad input).

---

### nova drift root-cause (experimental, ADR-0147)

Link an observed drift to the input(s) that changed between a baseline and a
drifted run. Diffs the two runs' lineage provenance ancestors down to the
model/prompt/tool/dataset that changed. The result is a **correlation, not a
cause** — a hypothesis to investigate.

```bash
nova drift root-cause rc.json
nova drift root-cause rc.json --json
```

Options:
- `<document>` (positional, required) — JSON: `{baseline: [{kind, ref}], drifted: [{kind, ref}], kinds?}`
- `--json` — emit the root-cause hypothesis as JSON

Exit codes: `0` (rendered, whether or not anything changed), `2` (bad input).

---

### nova toolschema impact (experimental, ADR-0148)

Report which historical runs break under a new tool schema: validates each
recorded tool call's arguments against the proposed JSON Schema using the
shipped ADR-0128 validator — it does not reimplement validation.

```bash
nova toolschema impact calls.json --new-schema new.json
nova toolschema impact calls.json --new-schema new.json --json
```

Options:
- `<document>` (positional, required) — JSON: `{tool_id, tool_calls: [{run_id, arguments}]}`
- `--new-schema PATH` (required) — the new JSON Schema to test past runs against
- `--json` — emit the `schema_impact` report as JSON

Exit codes: `0` (report rendered), `2` (missing or malformed input).

---

## Lineage commands (v0.4)

### nova lineage provenance

```
nova lineage provenance <ref> [--depth N] [--kind run|asset|artifact] [--edge-type TYPES] [--with-facets] [--output text|json]
```

Show what the given node depends on (forward traversal).

Options:
- `--edge-type TYPES` — comma-separated edge types to include. Valid values: `contains`, `spawned`, `delegated_to`, `replayed_from`. Omit for all edge types.
- `--with-facets` — print column-level lineage facets carried by traversed edges (experimental, ADR-0090): table, column names (never values), read/write access, extraction confidence. Also available on `blast-radius`.

```bash
nova lineage provenance my-agent --edge-type spawned,delegated_to
nova lineage provenance 01HX... --with-facets
```

### nova lineage blast-radius

```
nova lineage blast-radius <ref> [--depth N] [--kind run|asset|artifact] [--edge-type TYPES] [--output text|json]
```

Show what depends on the given node (backward traversal).

Options:
- `--edge-type TYPES` — comma-separated edge types to include. Same valid values as `provenance`. Invalid types exit 1.

```bash
nova lineage blast-radius my-model --edge-type contains,replayed_from
```

### nova lineage replay-chain

```
nova lineage replay-chain <run-id> [--output text|json]
```

Show the replay chain back to the original captured run.

### nova lineage time-travel

```
nova lineage time-travel <ref> --asof <iso8601> [--kind run|asset|artifact] [--output text|json]
```

Show the lineage state of a node as of a given timestamp.

### nova lineage import

```
nova lineage import <path>
```

(Re-)index lineage from a capsule directory or a parent runs/ directory.

### nova lineage metrics

**Experimental (ADR-0212).**

```bash
nova lineage metrics [--top N] [--output text|json]
```

Rank structurally critical nodes over the whole local lineage graph: degree, PageRank
(bounded power iteration), betweenness (seeded sampling above 2 000 nodes — the output
says when it sampled), and articulation points ("single points of failure"). Scores are
descriptive rankings for attention, not calibrated importance. Oversize graphs fail
loudly at the whole-graph read bound instead of silently truncating.

### nova lineage root-cause

**Experimental (ADR-0213).**

```bash
nova lineage root-cause <run-id> [--depth N] [--capsule-dir DIR] [--output text|json]
```

Walk the failed run's upstream provenance and rank suspect nodes with bounded additive
signals: error evidence (cues shared with ADR-0084), recency decay, an edge-confidence
multiplier, and failure correlation across sibling failed runs. With `--capsule-dir`,
the responsible run is additionally step-attributed via the `nova diagnose` engine. No
error signal upstream → `responsible: null`; the command never fabricates a culprit.

### nova lineage export-graph

**Experimental (ADR-0214).**

```bash
nova lineage export-graph [--format graphml|gexf|cypher] [-o FILE] [--ref R [--kind K] [--depth N]]
```

Byte-stable export of the lineage graph (whole graph, or one node's provenance +
blast-radius neighbourhood via `--ref`) for enterprise graph tooling: GraphML/GEXF for
Gephi/yEd, idempotent Cypher `MERGE` statements for Neo4j. Topology and attributes
only — seal/signature material never travels with this export; nested edge facets are
carried as a `facets_json` string attribute.

### nova insights

**Experimental (ADR-0215).**

```bash
nova insights [--top N] [--output table|json|markdown] [-o FILE] [--cost-db PATH]
```

One synthesized report over the captured lineage graph: top hubs and articulation
points (ADR-0212), seeded Louvain communities, orphan nodes, health ratios
(largest-component fraction, orphan ratio), and best-effort cost hotspots (lineage
node payloads, or a `--cost-db` evidence-fabric DuckDB aggregate). Unavailable data
sources are reported as unavailable, never fabricated. `--output markdown -o FILE`
produces a shareable weekly artifact.

### nova lineage emit-openlineage

```
nova lineage emit-openlineage <path> [--output TARGET] [--with-facets] [--otel-correlation]
```

Emit capsule runs as OpenLineage 2.0.2 events.

- `<path>` — a single capsule directory or a parent `runs/` directory (all capsules inside are emitted)
- `--output, -o TARGET` — destination: `-` for stdout, an `http://...` URL, or a file path
- `--with-facets` — **(NF-036, [ADR-0096](./decisions.md))** attach NovaFabric custom run facets to the COMPLETE event: `novafabric_capsule` (capsule id/run id/hash), `novafabric_eval` (verdict `passed`/`failed`/`n/a` + suite + metrics), `novafabric_policy` (promotion gate + decision), and the standard `executionParameters` facet (reproducibility run params). Additive — a consumer that ignores custom facets sees unchanged core OL events. Every facet is schema-validated before emission.
- `--otel-correlation` — also attach the `novafabric_otel_correlation` facet (`trace_id`/`span_id`) when the capsule records them, so a lineage node links to its OTel GenAI spans (NF-037). Implies `--with-facets`.

If `--output` is omitted, the target is resolved from `OPENLINEAGE_URL` → `OPENLINEAGE_FILE` → stdout.

```bash
# Stdout
nova lineage emit-openlineage .novafabric/runs/01HX.../  --output -

# HTTP endpoint (Marquez, Atlan, etc.)
nova lineage emit-openlineage .novafabric/runs/ --output http://marquez:5000

# With NovaFabric custom facets + OTel correlation
nova lineage emit-openlineage .novafabric/runs/01HX.../ --with-facets --otel-correlation

# Environment variable
OPENLINEAGE_URL=http://marquez:5000 nova lineage emit-openlineage .novafabric/runs/
```

**Dashboard equivalent:** Lineage tab → Export OpenLineage Events panel (returns JSON preview + copy button).

---

### nova lineage consume (experimental, cluster-scale)

```
nova lineage consume [--nats-url URL] [--kuzu-path DIR] [--subject SUBJECT]
                      [--batch-size N] [--fetch-timeout S]
                      [--flush-batch-size N] [--flush-interval-s S]
```

Runs the `LineageConsumer` NATS JetStream pull consumer (cap-006) in the
foreground: pulls capsule events, derives lineage edges, and bulk-COPYs them
into KuzuDB on a size-or-time flush trigger. Runs until interrupted (Ctrl-C).
This is the cluster-scale deployment path — local-mode capture never requires
this command; lineage is written directly by the local `SqliteLineageStore`.

Requires the `scale` extra (nats-py) and the `scale-kg` extra (kuzu):
`pip install 'novafabric[scale,scale-kg]'`.

- `--nats-url` — defaults to `$NOVA_NATS_URL`
- `--kuzu-path` — defaults to `$NOVA_KUZU_PATH` or `.nova/kg/lineage.kuzu`
- `--subject` — NATS subject pattern (default `novafabric.lineage.>`)
- `--batch-size` — max messages pulled per NATS fetch (default 500)
- `--fetch-timeout` — seconds per NATS fetch (default 1.0)
- `--flush-batch-size` — edges accumulated before a KuzuDB COPY flush (default
  2,000 — see `bench/lineage/MEASURED_CEILING.md`: the 10K-edges/second
  write-throughput gate passes at this batch size and above, and falls short
  below it)
- `--flush-interval-s` — max seconds between flushes even below
  `--flush-batch-size` (default 15.0)

```bash
nova lineage consume
nova lineage consume --nats-url nats://hub.example.com:4222 \
  --kuzu-path /var/lib/novafabric/lineage.kuzu
nova lineage consume --flush-batch-size 5000 --flush-interval-s 30
```

**Not exactly-once:** a NATS message is acked immediately once its edges are
extracted, before the size-or-time flush actually persists them to KuzuDB. If
a flush fails, the buffered edges for that flush are dropped and logged, not
redelivered — lineage is derived, non-authoritative data, not the evidence
chain, so this tradeoff favors simplicity over exact-once delivery guarantees.

**Producer taxonomy gap — resolved 2026-07-30 ([ADR-0220](./decisions.md)):**
the real event producer is `capture/orchestrator.py` (forwarded verbatim by
the Go `novafabric-spool-forwarder`, which never inspects `event_type`
itself — the earlier framing of this as a Go-vs-Python taxonomy problem was
itself found to be wrong during the fix). It now emits the canonical
`RunStarted`/`RunCompleted`/`RunFailed` event types with `parent_run_id`
populated, so this command correctly derives `SPAWNED_BY` edges from real
captured runs — verified end-to-end by
`tests/scale_architecture/test_lineage_consumer.py::TestRealProducerEndToEnd`.
**Remaining gap:** `ArtifactProduced`/`ArtifactConsumed` edges still require
a producer that emits those event types, which none currently does — only
run-boundary lineage is available from the NATS path today.

---

## Agent execution graph (experimental, ADR-0124)

### nova graph agent (experimental, ADR-0124)

```
nova graph agent <capsule-dir> [--format json|dot|mermaid] [-o FILE] [--digest] [--stats]
```

Reconstruct **one run's execution DAG** — which model call invoked which tools,
how calls nested under OTel spans, and the observed sibling order — from records
the capsule already holds (`model-calls.jsonl` + `tool-calls.jsonl` +
`trace.jsonl`). A **projection, not a capture**: read-only, offline, works on any
capsule ever captured (including v0.2), adds no capsule field. Distinct from
`nova lineage` (cross-run causation) and `nova kg` (fleet security graph); spec:
`agent-execution-graph-v0` (`schemas/agent-execution-graph.schema.json`).

The output is **content-addressed**: nodes and edges are canonically sorted and
hashed into a `graph_digest`, so the same capsule always yields byte-identical
JSON — comparing two digests is a cheap "did the control-flow shape change"
check. Three deterministic edge types only (`span_parent`,
`agent_invokes_tool`, `follows`); nothing is inferred beyond what the spans
encode. Calls with no recoverable parentage attach to a synthetic `root` node
with an explicit `reconstruction_note` (`missing_parent`, `orphan_tool_call`,
`unlinked_span`) — never a silent heuristic repair.

```bash
# Canonical JSON graph to stdout
nova graph agent .novafabric/capsules/<run_id>

# Only the content hash — for verify/diff pipelines
nova graph agent <capsule> --digest

# Shape summary: node/edge counts, max depth, max fan-out
nova graph agent <capsule> --stats

# Text exports for rendering elsewhere
nova graph agent <capsule> --format mermaid -o run-graph.mmd
nova graph agent <capsule> --format dot | dot -Tsvg > run-graph.svg
```

- `--format json|dot|mermaid` — `json` is canonical; `dot`/`mermaid` are
  deterministic text exports of the same nodes and edges (no new dependency).
- `--digest` — print only the `graph_digest` line.
- `--stats` — print only `{node_count, edge_count, max_depth, max_fan_out}`.
- `-o / --output FILE` — write to a file instead of stdout.

Exit codes: `0` success, `1` not a readable capsule directory, `2` bad flags.
A partially malformed capsule still yields a best-effort graph plus notes.

---

## Lineage store operations (Phase 6, E-3..E-5)

Commands for migrating lineage edges between backends and generating deployment
profiles for cluster-scale lineage infrastructure.

**Reference:** `src/novafabric/cli/lineage_migrate.py`, `design/adr/0053-lineage-at-scale.md`.

### nova lineage-store migrate

Migrate lineage edges from a Parquet file or an ObjectCapsuleStore to a SQLite
store.  Runs dry-run by default; pass `--commit` to actually persist.

```
nova lineage-store migrate [PARQUET] [--db PATH] [--commit|--dry-run]
    [--no-validate] [--from-ocs] [--ocs-tenant TEXT] [--ocs-run-ids TEXT]
    [--ocs-data-dir PATH]
```

| Flag | Default | Description |
|---|---|---|
| `PARQUET` | _(optional)_ | Source Parquet file (deprecated; use `--from-ocs` for the ADR-0022 canonical path) |
| `--db` | `lineage.db` | Target SQLite database path |
| `--commit` / `--dry-run` | dry-run | Commit to target store (default is dry run) |
| `--no-validate` | off | Skip post-load divergence check |
| `--from-ocs` | off | Migrate from ObjectCapsuleStore (ADR-0022 canonical path) |
| `--ocs-tenant` | `""` | OCS tenant identifier (required with `--from-ocs`) |
| `--ocs-run-ids` | `""` | Comma-separated run IDs to migrate (required with `--from-ocs`) |
| `--ocs-data-dir` | `.` | Local OCS data directory for the fallback adapter |

```bash
# Parquet migration (deprecated)
nova lineage-store migrate edges.parquet --db lineage.db --commit

# OCS-backed migration (ADR-0022 canonical)
nova lineage-store migrate --from-ocs \
    --ocs-tenant acme \
    --ocs-run-ids run-001,run-002 \
    --db lineage.db --commit
```

Exit codes: 0 = success, 1 = argument error, 2 = divergence detected.

### nova lineage-store profile

Print a docker-compose YAML deployment profile for the chosen lineage backend.

```
nova lineage-store profile [--target kuzudb-vertical|janusgraph-minimal]
    [--node-size TAG] [--rf N] [--image-tag TAG]
```

| Flag | Default | Description |
|---|---|---|
| `--target` | `kuzudb-vertical` | Profile target: `kuzudb-vertical` or `janusgraph-minimal` |
| `--node-size` | `16g-ram-500g-nvme` | Node size tag for `kuzudb-vertical` profiles |
| `--rf` | `3` | Cassandra replication factor (for `janusgraph-minimal`) |
| `--image-tag` | *(unset)* | **Deprecated.** For `kuzudb-vertical`, sets the single `nova-lineage` image tag (falls back to `latest` if unset). For `janusgraph-minimal`, overrides *all three* images (janusgraph/cassandra/novafabric) to the same tag — prefer leaving it unset so each image uses its own independently-pinned default (JanusGraph `1.1.0`, others `latest`) |

```bash
# KuzuDB vertical profile (single-node, NVMe-optimised)
nova lineage-store profile --target kuzudb-vertical --node-size 32g-ram-1t-nvme

# JanusGraph minimal cluster profile (Cassandra RF=3)
nova lineage-store profile --target janusgraph-minimal --rf 3
```

Prints a complete `docker-compose.yml` to stdout.  Pipe to a file or `kubectl apply`.

---

## Metadata DB recovery (BQ-013)

### nova rebuild-metadata-db

Rebuild the metadata database from the manifest chain log using
checkpoint-based replay.  Disaster-recovery path when the metadata DB is lost;
completes in minutes regardless of chain length (AC-1, BQ-013).

```
nova rebuild-metadata-db [--prefix TEXT] [--target-db PATH]
    [--backend local|s3|minio|ceph_rgw|azure_blob] [--data-dir PATH]
```

| Flag | Default | Env var | Description |
|---|---|---|---|
| `--prefix` | `""` (all tenants) | — | Tenant or `tenant/run_id` prefix to scan |
| `--target-db` | `nova-metadata-rebuild.db` | — | Path to the output SQLite database |
| `--backend` | `local` | `NOVA_OCS_BACKEND` | Storage backend: `local`, `s3`, `minio`, `ceph_rgw`, `azure_blob` |
| `--data-dir` | _(optional)_ | — | Local backend: path to the object store root directory |

```bash
# Rebuild from local storage (all tenants)
nova rebuild-metadata-db --target-db recovered.db

# Rebuild a single tenant from an S3 backend
NOVA_OCS_BACKEND=s3 nova rebuild-metadata-db \
    --prefix acme \
    --target-db acme-recovered.db
```

Prints per-step progress, total capsules found, elapsed time, and any integrity
warnings.  The output SQLite file is ready for use as a replacement metadata DB.

**Reference:** `src/novafabric/cli/rebuild.py`, `src/novafabric/object_capsule_store/rebuild.py`.

---

## Trust layer commands (v0.4)

### nova scan-secrets \<capsule\>

Read-only inspection of a capsule's `redaction-proof.json`. Reports findings without
modifying the capsule. Designed as a CI gate.

```bash
nova scan-secrets .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/
nova scan-secrets <capsule> --fail-on critical    # exit 2 on any critical finding
nova scan-secrets <capsule> --fail-on high        # exit 2 on critical or high
nova scan-secrets <capsule> --json                # emit the proof as JSON to stdout
```

Options:
- `--fail-on {critical,high,medium,low,info}` — exit 2 if any finding has severity at or above the threshold. Tab-completion available via `nova --install-completion`.
- `--json` — emit the full redaction proof as parseable JSON

Exit codes: `0` (clean or below threshold), `1` (missing proof / invalid input), `2` (threshold exceeded).

---

### nova assure \<capsule-path\> (v0.25, E-10)

Run OWASP Top 10 for LLM (2025) evidence checks against a captured capsule.

```bash
nova assure .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/
nova assure .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/ --format json
nova assure .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/ --format json > assurance.json
```

Performs 10 deterministic checks against capsule artifacts:

| Check | OWASP Category | What is verified |
|---|---|---|
| LLM01 | Prompt Injection | `redaction-proof.json` captured |
| LLM02 | Sensitive Info Disclosure | `secrets_found == 0` in redaction proof |
| LLM03 | Supply Chain | `env.lock` + `novafabric_version` recorded |
| LLM04 | Data/Model Poisoning | total input tokens < 100,000 |
| LLM05 | Improper Output Handling | `tool-calls.jsonl` present when `tool_call_count > 0` |
| LLM06 | Excessive Agency | tool/model call ratio < 10 (warn) / < 50 (fail) |
| LLM07 | System Prompt Leakage | no `system_prompt` in DEBUG model-call records |
| LLM08 | Vector/Embedding Weakness | `replay.yaml` policy captured |
| LLM09 | Misinformation | `trace.jsonl` has ≥1 span |
| LLM10 | Unbounded Consumption | `duration_ms` < 1,800,000 (warn) / < 3,600,000 (fail) |

Options:
- `--format {rich,json}` — Output format: `rich` (default, colored table) or `json`. Tab-completion available via `nova --install-completion`.

Exit codes: `0` = all PASS/WARN/SKIP, `1` = any FAIL, `2` = capsule path not found.

---

### nova redact \<capsule\>

Re-scan a capsule and update its `redaction-proof.json`. Two modes:

**Mode 1 — metadata-only** (no re-scan, edits the existing proof in place):

```bash
nova redact <capsule> --mark-unsafe-skip <finding_id> --rationale "test fixture"
nova redact <capsule> --clear-unsafe-skips
```

**Mode 2 — re-scan** (default):

```bash
nova redact <capsule>
nova redact <capsule> --strategy-override openai-api-key:hash
nova redact <capsule> --strategy-override pii-email:drop
```

Options:
- `--strategy-override RULE:STRATEGY` — `mask` (default), `hash` (SHA-256 first-8), or `drop` (remove). Repeatable.
- `--mark-unsafe-skip FINDING_ID` (with `--rationale`) — record a user-acknowledged false positive. The capsule's existing `unsafe_skips` are preserved across re-scans by default.
- `--clear-unsafe-skips` — drop all `unsafe_skips` before writing.
- `--review` — interactive triage (requires a TTY; v0.4 stub: exits 2 if no TTY, full triage UI **planned**).

`unsafe_skips` block `nova export-evidence` unless `--allow-unsafe-skips` is passed.

---

### nova comment add | list (experimental, ADR-0121)

Append-only human annotations on capsule evidence — a reviewer's note, an auditor's
rationale, a "this dataset was mislabelled upstream" flag — stored as one JSON line per
comment in an optional `comments.jsonl` inside the capsule, mirroring `scores.jsonl`
(ADR-0099). Comments are **asynchronous evidence annotations, not live chat**: they are
immutable, travel with the capsule, work fully offline, and are covered by the capsule
Merkle root at Evidence-Bundle time exactly like any other capsule file. A capsule
without `comments.jsonl` stays valid (additive-only; the Run Capsule schema is untouched).
Record contract: [`schemas/comment.schema.json`](../schemas/comment.schema.json).

There is no in-place edit and no destructive delete (append-only invariant): an **edit**
is a new comment with `--reply-to <comment_id>`; a **delete** is a **tombstone** comment
(`--tombstone --reply-to <id>`) — the retracted bytes stay in the file and `list` simply
hides them by default.

Scope: single capsule.

```bash
nova comment add --subject <capsule-dir> --body "retrieval pulled a stale doc — blocking promotion"
nova comment add --subject <capsule-dir> --body "confirmed fixed" --reply-to 01HXB0K3M7QM4YZ2K7N9DPBYK2
nova comment add --subject sha256:<hex> --capsule <capsule-dir> --kind span --body "this span is the culprit"
nova comment add --subject <capsule-dir> --tombstone --reply-to 01HXB0K3... --body "retracting: false alarm"
nova comment list --subject <capsule-dir>            # write order; tombstoned comments hidden
nova comment list --subject <capsule-dir> --all      # full audit trail, raw records
nova comment list --subject <capsule-dir> --json
```

`--subject` accepts a capsule directory (resolved to a stable content-addressed root
digest that excludes the annotation stream itself, so every comment on a capsule shares
one subject) or a `sha256:<hex>` digest (then `--capsule <dir>` locates `comments.jsonl`).
`asset://<type>/<name>@<version>` subjects annotate **registry assets** rather than
capsules. They are stored in the registry's `asset_comments` table (assets have no
capsule log), but are the same records with the same append-only semantics — threads,
tombstones and the secret gate behave identically:

```bash
nova comment add --subject asset://model/summarizer@1.2.0 --kind asset \
  --body "eval regression on the finance set — do not promote"
nova comment list --subject asset://model/summarizer@1.2.0
```

`--subject` and `--kind` must agree: an `asset://` ref requires `--kind asset`, and
`--kind asset` requires an `asset://` ref (exit 2 otherwise).

`nova comment thread <comment-id> --subject <capsule-dir>` resolves the reply chain
containing a comment, root first, indenting each level (or `--json` for the raw array):

```bash
nova comment thread 01HX... --subject <capsule-dir>
nova comment thread 01HX... --subject <capsule-dir> --json
```

Resolution is bounded and defensive, because an append-only log can legitimately hold
malformed links: a reply whose parent is missing is treated as an **orphan root**, not an
error, and a reply **cycle** is reported (exit 1) rather than looped over. An unknown
comment id exits 1.

**Secret hygiene is mandatory (ADR-0009):** the body passes the same secret-scan rules as
every other capsule text before storage. A body that trips a secret pattern is **refused**
(exit 3, and the secret is never echoed back); `--redact` masks the match in place
(`[REDACTED:<rule_id>]`) and records `redaction_applied: true` on the comment. A body
emptied by redaction is refused.

Options (`add`):
- `--body TEXT` (required) — free text, secret-scanned
- `--author ID` — defaults to the local username (self-asserted in local mode)
- `--kind {capsule,span,run,score}` — what the subject digest addresses (default `capsule`)
- `--reply-to COMMENT_ID` — thread a reply / supersede a prior comment
- `--tag LABEL` — repeatable free labels
- `--tombstone` — retract the comment named by `--reply-to`
- `--redact` — mask secret matches instead of refusing
- `--json` — print the stored record

Options (`list`): `--all` (raw audit trail, no tombstone folding), `--json`.

Exit codes: `0` (ok), `1` (invalid `comments.jsonl`), `2` (usage / unsupported subject),
`3` (body refused by the secret gate).

---

### nova annotate (experimental, ADR-0118)

Human annotation queues: route review subjects (capsules or spans) to human reviewers,
track each item through `pending → assigned → completed` (with an optional maker-checker
`checker_pending` step), and land every completed annotation as a typed, `HUMAN`-source
`Score` in the subject capsule's append-only `scores.jsonl` — the exact evidence path
every other score uses (ADR-0099). The metrics a reviewer grades are **score configs**
(ADR-0117), referenced by name; a queue cannot be created until each criterion is
registered with `nova eval score config add`. Queue/item workflow state lives in the
local registry SQLite DB (local-first, offline); annotation is entirely off the live
workload path. Record contracts:
[`schemas/annotation-queue.schema.json`](../schemas/annotation-queue.schema.json) and
[`schemas/annotation-queue-item.schema.json`](../schemas/annotation-queue-item.schema.json).

```bash
nova eval score config add --name factuality --value-type boolean --description "Factually correct?"
nova annotate queue create --name hallu-review --criteria factuality [--require-checker] [--policy manual]
nova annotate queue add hallu-review --capsule <capsule-dir>          # enqueue the capsule
nova annotate queue add hallu-review --capsule <dir> --subject sha256:<hex> --kind span
nova annotate queue populate hallu-review [--dry-run] [--json]        # enqueue everything matching the selector
nova annotate queue list [--json]                                     # queues + per-state progress
nova annotate queue show hallu-review [--json]
nova annotate next --queue hallu-review --as reviewer:a               # claim (round-robin)
nova annotate next --item <item_id> --as reviewer:a                   # claim a named item (manual)
nova annotate submit <item_id> --score factuality=true [--as reviewer:a] [--skip-criterion NAME]
nova annotate confirm <item_id> --as reviewer:b                       # checker step (SoD)
nova annotate skip <item_id> --note "out of scope"                    # terminal, writes no score
```

`queue populate` enqueues every stored capsule matching the queue's `subject_selector`
(all present keys ANDed). It is **idempotent** — a subject already queued is skipped, so
re-running after new capsules land adds only the new ones, which makes it safe to
schedule. A `sample` fraction is applied **deterministically** (hash of the subject
digest), so repeat runs choose the same subjects: an auditor asking "why was this run
reviewed and that one not?" gets a stable answer rather than "chance". A span-scoped
queue refuses auto-population, since spans are not enumerable from the capsule store.

`submit` validates every queue criterion against its score config **before any write**
(all-or-nothing: a bad value, missing criterion, or missing capsule appends nothing and
the item stays `assigned`, retryable), then appends one `HUMAN` score per criterion with
`evaluator_id` = the assignee and `eval_card_digest` = the config's `content_digest`
(content-addressed provenance). The submission — and, on `--require-checker` queues, the
checker's confirmation — is Ed25519-signed with the reviewer's keyring key (ADR-0058
paths); fingerprints and signatures are carried in the item record's `extensions`. The
checker's identity **and** key fingerprint must both differ from the maker's (separation
of duties, ADR-0003 pattern). A bare `--capsule` enqueue uses the capsule's
content-addressed root digest **excluding the annotation streams** (`scores.jsonl`,
`comments.jsonl`), so successive review rounds share one stable subject.

**Planned (not yet implemented):** evidence-bundle/NovaSeal sealing of completed
scores (`--seal` is recorded and warns; ADR-0118 P5); server-mode multi-user assignment.
(Corrected: automatic queue population by evaluating `subject_selector` over stored
capsules — described above as `nova annotate queue populate` — shipped in ADR-0118 P2;
an earlier version of this note called it not-yet-implemented, which was stale.)

Exit codes: `0` (ok, including an empty-queue `next`), `1` (validation / state /
separation-of-duties refusal), `2` (usage).

---

### nova score submit (experimental, ADR-0119)

Submit one **externally-computed** evaluation score — from a CI job, a third-party
LLM-as-judge, a human tool, a batch scorer — into a target capsule's append-only
`scores.jsonl`. This is the documented ingest surface over the same evidence-grade
`Score` record every other score uses (ADR-0099): NovaFabric records the value, it
never runs the evaluator (no model call, no paid API). Fully offline: no server, no
internet. The same validation core backs the SDK (`novafabric.scores.submit`) and the
optional server endpoints (`POST /api/runs/{run_id}/scores` on the dashboard,
`POST /v0/capsules/{run_id}/scores` on `nova server`). Contracts:
[`schemas/score-submission-request.schema.json`](../schemas/score-submission-request.schema.json)
and [`schemas/score-submission-response.schema.json`](../schemas/score-submission-response.schema.json).

```bash
nova score submit --capsule <capsule-dir> \
  --name answer_correct --value 0.87 --value-type numeric \
  --evaluator "ci://acme/repo#judge@v3" --source code \
  --subject sha256:<hex> --eval-card sha256:<hex> \
  [--score-id <ulid>] [--supersedes <score_id>] [--run-id <ulid>] [--json]
```

Validation is **fail-closed** — on any rejection nothing is written:

- **Well-formed**: all shipped `Score` invariants (ULID ids, `sha256:` digests,
  value/value-type agreement).
- **Config-valid** (ADR-0117): a value violating the score config governing `--name`
  is rejected; with no matching config the score is accepted and the `--json`
  envelope reports `config_bound: false`.
- **Subject anchored**: `--subject` must be a digest that exists in the target
  capsule (its stable root digest, current Merkle root, manifest digest, or any
  digest recorded in the capsule's top-level evidence files).
- **Append-only**: a correction is a *new* record whose `--supersedes` names an
  existing `score_id`; prior lines are never edited or removed.
- **Idempotent**: re-running with the same client-minted `--score-id` and body is a
  no-op that echoes the stored record; the same `--score-id` with a different body
  is refused (idempotency-key collision).

On success the appended (or replayed) record is echoed to stdout as JSON
(`--json` wraps it in the full `{score, idempotent_replay, config_bound}` envelope).
On rejection a structured `{"error": <code>, "message": …}` line goes to stderr.
Exit codes: `0` (ok, including an idempotent replay), `1` (rejection), `2` (usage).

---

### nova export-evidence \<capsule\> --output \<bundle.zip\>

Build a signed Evidence Bundle ZIP per [ADR-0011](./decisions.md). Signs with a
local ed25519 key; optionally publishes the DSSE envelope to a Rekor transparency log
when `--sigstore` and `NOVA_REKOR_URL` are both set.

```bash
nova export-evidence <capsule> --key ~/.novafabric/keys/ed25519.pem --output evidence.zip
nova export-evidence <capsule> --key key.pem --output e.zip --allow-unsafe-skips
```

Options:
- `--key PATH` (required) — PEM-encoded ed25519 private key
- `--output, -o PATH` (required) — output ZIP path
- `--allow-unsafe-skips` — permit export when redaction-proof.json contains `unsafe_skips`
- `--sigstore` — after building the bundle, publish the DSSE envelope to the Rekor
  transparency log. Requires `NOVA_REKOR_URL` env var; without it, prints a skip
  warning and exits 0. Network errors are warnings only (fail-open).
- `--with-custody` (experimental, ADR-0095) — embed the FRE-902(14) court-admissibility
  blocks (`chain_of_custody` + `self_authentication`, additive optional `$defs` on
  `evidence-bundle.schema.json`) in the bundle manifest, built from the hash-chained
  audit log + capsule Merkle root. Invariant I3: unwitnessed fields are `null` +
  `operator_declared`, never fabricated.
- `--custodian ID` — custodian identity recorded in the custody block (used with
  `--with-custody`).
- `--custodian-provenance {novaseal-identity|oidc|operator_declared}` — provenance of the
  custodian identity; only `novaseal-identity`/`oidc` can make a bundle
  `self-authenticating` (used with `--with-custody`).
- `--dsse` (experimental, NF-029/[ADR-0096](./decisions.md)) —
  also emit a **standard DSSE outer envelope** wrapping the final bundle manifest, written to
  `<bundle>.dsse.json` (payloadType `application/vnd.novafabric.bundle+json`, signed with the
  same `--key`). The manifest chains custody over every artifact via their sha256 +
  `manifest_hash`, so the envelope transitively covers the whole bundle. Verify it with
  `nova verify-envelope` or stock `cosign verify-blob-attestation`. **Opt-in:** without this flag
  the bundle output is byte-for-byte unchanged. Emitted after any `--timestamp` rewrite, so it
  wraps the final canonical manifest.

When the capsule has an `energy-receipts.jsonl` stream, a signed `PREDICATE_ENERGY` energy
attestation is added to the bundle automatically (experimental, ADR-0093).

Generate a fresh keypair from Python:

```python
from pathlib import Path
from novafabric.evidence.signing import generate_keypair
generate_keypair(Path("./keys"))    # writes ed25519.pem + ed25519.pub.pem
```

Bundle layout:

```
evidence.zip
├── manifest.json                       # index + manifest_hash
├── run-capsule/                        # byte-identical capsule copy
├── lineage-subgraph/edges.jsonl        # lineage for this run
├── attestations/                       # in-toto Statement v1, DSSE-enveloped
│   ├── run.intoto.json
│   ├── redaction.intoto.json
│   └── lineage.intoto.json
├── signatures/                         # raw signature + public key per envelope
│   ├── *.sig    (raw ed25519 signature bytes)
│   └── *.cert   (PEM public key)
├── schemas/                            # all 10 JSON schemas vendored
└── README.md                           # human-readable verification recipe
```

Verification requires only `sha256sum` and an ed25519 verifier. The vendored
schemas make the bundle a time capsule — verification works in 2031 against the
same schemas the bundle was built with.

---

### nova export --html \<capsule-dir\>

**experimental** — Shareable single-file offline capsule viewer (ADR-0140).
Renders one capsule's non-sensitive summary as exactly **one** self-contained
HTML file — inline CSS, no JavaScript, zero external requests — that a
recipient with no NovaFabric install and no server opens in any browser from
`file://` (email it, attach it to a ticket, carry it on a USB stick).

```bash
nova export --html .novafabric/runs/01HX.../
nova export --html .novafabric/runs/01HX.../ -o run.html --title "Nightly agent run"
```

Options:
- `--html` — select the single-file HTML renderer (required; the only renderer in v0)
- `--output, -o PATH` — output HTML file path (default: `<capsule_dir>.html` next to the capsule dir)
- `--title STR` — optional page title override

The page shows the capsule header (run id, capture mode, run window, agent
identity and capsule hash when the capsule carries them), model calls (model,
tokens, latency, status), tool calls (tool, `mutation_class`, status,
duration), eval scores from `scores.jsonl`, and lineage references from
`lineage.jsonl`. The underlying `CapsuleView` summary JSON
(`schemas/capsule-view.schema.json`) is embedded inline in a
`<script type="application/json" id="capsule-view-data">` block for
View-Source inspection.

Guarantees and limits:

- **Projection only.** The summary contains only fields the capsule already
  exposes — never tool arguments/results or message bodies, and it never adds
  content the capsule does not carry.
- **Redaction is inviolable.** The exporter reads the already-redacted capsule;
  redaction markers (`[REDACTED:…]`) render verbatim and there is no un-redact
  flag (ADR-0009).
- **A human view, not a verifier.** The page states plainly that real
  verification is `nova verify` (or the signed Evidence Bundle from
  `nova export-evidence`, which this command complements, never replaces).
- **Read-only and non-blocking.** Unreadable sections are skipped with a
  warning; the file is still produced.
- **Planned (not yet implemented):** `--include-verification` (rendered
  seal/signature status panel) and `--graph` (inlined Sigma.js lineage view) —
  ADR-0140 P3/P4.

Exit codes: `0` (file written), `1` (capsule not found), `2` (`--html` not passed).

---

### nova evidence (experimental, v0.50.0, ADR-0087)

Audit-grade evidence operations beyond the signed bundle: completeness
assertion, criterion→evidence bindings, and re-performance attestation.
All three are additive and optional — pre-existing bundles still verify.

```bash
# What does this capsule claim to contain? (per-stream counts, drop counters,
# capture level, time window, active hooks)
nova evidence completeness <run-id-or-capsule-path>
nova evidence completeness <capsule> --key ed25519.pem -o completeness.intoto.json

# Bind audit-profile controls to the capsule facts that evidence them
nova evidence bind <capsule> --profile eu-ai-act-high-risk
nova evidence bind <capsule> --profile gdpr --key ed25519.pem

# Replay the run and emit a DSSE-signed re-performance attestation
nova evidence attest-replay <capsule> --key ed25519.pem --mode mocked

# Also emit a signed determinism certificate (ReplayAttestation, ADR-0094 B)
# and anchor the attestation digest into the capsule's accountability ledger
nova evidence attest-replay <capsule> --key ed25519.pem --mode exact --certify --anchor
```

Options:
- `--key PATH` — PEM-encoded ed25519 private key; when given, output is a DSSE-signed in-toto envelope (predicate types `novafabric.io/{completeness,criterion-binding,reperformance}/v0`). `attest-replay` always requires it.
- `--profile ID|PATH` — audit profile (`eu-ai-act-high-risk`, `gdpr`, `iso42001`, `nist-ai-rmf`, `scientific-reproducibility`, `soc2-type2`, or a YAML path).
- `--mode {mocked,forensic,semantic,exact}` — replay mode for `attest-replay` (default `mocked`); the verdict (`exact`/`semantic-match`/`mismatch`) is recorded separately from the mode. Exit 2 on `mismatch`.
- `--certify` — (experimental, ADR-0094 B) after the re-performance attestation, also emit a DSSE-signed determinism certificate (`ReplayAttestation`, predicate `novafabric.io/replay-attestation/v0`) to `replay-attestation-<capsule>.intoto.json`. `determinism_class` ∈ `BIT_EXACT`/`BOUNDED_EQUIVALENT`/`NON_DETERMINISTIC` is derived honestly from the capsule's recorded pins (`model-calls.jsonl` `gen_ai.*` + `env.lock`): a missing seed/model digest or a non-`exact` verdict downgrades to `NON_DETERMINISTIC` with the reasons recorded — hosted models without a digest and pinned seed never classify `BIT_EXACT`. When a ledger checkpoint exists (or `--anchor` is given) the certificate back-links via `ledger_ref`.
- `--anchor` — (experimental, ADR-0094 A) append the attestation digest to `<capsule>/attestations.jsonl` and seal it via the accountability-ledger path (per-stream sidecar hash chains + a signed checkpoint with a local finalize anchor), making the attestation tamper-evident. Verify with `nova ledger verify`. Existing `.jsonl` evidence streams are never modified.
- `--output, -o PATH` — output file.

Without `--certify`/`--anchor`, `attest-replay` behavior is unchanged (including exit 2 on mismatch).

---

### nova verify \<capsule\>

Verify a capsule's cryptographic seal: DSSE envelope signature, RFC 3161 timestamp
integrity, and Merkle log inclusion proof. Requires NovaSeal configuration
([ADR-0041](./decisions.md)). **experimental** —
shipped v0.10; see [v0.10.0 release notes](releases/v0.10.0.md).

```bash
nova verify .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/
nova verify <capsule> --seal-config ~/.novafabric/novaseal.yaml
NOVAFABRIC_SEAL_CONFIG=./novaseal.yaml nova verify <capsule>
```

Checks (all three must pass for exit 0):

| Check | What is verified |
|---|---|
| Signature | DSSE envelope signature — ECDSA P-256 against the certificate embedded in the envelope |
| Timestamp | RFC 3161 TSR integrity — SHA-256 of the DSSE bytes matches the messageImprint in the TSR |
| Merkle log | Inclusion proof — leaf hash at the stored index recomputes the Merkle root |

Options:
- `--seal-config PATH` — path to `novaseal.yaml` (default: `~/.novafabric/novaseal.yaml`; env: `NOVAFABRIC_SEAL_CONFIG`)
- `--backend [local|sigstore]` — verification backend (default: `local`). Use `sigstore` to verify a Sigstore bundle stored alongside the capsule; requires `pip install novafabric[sigstore]`
- `--capsule-id TEXT` — capsule ID for Sigstore bundle lookup (required when `--backend sigstore`)
- `--home PATH` — `NOVAFABRIC_HOME` override (used for Sigstore bundle path)

Exit codes: `0` (all checks pass), `1` (any check fails or .seal/ missing).

Example output (all passing):

```
NovaSeal verification: 01HXAY7M5JZ8R7K4P9DPBYK2WX
  ✓ Signature (DSSE ECDSA P-256): OK
  ✓ Timestamp (RFC 3161): OK
  ✓ Merkle log inclusion: OK

signature_ok=True, timestamp_ok=True, log_integrity_ok=True
```

Capsules produced without a NovaSeal config have no `.seal/` directory; `nova verify`
exits 1 with an informational message (not an error — unsigned capsules are valid).

**Batch export manifests:** when the argument is an `export-manifest.json` file
(instead of a capsule directory), `nova verify` runs the offline batch-export
verification instead — DSSE ed25519 signature, batch digest, and every member's
bytes at the destination (`--public-key <pem>` required; optional `--dest` override).
See [`nova export-blob`](#nova-export-blob---dest-uri) (experimental, ADR-0141).

**Dashboard equivalent:** SealTab → Capsule Integrity Verify panel (run ID input + DSSE/TSA/Merkle result display).

**NovaSeal configuration (`~/.novafabric/novaseal.yaml`):**

```yaml
profile: local        # "local" | "aws_kms" | "azure_kv" | "gcp_kms"
key_path: ~/.novafabric/seal.key    # ECDSA P-256 PEM private key
cert_path: ~/.novafabric/seal.crt   # X.509 PEM certificate
tsa_url: https://freetsa.org/tsr    # FreeTSA for dev; QTSP for EU regulated
merkle_db: ~/.novafabric/novaseal-merkle.db  # SQLite Merkle log (default)
```

Key environment variables: `NOVAFABRIC_SEAL_CONFIG` (path to yaml),
`NOVAFABRIC_SEAL_DB_PATH` (Merkle DB override). Full configuration reference
including Docker/SLURM patterns and path resolution order: see
[docs/novaseal-configuration.md](novaseal-configuration.md).

Generate a test key pair:

```bash
openssl ecparam -name prime256v1 -genkey -noout | \
  openssl pkcs8 -topk8 -nocrypt -out ~/.novafabric/seal.key
openssl req -new -x509 -key ~/.novafabric/seal.key -days 365 \
  -out ~/.novafabric/seal.crt -subj "/CN=NovaSeal-Local"
```

---

### nova verify-envelope \<envelope.json\> --key \<pem\>

Verify the Ed25519 signature on a **standard outer envelope** — a DSSE envelope produced
by NovaFabric's `envelopes/` emitters (Evidence Bundle wrap, in-toto capsule Statement, or
SLSA provenance). Gives the same verdict a third party gets from stock
`cosign verify-blob-attestation`, with no NovaFabric dependency on the verifier side.
**experimental** — NF-029/030/031, [ADR-0096](./decisions.md).

```bash
nova verify-envelope bundle.dsse.json --key seal.pub.pem      # public-key PEM
nova verify-envelope capsule.intoto.json --key seal.key       # private-key PEM also accepted
```

Options:
- `--key PATH` — PEM-encoded Ed25519 key (public or private; the public half is derived from a private PEM). **Required.**

Exit codes: `0` (signature verifies), `1` (signature failure, tampered payload, or malformed
envelope), `2` (key is not an Ed25519 key / usage error).

The envelope's inner artifact is carried verbatim as the DSSE `payload` (**wrap, don't
replace**) — the original bundle/statement bytes are never rewritten. This command checks the
outer signature only; use `nova verify` for a capsule's full NovaSeal seal (signature +
RFC 3161 timestamp + Merkle inclusion). `nova export-evidence --dsse` emits a bundle DSSE
envelope today; `nova promote direct --slsa-provenance` emits a DSSE-signed SLSA v1 provenance
statement for the promotion (add `--slsa-ml-profile` for the SLSA-for-ML profile with dataset
hashes, seeds, and the gating eval-verdict digest — experimental).

---

### nova incident (experimental, v0.50.0, ADR-0088)

First-class incident records with an EU AI Act Art. 73 deadline clock and
two export regimes (OECD AIM + NIS2). Self-contained: the record lives in
`$NOVAFABRIC_HOME/incidents.db` (override: `NOVAFABRIC_INCIDENTS_DB_PATH`).
Deadline outputs are operational aids, not legal advice.

```bash
nova incident open --title "Tool misuse in prod agent" \
  --classification unauthorized_tool_use --severity high \
  --occurred-at 2026-06-10T08:00:00+00:00 --aware-at 2026-06-11T09:30:00+00:00 \
  --run-id 01HX...

nova incident list                       # status + most-pressing deadline
nova incident status inc-0123abcd4567    # full record + Art. 73 deadline table
nova incident export inc-0123abcd4567 --format aim     # OECD AIM JSON
nova incident export inc-0123abcd4567 --format nis2    # NIS2 report from the stored record
```

Notes:
- Deadlines anchor at `--aware-at` (fallback `--occurred-at`): 15 days standard (Art. 73(2)), 10 days death (Art. 73(4)), 2 days widespread infringement / critical infrastructure (Art. 73(3)). Classification keywords `death`, `widespread`, `critical_infrastructure` select the stricter clocks; an empty classification emits all candidates (fail-informative).
- Lifecycle is forward-only `open → reported → closed`; `export` transitions an open incident to `reported`. A wrongly-opened incident is closed, never deleted.
- `attestation_ref` can reference an ADR-0087 re-performance attestation (post-incident replay evidence).

---

## Trust visualization and assurance commands (experimental, v0.59–v0.61)

Read-only projections over trust-layer evidence. Each command loads a JSON
document, renders a terminal report (or `--json`), and prints only references,
states, and short hash prefixes — never evidence bodies, field values, or full
hashes.

### nova merkle-tree \<document\> (experimental, ADR-0172)

Render an Evidence Provenance Merkle proof tree from a sealed capsule's hashes:
`leaf → intermediate → seal-root → tsr`. The tree is derived from NovaSeal's
canonical layer enumerator, so the recomputed root matches the sealed root
byte-for-byte. Full leaf hashes are never printed — only short prefixes
(ADR-0009); leaf labels are field paths, never values.

> **Why there is no `--capsule` here**, when `nova trust-radar` and
> `nova redaction-xray` both have one. The proof tree visualises **NovaSeal's**
> Merkle tree, which uses a different construction from the RFC 6962 tree behind
> `capsule_merkle_root` (pairwise with odd-duplicate padding, vs. `0x00`/`0x01`
> prefixes and a power-of-2 split). An adapter that computed leaves with one and
> combined them with the other produced a root matching *neither* — verified on a
> real capsule and reverted rather than shipped, because two honest-looking roots
> that disagree are worse than no proof tree. A correct adapter must use NovaSeal
> leaves and is only meaningful for a **sealed** capsule. See ADR-0172.

```bash
nova merkle-tree tree.json
nova merkle-tree tree.json --json --capsule-id run-42
```

Options:
- `<document>` (positional, required) — JSON with `leaf_hashes` (+ optional `labels` / `sealed_root` / `tsr_hash`)
- `--capsule-id TEXT` — capsule id to label the tree with. **A label only** — it selects nothing
- `--json` — emit the proof tree as JSON

Exit codes: `0` (rendered — sealed+verified, or unsealed), `1` (seal-root mismatch — tamper evidence), `2` (missing or malformed input).

---

### nova trust-radar \<verification\> | --capsule \<dir\> (experimental, ADR-0173)

Render a Trust Attestation Radar from a capsule's verification output. The
input is a JSON object of the seven Trust-Layer guarantees (`signature_ok`,
`timestamp_ok`, `log_integrity_ok`, `redaction_coverage`, `secret_scan_clean`,
`policy_pass`, `eval_gate_pass`) — for example the summarized output of
`nova verify` plus the evidence-bundle scan flags. Any absent/null guarantee
becomes an `n/a` axis (e.g. an unsealed capsule has no `signature_ok`).

With `--capsule` the guarantees are derived from the capsule itself instead of
being supplied by hand (`trust/capsule_flags.py`).

**Absent is not false.** A guarantee the capsule cannot evidence renders `n/a`,
never `fail`:

| Guarantee | Source | When unavailable |
|---|---|---|
| `signature_ok` / `timestamp_ok` / `log_integrity_ok` | NovaSeal verification over `.seal/` | Unsealed capsule, or no NovaSeal profile configured → `n/a`. **Unverified is not failed**, and "could not verify" is not "verification failed". |
| `redaction_coverage` / `secret_scan_clean` | the capsule's `redaction-proof.json` | Captured without the masking pipeline → `n/a` |
| `policy_pass` / `eval_gate_pass` | **never derived from a capsule** | Always `n/a`. These are registry/promotion facts; a capsule records that a run happened, not whether an asset later cleared a gate. Inferring them would attach a promotion verdict to the wrong artifact. |

A capsule with no findings reports `redaction_coverage: 1.0` — nothing sensitive
to cover — rather than `0.0`, which would paint a clean capsule red.

```bash
nova trust-radar --capsule .novafabric/runs/01HX.../
nova trust-radar verify.json
nova trust-radar verify.json --json --capsule-id run-42
```

Options:
- `<verification>` (positional) — path to a JSON object of the 7 verification guarantees. Omit when using `--capsule`
- `--capsule PATH` — capsule directory to derive the guarantees from
- `--capsule-id TEXT` — label the radar with this capsule id. **A label only** — it selects nothing; use `--capsule` to read from a capsule
- `--json` — emit the radar model as JSON

Exit codes: `0` (attested / partial / unsealed — informational), `1` (critical: a seal-integrity anchor — signature or log-integrity — failed), `2` (missing or malformed input).

---

### nova redaction-xray \<document\> | --capsule \<dir\> (experimental, ADR-0174)

Render a Redaction / Secret-scan X-Ray from a capsule's protection metadata: a
per-field state overlay, a coverage meter, and per-state counts. **No field
value is ever printed** — the command surfaces field paths and states only
(ADR-0174 §1); any value handed in alongside a record is dropped by the
projection.

With `--capsule` the report is built from the capsule's own
`redaction-proof.json` instead of a hand-assembled document. A capsule with
**zero findings is a genuine result** (nothing sensitive was detected), not an
error; a capsule captured without the masking pipeline says so explicitly
rather than failing opaquely.

```bash
nova redaction-xray --capsule .novafabric/runs/01HX.../
nova redaction-xray xray.json
nova redaction-xray xray.json --json --capsule-id run-42
```

Options:
- `<document>` (positional) — JSON with either `fields` (`[{path, state}]`, state one of `clear|redacted|secret_scrubbed|never_captured|unknown`) or raw `findings` (MaskingPipeline finding records). Omit when using `--capsule`
- `--capsule PATH` — capsule directory to read `redaction-proof.json` from
- `--capsule-id TEXT` — label the report with this capsule id. **A label only** — it selects nothing; use `--capsule` to read from a capsule. When `--capsule` is used, the id comes from the proof's own `capsule_run_id`
- `--json` — emit the X-Ray report as JSON

Exit codes: `0` (report rendered), `2` (missing or malformed input, both a document and `--capsule`, or neither).

---

### nova assure-case \<document\> (experimental, ADR-0166)

Inspect an assurance-case document: structural validity, currency/drift, a
conformance receipt, and open defeaters. Prints only references and digests,
never evidence bodies.

The document is a JSON object with:
- `case` (required) — the argument graph: `{case_id, nodes[]}`
- `resolvable_digests` (optional) — evidence digests that currently resolve
- `currency` (optional) — `{nodes[]}`, a currency ledger (needs `--as-of`)
- `conformance` (optional) — `{entries[]}`, standard/clause mappings
- `defeaters` (optional) — recorded challenges to nodes

```bash
nova assure-case case.json
nova assure-case case.json --as-of 2026-06-01T00:00:00Z
nova assure-case case.json --json
```

Options:
- `--as-of TEXT` — ISO-8601 instant to evaluate currency at (required if the document carries a currency ledger; never inferred from the system clock)
- `--json` — emit a machine-readable JSON report

Exit codes: `0` (case structurally valid AND no defeater open), `1` (structurally invalid, or at least one defeater open — argument defeated), `2` (document missing/malformed, or a currency ledger present without `--as-of`).

---

### nova assure-coverage \<document\> (experimental, ADR-0166)

Report structural coverage of an assurance case — **counts and gaps, never a
grade**. Takes the same document shape as `nova assure-case` (minus
`conformance`).

```bash
nova assure-coverage case.json
nova assure-coverage case.json --as-of 2026-06-01T00:00:00Z --json
```

Options:
- `--as-of TEXT` — ISO-8601 instant to evaluate currency at (required if the document carries a currency ledger; never inferred from the system clock)
- `--json` — emit the coverage report as JSON

Exit codes: `0` (coverage report rendered), `2` (missing or malformed input).

---

### nova passport issue | verify (experimental, ADR-0149)

Portable agent-passport projection (ADR-0149 / NF-179): a green/amber/red
verdict projected from component refs, re-derivable offline.

```bash
nova passport issue refs.json          # human-readable passport
nova passport issue refs.json --json   # passport document (feed to verify)
nova passport verify agent-passport.json
```

`issue` options:
- `<document>` (positional, required) — JSON: `{agent_ref, present: {component: ref}, opaque: [...]}`
- `--json` — emit the passport document as JSON

`verify` takes a passport document produced by `nova passport issue --json`,
re-derives the verdict offline, and confirms it matches the document.

Exit codes: `issue` — `0` (rendered), `2` (malformed input). `verify` — `0` (recomputed status matches the document), `3` (status mismatch), `2` (malformed input).

---

## Incident forensics and subject-rights commands (experimental, ADR-0155/0161)

Read-only reconstructions over already-sealed evidence supplied as a JSON
document. Missing evidence is shown as `gaps`, never raised (fail-open); the
commands print references and states only.

### nova forensics timeline (experimental, ADR-0155)

Render an incident's forensic timeline: a deterministic
(byte-identical-on-re-run) view over the incident's collected sealed evidence.

```bash
nova forensics timeline incident-evidence.json
nova forensics timeline incident-evidence.json --json
```

Options:
- `<evidence>` (positional, required) — JSON: `{incident_id, events: [{ts, source_capsule, seq, kind}], gaps?}`
- `--json` — emit the timeline as JSON

Exit codes: `0` (timeline rendered), `2` (missing or malformed input).

---

### nova dsar assemble (experimental, ADR-0161)

Assemble a subject's cross-capsule DSAR package: a deterministic assembly of
every capsule that processed a subject, keyed on the **HMAC pseudonym** — the
raw subject id never enters the artifact.

```bash
nova dsar assemble subject-records.json
nova dsar assemble subject-records.json --json
```

Options:
- `<document>` (positional, required) — JSON: `{subject_hmac, records: [{capsule_id, categories}], gaps?}`
- `--json` — emit the DSAR package as JSON

Exit codes: `0` (package rendered), `2` (missing or malformed input).

---

### nova dsar sla (experimental, ADR-0161)

Compute a DSAR's turnaround against a deadline (default GDPR Art. 12(3), one
month). `met_deadline` is a factual `fulfilled_at <= deadline` comparison over
recorded timestamps — evidence a request was met on time, not a compliance
verdict.

```bash
nova dsar sla request.json
nova dsar sla request.json --json
```

Options:
- `<document>` (positional, required) — JSON: `{request_open, fulfilled_at, deadline?, subject_hmac?}` (ISO 8601 UTC)
- `--json` — emit the SLA record as JSON

Exit codes: `0` (record rendered, whether or not the deadline was met), `2` (bad input).

---

## Lifecycle events and webhooks (experimental, ADR-0137)

Opt-in outbound surface: on defined lifecycle transitions NovaFabric emits one
structured, non-sensitive event record to a local append-only `events.jsonl`
and, optionally, POSTs it to user-configured webhook URLs so your **own**
CI/automation can react. Strictly opt-in — nothing is emitted until you set
`NOVA_EVENTS_LOG` and/or `NOVA_EVENTS_WEBHOOK`; there is no default
destination. Delivery is emit-and-forget: fail-safe (a broken webhook can
never break a capture or validation), bounded retries (`NOVA_EVENTS_MAX_RETRIES`,
default 2), no queue, no delivery guarantee — the local log is the durable
record. Payloads carry only refs, digests, enums, and counts (never
prompt/response text or secrets) and pass the capsule secret scanner before
leaving the process. Optional HMAC-SHA256 signing (`NOVA_EVENTS_SIGN_SECRET`)
adds a `signature` field and an `X-NovaFabric-Signature` header.

Wired transitions in this slice (**works today**): `capsule.created`
(`nova capture`) and `capsule.validated` (`nova validate` on a capsule).
The remaining taxonomy (`promotion.*`, `policy.failed`, `retention.applied`,
`promotion.bypass.*`) is defined in `schemas/lifecycle-event.schema.json` and
can be emitted manually; wiring those transitions is **planned** (ADR-0137 P3),
as are the local command sink, `tail --follow`, and NovaSeal signing.

### nova events tail

```bash
export NOVA_EVENTS_LOG=~/.novafabric/events.jsonl
nova events tail                                  # summary lines
nova events tail --type capsule.created --json    # filter + raw JSON
nova events tail --since 2026-07-15T00:00:00Z --last 10
```

### nova events emit

Manually emit one event through the configured sinks — for testing a wired CI
hook end-to-end.

```bash
NOVA_EVENTS_WEBHOOK=https://ci.internal.example/nova-hook \
nova events emit --type policy.failed \
  --subject policy:promotion:agent-a@1.4.0 \
  --payload '{"gate": "eval-regression", "decision": "deny"}'
```

Exit 1 when no sink is configured; delivery itself is best-effort.

---

## Compliance evidence commands (v0.15.0, BQ-005)

These commands require `pip install 'novafabric[compliance]'` for full functionality.

> **OQ-01 status (corrected):** `nova subject-proof` (below) remains **LEGAL-HOLD DRAFT MODE** — it looks up an existing redaction proof; PII stays in a `legal_hold/` staging area, not sealed capsules. The DEK-based crypto-shredding strategy this note used to describe as "planned for v0.26.x" **has since shipped** (ADR-0069, v0.44.0) as separate commands — see [`nova pii erase`](#nova-pii-erase-subject_id) and [`nova erasure request`](#nova-erasure-request) below — which actually destroy the wrapping DEK for a data subject rather than only staging a proof.

### nova export-annex-iv \<capsule\> --output-dir \<dir\> --deployment-id \<id\>

Export EU AI Act Annex IV technical documentation from a Run Capsule (Regulation (EU) 2024/1689 Art. 11). Produces a JSON-LD file covering all 15 mandatory Annex IV elements, with an optional PDF.

```bash
nova export-annex-iv .novafabric/runs/01HX.../ \
  --output-dir ./compliance/ \
  --deployment-id "prod-eu-classifier-v2"

nova export-annex-iv <capsule> \
  --output-dir ./compliance/ \
  --deployment-id "prod-eu-classifier-v2" \
  --pdf    # requires weasyprint
```

Options:
- `--output-dir, -o PATH` (required) — directory to write `annex-iv-<deployment_id>.jsonld`
- `--deployment-id TEXT` (required) — operator-assigned deployment identifier
- `--pdf / --no-pdf` — also render a PDF (requires `weasyprint`, included in `novafabric[compliance]`)

Output fields include: system description, intended purpose, training data characteristics, human oversight measures, robustness metrics, and post-market monitoring plan. Fields are marked `complete`, `partial`, or `missing` based on capsule content, and each element carries an [`evidence_source`](#provenance-marking--evidence_source-adr-0197-experimental) provenance marker (ADR-0197).

Exit codes: `0` (success), `1` (missing compliance extra or export error).

---

### nova export-nis2 \<capsule\> --output \<file\> --incident-id \<id\>

Export a NIS2 incident report (Directive (EU) 2022/2555 Art. 23) from a Run Capsule.

```bash
nova export-nis2 .novafabric/runs/01HX.../ \
  --output incident-report.json \
  --incident-id "INC-2026-0042" \
  --phase 1    # initial report (≤24h, default)

nova export-nis2 <capsule> --output report.json --incident-id INC-001 --phase 2   # ≤72h
nova export-nis2 <capsule> --output report.json --incident-id INC-001 --phase 3   # ≤1 month
```

Options:
- `--output, -o PATH` (required) — path to write the NIS2 incident report JSON
- `--incident-id TEXT` (required) — operator-assigned incident identifier
- `--phase INT` — reporting phase: `1` (initial, ≤24h), `2` (detailed, ≤72h), `3` (final, ≤1 month). Default: `1`

Fields requiring cross-session lineage (cap-006) are explicitly marked `missing` with reason `requires_cap_006_cross_session_lineage`.

Exit codes: `0` (success), `1` (missing compliance extra or export error).

---

### nova export-ropa \<capsule\> --output \<file\>

Export a GDPR Art.30 Records of Processing Activities (RoPA) entry from a Run Capsule (cap-007).

```bash
nova export-ropa .novafabric/runs/01HX.../ --output ropa-entry.json

nova export-ropa .novafabric/runs/01HX.../ \
  --output ropa.json \
  --controller-name "Acme Corp" \
  --controller-contact "dpo@acme.example"
```

Options:
- `--output, -o PATH` (required) — path to write the RoPA JSON-LD document
- `--controller-name TEXT` — GDPR Art.30(1)(a) controller name (operator-declared; marked missing if omitted)
- `--controller-contact TEXT` — controller contact / DPO email

Output is a JSON-LD document with `gdpr:` and `nova:` namespaces. Fields derivable from
capsule metadata are populated automatically; operator-declared fields are stubs with
`missing_fields` markers. Use `--controller-name` / `--controller-contact` to complete the entry.

Exit codes: `0` (success), `1` (export error).

---

### nova export-nist-rmf \<capsule\> --output \<file\>

Export a NIST AI RMF 1.0 quantitative risk assessment report from a Run Capsule (cap-009).

```bash
nova export-nist-rmf .novafabric/runs/01HX.../ --output nist-rmf.json
```

Options:
- `--output, -o PATH` (required) — path to write the NIST AI RMF JSON report

Derives scores for all four NIST AI RMF Core functions:
- **GOVERN** (GV-1.1 policy coverage, GV-1.2 evidence signing)
- **MAP** (MP-2.1 tool permission observability, MP-2.2 capture level adequacy)
- **MEASURE** (MS-2.5 eval performance score, MS-2.6 regression detection)
- **MANAGE** (MG-2.2 PII redaction rate, MG-4.1 lifecycle management score)

`risk_level` = `low|medium|high|critical` based on overall score and non-compliant metric count.
`completeness` = `complete|partial` based on whether all expected evidence files are present.

Exit codes: `0` (success), `1` (export error).

---

### nova subject-proof \<subject-id\>

Look up PII redaction proof for a data subject (GDPR Art.17). HMACs the subject ID with `NOVA_PII_PEPPER`, queries the `RedactionSubjectIndex`, and returns a `redaction_proof_report.json`.

> **Requires:** `NOVA_PII_PEPPER` environment variable (the same pepper value used during capture). This command will exit 1 if the env var is not set.

```bash
NOVA_PII_PEPPER="<secret>" nova subject-proof "user@example.com"

NOVA_PII_PEPPER="<secret>" nova subject-proof "user@example.com" \
  --output proof.json \
  --key ~/.novafabric/keys/ed25519.pem
```

Options:
- `<subject-id>` (positional, required) — data-subject identifier (email, phone, GDPR subject ID, etc.)
- `--db PATH` — path to `redaction_subject_idx.db` (default: `$NOVAFABRIC_HOME/compliance/redaction_subject_idx.db`)
- `--key PATH` — ed25519 private key for signing the proof report
- `--output, -o PATH` — path to write `redaction_proof_report.json` (default: stdout)

Exit codes: `0` (subject found), `1` (env var missing, compliance extra missing, or DB error).

---

## Sector and transparency export commands (experimental)

Thirteen read-only renderers that project supplied references and sealed
evidence into sector-specific disclosure and attestation artifacts. Shared
conventions:

- each command takes one positional JSON document path and supports `--json`
  for a machine-readable artifact;
- exit codes are `0` (artifact rendered) and `2` (missing or malformed input) —
  rendering never mutates a capsule;
- outputs are **evidence artifacts, not judgments**: register/database entries
  are explicitly `DRAFT`, claims are declared claims, and the scorecard is
  coverage — the commands never certify, score, or judge compliance.

| Command | Framework / audience | ADR |
|---|---|---|
| [`nova export-compliance`](#nova-export-compliance-subcommand) | EU AI Act / ISO 42001 / NIST GenAI + CSA exporters | ADR-0107 |
| [`nova export-model-risk`](#nova-export-model-risk-evidence) | SR 26-2 / SR 11-7 model-risk evidence | ADR-0159 |
| [`nova export-part11`](#nova-export-part11-document) | 21 CFR Part 11 electronic records | ADR-0160 |
| [`nova export-rai-scorecard`](#nova-export-rai-scorecard-document) | Responsible-AI coverage scorecard | ADR-0158 |
| [`nova export-public-annex-viii`](#nova-export-public-annex-viii-document) | EU AI Act Annex VIII public DB (DRAFT) | ADR-0169 |
| [`nova export-transparency-register`](#nova-export-transparency-register-document) | Algorithm registers: ATRS / Amsterdam / Helsinki (DRAFT) | ADR-0169 |
| [`nova export-public-disclosure`](#nova-export-public-disclosure-document) | Public-sector disclosure record (DRAFT) | ADR-0169 |
| [`nova export-foia`](#nova-export-foia-document) | FOIA / public-records decision export (DRAFT) | ADR-0169 |
| [`nova export-whistleblower`](#nova-export-whistleblower-document) | Source-protecting whistleblower attestation | ADR-0169 |
| [`nova export-citizen-explanation`](#nova-export-citizen-explanation-document) | Subject-facing decision explanation | ADR-0169 |
| [`nova export-public-incident`](#nova-export-public-incident-document) | Public-interest incident summary (DRAFT) | ADR-0169 |
| [`nova export-election-disclosure`](#nova-export-election-disclosure-document) | Election / civic content-provenance disclosure | ADR-0169 |
| [`nova export-accessibility-claim`](#nova-export-accessibility-claim-document) | Declared accessibility-conformance claim | ADR-0169 |
| [`nova export-control-attestation`](#nova-export-control-attestation-document) | Governance-control attestation pack | ADR-0170 |

### nova export-model-risk \<evidence\>

Assemble an SR 26-2 / SR 11-7 model-risk evidence file from per-pillar refs.

```bash
nova export-model-risk evidence.json --json
```

- `<evidence>` — JSON: `{model_id, development[], independent_validation[], ongoing_monitoring[], model_inventory[], partial[]}`

---

### nova export-part11 \<document\>

Render a 21 CFR Part 11 electronic-records evidence artifact — the
records/signatures elements a run recorded (signer identity, §11.50 signing
intent, DSSE signature binding, record integrity, trusted timestamp, audit
trail), each `complete` / `partial` / `missing`. **Facts, never a Part 11
call.**

```bash
nova export-part11 part11.json --json
```

- `<document>` — JSON: `{capsule_root, elements: {name: ref}, partial: {name: reason}}`

---

### nova export-rai-scorecard \<document\>

Render a Responsible-AI coverage scorecard — presence of evidence per
dimension (`supported` / `partial` / `unsupported` / `not_applicable`),
**never a score**: no threshold, no fair/unfair or pass/fail label.

```bash
nova export-rai-scorecard rai.json --json
```

- `<document>` — JSON: `{evidence: {dimension: ...}, not_applicable[], partial[]}`

---

### nova export-public-annex-viii \<document\>

Render a DRAFT EU AI Act Annex VIII / Art. 71 public-database entry from
sealed evidence + operator declarations. Each field is either
`capsule_evidence` (a digest/ref into the sealed capsule — never the raw
value) or `operator_declared` (the operator's public declaration).

```bash
nova export-public-annex-viii entry.json --json
```

- `<document>` — JSON: `{capsule_root, capsule_evidence{}, operator_declared{}}`

---

### nova export-transparency-register \<document\>

Render a DRAFT algorithm-register record from sealed evidence + operator
declarations, crosswalked to a public register shape.

```bash
nova export-transparency-register reg.json --standard atrs
nova export-transparency-register reg.json --standard amsterdam --json
```

- `<document>` — JSON: `{capsule_root, capsule_evidence{}, operator_declared{}}`
- `--standard TEXT` — register shape: `atrs` | `amsterdam` | `helsinki` (default: `atrs`)

---

### nova export-public-disclosure \<document\>

Render a DRAFT public-sector disclosure record from supplied references.

```bash
nova export-public-disclosure disclosure.json --json
```

- `<document>` — JSON: `{authority_ref, agent_ref, decision_scope, human_oversight_ref, capsule_refs[], system_card_ref?}`

---

### nova export-foia \<document\>

Render a DRAFT FOIA/public-records decision export from a decision's ordered
record index + redaction claims (each redaction a salted digest plus the
**claimed** statutory `exemption_ref`).

```bash
nova export-foia foia.json --json
```

- `<document>` — JSON: `{decision_ref, record_index[], redactions[{digest, exemption_ref}]}`

---

### nova export-whistleblower \<document\>

Render a source-protecting whistleblower attestation over a sealed bundle.

```bash
nova export-whistleblower wb.json --json
```

- `<document>` — JSON: `{content_digest, authenticity_attestation, anonymity_set_ref?}`

---

### nova export-citizen-explanation \<document\>

Render a plain-language, subject-facing decision explanation.

```bash
nova export-citizen-explanation cit.json --json
```

- `<document>` — JSON: `{decision_ref, factors[], human_involvement, contest_channel_ref?, logic_summary_ref?}`

---

### nova export-public-incident \<document\>

Render a DRAFT public-interest incident summary from a sealed incident.

```bash
nova export-public-incident incident.json --json
```

- `<document>` — JSON: `{incident_ref, public_summary?, affected_scope?, remediation_ref?}`

---

### nova export-election-disclosure \<document\>

Render an election/democratic-process content-provenance disclosure —
**records, never judges**. Binds a provenance receipt (e.g. NF-094 / C2PA /
SynthID) by digest.

```bash
nova export-election-disclosure elec.json --json
```

- `<document>` — JSON: `{content_ref, provenance_receipt_ref, disclosure_label, capsule_refs[]}`

---

### nova export-accessibility-claim \<document\>

Render a declared accessibility-conformance claim — **a declared claim, never
a guarantee**.

```bash
nova export-accessibility-claim a11y.json
nova export-accessibility-claim a11y.json --standard en_301_549_v4_1_1 --json
```

- `<document>` — JSON: `{declared_standard?, audit_digest?, export_format_check?}`
- `--standard TEXT` — declared standard (`wcag_2_2_aa` | `en_301_549_v4_1_1`); overrides the document

---

### nova export-control-attestation \<document\>

Render a governance-control attestation pack — **presents evidence, never
certifies**.

```bash
nova export-control-attestation ctrl.json --json
```

- `<document>` — JSON: `{capsule_root, catalog: [{control_id, evidence_kind?}], present_evidence{}, declared[]}`

---

## Compliance commands — full reference

This section consolidates all compliance-related CLI surfaces. Commands are labeled per
the docs honesty rule: **works today**, **planned** (implementation in progress or
scheduled for v0.26.x), or **future** (ADR accepted, implementation not yet scheduled).

### Already implemented — works today

| Command | Regulation | Version |
|---|---|---|
| `nova seal sign --intent <intent>` | FDA 21 CFR Part 11 §11.50 | v0.12.15+ |
| `nova export-annex-iv <capsule_id>` | EU AI Act Annex IV (Reg. 2024/1689 Art. 11) | v0.15.0 |
| `nova export-nis2 <capsule_id>` | NIS2 Directive Art. 23 (Phase 1/2/3) | v0.15.0 |
| `nova subject-proof <subject_id>` | GDPR Art.17 HMAC subject lookup | v0.15.0 |
| `nova audit coverage` | Multi-profile control coverage scoring | v0.16.0 |
| `nova audit bundle` | ZIP export of full audit report | v0.16.0 |
| `nova audit verify` | AuditReport schema validation | v0.16.0 |
| `nova classify run` | EU AI Act / NIST RMF / OMB M-24-10 risk tier | v0.16.0 |
| `nova verify <capsule>` | DSSE + RFC 3161 TSR + Merkle log verification | v0.10.0+ |
| `nova export-ropa <capsule_id>` | GDPR Art.30 Records of Processing Activities (cap-007) | v0.25.1 |
| `nova export-aibom <capsule_dir>` | CycloneDX 1.7 AI-SBOM / ML-BOM (cap-008); default output `aibom.json` | v0.25.1 |
| `nova aibom status` | CRA SBOM compliance status: deadline 2026-09-11, format, per-capsule coverage | v0.35.0 |
| `nova aibom generate [--all] [--force]` | Per-deployment automation: generate/refresh `aibom.json` for one or all capsules | v0.45.0 |
| `nova export-nist-rmf <capsule_id>` | NIST AI RMF 1.0 quantitative risk report (cap-009) | v0.25.1 |
| `nova assure <capsule_id>` | OWASP LLM Top 10 (2025) evidence checks — exit 1 on failure (E-10) | v0.25.1 |
| `nova mcp scan <manifest>` | OWASP LLM supply-chain risk scanner for MCP manifests (E-9) | v0.25.1 |

#### nova seal sign --intent \<intent\>

Sign a capsule with a declared signing intent per FDA 21 CFR Part 11 §11.50.

```bash
nova seal propose <capsule-id> \
  --key signer.pem \
  --cert signer_cert.pem \
  --justification "FDA §11.50 review approved — all eval gates green"
```

Signing intents are expressed via the `SigningIntent` enum (`src/novafabric/trust/novaseal/envelope.py`):

| Intent | Meaning (FDA §11.50 mapping) |
|---|---|
| `approved` | Approval signature (§11.50(a)(1)) |
| `reviewed` | Review signature (§11.50(a)(2)) |
| `authored` | Authorship signature (§11.50(a)(3)) |
| `witnessed` | Witnessed signature |
| `verified` | Verification / QA signature |

The intent value is embedded in the DSSE predicate and verified by `nova seal verify`.
The five-check SoD verifier (exit codes 3–7) enforces the separation-of-duties
required by FDA §11.50(b).

#### nova seal sign --backend sigstore

**works today** — Keyless Sigstore signing (ADR-0071, v0.44.0). Produces a Sigstore
Bundle v0.3 using an ephemeral OIDC-bound certificate and Rekor v2 transparency log
inclusion. Requires `pip install novafabric[sigstore]`.

```bash
nova seal sign --backend sigstore --capsule-id 01HX...
nova seal sign --backend sigstore --capsule-id 01HX... --home /data/nova
```

Options:
- `--backend [local|sigstore]` — signing backend (default: `local`; `sigstore` requires `novafabric[sigstore]`)
- `--capsule-id TEXT` — capsule ID to sign (required)
- `--home PATH` — `NOVAFABRIC_HOME` override for bundle storage path

Bundles stored at `$NOVAFABRIC_HOME/sigstore/<capsule_id>.bundle.json`.
Verify with `nova verify --backend sigstore --capsule-id <id>`.

**Note:** The `sigstore` SDK performs OIDC browser-based or ambient identity
(GitHub Actions, GCP, AWS) credential lookup at sign time. Not suitable for
air-gapped HPC environments (use `--backend local` with RFC 3161 instead).

---

#### nova pii erase \<subject_id\>

**works today** — GDPR Art.17 crypto-shredding erasure (ADR-0069, v0.44.0). Destroys
the AES-256-GCM DEK for a data subject, rendering all PII encrypted under that DEK
permanently unreadable. Writes an `ErasureReceipt` (immediate) or
`ErasureDeferredReceipt` (Art.17(3)(b) retention window still active).

```bash
nova pii erase "user@example.com"
nova pii erase "user@example.com" --output receipt.json
nova pii erase "user@example.com" --retention-months 6
```

Options:
- `--output, -o PATH` — path to write the `ErasureReceipt` JSON (default: stdout)
- `--capsule-dir PATH` — search for redaction manifests here (default: `$NOVAFABRIC_HOME/capsules/`)
- `--retention-months INT` — Art.17(3)(b) retention floor in months (default: `6`; provider mode: `120`)

Exit codes: `0` (receipt written), `1` (subject not found or error).

Scope: single data subject. Requires DEK store at `$NOVAFABRIC_HOME/dek.db`.

#### nova pii status \<capsule_id\>

**works today** — Show PII encryption status for a capsule — which fields are encrypted,
which subjects have active DEKs, and which have been erased (crypto-shredded, ADR-0069).
Read-only and local-first: correlates the capsule's `redaction_manifest.json` against
the DEK store at `$NOVAFABRIC_HOME/dek.db` without creating or modifying either.
Subjects appear only as HMACs — no key material or plaintext PII is ever shown.

```bash
nova pii status 01HXAMPLECAPSULEID                  # resolve by capsule ID
nova pii status .novafabric/runs/01HX.../           # or by capsule directory path
nova pii status 01HXAMPLECAPSULEID --json           # machine-readable PIIStatusReport
NOVA_PII_PEPPER=secret nova pii status 01HX...      # pepper enables DEK correlation
```

Options:
- `--capsule-dir PATH` — directory scanned for the capsule's `redaction_manifest.json` when a bare capsule ID is given (default: `$NOVAFABRIC_HOME/capsules/`)
- `--json` — emit the status report as JSON instead of human-readable text

Per-subject `dek_state`:
- `active` — a live DEK exists; ciphertext is still recoverable by the key holder
- `erased` — no DEK exists (crypto-shredded via `nova pii erase`, or `dek.db` absent)
- `unknown` — `dek.db` is present but `NOVA_PII_PEPPER` is not set, so manifest HMACs cannot be correlated

Exit codes: `0` (report produced — including capsules with no redaction manifest), `1` (capsule not found or manifest unreadable).

Scope: single capsule. Works without a DEK store and without `NOVA_PII_PEPPER` (degrades honestly as above).

#### nova export-rocrate \<capsule_dir\>

Export a Run Capsule as a FAIR-compliant RO-Crate v1.1 research object (ZIP archive).

**works today** — implemented in v0.32.0.

```bash
nova export-rocrate .novafabric/runs/01HX.../
nova export-rocrate .novafabric/runs/01HX.../ --output ./out.rocrate.zip
```

Options:
- `--output, -o PATH` — output ZIP file path (default: `<capsule_dir>.rocrate.zip` next to the capsule dir)

#### nova lineage export-prov \<capsule_dir\>

Export a W3C PROV provenance graph from a capsule's `lineage.jsonl`, as PROV-JSON
(default) or PROV-N text — both serializations of the same graph.

**works today** — PROV-JSON since v0.32.0; PROV-N added (ADR-0176).

```bash
nova lineage export-prov .novafabric/runs/01HX.../
nova lineage export-prov .novafabric/runs/01HX.../ --output prov.json
nova lineage export-prov .novafabric/runs/01HX.../ --format prov-n -o prov.provn
```

Options:
- `--format, -f {prov-json,prov-n}` — serialization (default `prov-json`)
- `--output, -o PATH` — output path (default: `<capsule_dir>/prov.json` or `prov.provn`)

#### nova export-hipaa-proof \<capsule_dir\>

**works today** — Export a HIPAA Safe Harbor (§164.514(b)) de-identification
proof artifact for a capsule, documenting which of the 18 identifier categories
were absent, redacted, or not assessable from available capsule evidence files.

**DISCLAIMER:** This is a technical evidence artifact only. It is NOT legal
advice, NOT a compliance certification, and does NOT guarantee Safe Harbor
legal sufficiency.

```bash
nova export-hipaa-proof .novafabric/runs/01HX.../                           # writes hipaa-proof.json in capsule dir
nova export-hipaa-proof .novafabric/runs/01HX.../ --output hipaa-proof.json
```

Options:
- `--output, -o PATH` — path to write the proof JSON (default: `<capsule_dir>/hipaa-proof.json`)

#### nova export-aibom \<capsule_dir\>

Export an AI Bill of Materials (AIBOM) using CycloneDX ML-BOM 1.7 (ECMA-424 2nd Edition). Required for
EU Cyber Resilience Act (CRA) compliance. Deadline: **2026-09-11** (ADR-0073).

**works today** — implemented in v0.25.1; `--output` default + `nova aibom status` added v0.35.0; `nova aibom generate` (per-deployment automation) added 2026-06-04.

```bash
nova export-aibom .novafabric/runs/01HX.../            # writes aibom.json in the capsule dir
nova export-aibom .novafabric/runs/01HX.../ --output /tmp/aibom.cdx.json
nova aibom status                                       # show deadline + per-capsule coverage
nova aibom status --capsules-dir ~/novafabric-data/capsules
nova aibom generate .novafabric/runs/01HX.../          # generate one (skips if present)
nova aibom generate --all                              # batch: every capsule missing an aibom.json
nova aibom generate --all --force                      # batch: refresh all (overwrite)
# NF-056 CycloneDX 1.7 extensions (opt-in; default output unchanged):
nova aibom generate <cap> --citations --force          # bind components to capsule/evidence digests
nova aibom generate <cap> --tlp TLP:AMBER --force      # distribution marker in metadata.properties
nova aibom generate <cap> --model-card auto --force    # model-card externalReference per model
nova aibom generate <cap> --no-include-datasets --force # suppress type:data components
nova aibom validate <cap>/aibom.json                   # structural + binding check (exit 1 on error)
```

Options (`nova export-aibom`):
- `--output, -o PATH` — path to write the AIBOM JSON (default: `<capsule_dir>/aibom.json`)

Options (`nova aibom status`):
- `--capsules-dir PATH` — root directory to scan for capsules (default: `$NOVAFABRIC_CAPSULE_DIR` or `$NOVAFABRIC_HOME/capsules`)

Options (`nova aibom generate`) — **per-deployment automation (CRA)**:
- `<capsule_dir>` — optional positional: generate for a single capsule
- `--all` — batch-generate across `--capsules-dir`, skipping capsules that already have an `aibom.json`
- `--capsules-dir PATH` — capsule store for `--all` (default: `$NOVAFABRIC_CAPSULE_DIR` or `$NOVAFABRIC_HOME/capsules`)
- `--force` — regenerate even when an `aibom.json` already exists
- `--citations` — **(NF-056, [ADR-0105](./decisions.md))** bind each model/dataset component to its source capsule/evidence digest (+ ADR-0097 inclusion proof when the manifest records one). Default off (byte-stable output).
- `--tlp TLP:CLEAR|GREEN|AMBER|AMBER+STRICT|RED` — TLP 2.0 distribution marker in `metadata.properties` (`novafabric:tlp`). Default: none.
- `--model-card auto|<path>` — add a `model-card` externalReference to each model component (`auto` derives a `registry://` URI). Default: none.
- `--include-datasets / --no-include-datasets` — emit `type:data` dataset components from lineage (default on).

Options (`nova aibom validate <bom.json>`) — **NF-056**: structural CycloneDX 1.7 + NovaFabric-binding check (specVersion/bomFormat/serialNumber, TLP marker validity, per-component name/bom-ref/hash-alg/citation digest). Exit `0` valid, `1` on validation errors, `2` on a missing/malformed file. `--json` emits `{valid, errors}`.

#### nova dataset provenance-card \<asset\> (experimental)

> **Experimental — NF-058, [ADR-0105](./decisions.md).** Emit (and optionally
> sign) a dataset provenance card recording a dataset's source, version, content hash, and **transform
> history**. Feeds the AI-BOM (NF-056) and the SLSA-for-ML attestation (NF-057). Op digests only — never raw
> values, prompts, or cell contents.

```bash
nova dataset provenance-card dataset:gaia@2026-05 \
    --source oci://reg/gaia:2026-05 --version 2026-05 --hash b17a... \
    --license CC-BY-4.0 --tlp TLP:CLEAR --registry-digest b17a... --sign
# pull the transform history from a capsule's lineage derivation edges:
nova dataset provenance-card dataset:x@1 --source oci://x --version 1 --hash <sha256> \
    --from-capsule ./my-capsule --sign --out card.json
```

- `--source`, `--version`, `--hash` (required) — the dataset's source URI, version label, and content sha256.
- `--from-capsule <dir>` — derive `transformHistory[]` from the capsule's `lineage.jsonl` derivation edges (each `signedOpDigest` is the content hash of the recorded edge).
- `--license`, `--tlp`, `--registry-digest`, `--retrieved-at` — optional card fields.
- `--sign [--identity <id>]` — Ed25519-sign the card with the keyring key (reuses the NovaSeal/Evidence-Bundle path). The signature is taken over the canonical-JSON body excluding the signature block. **An unsigned card is schema-invalid** — a signed card is evidence.
- `--out, -o <path>` — write the card JSON (default: stdout).

#### nova export-c2pa \<capsule_dir\>

Export a C2PA v2.3-compatible provenance manifest for AI-generated content (ADR-0074).
Required by EU AI Act Art.50. Deadline: **2026-08-02**.

**works today** — implemented in v0.33.0.

```bash
nova export-c2pa .novafabric/runs/01HX.../
nova export-c2pa .novafabric/runs/01HX.../ --output manifest.json
nova export-c2pa .novafabric/runs/01HX.../ --training-mining   # adds notAllowed assertion
```

Options:
- `--output, -o PATH` — output JSON file (default: `<capsule_dir>/c2pa-manifest.json`)
- `--training-mining` — include `c2pa.training-mining: notAllowed` assertion (opt-in)

Note: the manifest is structurally valid for TSP signing. Hard-binding C2PA verification
requires operator enrollment with a C2PA-certified Trust Service Provider (TSP signing
path deferred to a future release).

#### nova export-compliance \<subcommand\>

Export EU AI Act / ISO / NIST compliance evidence from the ADR-0107 exporters. Each
subcommand reads a capsule or a JSON input and writes a report as JSON (to `--out`, else
stdout). The CLI only parses and serialises — the exporter logic lives in
`novafabric.compliance.export`.

**works today** — implemented in v0.89.0 (`experimental`).

```bash
# NIST GenAI + CSA Agentic profile, built from a capsule's NIST-RMF report (NF-097)
nova export-compliance genai-profile .novafabric/runs/01HX.../ --evidence tool_permissions,eval_gate --out profile.json

# ISO/IEC 42001 control-evidence mapping from a declared catalog (NF-095)
nova export-compliance iso42001 --catalog controls.json --evidence eval_gate --capsule-id run-123 --out iso.json

# First sealed revision of a GPAI Art. 53 documentation form (NF-093)
nova export-compliance gpai53 --model my-gpai --fields art53.json --out form.json

# Art. 72 post-market-monitoring report; serious findings refer Art. 73 incidents (NF-091)
nova export-compliance pmm --system triage --findings findings.json --occurred-at 2026-07-01 --out pmm.json
```

Subcommands and key options:
- `genai-profile <capsule>` — `--evidence a,b` (governance-evidence kinds present), `--out`.
- `iso42001 --catalog c.json` — `--evidence a,b`, `--capsule-id ID` (supplies the
  re-performable reference evidenced controls require; without it, evidenced controls
  honestly degrade to `not_evidenced`), `--out`.
- `gpai53 --model NAME --fields f.json` — `--out`. `fields.json` is a `{field: value}` object.
- `pmm --system NAME --findings f.json --occurred-at ISO` — `--period-start`, `--period-end`,
  `--out`. A serious finding (severity `critical`/`high`) must carry an
  `incident_classification`, or the command fails closed.

Note: NF-090 (Art. 12) is served by `nova euaiact`; NF-092 (Annex IV) by the AIBOM/Annex-IV
exporters; NF-094 (Art. 50 dual-layer receipt) composes with `nova export-c2pa`.

#### nova export-system-card \<capsule_dir\>

Generate and **seal** an auto-generated system/audit card (ADR-0085). The card is
assembled from capsule + eval + lineage facts (`capsule.yaml`, `eval_result.json`,
`lineage.jsonl`) and sealed by reusing the existing DSSE/Ed25519 signing path — no
new crypto. It is **generated, never hand-written**, so it cannot drift from the
capsule it describes. The sealed output is a DSSE envelope (predicate type
`https://novafabric.io/system-card/v0`) that verifies with the same verifier used
for Evidence Bundles.

**works today** — implemented in v0.48.0.

```bash
nova export-system-card .novafabric/runs/01HX.../
nova export-system-card .novafabric/runs/01HX.../ -o card.intoto.json --key ed25519.pem
```

Options:
- `--output, -o PATH` — output DSSE JSON (default: `<capsule_dir>/system-card.intoto.json`)
- `--key PATH` — PEM-encoded ed25519 private key to seal with. If omitted, a local
  key under `$NOVAFABRIC_HOME/keys/` is created/loaded.

Related: eval results now also pin the **asset version** and an optional
**dataset version** (`nova eval`-driven `run_evals`, ADR-0085) so a stored eval
result can be tied back to exactly what was measured.

#### nova export-blob --dest \<uri\>

Export a **batch** of capsules to blob storage with one **signed, verifiable
completeness manifest** ([ADR-0141](./decisions.md),
spec batch-blob-export-v0). Members are
selected explicitly (`--capsule`, repeatable) or by scanning the capsule directory
with an optional `created_at` time range (`--since`/`--until`, inclusive); the
selection is frozen at export start (a batch is a snapshot). Each member is packed
deterministically and written **content-addressed** under `objects/<sha256>` at the
destination — already-present blobs are skipped, so re-runs are **idempotent** and an
interrupted export **resumes**. The Ed25519/DSSE-signed `export-manifest.json`
(validates against `schemas/export-manifest.schema.json`) is written **last** as the
atomic completion marker: it lists every member's `capsule_id`, `content_hash`, and
`size`, plus a `batch_digest` over the sorted member list, so a recipient can verify
integrity **and completeness** offline. Source capsules are never modified.

**experimental** — implemented; API may change.

```bash
# Local directory (always works, fully offline)
nova export-blob --dest ./exports/audit-q3 -c 01HX... -c 01HY...

# Time-range scan of the capsule store
nova export-blob --dest ./exports/w27 --since 2026-06-29 --until 2026-07-05

# Query filter (ADR-0129 DSL) — composes with the time window
nova export-blob --dest ./exports/gpt4o --query "model = 'gpt-4o'"
nova export-blob --dest ./exports/failures \
  --query "status = 'error'" --since 2026-07-01

# Any S3-compatible endpoint (existing optional boto3 adapter; private endpoints OK)
NOVA_S3_ENDPOINT_URL=https://s3.internal nova export-blob \
  --dest s3://audit-bucket/exports/2026-07/ --worm --worm-retention-days 2555
```

Options:
- `--dest URI` — local directory or `s3://bucket[/prefix]` (any S3-compatible
  endpoint via `NOVA_S3_ENDPOINT_URL`). `azure://` / `gcs://` are **planned**
  (ADR-0141 P2) and report a clear error.
- `--capsule, -c ID|PATH` — explicit member (repeatable); records `query: null`.
- `--capsule-dir PATH` — scan root (default `$NOVAFABRIC_HOME/capsules`).
- `--since / --until RFC3339` — inclusive `created_at` window for the scan;
  recorded as the manifest `query` for reproducibility.
- `--query EXPR` — select members with an [ADR-0129](./decisions.md)
  `where` filter, e.g. `"model = 'gpt-4o'"` or `"status = 'error'"`. **Composes
  with** `--since`/`--until` (both must hold), and is mutually exclusive with
  `--capsule`. The *parsed, canonical* predicates are recorded in the manifest
  `query` field alongside the time window — so a manifest reader sees what was
  asked, not free text they must re-parse. Filters on the query's `where`
  clause only: `run_id` is not a DSL dimension, and grouping by it would invite
  the high-cardinality blow-up the DSL's cardinality cap exists to prevent.
- `--key PATH` — PEM ed25519 signing key (default: the NovaFabric keyring key,
  created if absent).
- `--worm` / `--worm-retention-days N` — write members **and** manifest under WORM
  retention (S3 Object Lock COMPLIANCE via the existing ADR-0031 adapter; local
  dirs get best-effort read-only files); intent recorded in the manifest.
- `--out PATH` — also save a local copy of the manifest.
- `--public-key-out PATH` — save the signer's public key PEM for recipients.

Verify offline (no NovaFabric service; extends `nova verify`):

```bash
nova verify export-manifest.json --public-key export.pub.pem
nova verify manifest.json --public-key pub.pem --dest s3://audit-bucket/exports/2026-07/
```

Verdicts: `VALID` (signature + batch digest + every member's bytes at the
destination check out), `INCOMPLETE` (a member missing, or size/hash mismatch —
e.g. a deleted or tampered blob), `INVALID` (bad signature, count, or batch
digest — e.g. a member quietly dropped from the manifest). Exit 0 only on `VALID`.

#### nova import \<source\>

Import a **batch export** ([`nova export-blob`](#nova-export-blob---dest-uri))
into the local capsule store — the verified inverse of the export
([ADR-0207](./decisions.md), spec
batch-import-v0). `<source>` is either a
directory containing `export-manifest.json` + `objects/`, or a single
`.tar`/`.tar.gz` archive of that layout (the air-gap courier format).

**Verification-first, fail-closed:** before any byte enters the store the
manifest must verify (`VALID` — DSSE signature via the supplied public key,
`count`/`batch_digest` consistency, and every member's bytes re-hashed).
`INVALID` or `INCOMPLETE` refuses the **whole** import. Unpacking is hardened
against hostile archives (absolute paths, `..`, links, devices — refused) and
staged: members extract to a scratch directory and are atomically renamed into
place, so a crash never leaves a half-written capsule. After unpack, lineage
(`lineage.jsonl` → lineage store) and the dashboard `runs_cache` are reindexed
(the offline query index self-scans and needs no rebuild). Every run —
including refusals and dry runs — writes an **import receipt** JSON under
`$NOVAFABRIC_HOME/import-receipts/<import_id>.json` and one hash-chained audit
entry (`capsule.import`).

**Idempotent by content address:** a member whose `run_id` already exists
locally with byte-identical content (same deterministic re-pack hash) is
skipped, so re-running an import is a no-op and an interrupted import resumes.
**Collisions are never overwritten:** same `run_id` with different local
content is reported per-member with both hashes (exit 5); delete the local
capsule with existing retention tooling and re-import to replace it — there is
deliberately no `--force`.

**experimental** — implemented (ADR-0207 P1); API may change.

```bash
# Verified import (the normal path; key from `nova export-blob --public-key-out`)
nova import ./exports/audit-q3 --public-key export.pub.pem

# DR drill: verify + classify with zero writes, same exit codes
nova import ./exports/audit-q3 --public-key export.pub.pem --dry-run

# Air-gap courier archive
nova import batch.tar.gz --public-key export.pub.pem

# Machine-readable report
nova import ./exports/audit-q3 --public-key export.pub.pem --json
```

Options:
- `--public-key PATH` — PEM ed25519 public key of the export signer. Required
  unless `--allow-unsigned`.
- `--allow-unsigned` — skip the DSSE **signature** check only (loud warning;
  recorded permanently as `verification.mode: "unsigned"` in the receipt and
  audit log). All content-hash/size/consistency checks still run — tamper
  still refuses.
- `--capsule-dir PATH` — target store root (default `$NOVAFABRIC_HOME/capsules`).
- `--dry-run` — verify + classify (would-import / would-skip / collision),
  write nothing to the store or indexes; the receipt is still written.
- `--no-reindex` — unpack only; skip lineage + runs-cache updates.
- `--json` — print the import receipt JSON to stdout.

Exit codes: `0` success (imported / already present), `2` usage error, `3`
verification `INVALID` (structure, signature, digest), `4` verification
`INCOMPLETE` (member missing / hash or size mismatch), `5` collision(s), `6`
member(s) failed during unpack. Nothing is imported on 3/4; non-colliding
members still import on 5.

#### nova euaiact export

Export structured EU AI Act Art.12 log events for authority access requests (Art.74).
Binding for high-risk AI systems: **2026-08-02** (ADR-0076).

**works today** — implemented in v0.33.0.

```bash
nova euaiact export --from 2026-01-01 --to 2026-06-30 --output euaiact-report.json
NOVA_EUAIACT_HIGH_RISK=true nova euaiact export --from 2026-01-01
nova euaiact export --pretty    # Rich table output to terminal
nova euaiact status             # show active configuration
```

Options:
- `--from DATE` — start date ISO 8601 (default: no lower bound)
- `--to DATE` — end date ISO 8601 (default: now)
- `--capsules-dir PATH` — capsule root (default: `$NOVAFABRIC_CAPSULE_DIR` or `$NOVAFABRIC_HOME/capsules`)
- `--output, -o PATH` — output file (default: stdout as JSON)
- `--pretty` — Rich table output instead of JSON

Environment:
- `NOVA_EUAIACT_HIGH_RISK=true` — declares this is a high-risk AI system under EU AI Act Annex III
- `NOVA_EUAIACT_PROVIDER=true` — enables provider mode (120-month / Art.18 retention floor instead of 6-month deployer floor)

---

## Registry commands (v0.1)

### nova register \<spec.yaml\>

Register an asset from a YAML spec file. Exits 0 on success, 1 on validation or duplicate error.

### nova suggest-register [\<capsule-ref\>] [OPTIONS]

Analyze captured run capsules and suggest assets to register. Inverts the onboarding workflow —
capture first, then let NovaFabric propose what to register from observed evidence.

**Three modes:**

- **Interactive** (default) — prompts per suggestion: `y` register, `n` skip, `e` open editor, `s` skip.
- `--draft-only` — write draft YAML files to `--output-dir` without registering.
- `--auto` — register all suggestions above `--min-confidence` (default 0.8) without prompting.

**Options:**

| Flag | Description |
|---|---|
| `<capsule-ref>` | Run ID or path. Omit to scan 10 most recent capsules. |
| `--output-dir / -o` | Directory for draft YAML files (default `.novafabric/drafts`). |
| `--draft-only` | Write drafts without registering. |
| `--auto` | Auto-register all suggestions above `--min-confidence`. |
| `--min-confidence` | Confidence threshold for `--auto` mode (default `0.8`). |
| `--skip-types` | Comma-separated asset types to skip (e.g. `prompt,agent`). |
| `--runs-dir` | Base directory containing capsule runs. |

**Confidence scoring:** models = 1.0 (every observed call), tools = 0.8 (min 2 calls), agents = 0.7.

**Post-capture hint:** after every successful `nova capture`, a one-line hint is printed when
unregistered assets are detected. Disable with `NOVAFABRIC_SUGGEST=0`.

**Examples:**

```bash
# Scan 10 most recent capsules interactively
nova suggest-register

# Analyze a specific run
nova suggest-register 01HXAY7M5JZ8R7K4P9DPBYK2WX

# Write draft YAML files for review
nova suggest-register --draft-only --output-dir ./drafts/

# Auto-register high-confidence suggestions, skipping agents
nova suggest-register --auto --min-confidence 0.8 --skip-types agent
```

**Dashboard equivalent:** Registry tab → Suggest Register panel (live suggestions table with one-click Register).

### nova list [--type TYPE] [--status STATUS] [--stale] [--stale-days N]

List registered assets. Optional filters by asset type (`model`, `agent`, `prompt`, etc.) or
lifecycle status (`development`, `staging`, `production`, `archived`).

- `--stale` — only show assets with no promotion, consumption, or eval activity in the last N days.
- `--stale-days N` — inactivity threshold (default: 30). Filters can be combined with `--status`/`--type`.

### nova inspect \<name[@version]\>

Show full metadata for an asset. If version is omitted, shows the latest registered version.

### nova promote

**v0.13.0:** `nova promote` is now a sub-group with three sub-commands:
`direct`, `propose`, and `approve`. Old scripts must add `direct` as the first
positional argument.

#### nova promote direct \<name@version\> --to \<status\> [--force] [--significance-gate]

Promote an asset in a single step (no SoD enforcement). Valid transitions:

| Current status | Valid targets |
|---|---|
| `development` | `staging`, `archived` |
| `staging` | `production`, `archived` |
| `production` | `archived` |
| `archived` | `staging` |

Agent assets require a passing eval result to promote to `staging` or `production`.
Use `--force` to override the eval gate (interactive confirmation required; audit-logged).

**`--significance-gate`** (opt-in, ADR-0080) — replace the single-passing-eval
check with a *statistical* regression gate. A Wald SPRT runs over the asset's
recent pass/fail eval sequence and **only** blocks on a statistically significant
regression (`ACCEPT_H1`). Noise (`ACCEPT_H0`) and inconclusive evidence
(`CONTINUE`, too few runs) do **not** block, so a single-run dip cannot fire the
gate. Default hypotheses are `p0=0.9, p1=0.7, alpha=0.05, beta=0.05` (overridable
via the `promote_asset()` API). The flag is opt-in: omitting it preserves the
default single-passing-eval behavior. `--force` bypasses it.

**`--scores-file <path> [--metric task_pass]`** (NF-007, experimental) — source the
gate's pass/fail sequence from an evidence-grade `scores.jsonl` (the boolean `--metric`)
instead of the `eval_results` table. Omitting `--scores-file` preserves the exact
`eval_results` behavior.

**`--slsa-provenance [--slsa-out <path>] [--identity <id>]`** (NF-031, experimental,
[ADR-0096](./decisions.md)) — on a **successful** promotion,
also emit a DSSE-signed SLSA v1 provenance (`slsa.dev/provenance/v1`) recording the promotion
decision (and the `significance-gate/v1` gate when `--significance-gate` is set). The subject
digest is the sha256 of the registered asset spec; the attestation is signed with the keyring
Ed25519 key for `--identity` (default: OS username) and written to `<name>-<version>.slsa.json`
(or `--slsa-out`). Verify it with `nova verify-envelope`. **Opt-in:** without the flag, promotion
behavior and output are unchanged.

**`--slsa-ml-profile`** (NF-057, experimental, [ADR-0105](./decisions.md)) — with
`--slsa-provenance`, emit the **SLSA-for-ML profile** instead of the generic one: the build type becomes
`https://novafabric.dev/promote-ml/v1`, a `gate-rule` byproduct records the gating rule, and an `eval-verdict`
byproduct carries the sha256 digest of the gating eval verdict — binding the promoted model to the exact eval
result that justified it. Use for model assets. Still a valid `slsa.dev/provenance/v1` Statement, DSSE-signed
and `nova verify-envelope`-verifiable.

```bash
nova promote direct my-agent@v1.1 --to staging                       # default gate
nova promote direct my-agent@v1.1 --to staging --significance-gate   # statistical gate (eval_results)
nova promote direct my-agent@v1.1 --to staging --significance-gate \
    --scores-file cand/scores.jsonl --metric task_pass               # gate on evidence-grade scores
nova promote direct my-model@1.0.0 --to staging --slsa-provenance \
    --slsa-out my-model.slsa.json                                    # emit SLSA v1 provenance
nova promote direct my-model@1.0.0 --to staging --slsa-provenance \
    --slsa-ml-profile --slsa-out my-model.slsa.json                  # SLSA-for-ML (NF-057)
```

The dashboard Promote dialog (`Registry tab → PROMOTE →`) maps to `nova promote direct`.

#### nova promote propose \<name@version\> --to \<status\> [--identity NAME]

Open a maker-checker proposal (maker step). Signs a canonical payload with the
proposer's Ed25519 key (auto-generated at `~/.config/novafabric/keyring/<identity>.pem`
if absent). The proposal ID is printed and stored in the `promotion_proposals` table.

- `--identity NAME` — name for the keypair (default: OS username). Must differ from
  the approver's identity.

#### nova promote approve \<name@version\> [--identity NAME]

Approve the latest open proposal for `name@version` (checker step). Enforces
Separation of Duties at the cryptographic level:

- The approver's key fingerprint must differ from the proposer's.
- The approver's identity string must differ from the proposer's.

On success, promotes the asset and records a `promote.approve` audit event.

**Opt-in Rego gate:** Load `src/novafabric/policies/novafabric/defaults/maker_checker_gate.rego`
to block `nova promote direct` to `staging`/`production` and require the two-step flow.

---

## NovaSeal linked-envelope chain maker-checker (v0.14.0, ADR-0059)

Cryptographic two-person authorization at the **capsule signing level**. Two separate DSSE envelopes (Proposal + Approval) are linked via SHA-256 of RFC 8785 JCS-canonicalized bytes. Offline-verifiable without a database.

Distinct from `nova promote propose/approve` (ADR-0058), which enforces two-actor approval for **asset registry lifecycle**. Both can be used together.

### nova policy sign

Sign and version a promotion policy document. The policy defines which certificate subjects are allowed as proposers and approvers. Stored in the NovaSeal Merkle log with a monotonically increasing version number.

```bash
nova policy sign \
  --key admin.pem \
  --cert admin_cert.pem \
  --proposer-subjects "alice,alice-ci" \
  --approver-subjects "bob,carol" \
  --bypass-valid-hours 24
# Policy signed and stored: version 1
```

Options:

- `--key PATH` — path to ECDSA P-256 PEM private key (admin key)
- `--cert PATH` — path to X.509 PEM certificate matching `--key`
- `--proposer-subjects TEXT` — comma-separated list of certificate CN values allowed to propose
- `--approver-subjects TEXT` — comma-separated list of CN values allowed to approve
- `--bypass-valid-hours N` — bypass validity window in hours (default: 24; range: 1–168)
- `--db PATH` — SQLite Merkle log DB path (default: resolved by `resolve_merkle_db_path()` — see [NovaSeal Configuration Reference](novaseal-configuration.md#31-path-resolution-order))

Prints `Policy signed and stored: version N` on success.

---

### nova seal propose \<capsule-id\>

Create a promotion proposal (maker step). Builds a `promote/proposal/v1` DSSE predicate, validates it against the JSON Schema, signs with ECDSA P-256, and stores at `{data_dir}/promote/{capsule_id}/proposal/{uuid}.json`.

```bash
nova seal propose capsule-abc123 \
  --justification "All eval gates green; p99 latency < 200ms. Ready for staging." \
  --key proposer.pem \
  --cert proposer_cert.pem
# Proposal created: b7820d90-039c-4d1f-a586-5023c82e9dd4
#   capsule: capsule-abc123
#   proposer: alice
#   policy version: 1
#   Run nova seal approve b7820d90-... --capsule-id capsule-abc123 as a different identity to complete.
```

Options:

- `--justification, -j TEXT` — rationale for the promotion; must be ≥ 20 characters (validated before signing; shorter values cause exit 1 without writing any bundle)
- `--key PATH` — ECDSA P-256 PEM private key (proposer key)
- `--cert PATH` — X.509 PEM certificate matching `--key`
- `--target-env TEXT` — target deployment environment (default: `staging`)
- `--db PATH` — Merkle log DB path (default: resolved by `resolve_merkle_db_path()` — see [NovaSeal Configuration Reference](novaseal-configuration.md#31-path-resolution-order))
- `--data-dir PATH` — root directory for bundle storage (default: `~/.novafabric`)

Exit codes: `0` (proposal created), `1` (validation error, signing error, or no policy found).

**Prerequisites:** a policy must exist (`nova policy sign` must have been run). The command reads the latest policy and embeds its version number in the predicate.

---

### nova seal approve \<proposal-uuid\>

Counter-sign a proposal (checker step). Fetches the Proposal bundle, displays its contents, prompts for confirmation, then builds and stores an Approval bundle.

```bash
nova seal approve b7820d90-039c-4d1f-a586-5023c82e9dd4 \
  --capsule-id capsule-abc123 \
  --key approver.pem \
  --cert approver_cert.pem
# Proposal details:
#   capsule_id:    capsule-abc123
#   justification: All eval gates green; p99 latency < 200ms. ...
#   proposer:      alice
#   timestamp:     2026-05-15T10:30:00Z
# Approve this proposal? [y/N]: y
# Approval recorded: 8b778f50-ea38-49c2-bcdc-3a69a1f8e841
```

Options:

- `--capsule-id TEXT` — capsule ID (required; must match the proposal's `capsule_id` field)
- `--key PATH` — ECDSA P-256 PEM private key (approver key)
- `--cert PATH` — X.509 PEM certificate matching `--key`
- `--db PATH` — Merkle log DB path
- `--data-dir PATH` — bundle storage root

If the operator types anything other than `y` at the prompt → exits 0 with "Promotion approval cancelled." No bundle is written.

**`proposal_digest` computation:** `SHA-256(JCS(proposal_envelope_bytes))` — RFC 8785 JSON Canonicalization Scheme, not `json.dumps`. This makes the approval cryptographically bound to the exact bytes the proposer signed.

Exit codes: `0` (approval recorded or cancelled), `1` (proposal not found or signing error).

---

### nova seal verify \<capsule-id\> [--offline]

Run the five-check SoD verifier. Loads the Proposal and Approval bundles, loads the policy version recorded in the Proposal, and runs five checks in order (stops at first failure).

```bash
nova seal verify capsule-abc123
# SoD verification passed   (exit 0)

nova seal verify capsule-abc123 --offline
# SoD verification passed   (exit 0; --offline accepted, no Rekor calls in v0.1)
```

**The five checks and their exit codes on failure:**

| Check | Description | Exit code |
|---|---|---|
| 1 | Proposer cert subject ∈ `policy.proposer_key_ids` | 3 |
| 2 | Approver cert subject ∈ `policy.approver_key_ids` | 4 |
| 3 | `proposal_digest` in Approval = SHA-256(JCS(Proposal envelope bytes)) | 5 |
| 4 | `approver_subject ≠ proposer_subject` (no self-approval) | 6 |
| 5 | Approval timestamp > Proposal timestamp (ordering) | 7 |

Additional exit codes:
- `8` — Approval bundle not found for capsule
- `9` — Proposal bundle not found for capsule
- `1` — other error (missing policy, malformed envelope)

Options:

- `--offline` — skip any network calls (accepted; fully functional in v0.1 since no Rekor calls are made at verify time)
- `--db PATH` — Merkle log DB path
- `--data-dir PATH` — bundle storage root

**Policy-time loading:** the verifier loads the policy version recorded in the Proposal's `policy_version` field, not the latest policy. A policy change after proposal creation does not retroactively invalidate in-flight proposals (ADR-0059 §Proposal-time policy).

---

### nova seal bypass

Declare a supervised bypass — logs an approved deviation from the seal gate without blocking the run.

```bash
nova seal bypass <reason> [--valid-hours N] [--run-id RUN_ID]
```

Options:
- `<reason>` (required) — human-readable justification (e.g. `"staging environment, no TSA access"`)
- `--valid-hours N` — how long the bypass is valid (default: `24`). After expiry, subsequent `nova verify` calls will flag it.
- `--run-id RUN_ID` — associate the bypass with a specific run (default: current `$NOVAFABRIC_SPAN_ID`)

The bypass is recorded in the NovaSeal bypass log. The dashboard **SealTab** shows all active and expired bypasses with approver identity.

ECDSA P-256 key is auto-generated at first use if not already present.

---

### nova seal log verify

Verify the integrity of the NovaSeal Merkle log — checks that the log has not been tampered with since the last append.  Supports both SQLite (default) and Postgres backends (Scale-S4).

```bash
nova seal log verify [--db URI] [--full] [--verbose]
```

Options:
- `--db URI` — Merkle log path (SQLite file) **or** `postgresql://` DSN for Postgres backend.  Default: `$NOVAFABRIC_SEAL_DB_PATH` or `~/.novafabric/novaseal-merkle.db`.  Postgres requires `pip install novafabric[seal-postgres]`.
- `--full` — full O(N) re-hash audit; re-computes every leaf hash from its stored entry (slow at large N).  Default: sampled check (spot-checks up to 1 000 random leaves + verifies the Merkle root) — p99 < 200 ms at 1 M entries on Postgres.
- `--verbose` / `-v` — show per-leaf details on failure.
- `--consistency N` — additionally emit + verify an append-only consistency proof from tree size `N` to the current head (experimental, ADR-0041 v0.2). The proof is an aligned perfect-subtree decomposition (O(log n) verifier) valid for the v0.1 duplicate-padding tree shape.

Returns exit code 0 if the log is consistent, 1 if tampered or inconsistent, 2 if a `--consistency` proof fails.

**Examples:**

```bash
# SQLite (default)
nova seal log verify

# Postgres — fast sampled check at 1M+ entries
nova seal log verify --db postgresql://user:pass@db.example.com/nova

# Full audit (slower — re-hashes every entry_json)
nova seal log verify --db postgresql://... --full
```

**Env var:** `NOVAFABRIC_SEAL_DB_PATH` — sets the default `--db` value; accepts both file paths and `postgresql://` DSNs.

---

### nova seal ratchet (experimental, v0.50.0, ADR-0089)

Forward-secure per-node signing key ratchet. Opt-in — the static-key NovaSeal
signing path remains the default. State lives under
`$NOVAFABRIC_HOME/seal/ratchet` (override: `NOVAFABRIC_RATCHET_DIR`).

```bash
nova seal ratchet init --node-id node-a      # provision epoch-0 chain key
nova seal ratchet rotate --node-id node-a    # advance epoch; erase old chain key (best-effort)
nova seal ratchet status --node-id node-a    # current epoch + registry history
```

Properties:
- `K_{i+1} = HKDF-SHA256(K_i)`; per-epoch Ed25519 key derived deterministically from `K_i`.
- Compromise at epoch N cannot forge epochs < N (old chain keys erased); current/future epochs remain forgeable until detected — rotate often.
- Epoch public keys are appended to an append-only registry; verification rejects seals claiming an epoch older than the registry's latest (rollback detection).
- Secure erase is best-effort (overwrite + delete); journaling filesystems and SSD wear-levelling may retain stale blocks — see ADR-0089.

---

### nova eval \<agent@version\>

Run declared evaluation suites for an agent and store results in the registry.
Suites are resolved via the `novafabric.evals` entry-point group.

### nova eval agent

Evaluate an agent version using all registered eval suites for that asset type. Equivalent to `nova eval run --all-suites`.

```bash
nova eval agent <name@version> [--db-path PATH] [--timeout SECONDS]
```

Options:
- `<name@version>` (required) — asset reference (e.g. `my-agent@0.3.0`)
- `--db-path PATH` — registry DB path
- `--timeout SECONDS` — per-suite timeout (default: `600`)

Results are stored in `eval_results` table and checked against the Rego gate for promotion eligibility.

### nova eval run

Run a specific eval suite against a named agent version.

```bash
nova eval run <name@version> --suite SUITE_NAME [--db-path PATH]
```

Options:
- `<name@version>` (required) — asset reference
- `--suite SUITE_NAME` — suite to run: `smoke-v1`, `gaia`, `swe-bench`, `agentbench`, `mmlu`, `truthfulqa`
- `--db-path PATH` — registry DB path

### nova eval cost \<document\>

Render a self-reported eval-cost / compute disclosure (ADR-0154 D2, NF-229,
**experimental**).

```bash
nova eval cost cost.json
nova eval cost cost.json --json
```

Options:
- `<document>` (required) — JSON file with `wall_seconds`, `token_in`, `token_out`,
  `usd_cost`, and optionally `energy_wh` / `hardware_ref`
- `--json` — emit the record as JSON instead of text

**Scope, honestly:** every figure is **self-reported by the harness**. NovaFabric
discloses what it was given — it does not measure, verify, or certify these values,
and it does not run the eval. Both output modes carry that honesty line, `--json`
included, as an `honesty_line` field.

This slice reads the figures from the document you pass. Reading `facets.eval_cost`
from a sealed capsule (`--capsule <run_id>`) is **planned**, not implemented.

### nova eval compare

Compare eval results between two agent versions and generate a regression report.

```bash
nova eval compare <name@v1> <name@v2> [--suite SUITE_NAME] [--output FORMAT]
```

Options:
- `<name@v1>` `<name@v2>` (required) — two asset references to compare
- `--suite SUITE_NAME` — limit comparison to one suite (default: all)
- `--output FORMAT` — `text` (default), `json`, `markdown`

A regression is flagged when any metric drops by more than the threshold defined in `regression_gate.rego`.

### nova eval list

List all registered eval suite adapters discovered via the `novafabric.eval_suites` entry-point group.

```bash
nova eval list
```

Output columns: **Suite ID**, **Version**, **OCI Digest** (`host-env` for suites that run locally), **Entry Point** (importable module path).

Built-in suites:

| Suite ID | Version | Notes |
|---|---|---|
| `novafabric-smoke-v1` | 0.1.0 | Fast structural check; no OCI container |
| `gaia-v1` | 0.1.0 | GAIA Lvl-1/2/3 benchmark (OCI-pinned) |
| `mmlu-v1` | 0.1.0 | MMLU 57-subject knowledge benchmark (OCI-pinned) |
| `truthful-qa-v1` | 0.1.0 | TruthfulQA adversarial honesty benchmark (OCI-pinned) |
| `swe-bench-verified-v1` | 0.1.0 | SWE-bench Verified coding benchmark (OCI-pinned) |
| `agentbench-v1` | 0.1.0 | AgentBench multi-task agent benchmark (OCI-pinned) |

Third-party suites register via `[project.entry-points."novafabric.eval_suites"]` in their `pyproject.toml`. Any load errors for registered adapters are shown inline without crashing the command.

### nova eval card / nova eval score (experimental)

> **Experimental — NF-002 / NF-010, [ADR-0099](./decisions.md).**
> Evidence-grade evaluation: a **score is not a number — it is a signed record** binding
> `(value, evaluator-identity, subject-span-digest, verdict)`. Library + CLI are shipped
> behind the experimental label; the API may change and there is no dashboard UI yet.

An **eval card** is the reproducibility key for a score: it pins the exact evaluator
(judge model identity + endpoint reference, prompt version, rubric, dataset version,
human-agreement calibration), is content-addressed (`eval_card_digest` = sha256 over the
card's canonical JSON excluding its signature), and is Ed25519-signed with the local
keyring. A **score** is one line of an additive, optional `scores.jsonl` file; a capsule
without that file remains valid.

```bash
# create → sign → register an evaluator
nova eval card new --source code --card-id exact-match --name "Exact Match" --out card.json
nova eval card sign card.json                       # local Ed25519 keyring; prints digest
nova eval card register card.json                   # into the eval-card registry (must be signed)
nova eval card show   exact-match@0.1.0             # card JSON + digest
nova eval card verify exact-match@0.1.0             # signature_ok / calibration → exit code

# record and list evidence-grade scores
nova eval score add  --card exact-match@0.1.0 --subject sha256:<hex> \
                     --value true --value-type boolean --source code \
                     --name exact_match --capsule ./my-capsule      # or --scores-file scores.jsonl
nova eval score list --capsule ./my-capsule [--source judge] [--json]
```

`nova eval card verify` exits non-zero on a broken signature, a missing local key
(`key_id` mismatch → exit 2), or missing calibration on a `judge` card. `nova eval score
add` refuses a `--card` ref that does not resolve to a registered eval card. Judge models
are referenced by identity + a configurable endpoint (`env:NOVA_JUDGE_ENDPOINT`); no
external URL is hardcoded. **Writing a score into a `--capsule` directory seals it:** the
score log (`scores.jsonl`) is covered by the capsule Merkle root, so any Evidence Bundle
built from that capsule (`nova export-evidence`) detects score tampering — no separate
re-seal step is required.

### nova eval score config (experimental)

> **Experimental — [ADR-0117](./decisions.md),
> spec `score-config-v0.md`.** The
> **score-configuration catalog**: named, reusable, **immutable, content-addressed**
> definitions of what a valid score for a given metric `name` looks like, so scores
> under one name are coherent and comparable across capsules. Fully additive: the
> `Score` record and `scores.jsonl` are unchanged, and a metric without a config is a
> *free score* — exactly the previous behavior.

A `ScoreConfig` declares a metric's `value_type` (`boolean` | `categorical` | `numeric`
— the same vocabulary as `Score.value_type`), plus the allowed `categories` (optionally
ordinal-ranked, e.g. `bad=0 < ok=1 < good=2`) or the inclusive numeric range with an
optional direction (`higher-better` | `lower-better`). Configs live in the local
registry SQLite (no server, no internet); each is pinned by a `sha256:` `content_digest`
over its canonical definition body (wire contract:
[`schemas/score-config-v0.schema.json`](../schemas/score-config-v0.schema.json)).

```bash
# declare metric shapes (immutable; a changed body bumps the version, never edits)
nova eval score config add --name helpfulness --value-type categorical \
    --description "How helpful the turn was." \
    --category bad:0 --category ok:1 --category good:2
nova eval score config add --name toxicity --value-type numeric \
    --description "Lower is better." --min 0 --max 1 --direction lower-better
nova eval score config add --name grounded --value-type boolean \
    --description "True iff every claim is supported."

nova eval score config list [--all] [--json]    # latest per name; --all = every version
nova eval score config get  toxicity@1          # canonical JSON (also: bare name, sha256:<hex>)
nova eval score config show toxicity            # human view + version history

# opt-in enforcement on the append path (default OFF — free scores stay legal)
nova eval score add --card tox-scan@0.1.0 --subject sha256:<hex> \
    --value 0.3 --value-type numeric --source code --name toxicity \
    --scores-file scores.jsonl --validate-scores
```

Re-registering an **identical** body is a no-op (same digest, same version).
`nova eval score add --validate-scores` resolves the latest config for `--name` and
refuses the append (`exit 1`, nothing written) when the score's `value_type` disagrees,
a categorical value is not in the allowed set, or a numeric value falls outside the
inclusive `[min, max]`. Without the flag — or when no config governs the name — the
score is appended unchanged. Because a config is immutable and content-addressed, an
aggregate ("avg `helpfulness` over 500 capsules") can pin the exact `content_digest` it
was computed against, making cross-capsule comparability reproducible evidence.

### nova eval contamination-check (experimental)

> **Experimental — NF-028, [ADR-0108](./decisions.md).** Flags a capsule that
> was run against a *contaminated* or *superseded* benchmark version (contamination silently inflates eval
> scores). Detection/flagging only — no remediation.

```bash
# check a capsule's dataset_provenance facets (recorded status only)
nova eval contamination-check ./my-capsule

# resolve against a configurable known-bad hash registry (no default URL)
nova eval contamination-check ./my-capsule --registry known-bad.json --json
```

Reads the additive `dataset_provenance` facets stored under the capsule's
`extensions/dev.novafabric.dataset-provenance/` namespace (schema
[`schemas/dataset-provenance-v1.schema.json`](../schemas/dataset-provenance-v1.schema.json)) — each carrying
`name`, `version`, `dataset_hash`, `split_hash`, and `status` ∈ `current|superseded|contaminated|unknown`. The
`--registry` JSON (`{"contaminated": [...], "superseded": [...]}` of `sha256:` hashes) upgrades a facet's status
when a dataset/split hash matches; the registry never downgrades a facet's recorded severity. **Exit codes:**
`4` when any dataset is contaminated or superseded (CI-gateable), `0` when all are current/unknown, `2` on a
usage error.

### nova eval import-inspect / export-inspect (experimental)

> **Experimental — NF-024, [ADR-0108](./decisions.md).** Score-level bridge
> between [Inspect AI](https://inspect.aisi.org.uk) (UK AISI) JSON eval logs and NovaFabric's
> evidence-grade `scores.jsonl`. Pure stdlib JSON parsing against the documented Inspect log structure —
> **no `inspect-ai` dependency**. The Solver-steps → span-tree import and byte-equal native round-trip
> from the NF-024 spec remain *planned*.

```bash
# import an Inspect JSON eval log's scorer results into a capsule's score log
nova eval import-inspect ./logs/hello.json --capsule ./my-capsule

# export a capsule's scores.jsonl as an Inspect-compatible JSON log
nova eval export-inspect ./my-capsule --output inspect-log.json   # or stdout
```

**Import** maps each sample scorer result (and each aggregate `results` metric) to a typed `Score`:
Inspect `"C"`/`"I"` verdict strings stay `categorical` (no lossy coercion), numbers become `numeric`,
booleans `boolean`; `model_graded_*` scorers map to `source: judge`, everything else to `source: code`.
Every imported score is provenance-stamped — `evaluator_id: inspect-ai:<scorer>` plus a synthetic
content-addressed `eval_card_digest` derived from the foreign scorer identity + mapping version (it is
*not* a signed NovaFabric eval card). The mapping is versioned and only pinned Inspect log versions are
accepted; an unsupported `version` errors naming it. **Nothing is dropped silently:** Inspect fields with
no `Score` target are preserved in `extensions/org.inspect/import.json` (`unmapped`), and content-bearing
fields (prompts, outputs, transcripts) are enumerated by name in `omitted` but never copied (ADR-0021 §4).
The log's dataset name is also recorded as an NF-028 `dataset_provenance` facet (status `unknown` — Inspect
logs carry no content hashes).

**Export** produces an Inspect-shaped JSON log from the capsule's `scores.jsonl`: one sample per span
subject, per-scorer aggregates under `results.scores` (booleans → `accuracy`, numerics → `mean`), and
NovaFabric identities carried in score `metadata` under `dev.novafabric.*` keys. A capsule imported from
Inspect restores the preserved task/model/run-id header; a capsule without scores exports a valid empty
log. **Exit codes:** `0` on success, `2` on a usage error (missing/invalid log, unsupported version,
not a capsule directory).

### nova eval offline (experimental)

> **Experimental — NF-009, [ADR-0099](./decisions.md).** Trace-first
> structural checks over an already-stored capsule that run with **zero model calls** and emit a `code`
> score bound to the capsule Merkle root.

```bash
# did the run exercise every declared tool? (reads tool-calls.jsonl)
nova eval offline --capsule ./my-capsule --check coverage --declared-tools search,fetch,write [--emit-score]

# do recorded outputs satisfy a JSON-schema contract?
nova eval offline --capsule ./my-capsule --check contract --schema out.schema.json [--field output] [--emit-score]

# does a recorded input transform preserve a recorded invariant? (declarative check-spec)
nova eval offline --capsule ./my-capsule --check metamorphic --spec check-spec.yaml [--emit-score]
```

`--emit-score` appends the resulting score to `<capsule>/scores.jsonl`, so it is sealed by the capsule
Merkle root (see `nova eval score`). Because the capsule is already on disk, these checks spend **zero
tokens** — they are pure arithmetic/validation over recorded events.

The `metamorphic` check is driven by a declarative check-spec
([`schemas/features/metamorphic-check-v0.schema.json`](../schemas/features/metamorphic-check-v0.schema.json)):
records whose *input* collapses to the same value under `transform` form metamorphic pairs, and every
pair's *output* must satisfy `invariant`. Example `check-spec.yaml`:

```yaml
records_file: tool-calls.jsonl   # where the (input, output) records live (default)
input_field: input               # record field forming the pairing key (default)
output_field: output             # record field the invariant is asserted over (default)
transform: [lower, strip]        # identity | lower | strip | collapse_whitespace | remove_punctuation
invariant: equal                 # equal | equal_normalized | numeric_close | length_within
tolerance: 0                     # slack for numeric_close / length_within
```

A passing check means equivalent inputs produced consistent outputs (a zero-token consistency/robustness
signal); it emits a boolean `code` score. A malformed spec or unknown transform/invariant exits `2`.

### nova diff \<name@v1\> \<name@v2\>

Show field-level differences between two registered versions of an asset.
Both arguments must contain `@` to trigger asset diff; otherwise capsule diff is used (see above).

### nova diff --significance (experimental)

> **Experimental — NF-007, [ADR-0099](./decisions.md); extends
> [ADR-0080](./decisions.md).** Compares two run sets by
> **statistical significance, not raw delta**, so a single-run dip cannot fire a regression gate.

Reads a boolean pass/fail metric from stored `scores.jsonl` files (or capsule directories) and computes a
Wilson interval per side plus a Wald SPRT over the candidate sequence, yielding a three-valued verdict
(`accept_h0` / `accept_h1` / `continue`). It is **offline and zero-token** — pure arithmetic over
already-recorded outcomes; the workload is never re-run.

```bash
nova diff --significance \
    --baseline base/scores.jsonl --candidate cand/scores.jsonl \
    --metric task_pass [--p0 0.9 --p1 0.7 --alpha 0.05 --beta 0.05] [--json]
```

Exit codes: `0` = `accept_h0`/`continue` (no block), **`3` = `accept_h1`** (significant regression — gate
CI on this), `2` = usage error (unknown metric, non-boolean metric, or invalid SPRT parameters).
`--baseline`/`--candidate` accept either a `scores.jsonl` path or a capsule directory (reads
`<dir>/scores.jsonl`). Known limitation: the SPRT assumes i.i.d. Bernoulli outcomes; correlated
early-token cascades violate this (see ADR-0080). Related shipped surfaces: `nova eval offline`
(zero-token structural checks) and `nova promote direct --significance-gate` (which can also read its
sequence from a `scores.jsonl`).

### nova experiment run | list | show | compare (experimental, ADR-0120)

> **Experimental — [ADR-0120](./decisions.md);
> spec `dataset-experiment-v0`.** Run a target command
> across **every item** of a pinned local JSONL dataset (one Run Capsule per item — the capsule
> invariant is untouched) and record an **immutable, content-addressed `Experiment`**
> (`schemas/experiment.schema.json`). Compare two experiments per item and in aggregate; the verdict
> is produced **verbatim** by the shipped ADR-0080 significance gate, so CI fails only on a
> *statistically significant* regression. Fully local, offline, zero-token (scores come from the
> built-in exact-match `code` scorer — no model call, ever).

The dataset is a JSONL file, one item per line:

```json
{"item_id": "q-001", "input": "What is 2+2?", "expected": "4"}
```

`item_id` is the A/B alignment key; `expected` (optional) drives the built-in boolean exact-match
score over the item capsule's recorded stdout. Loading pins `dataset_hash` (sha256 of the file
bytes) and `split_hash` (sha256 of the ordered `item_id` sequence) into the record **and** into
each item capsule's ADR-0108 dataset-provenance facet, so per-item contamination checks keep
working.

```bash
# run: one capsule per item; {input}/{item_id}/{expected} placeholders are substituted
nova experiment run --dataset items.jsonl --target my-agent@1.2.0 -- \
    python agent.py --question "{input}"

# read side
nova experiment list [--json]
nova experiment show <experiment_id> [--json]

# A/B diff: exit 3 on a statistically significant regression (ADR-0080, verbatim)
nova experiment compare <baseline_id> <candidate_id> --metric exact_match \
    [--p0 0.9 --p1 0.7 --alpha 0.05 --beta 0.05] [--json] [-o comparison.json]

# CI gate in one shot: run the candidate and compare against a stored baseline
nova experiment run --dataset items.jsonl --target my-agent@1.3.0 \
    --baseline <baseline_id> -- python agent.py "{input}"
```

Records live under `./.novafabric/experiments/` (override: `--experiments-dir` /
`NOVAFABRIC_EXPERIMENTS_DIR`); item capsules under `./.novafabric/runs/` (override: `--runs-dir`).
A finalized experiment is **never mutated** — a re-run mints a new `experiment_id`, and the store
refuses overwrites. Comparing experiments over **different** pinned datasets (any of
name/version/`dataset_hash`/`split_hash`) is a hard error, never a silent skew; items present on one
side only, errored items, and items without the metric are reported unmatched and excluded from the
SPRT sequences. Exit codes: `0` = no significant regression, **`3` = significant regression**
(`accept_h1`), `2` = usage error. The comparison record
(`schemas/experiment-comparison.schema.json`) also renders a `regression_report`-shaped gate input
for the existing Rego regression gate (ADR-0003/0019) — no new gate engine.

---

## Asset lifecycle commands (v0.11.1)

### nova asset diff \<name@v1\> \<name@v2\>

Produce a unified diff of the spec JSON between two registered versions of the same (or different) asset.
Exits with code `1` if differences are found — useful as a CI gate.

```bash
nova asset diff fraud-model@1.0.0 fraud-model@2.0.0
nova asset diff my-agent@v1 my-agent@v2 --unified 5
nova asset diff fraud-model@1.0.0 fraud-model@2.0.0 --output-format json
```

Options:

- `--unified, -U INT` — lines of context in unified diff output (default: `3`)
- `--output-format text|json` — `text` (default) renders a coloured unified diff; `json` returns a structured payload with `added`, `removed`, `changed` field maps and an `identical` boolean

**JSON output shape:**

```json
{
  "ref_a": "fraud-model@1.0.0",
  "ref_b": "fraud-model@2.0.0",
  "identical": false,
  "added":   { "dotted.field": "new-value" },
  "removed": { "dotted.field": "old-value" },
  "changed": { "dotted.field": { "from": "old", "to": "new" } }
}
```

Both arguments must use the `name@version` format.  If either version is not found,
the command exits `1` with a clear error message.

**Declared asset dependencies (C-1.3).** An asset spec may declare its
dependencies in the `dependencies:` field:

```yaml
novafabric_spec_version: "1"
asset_type: model
name: my-model
version: 1.0.0
dependencies:
  - base-prompt@2.0.0
  - eval-dataset@3.1.0
```

When `nova register` processes the spec, a `depends_on` lineage edge
(confidence: `declared`) is written for each dependency.  These edges are
immediately visible to `nova lineage blast-radius <dep-ref>` and
`nova lineage provenance <asset-ref>`.

**Status at consumption (C-1.1).** When your SDK code calls
`record_asset_consumption(asset_ref, status, capsule_dir)` before using an
asset, the lifecycle status of the asset *at that moment* is stored in
`assets.jsonl` and propagated into the `facets.status_at_consumption` field
of the resulting `consumed` lineage edge.  This makes blast-radius queries
answer "was this asset already in `production` when run X consumed it?"

## Prompt versioning commands (experimental, ADR-0112)

Prompts as first-class, immutable, content-addressed registry versions. Every edit registers a
new version (never a mutation); each version's `content_hash` is a sha256 over the canonical
`{template, variables, config}` body, so a capsule reference
`prompt:<id>@<version>+sha256:<hex>` resolves to exactly one verifiable version. Stored in the
existing local SQLite registry — no new store, no schema change, fully offline. NovaFabric
records prompt evidence; it never renders templates or serves prompts to inference.

### nova prompt register \<prompt_id\> [--template TEXT | --file PATH] [--var NAME]... [--config JSON] [-m MSG]

Register a new immutable prompt version. The version number auto-increments per `prompt_id`;
re-registering identical content is idempotent (the existing version is returned, no new row).
Prints the replay-verifiable frozen reference.

```bash
nova prompt register triage -t "You triage {ticket_body}." --var ticket_body -m "first cut"
nova prompt register triage -f messages.json --config '{"temperature": 0.2}'
```

Options:

- `--template, -t TEXT` — inline text-form template (exactly one of `--template`/`--file`)
- `--file, -f PATH` — template file; `.json` = chat-form array of `{role, content}` messages (`role` ∈ `system|user|assistant|tool`), anything else = text-form read verbatim
- `--var NAME` — declared placeholder name (repeatable); documentation/validation only — NovaFabric never renders. Mismatches between declared and used `{tokens}` print a warning, never block
- `--config JSON` — model-agnostic hints as a JSON object (`model`, `temperature`, `max_tokens`, ...)
- `--message, -m MSG` — commit message (the version's "why")
- `--json` — print the raw record as JSON

### nova prompt get \<prompt_id\>[@\<version\>]

Fetch one version (latest when `@<version>` is omitted), frozen to version + content hash. The
printed `ref:` line is the exact `prompt:<id>@<version>+sha256:<hex>` form a Run Capsule
records. `--json` prints the raw record.

### nova prompt list [\<prompt_id\>] [--status STATUS]

Without an argument: one summary row per managed prompt (latest version, status, version
count). With a `prompt_id`: all versions of that prompt. `--status` filters by lifecycle
status.

### nova prompt history \<prompt_id\>

Chronological version log with created-at, status, content hash, and commit message.
`--json` prints the full version records.

### nova prompt diff \<prompt_id\>@\<a\> \<prompt_id\>@\<b\>

Structural diff of the canonical content triple (`template`, `variables`, `config`) between two
pinned versions. Exits `1` when the versions differ (CI-gateable), `0` when identical. Both
refs must pin a version.

```bash
nova prompt diff triage@6 triage@7
nova prompt diff triage@6 triage@7 --output-format json
```

Options:

- `--unified, -U INT` — lines of context in unified diff output (default: `3`)
- `--output-format unified|json` — `json` returns `added`/`removed`/`changed` dotted-path field maps and an `identical` boolean (same shape as `nova asset diff`)

**Promotion.** Prompt versions move through the standard lifecycle with the existing eval-gated
machinery — `nova promote direct triage@2 --to staging` works unchanged (ADR-0112 D3). A
dedicated `nova prompt promote <prompt_id>@<version> --to <status> [--force]` alias is also
available (works today, ADR-0112 P4): a thin, deliberate wrapper around `nova promote direct`
so prompts go through the exact same eval/policy gates and audit trail as any other asset —
it adds only a type check that refuses to "promote" a non-prompt asset through this surface.

```bash
nova prompt promote triage@1.2.0 --to staging
nova prompt promote triage@1.2.0 --to production
```

### Prompt composition (experimental, ADR-0115)

A prompt body MAY reference other registered prompt assets inline:

```
{{@prompt:<asset-name>@<selector>}}
```

where `<selector>` is an explicit integer version (`@3`) or a deployment label
(`@production`, `@latest` — ADR-0113). The reference splices in the referenced asset's
resolved body (textual inclusion only; referenced children must be text-form templates).
The composition graph is a **bounded acyclic DAG**: cycles, trees deeper than 8 levels,
and unknown references are rejected **at register time** with named errors — a malformed
composition never enters the registry. Each direct reference is snapshotted (the version +
content hash it resolved to at register time) into the version's frozen `composition` block.

### nova prompt compose \<prompt_id\>[@\<version\>|@\<label\>]

Resolve the full composition DAG for a prompt (latest version when bare) and print the
flattened, content-addressed `resolved_composition_manifest`: every transitively-included
prompt version + hash, the resolved DAG edges (what each label reference pointed at, at this
instant), the deepest resolution level, and the sha256 of the final assembled prompt.
Read-only — nothing is captured or written. Rebuilding from the manifest's pins reproduces
the assembled prompt byte-identically even after children are edited or labels move.

```bash
nova prompt compose triage-agent
nova prompt compose triage-agent@production --json
nova prompt compose triage-agent@9 --assembled
```

Options:

- `--json` — print the raw `resolved_composition_manifest` as JSON (validates against `schemas/resolved-composition-manifest.schema.json`)
- `--assembled` — print the fully spliced template instead of the manifest

### nova prompt tree \<prompt_id\>[@\<version\>|@\<label\>]

Print the composition DAG as an indented tree — each node shows the reference as written,
the version it resolved to, its content hash, and a `[label]` flag when pinned through a
deployment label. Read-only.

```bash
nova prompt tree triage-agent
```

```
triage-agent@9  aaaa0000…
├── system-preamble  @production → v4  11aa22bb…  [label]
│   └── org-header   @2 → v2  55ee66ff…
└── safety-footer    @7 → v7  33cc44dd…
```

*Capsule wiring (recording the manifest during `nova capture`) and replay verification are
planned (ADR-0115 P4) — today `compose`/`tree` and the register-time gate are shipped.*

---

## Deployment label commands (experimental, ADR-0113)

A **deployment label** is a mutable named pointer (`production`, `staging`, or any custom
lowercase name) from an asset name to exactly one immutable registry version. Labels are scoped
per asset: `production` on `prompt:triage` is unrelated to `production` on `prompt:router`.
Moving a label appends an audit row to the `asset_label_history` table (append-only —
`UPDATE`/`DELETE` are blocked by SQLite triggers); the current pointer is always the newest row.
The reserved label `latest` is auto-maintained (always the highest registered version) and is
never user-settable. Stored in the existing local SQLite registry — additive table, fully
offline, no new dependency.

**Resolution freeze.** A `<asset_type>:<asset_name>@<label>` reference (e.g.
`prompt:triage@production`) resolves at capture time to the concrete version + content hash,
recorded as a `resolved-asset-ref` record (`schemas/resolved-asset-ref.schema.json`), so a
capsule stays deterministically replayable even after the label later moves. The library API is
`novafabric.registry.labels.resolve_asset_ref()`; wiring into `nova capture` is planned
(ADR-0113 P2).

Assets are named `[<asset_type>:]<asset_name>` — the type prefix is optional but recommended
(e.g. `prompt:triage`).

### nova label set \<asset\> \<label\> \<version\> [--reason TEXT] [--json]

Point a label at an existing immutable version, appending an audit row. Fail-closed: the target
version must exist (no row is written otherwise); setting `latest` errors; mixed-case label
names are rejected, never lowercased. Re-pointing a label at its current target is a no-op.

```bash
nova label set prompt:triage production 4
nova label set prompt:triage production 3 --reason "rollback: v4 regressed on eval GAIA"
```

Options:

- `--reason, -r TEXT` — free-text note recorded on the move (shows in `history`)
- `--json` — print the full move record (`move_id`, `previous_version`, `content_hash`, …)

### nova label get \<asset\> \<label\> [--json]

Resolve a label to its current target version + content hash.

```bash
nova label get prompt:triage production      # → production → 4  (1a77…)
nova label get prompt:triage latest --json
```

### nova label list \<asset\> [--json]

All labels on an asset and their current targets — the auto-maintained `latest` first, then
explicit labels alphabetically.

```bash
nova label list prompt:triage
```

### nova label history \<asset\> [\<label\>] [--json]

The append-only label-move audit log, newest first: when each move happened, `previous →
target`, who moved it, and why. Answers "when did production point at the poisoned version,
and who moved it there?".

```bash
nova label history prompt:triage
nova label history prompt:triage production --json
```

### Protected labels — maker-checker moves (experimental, ADR-0114)

A **protected label** is a deployment label whose reassignment requires two distinct
principals: a *maker* proposes the move, a *checker* approves it. Direct `nova label set`
on a protected label is refused with guidance. Both steps are Ed25519-signed with the
per-identity keyring (ADR-0058: `~/.config/novafabric/keyring/`); self-approval is refused
at the crypto level (matching key fingerprint **or** identity). The applied move lands in
the same append-only `asset_label_history` audit table, reusing the pending move's ULID as
its `move_id`. Free (unprotected) labels are completely unchanged. Fully offline; the
optional `--policy-ref` Rego gate (ADR-0019) fails closed if the policy file is unreadable.

### nova label protect \<asset\> \<label\> [--required-approvals N] [--policy-ref PATH] [--note TEXT] [--unprotect] [--json]

Mark a label protected (or `--unprotect` to revert to free ADR-0113 behaviour). Protecting an
already-assigned label does not move it — it only governs future moves. Config changes are
append-only events; the active setting is the newest one.

```bash
nova label protect prompt:triage production
nova label protect prompt:triage production --required-approvals 2 --note "gates live traffic"
nova label protect prompt:triage production --unprotect
```

### nova label propose-move \<asset\> \<label\> --to \<version\> [--reason TEXT] [--identity NAME] [--json]

Maker step: create an Ed25519-signed pending move. The label does **not** move. Fail-closed:
the target version must exist, the label must be protected (free labels use `set`), and at
most one pending move per `(asset, label)` may exist at a time.

```bash
nova label propose-move prompt:triage production --to 8 --reason "v8 passed the eval gate"
```

### nova label approve-move \<asset\> \<label\> \<move_id\> [--reject] [--note TEXT] [--identity NAME] [--json]

Checker step: approve (or `--reject`, terminal) a pending move. SoD is enforced before
anything is recorded — the approver's keyring key fingerprint and identity must both differ
from the proposer's. When distinct approvals reach `required_approvals` **and** the policy
gate allows, the label is reassigned atomically with its audit row. A duplicate approver is
recorded but counts once.

```bash
nova label approve-move prompt:triage production 01J2Q8ZK7M4YZ2K7N9DPBYK2WX --identity bob
nova label approve-move prompt:triage production 01J2Q8ZK7M4YZ2K7N9DPBYK2WX --reject --note "regressed"
```

### nova label status \<asset\> [--label L] [--json]

Protection config, current label targets, and all recorded moves (pending and terminal) for
an asset.

```bash
nova label status prompt:triage
nova label status prompt:triage --label production --json
```

### nova report [--format {markdown,json,html,pdf}] [--output FILE]

Generate an asset inventory report. Defaults to Markdown on stdout.
Use `--output` to write to a file. Valid `--format` values: `markdown` (default), `json`, `html`, `pdf`. Tab-completion available via `nova --install-completion`.

**`html`** ([ADR-0201](./decisions.md); **works today**) — one
self-contained page (no JS, no external requests) with an assets-by-type inline-SVG chart, the
inventory table, and the canonical JSON embedded for machine consumption.

**`pdf`** (**works today**, optional dependency) — the same page rendered through WeasyPrint.
Requires the optional extra (`pip install 'novafabric[compliance]'`) and `--output`; without the
extra the command exits with the install hint instead of failing cryptically.

```bash
nova report --format html --output inventory.html
nova report --format pdf  --output inventory.pdf   # needs novafabric[compliance]
```

### nova validate \<path\>

Smart routing:

- If `path` is a **directory containing `capsule.yaml`**: validates the run capsule
  against `run-capsule.schema.json`, `environment.schema.json`, and `secret-redaction.schema.json`,
  and checks all required files exist.
- Otherwise: validates `path` as an asset YAML spec (v0.1 behavior).

```bash
nova validate .novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX/
nova validate my-model.yaml
```

Tool-call schema conformance ([ADR-0128](./decisions.md); **experimental**) —
capsule directories only:

| Option | Default | Effect |
|---|---|---|
| `--schemas` | off | Also validate each tool call's `arguments`/`result` against its declared `arguments_schema_ref`/`result_schema_ref` JSON Schemas and print a conformance summary. Report-only: exit 0 even on violations. Records with no schema_ref are counted as `null` ("no schema declared" — not a failure); unresolvable refs are reported, never fatal. Schema resolution is **local-only** (relative refs inside the capsule directory or absolute local paths; `http(s)://` refs are never fetched). |
| `--fail-on-schema-violation` | off | With `--schemas`: exit non-zero if any checked payload violates its schema (CI gate). |
| `--write` | off | With `--schemas`: persist the computed `schema_validation` verdict blocks back into `tool-calls.jsonl` (backfill capsules captured before this feature; `checked_at` records the backfill time). |

```bash
nova validate --schemas .novafabric/runs/<run-id>/                            # report-only
nova validate --schemas --fail-on-schema-violation .novafabric/runs/<run-id>/ # CI gate
nova validate --schemas --write .novafabric/runs/<run-id>/                    # backfill verdicts
```

At capture time the same verdict is attached automatically whenever a tool-call record declares a
schema_ref — record-only, never raised into the workload. At replay time stored tool calls are
re-validated against their current schemas: drift is recorded as `schema_drift` in
`replay_result.yaml` in every mode, and blocks eligibility (`exact_eligible: false`) in `exact`
mode only.

---

## Asset lifecycle commands — v0.12 additions (C-1.4, C-1.5)

### nova rollback \<name\> --actor \<id\>

Atomically roll back an asset to its most recent previous production version.

```bash
nova rollback my-agent --actor on-call-eng
nova rollback my-agent --to v1.8.0 --actor on-call-eng
```

Steps performed in one DB transaction:
1. Find the current `production` version (error if none).
2. Find the most recent prior `production` version (auto, or use `--to`).
3. Archive the current production version.
4. Promote the prior version back to `production`.
5. Write an audit log entry with a `rollback_reason` field.

Options:
- `--to <version>` — target an explicit rollback version instead of auto-discovery.
- `--actor <id>` — required; recorded in the audit log.

Errors clearly if the discovered prior version is archived (requires `--to`) or if
no production history exists.

### nova unregister \<name@version\>

Hard-delete an asset version from the registry. Removes the asset record, its eval
results, and approvals. Blocked for `staging`, `production`, and `pending_approval`
assets unless `--force` is given. Always writes an `UNREGISTER` audit entry.

```bash
nova unregister fraud-model@1.0.0
nova unregister fraud-model@1.0.0 --yes          # skip confirmation
nova unregister broken-agent@dev --force          # override status guard
nova unregister old-model@v2 --actor ops-eng --db ./registry.db
```

Options:
- `--actor <id>` — identity recorded in the audit trail (default: OS username).
- `--force` — override the status guard; permits deletion of `staging`/`production` assets.
- `--yes, -y` — skip interactive confirmation (for CI/scripts).
- `--db <path>` — override registry DB path (`NOVAFABRIC_DB_PATH` env var also accepted).

| Status at deletion | Default | With `--force` |
|---|---|---|
| `development`, `validated`, `archived` | ✅ allowed | ✅ allowed |
| `staging`, `production`, `pending_approval` | ❌ blocked | ✅ allowed |

> **Note:** Consumed and `depends_on` lineage edges referencing the deleted asset are
> preserved — they record what actually happened in captured runs. Only the synthetic
> `reg:<name>@<version>` dependency edges (written at registration time) are removed.

### nova capture ... --asset \<ref\> --require-asset-status \<statuses\>

Gate a capture run on the named asset's lifecycle status.

```bash
nova capture --asset my-agent@v1 --require-asset-status staging,production -- python agent.py
nova capture --asset my-agent@v1 --warn-if-asset-status development -- python agent.py
nova capture --asset my-agent@v1 --require-asset-status production --require-registered -- python agent.py
```

Options:
- `--asset <ref>` — asset to check (e.g. `my-agent@v1` or `my-agent`).
- `--require-asset-status <statuses>` — comma-separated statuses; exits non-zero before spawning the subprocess if the asset's status is not in the allowed set. Nothing is written to disk.
- `--warn-if-asset-status <statuses>` — emits a structured warning but does not block.
- `--require-registered` — blocks if the named asset is absent from the registry (default: warn only).

---

## Storage and server commands (v0.7)

> **Two optional server-mode components ship in v0.7 — they are independent:**
>
> | Command | Package extra | Purpose | Auth |
> |---|---|---|---|
> | `nova serve --experimental` | `novafabric[serve]` | **Local read-only dashboard** — browse capsules, registry, and lineage in a browser. Single-user, loopback-only. | Single-use token |
> | `nova server start` | `novafabric[server]` | **Multi-user REST API** — Postgres/SQLite backend, OIDC + RBAC, offline tokens. For teams and CI pipelines. | OIDC Bearer / offline JWT |
>
> Local CLI commands (`nova capture`, `nova validate`, `nova replay`, etc.) require **neither** extra and work without any server.

Requires: `pip install 'novafabric[server]'`

### nova doctor [--check-storage] [--check-scheduler]

Run diagnostic checks on the NovaFabric installation.

```bash
nova doctor --check-storage
nova doctor --check-storage --backend postgres
nova doctor --check-storage --backend postgres --postgres-dsn "postgresql://..."
nova doctor --check-storage --db-path /path/to/custom.db
nova doctor --check-scheduler
```

Without any flag, prints a hint and exits 0. With `--check-storage`:

- Reports the active backend (`sqlite` or `postgres`).
- Shows the Alembic schema version and migration status.
- Prints per-table row counts.
- Exits 0 on success, 1 on error.

With `--check-scheduler` (**works today**, OQ-06 / PAR-ADR-003 condition 2): detects a
mismatch between a scheduler's own native env vars (e.g. Slurm's `SLURM_JOB_ID`, set by
the scheduler daemon itself) and the `NOVAFABRIC_*` env-var contract (FR-18) a submission
wrapper should have propagated into the job. This surfaces, on demand, the case FR-20's
runtime fallback otherwise handles silently: capture still defaults to
`capsule_role=STANDALONE` with a synthesised `global_run_id` so the workload is never
blocked, but that quietly loses parent/child linkage for the run. For Slurm specifically,
also reads `SLURM_EXPORT_ENV` to distinguish a site `--export=NONE`/`NIL` policy (env
export disabled at the cluster level) from a submission-script gap (the wrapper simply
never set `NOVAFABRIC_GLOBAL_RUN_ID`). Exits 0 if no scheduler is detected or the contract
vars are present; exits 1 with a diagnosis + remediation hint otherwise.

Options:
- `--check-storage` — enable storage health report (ADR-0016)
- `--check-scheduler` — enable the scheduler/env-var contract check (OQ-06)
- `--backend TEXT` — `sqlite` (default) or `postgres`
- `--db-path PATH` — override the SQLite path; defaults to `NOVAFABRIC_DB_PATH`
- `--postgres-dsn TEXT` — Postgres DSN; defaults to `NOVAFABRIC_POSTGRES_DSN`

---

### `nova ingest-capsule`

Populate the `runs_cache` index from capsule files on disk. Does not require
`nova serve` to be running. Requires Scale-S1 (`runs_cache` table) which ships
with `nova serve`.

```
nova ingest-capsule [RUN_ID] [OPTIONS]
```

**Arguments:**
- `RUN_ID` — Run ID to ingest (required unless `--all` or `--watch`)

**Options:**

| Flag | Default | Description |
|---|---|---|
| `--all` | off | Re-index all capsules in `capsule-dir` |
| `--watch` | off | Foreground watcher loop (Ctrl+C to stop) |
| `--interval FLOAT` | 2.0 | Poll interval in seconds (`--watch` only) |
| `--backend TEXT` | auto | `auto` \| `polling` \| `watchdog` |
| `--capsule-dir PATH` | `$NOVAFABRIC_CAPSULE_DIR` | Override capsule directory |
| `--db-path PATH` | `$NOVAFABRIC_DB_PATH` | Override registry DB path |

**Environment variables:**

| Variable | Default | Effect |
|---|---|---|
| `NOVA_WATCHER_BACKEND` | `auto` | Override backend selection globally |
| `NOVA_WATCHER_INTERVAL` | `2.0` | Override poll interval globally |

**Examples:**
```bash
# Index a specific run
nova ingest-capsule abc123

# Full re-index after moving capsules from another machine
nova ingest-capsule --all

# Foreground watcher — prints each new capsule as it appears
nova ingest-capsule --watch --interval 5

# Use inotify/FSEvents backend (requires pip install novafabric[watch])
nova ingest-capsule --watch --backend watchdog
```

**Works today.** Requires `novafabric` ≥ v0.36.0.

---

### nova migrate-to-postgres

One-time idempotent migration from a local SQLite registry to Postgres. Per [ADR-0016](./decisions.md).

```bash
# Dry run — see what would be migrated without writing
nova migrate-to-postgres --dry-run

# Full migration
nova migrate-to-postgres \
  --source ~/.novafabric/registry.db \
  --target "postgresql://user:pass@host/db"

# With a JSONL migration log
nova migrate-to-postgres --target "$NOVA_DSN" --log migration.jsonl
```

Options:
- `--source PATH` — source SQLite database (default: `~/.novafabric/registry.db`)
- `--target TEXT` — Postgres DSN; can also be set via `NOVAFABRIC_POSTGRES_DSN`
- `--dry-run` — list capsules that would be migrated without writing
- `--log PATH` — optional JSONL log file (one record per table)

Exit codes: `0` = success (row counts verified), `1` = verification failure, `2` = connection error.

The SQLite file is never modified or deleted. Upsert semantics prevent
duplicates on re-run after a partial failure.

---

### nova migrate-schema

Batch-migrates capsule directories from schema v0 to v1.0.0. Walks every capsule sub-directory under `--capsule-dir`, inspects the manifest, and applies any needed upgrades. Safe to re-run — already-migrated capsules are skipped.

```bash
# Preview changes without writing (recommended first pass)
nova migrate-schema --capsule-dir ~/.novafabric/capsules --dry-run

# Migrate in-place
nova migrate-schema --capsule-dir /data/nova/capsules

# Migrate with .v0.bak rollback copies
nova migrate-schema --capsule-dir /data/nova/capsules --backup
```

What the migration does for each capsule whose `manifest.json` is below v1:

1. Sets `schema_version` → `"1.0.0"`
2. Renames `event_log.jsonl` → `model-calls.jsonl` (legacy v0 file name)
3. Adds `format_version: "1"` to the metadata block when absent

Options:
- `--capsule-dir PATH` — root directory containing one sub-directory per capsule (default: `~/.novafabric/capsules/`)
- `--dry-run` — preview what would change; no files are written
- `--backup` — copy original files to `<file>.v0.bak` before overwriting; allows manual rollback

Exit codes: `0` = all capsules migrated or already at v1, `1` = one or more capsule migrations failed.

Implemented in `src/novafabric/cli/migrate_schema.py` (G-F track, v0.29.0).

---

### nova db (Phase 5 — MetadataStore management)

MetadataStore management commands (ADR-0040, FR-05, FR-06). Requires `novafabric[server]` for Postgres operations.

#### nova db migrate-to-postgres

Idempotent migration of MetadataStore tables (runs, capsules, signatures, retention_policies) from a local SQLite metadata database to Postgres.

```bash
nova db migrate-to-postgres \
  --source ~/.novafabric/metadata.db \
  --target "postgresql://nova:pass@host:5432/novafabric"

# With batch size and JSON report
nova db migrate-to-postgres \
  --source ~/.novafabric/metadata.db \
  --target "$NOVA_META_DSN" \
  --batch-size 500 \
  --report /tmp/migration-report.json
```

Options:
- `--source PATH` — source SQLite metadata.db (**required**)
- `--target TEXT` — Postgres DSN (**required**)
- `--batch-size INT` — rows per INSERT batch (default: 1000)
- `--report PATH` — write a JSON migration report to this path

Exit codes: `0` = success (row counts verified), `1` = count mismatch, `2` = connection/import error.

All inserts use `ON CONFLICT DO NOTHING` — safe to re-run after a partial failure. The source SQLite file is never modified.

Requires `pip install novafabric[server]` (psycopg3).

#### nova db upgrade

Run `alembic upgrade <revision>` for the selected migration track. Two
parallel Alembic tracks exist (ADR-0211 D5): the **MetadataStore** tier
(default — unchanged behavior) and the **registry/server** database (the DB
the server lifespan opens and `nova backup create --profile pg` dumps; the
startup schema-skew guard names this track). `--track registry` is
**experimental**.

```bash
# MetadataStore tier (default; reads NOVAFABRIC_DB_PATH,
# defaults to ~/.novafabric/metadata.db)
nova db upgrade

# MetadataStore tier on Postgres (reads NOVAFABRIC_METADATA_DSN)
export NOVAFABRIC_METADATA_DSN="postgresql://nova:pass@host:5432/novafabric"
nova db upgrade --backend postgres

# Registry/server DB (experimental, ADR-0211): SQLite registry
nova db upgrade --track registry --backend sqlite

# Registry/server DB on Postgres (reads NOVAFABRIC_POSTGRES_DSN) — the
# command the schema-skew guard and the pg restore runbook name
export NOVAFABRIC_POSTGRES_DSN="postgresql://nova:pass@host:5432/nova"
nova db upgrade --track registry --backend postgres
```

Options:
- `--backend TEXT` — `sqlite` (default) or `postgres`
- `--track TEXT` — `metadata` (default) or `registry` (experimental)
- `--revision TEXT` — alembic revision target (default `head`)

Environment variables consumed:
- `NOVAFABRIC_DB_PATH` — SQLite database path (metadata track)
- `NOVAFABRIC_METADATA_DSN` — Postgres DSN (metadata track)
- `NOVAFABRIC_HOME` — registry DB location (registry track, sqlite)
- `NOVAFABRIC_POSTGRES_DSN` — Postgres DSN (registry track; never printed)

The registry-track migration trees are packaged into the wheel
(`novafabric/migrations/registry/`), so `--track registry` works from an
installed package, not only a source checkout.

Exit codes: `0` = success, `1` = alembic/database failure (registry track),
`2` = config/connection error.

---

### nova server start

Start the multi-user REST API server. Per [ADR-0017](./decisions.md) and [ADR-0029](./decisions.md).

```bash
nova server start
nova server start --backend postgres
nova server start --config /etc/novafabric/nova-server.yaml
nova server start --host 0.0.0.0 --port 7433

# Horizontal scaling (v0.98.0, experimental) — requires the postgres backend
nova server start --backend postgres --workers 4

# Machine-parseable logs with per-request correlation ids (v0.98.0, experimental)
nova server start --log-format json
```

The server reads `~/.config/novafabric/nova-server.yaml` by default. CLI flags
override the config file. See [`docs/ops/server-deployment.md`](ops/server-deployment.md)
for config examples.

**Single-tenant scope (ADR-0178).** Capsule storage is **not partitioned per
organization** — organizations and workspaces scope the *registry* tier, not
capsule bytes. The server therefore refuses to start when more than one
organization exists, unless you acknowledge this with
`--i-accept-shared-capsule-store`. Single-organization deployments (the default,
and what the bootstrap creates) are unaffected. Per-tenant capsule partitioning
is pending a security review; until it lands, do not rely on organizations for
capsule isolation.

Options:
- `--config, -c FILE` — path to server YAML config (default: `~/.config/novafabric/nova-server.yaml`)
- `--backend TEXT` — `sqlite` (default) or `postgres`
- `--host TEXT` — bind address (default from config or `127.0.0.1`)
- `--port, -p INTEGER` — bind port (default from config or `7433`)
- `--insecure-no-auth` — disable local-token auth; anonymous admin (ADR-0184). Loopback only unless also passing `--i-know-this-is-public`
- `--i-know-this-is-public` — second confirmation required to combine `--insecure-no-auth` with a non-loopback `--host`
- `--i-accept-shared-capsule-store` — acknowledge the unpartitioned capsule store and run multiple organizations anyway. Env: `NOVAFABRIC_SERVER_I_ACCEPT_SHARED_CAPSULE_STORE`
- `--workers, -w INTEGER` — uvicorn worker processes for horizontal scaling (default `1`, **experimental**, v0.98.0). Values `>1` require `--backend postgres` — multiple processes cannot safely share a SQLite file — and launch the app through an import-string factory (`server/factory.py`) so each worker reconstructs its config from the config file and environment
- `--log-format TEXT` — `text` (default) or `json` (**experimental**, v0.98.0). JSON emits one object per log record including the request-correlation id. Env: `NOVAFABRIC_SERVER_LOG_FORMAT`; the CLI exports it so `--workers` processes inherit the format

**Request correlation (v0.98.0, experimental).** Every request carries an id — taken from an inbound
`X-Request-ID` header (sanitised: safe characters, max 128, otherwise regenerated to block log
injection) or freshly generated — echoed on the response and attached to every log record, so a
request can be traced across access logs, audit events, and SIEM egress.

**Connection pooling (ADR-0221, experimental, opt-in).** Set `NOVAFABRIC_METADATA_DB_POOL=1` to
back the Postgres metadata store with a psycopg pool (`NOVAFABRIC_METADATA_DB_POOL_MIN` /
`_MAX`, default `1`/`10`). Off by default; SQLite is unaffected. Pool utilisation is exported as
the `nova_db_pool_in_use` / `nova_db_pool_size` gauges, sampled at scrape time.

---

### nova server issue-token

Issue a signed offline JWT for airgapped or SLURM deployments. Per [ADR-0018](./decisions.md).

```bash
nova server issue-token --subject user@example.com --roles writer --expires-in 90d
nova server issue-token --subject ci-runner --roles reader,writer --expires-in 30d
nova server issue-token --subject admin@cluster --roles admin \
  --key-path /etc/novafabric/keys/offline-key.pem
```

Prints the raw JWT to stdout. If the key file does not exist, a new ed25519
keypair is generated.

Options:
- `--subject TEXT` (required) — token subject (email or identifier)
- `--roles TEXT` — comma-separated roles: `reader`, `writer`, `admin`, `auditor` (default: `reader`)
- `--expires-in TEXT` — token lifetime, e.g. `90d` or `30d` (default: `90d`)
- `--key-path PATH` — ed25519 private key PEM; defaults to `NOVAFABRIC_OFFLINE_KEY_PATH` or `~/.novafabric/keys/offline-key.pem`

---

### nova server revoke-token \<token-id\>

Revoke an offline token by its `jti` claim. Records the revocation in the
token audit table; the token returns HTTP 401 on the next API call.

```bash
nova server revoke-token 01HX7K4P9DPBYK2WX01HXAY7M
```

Arguments:
- `TOKEN_ID` (required) — the `jti` value from the issued JWT

Options:
- `--key-path PATH` — path to ed25519 private key PEM (used to locate the audit store)

---

### nova server assign-role \<user\> \<role\>

Assign a local role to a user. Writes to the `role_assignments` table on the
active backend. Per [ADR-0018](./decisions.md).

```bash
nova server assign-role user@example.com writer
nova server assign-role ci-runner@cluster reader --assigned-by ops-team
```

Valid roles: `reader`, `writer`, `admin`, `auditor`.

Arguments:
- `USER` (required) — subject (email or identifier)
- `ROLE` (required) — role to assign

Options:
- `--assigned-by TEXT` — who is making the assignment (default: `cli`)
- `--db-path PATH` — SQLite database path (overrides default)

REST equivalent (per [ADR-0060](./decisions.md)):
```bash
curl -X POST http://localhost:7433/v0/admin/roles \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"subject": "user@example.com", "role": "writer"}'
```

---

### nova server revoke-role \<user\> \<role\>

Revoke a role from a user. Per [ADR-0060](./decisions.md);
enforces the **last-admin lockout invariant** at the store layer — if revoking
would leave `role_assignments` with no `admin` row AND no `NOVA_OIDC_ISSUER`
configured, the command refuses with exit code 2.

```bash
nova server revoke-role user@example.com writer
nova server revoke-role old-bot@cluster admin --db-path /opt/nova/registry.db
```

Arguments:
- `USER` (required) — subject (email or identifier)
- `ROLE` (required) — role to revoke

Options:
- `--db-path PATH` — SQLite database path (overrides default)

Exit codes:
- `0` — success (role revoked)
- `1` — assignment not found, or other generic failure
- `2` — refused by last-admin lockout guard

REST equivalent:
```bash
curl -X DELETE http://localhost:7433/v0/admin/roles/user@example.com/writer \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

---

### nova server scim-map-group \<group\> \<role\> (experimental, ADR-0139 D3)

Declare, `--remove`, or `--list` IdP-group → RBAC-role mappings in the server's
YAML config (`scim.group_role_map`, [ADR-0029](./decisions.md)),
per [ADR-0139](./decisions.md) D3. SCIM group membership then
grants/revokes the mapped role through the `/scim/v2/Groups` routes. `--list` reads the
effective loaded config; setting or removing a mapping edits the YAML in place — a running
server picks up the change on restart. Scope: single server.

```bash
nova server scim-map-group Engineering writer
nova server scim-map-group SRE-Admins admin
nova server scim-map-group Engineering --remove
nova server scim-map-group --list
nova server scim-map-group --list --config /opt/nova/server.yaml
```

Arguments:
- `GROUP` — IdP group `displayName` (omit only with `--list`)
- `ROLE` — RBAC role: `reader`, `writer`, `admin`, `auditor`, `promoter`, `approver` (omit with `--remove` or `--list`)

Options:
- `--remove` — remove the mapping for `GROUP` instead of setting it
- `--list` — print the effective group→role map and exit
- `--config PATH` — server YAML config path (default: `~/.config/novafabric/server.yaml`)

---

### nova server list-scim-events (experimental, ADR-0139 D5)

Read-only append-only audit trail of SCIM provisioning: who was provisioned or
deprovisioned, when, and every group→role remap. Scope: single server.

```bash
nova server list-scim-events
nova server list-scim-events --subject alice@example.com
nova server list-scim-events --json
```

Options:
- `--subject TEXT` — filter the trail to one subject (`userName`)
- `--json` — emit the events as a JSON array for tooling
- `--db-path PATH` — SQLite database path (overrides default)

---

### nova server flush-jwks-cache

Force the running server to re-fetch its JWKS from the OIDC provider. Use
after rotating signing keys at the identity provider.

```bash
nova server flush-jwks-cache --server http://nova.example.com:7433
nova server flush-jwks-cache --server http://localhost:7433 --token "$ADMIN_TOKEN"
```

Options:
- `--server TEXT` — server URL (default: `http://localhost:7433`)
- `--token TEXT` — Bearer token with `admin` role; can also be set via `NOVA_ADMIN_TOKEN` or by running `nova login` first

---

### nova server saml-metadata

**Experimental (ADR-0138, partial slice).** Emit this server's SAML 2.0
Service Provider metadata XML (entity ID, ACS URL, SP signing certificate) so
an IdP administrator can register NovaFabric as an SP. Read-only, no side
effects. Requires a `saml:` block with `enabled: true` in the server config
([ADR-0138](./decisions.md),
spec).

```bash
nova server saml-metadata
nova server saml-metadata --config /etc/novafabric/server.yaml > sp-metadata.xml
```

Options:
- `--config, -c PATH` — server YAML config file (default: `~/.config/novafabric/server.yaml`)

Honest status: the `server.saml` config block, this metadata emitter,
attribute→role mapping, and the assertion validation policy are implemented;
**live SAML login is not** — the assertion consumer endpoint refuses (HTTP 501)
until the ADR-0138 D5 XML-signature library clears the ADR-0024
transitive-license gate. NovaFabric never consumes an assertion without
verified signatures.

---

### nova server api-key create (experimental, ADR-0193)

**Experimental (ADR-0193, first slice).** Create a first-class API key
`nvfk_<key_id>_<secret>` bound to an owning principal and a role set from the
existing RBAC vocabulary (`reader`, `writer`, `admin`, `auditor`). Only the
sha256 of the secret is stored — the full key is printed **once** and cannot
be recovered later. Creation is appended to the hash-chained audit log
(spec).

```bash
nova server api-key create --owner alice@example.com --roles reader
nova server api-key create --owner svc:ci-bot --roles reader,writer --expires-in 90d
```

Requests then authenticate with `Authorization: Bearer nvfk_...`; the server
resolves the key before any JWT parsing, so keys work in both OIDC and local
modes.

Options:
- `--owner TEXT` (required) — owning principal (user or `svc:<name>`)
- `--roles TEXT` — comma-separated roles (default: `reader`)
- `--workspace TEXT` — optional workspace scope (ADR-0178; stored, not yet enforced)
- `--expires-in TEXT` — optional lifetime, e.g. `90d` (default: no expiry)
- `--created-by TEXT` — actor recorded in the audit log (default: `cli`)
- `--db-path PATH` — SQLite database path (overrides default)

Honest status: `create`/`list`/`revoke` work today; `rotate` (successor key
with an overlap window) and `last_used_at` tracking are the next ADR-0193
slice and are **not implemented yet**.

---

### nova server api-key list (experimental, ADR-0193)

List API keys — metadata only (key_id, owner, roles, workspace, created,
expiry, status). Secrets and hashes are never stored, so they can never be
shown.

```bash
nova server api-key list
nova server api-key list --json
```

Options:
- `--json` — emit a JSON array for tooling
- `--db-path PATH` — SQLite database path (overrides default)

---

### nova server api-key revoke \<key-id\> (experimental, ADR-0193)

Revoke an API key by its public `key_id` — effective on the next request
(verification is a DB lookup; there is no token-style revocation-propagation
gap). The revocation is appended to the hash-chained audit log.

```bash
nova server api-key revoke a1b2c3d4
```

Arguments:
- `KEY_ID` (required) — the public key identifier shown by `create` and `list`

Options:
- `--revoked-by TEXT` — actor recorded in the audit log (default: `cli`)
- `--db-path PATH` — SQLite database path (overrides default)

Exit codes:
- `0` — success (key revoked)
- `1` — key_id not found, or other failure

---

### nova server api-key rotate \<key-id\> (experimental, ADR-0193)

Rotate an API key: mint a **successor** with identical bindings (owner, roles,
workspace, expiry) and print it **once**. Both the predecessor and successor
stay valid for a bounded, configurable **overlap window**; after it elapses the
predecessor is auto-revoked at verify time (checked on the next request — there
is no background job). A zero-downtime credential swap for deployed agents.
Both transitions are appended to the hash-chained audit log.

```bash
nova server api-key rotate a1b2c3d4
nova server api-key rotate a1b2c3d4 --overlap-seconds 3600
```

Arguments:
- `KEY_ID` (required) — the public key identifier to rotate

Options:
- `--overlap-seconds INT` — overlap window in seconds during which BOTH keys
  verify (default: `NOVA_API_KEY_ROTATE_OVERLAP_S`, or `86400` = 24h)
- `--rotated-by TEXT` — actor recorded in the audit log (default: `cli`)
- `--db-path PATH` — SQLite database path (overrides default)

Exit codes:
- `0` — success (successor printed once)
- `1` — key_id not found, key already revoked, or other failure

> The successor key is shown once and cannot be recovered — store it
> immediately. `last_used_at` (coarse, at most one write per
> `NOVA_API_KEY_LASTUSED_INTERVAL_S`, default daily) is surfaced by `list`.

The same lifecycle is also available over REST at the admin-gated
`/v0/api-keys` resource (`POST` create, `GET` list, `DELETE {key_id}` revoke,
`POST {key_id}/rotate`); the dashboard admin console reads it read-only via
`GET /api/admin/api-keys`.

---

### nova login

Authenticate with a NovaFabric server via Device Authorization Grant (RFC 8628,
[ADR-0018](./decisions.md)). Credentials are stored in
`~/.config/novafabric/credentials.json` at mode `0600`.

```bash
nova login
nova login --server http://nova.example.com:7433
```

The CLI prints a URL and user code. After the user approves in the browser,
the access token is stored and used automatically on subsequent commands.
Tokens are auto-refreshed; re-run `nova login` when the refresh token expires.

Options:
- `--server TEXT` — server URL (default: `http://localhost:7433`)

Local CLI commands (`nova capture`, `nova validate`, `nova replay`, etc.) never
require credentials — authentication is only needed for server-mode operations.

---

### nova logout

Remove stored credentials for a NovaFabric server.

```bash
nova logout --server http://nova.example.com:7433   # remove one server
nova logout                                           # remove all servers
```

Options:
- `--server TEXT` — server URL to log out of; omit to remove all stored credentials

---

## Backup, restore, and support diagnostics (experimental)

Evidence-grade operational tooling: signed local backup sets with offline
verification (ADR-0181), a verification-gated restore path, and a secret-safe
diagnostics tarball for support (ADR-0187).

### nova backup create (experimental, ADR-0181 / ADR-0216)

Create a backup set covering **every persistent local store** (ADR-0216):
registry, capsules, incidents, metadata, the PII DEK store, seal transparency
log, TSA nonces, ratchet state, dashboard state, spool, the audit log, and a
secret-redacted config. SQLite stores are snapshotted with the online-backup
API, so live writers are safe; `dashboard.duckdb` is snapshotted via DuckDB's
own consistent-copy mechanism (skipped honestly when a live `nova serve`
holds the writer lock — it is a rebuildable derived cache). The signed
manifest carries a **coverage table**: what was NOT captured is recorded,
never silent. The `pg` profile adds a `pg_dump` member; the `manifest`
profile (WORM object-store deployments) records chain heads + checkpoints
instead of blobs. Signing keys are excluded unless `--include-keys`.
Connection strings are treated as secrets: the DSN never appears in the set,
the manifest, or any output.

> **Custody note:** the default set includes the PII DEK store (`dek.db`) so
> restored PII stays readable — treat backup sets as sensitive artifacts and
> store them encrypted at rest. Crypto-shred replay on restore guarantees
> shredded subjects stay shredded regardless.

```bash
nova backup create
nova backup create -o /mnt/backups/
nova backup create -o nightly.tar.gz --include-keys
nova backup create --profile pg --dsn postgresql://…  -o pg-nightly.tar.gz
nova backup create --profile manifest --backend s3 -o manifest-set.tar.gz
```

Options:
- `--output, -o PATH` — target `.tar.gz` file (or existing directory). Default: `./nova-backup-<set_id>.tar.gz`
- `--home PATH` — NovaFabric home to back up (default: `NOVAFABRIC_HOME` or `~/.novafabric`)
- `--profile TEXT` — backup profile: `local` (SQLite deployment), `pg` (adds a `pg_dump --format=custom` member), or `manifest` (chain heads + checkpoints against a WORM object store, no blobs). Default: `local`
- `--dsn TEXT` — Postgres DSN for `--profile pg` (default: `NOVA_DSN` or `NOVAFABRIC_POSTGRES_DSN`). Never logged; the manifest stores only the redacted host/dbname
- `--include-keys` — ALSO pack the signing keyring and `novaseal.yaml` + its key/cert PEMs (ADR-0216 D4). Default off; a set created with this flag requires key-custody care
- `--backend TEXT` — object-store backend for `--profile manifest`: `local | s3 | minio | ceph_rgw | azure_blob` (env: `NOVA_OCS_BACKEND`)
- `--tenant TEXT` — scope the manifest listing to these tenant(s) (repeatable)
- `--allow-pending-wal` — proceed with `--profile manifest` even when the local WAL has pending un-chained uploads (the gap is recorded in the listing); default: refuse
- `--deep` — `--profile manifest`: fully verify every chain at create time, not just pin the heads

Exit codes: `0` (set created), `1` (backup error).

---

### nova backup verify (experimental, ADR-0181)

Verify a backup set offline against its manifest. Recomputes every member's
SHA-256 and, when the set is signed, verifies the manifest's DSSE envelope.
Requires no live deployment, network, or private keys.

```bash
nova backup verify nova-backup-01J....tar.gz
```

Options:
- `<set-path>` (positional, required) — backup-set archive (`.tar.gz`) to verify offline

Exit codes: `0` (all members and signature verify), `1` (any mismatch).

---

### nova restore \<set-path\> (experimental, ADR-0181 / ADR-0211)

Restore a backup set, then run the verification chain. The verified
manifest's profile drives dispatch — there is no restore-side `--profile`
flag. Normative order (ADR-0181/0216/0217): verify the set → prepare the home
→ extract (sensitive members restored 0600; external-origin members such as
the audit log restored to their real roots, never silently overwritten) →
migrate to head → replay crypto-shreds (shredded data stays shredded — a
moved-aside live audit log is also replayed, so shreds applied after the
backup survive) → advance regressed ratchet epochs → storage, seal-log, and
per-store integrity checks. The restore is complete ONLY when verification
passes — there is no flag to skip it.

`pg`-dump sets restore automatically (ADR-0217): a non-empty target DB is
refused without `--force` (which first takes a safety dump into the
`.pre-restore-…/` directory), `pg_restore` runs in a single transaction
(failure leaves the DB unchanged), then alembic migrations, manifest-anchored
row counts, and RLS enforcement are verified. `manifest`-only sets verify
every pinned chain head against the live bucket and rebuild the metadata DB
from the chain.

```bash
nova restore nova-backup-01J….tar.gz
nova restore set.tar.gz --home /srv/novafabric --force
nova restore pg-nightly.tar.gz --dsn postgresql://…
nova restore manifest-set.tar.gz --backend s3 --sample 10
```

Options:
- `<set-path>` (positional, required) — backup-set archive (`.tar.gz`) to restore from
- `--home PATH` — target NovaFabric home (default: `NOVAFABRIC_HOME` or `~/.novafabric`)
- `--force` — restore into a non-empty home: existing data is moved aside into a timestamped `.pre-restore-…/` directory (never deleted)
- `--restore-keys` — restore `key_material` members from a set created with `--include-keys` (ADR-0216 D4); default off — the opt-in is required on both sides
- `--dsn TEXT` — target Postgres DSN when restoring a pg-dump set (default: `NOVA_DSN` or `NOVAFABRIC_POSTGRES_DSN`). Never logged
- `--backend TEXT` — object-store backend when restoring a manifest-only set (env: `NOVA_OCS_BACKEND`)
- `--sample INT` — manifest-only sets: spot-check this many capsule payload hashes against the live bucket (default: `0`)

Exit codes: `0` (restore + verification chain passed), `1` (any failed step), `2` (manifest-only set: the listed bucket is unreachable).

---

### nova support-bundle (experimental, ADR-0187)

Produce a secret-safe diagnostics tarball for support. Contains ONLY
allowlisted members: `doctor.json`, `versions.json`, `env.txt`
(`NOVAFABRIC_*`/`NOVA_*` variable **names** only — never values),
`health.json`, `config.redacted.yaml` (if a server config exists, with
secret-keyed values redacted), and a `manifest.json` with the SHA-256 of every
member plus the redaction ruleset version. No tokens, keys, credentials,
capsule payloads, prompts, or responses are ever included. Scope: global
(snapshots the whole installation).

```bash
nova support-bundle                     # default name in the current directory
nova support-bundle -o /tmp/diag.tar.gz
```

Options:
- `--output, -o PATH` — output tarball path (default: `./nova-support-bundle-<timestamp>.tar.gz`)
- `--log-window-hours INT` — bound for the structured-log window recorded in the manifest (default: `24`)

Exit codes: `0` (bundle written), `1` (bundle error).

---

## Audit-log SIEM egress (experimental, ADR-0191)

Export local audit logs in SIEM-native formats. NovaFabric produces correctly
formatted, correctly redacted lines; the site's own shipper (Splunk UF,
Filebeat, Vector, Fluent Bit, rsyslog) does transport — there is no network
sender, no default endpoint, no background egress.

### nova audit-log export (experimental, ADR-0191)

One-shot export of an audit source over a time window, one entry per line, to
stdout or a file. The first output line is a manifest recording the
redaction-ruleset versions in force. Every line passes the deny-by-default
redaction pipeline (strict field allowlist plus the ADR-0187 support-bundle
secret ruleset) — non-allowlisted fields never leave.

For `--source audit` (the hash-chained log) the chain is re-verified during
the walk; `entry_hash`/`prev_hash` are exported verbatim in `jsonl` and ride
in the OCSF `unmapped` object, so a SIEM analyst can check that what the SIEM
holds is what the chain produced. In `ocsf` format, audit event types map onto
OCSF classes (API Activity 6003, Application Lifecycle 6002, Authentication
3002) per the mapping table in `design/spec/audit-siem-egress-v0.md`; fields
OCSF has no slot for are preserved verbatim under `unmapped` — no silent loss.

In `cef` format (for legacy ArcSight-style collectors) each entry becomes one
`CEF:0` event. The OCSF class selection above is reused, so the two formats
never disagree about what an event is; the CEF signature id keeps the **native**
`event_type`/`action` and the numeric OCSF class rides in `cs5`. The chain
hashes map to labelled custom strings (`cs1`=`entryHash`, `cs2`=`prevHash`) and
every remaining redacted field is packed into `cs6` as compact JSON — again, no
silent loss. The manifest line is itself a CEF event, so a `cef` stream is pure
CEF with no JSON line to special-case. Full mapping and escaping tables:
`design/spec/audit-siem-egress-v0.md` §2b.

There is no `server` source and none is planned: the server app writes its
route events into the same log as the dashboard, so `--source dashboard`
already covers them (OQ-038, resolved).

```bash
nova audit-log export
nova audit-log export --source audit --format ocsf --out audit.ocsf.jsonl
nova audit-log export --source audit --format cef --out audit.cef
nova audit-log export --source dashboard --since 2026-07-01T00:00:00Z
nova audit-log export --since 2026-07-01T00:00:00Z --until 2026-07-08T00:00:00Z
```

Options:
- `--source TEXT` — audit source: `audit` (hash-chained log) or `dashboard` (`nova serve` mutation log). Default: `audit`
- `--format TEXT` — output format: `jsonl` (native, zero-mapping-loss), `ocsf`, or `cef` (ArcSight CEF:0 for legacy collectors). Default: `jsonl`
- `--since TEXT` — inclusive ISO-8601 lower bound (naive timestamps are treated as UTC)
- `--until TEXT` — exclusive ISO-8601 upper bound (naive timestamps are treated as UTC)
- `--out PATH` — output file (default: stdout)

Exit codes: `0` (exported OK), `2` (bad parameters), `3` (chain verification
failed — the export is still written, so pipelines can alert on the tamper
evidence itself).

### nova audit-log tail (experimental, ADR-0191)

Streams audit entries to stdout as they are written, in the same three
formats and through the same redaction pipeline as `export`. This is a
foreground process **you** run — a systemd unit or a sidecar — not a
NovaFabric-managed daemon. There is no network sink and no default endpoint:
pipe stdout into your own shipper.

By default it starts at the end of the log (`tail` semantics); `--from-start`
replays the existing log first. Without `--follow` it makes one bounded pass
and exits, so it is safe in a script.

Log **rotation** (rename-based, as `logrotate` does by default) and in-place
**truncation** (`copytruncate`) are detected and followed; entries written
just before a rename are drained from the old file rather than lost. Chain
continuity *across* a rotation cannot be verified — the predecessor entry's
hash goes with the old file — so a restart is reported as a chain event
rather than passed off as an unbroken chain. Honest limit: a truncation that
is refilled past the old offset within a single poll interval is
indistinguishable from an append by size alone, and those entries are missed.

```bash
nova audit-log tail --follow | your-shipper
nova audit-log tail --follow --format cef --source dashboard
nova audit-log tail --from-start --format ocsf
```

With `--out` the sink is a **size-bounded rotating file** instead of stdout,
for a file shipper (Filebeat, Vector, Fluent Bit) to pick up. Rotation uses
the conventional numbered-suffix scheme (`audit.cef` → `audit.cef.1` → …),
happens *before* a write that would cross the threshold so a rendered record
is never split across two files, and deletes the generation beyond
`--backup-count`. With `--backup-count 0` the file is truncated instead of
kept, bounding disk use to `--max-bytes` total. Reopening appends rather than
truncating, so a restarted tailer does not destroy what it already shipped.

```bash
nova audit-log tail --follow | your-shipper
nova audit-log tail --follow --format cef --source dashboard
nova audit-log tail --from-start --format ocsf
nova audit-log tail --follow --format cef --out /var/log/nova/audit.cef
```

Options:
- `--source TEXT` — audit source: `audit` or `dashboard`. Default: `audit`
- `--format TEXT` — output format: `jsonl`, `ocsf`, or `cef`. Default: `jsonl`
- `--follow / --no-follow`, `-f` — keep running and render new entries as they arrive. Default: off (single bounded pass)
- `--from-start` — replay the existing log first, then follow
- `--poll-interval FLOAT` — seconds between polls when the log is idle. Default: `1.0`
- `--out PATH` — write to a rotating file instead of stdout
- `--max-bytes INT` — rotate `--out` at this size. Default: `10485760` (10 MiB)
- `--backup-count INT` — rotated generations to keep. Default: `5`

- `--syslog ADDRESS` — send RFC 5424 messages to a **local** syslog endpoint: a unix socket path (`/dev/log`) or a loopback `host:port`. No default.
- `--syslog-transport TEXT` — `auto` (unix for a path, else udp), `unix`, `udp`, or `tcp`. Default: `auto`

`--out` and `--syslog` are alternative sinks; passing both is an error.

**Syslog sink.** Messages are RFC 5424 (`<134>1 <ts> <host> novafabric <pid>
audit-<format> - <line>`); the MSG body is byte-identical to what the other
sinks emit. TCP uses RFC 6587 octet counting so a stream receiver can find
boundaries. UDP messages are bounded and, if shortened, **marked** with
`…[NOVAFABRIC-TRUNCATED]` — a silently shortened audit record would read as
a complete one. Ship large CEF records over TCP or a unix stream socket.

**The endpoint must be local.** A non-loopback host is refused, not
warned about: ADR-0191 D3 scopes this to a local endpoint, and getting audit
data off the box remains your syslog daemon's job — it already owns the TLS,
retry and buffering that NovaFabric deliberately does not implement. There
are still no built-in senders to Splunk/Elastic/Sentinel (ADR-0191 D6).

Exit codes: `0` (streamed OK), `2` (bad parameters — absurd rotation config,
non-loopback syslog host, unreachable endpoint), `3` (chain verification
failed).

---

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `NOVAFABRIC_HOME` | `~/.novafabric` | Root directory for all NovaFabric data. Controls the default locations of the capsule spool, registry DB, audit log, and token file. Set this for Docker (`/data/nova`), multi-user, or non-default-home deployments. |
| `NOVAFABRIC_DB_PATH` | `~/.novafabric/registry.db` | Path to the SQLite registry database |
| `OPENLINEAGE_URL` | — | HTTP endpoint for `nova lineage emit-openlineage` (and auto-emit at capture) |
| `OPENLINEAGE_FILE` | — | File path for `nova lineage emit-openlineage` (fallback if URL not set) |
| `NOVAFABRIC_CAPSULE_DIR` | `$NOVAFABRIC_HOME/capsules` | Capsule storage root; overrides the `NOVAFABRIC_HOME`-derived default for all commands |
| `NOVA_OBJECT_STORE_BACKEND` | `filesystem` | OCS backend: `filesystem`, `s3`, `gcs`, or `azure`. Set to `s3` for production deployments. |
| `NOVA_OBJECT_STORE_PATH` | `$NOVAFABRIC_HOME/capsules` | Root path for the filesystem OCS backend. Ignored when `NOVA_OBJECT_STORE_BACKEND=s3`. |
| `NOVA_S3_BUCKET` | — | S3 bucket name for the S3 OCS backend. Required when `NOVA_OBJECT_STORE_BACKEND=s3`. |
| `NOVA_S3_ENDPOINT_URL` | — | S3-compatible endpoint URL (e.g. `http://minio:9000` for MinIO). Defaults to AWS S3 when unset. |
| `NOVAFABRIC_SPAN_ID` | — | Root span id injected into the subprocess |
| `NOVAFABRIC_SUGGEST` | `1` | Set to `0` to disable the asset registration suggestion prompt after `nova capture`. |
| `NOVAFABRIC_ENVIRONMENT` | — | Deployment-environment tag recorded on captured capsules as `deployment_environment` with `environment_source: env-var` (ADR-0126). Overridden by `nova capture --environment`; overrides the SDK `deployment_environment=` argument. Distinct from the `env.lock` technical environment. |
| `NOVAFABRIC_VARIANT` | — | ADR-0116 (**experimental**, record-only): id of the externally assigned A/B variant (arm), recorded verbatim as `variant.variant_id`. Must be set together with `NOVAFABRIC_VARIANT_EXPERIMENT` and `NOVAFABRIC_VARIANT_SOURCE` (an incomplete set warns and is ignored). Overridden by the `nova capture --experiment/--variant/--variant-source` flags; overrides the SDK `variant=` argument. |
| `NOVAFABRIC_VARIANT_EXPERIMENT` | — | ADR-0116: experiment id recorded verbatim as `variant.experiment_id`. |
| `NOVAFABRIC_VARIANT_SOURCE` | — | ADR-0116: the **external** system that assigned the arm (e.g. `launchdarkly`, `statsig`), recorded verbatim as `variant.assignment_source`. Never defaulted — NovaFabric never allocates variants. |
| `NOVAFABRIC_VARIANT_LABEL` | — | ADR-0116 (optional): human-readable arm name recorded verbatim as `variant.variant_label`. |
| `NOVAFABRIC_VARIANT_ASSIGNED_AT` | — | ADR-0116 (optional): RFC 3339 UTC timestamp of the external assignment, recorded verbatim as `variant.assigned_at`. Never substituted with the capture time; a malformed value warns and is dropped (the arm itself is still recorded). |
| `NOVAFABRIC_GLOBAL_RUN_ID` | — | Distributed-run contract: parent run ID injected by SlurmRunner/KubernetesRunner into worker subprocesses. |
| `NOVAFABRIC_PARENT_RUN_ID` | — | Distributed-run contract: direct parent run ID for child capsule linkage. |
| `NOVAFABRIC_RANK` | — | Distributed-run contract: MPI/DDP rank of this worker (integer, 0-based). |
| `NOVAFABRIC_WORLD_SIZE` | — | Distributed-run contract: total number of workers in this distributed run. |
| `NOVAFABRIC_DISTRIBUTION_ROLE` | — | Distributed-run contract: `driver` or `worker`. |
| `NOVAFABRIC_FAIL_MODE` | `warn` | Distributed-run contract: `warn` (log + continue) or `fail` (raise). Controls behaviour when parent capsule is not found. |
| `NOVAFABRIC_PENDING_PARENT_TIMEOUT` | `30` | Distributed-run contract: seconds to wait for the parent capsule directory to appear before proceeding. |
| `NOVAFABRIC_INFERENCE_ENGINE` | — | Inference-determinism contract (recorded in `env.lock` `hardware.inference`): serving engine, e.g. `vllm`, `tgi`, `sglang`. Best-effort; omitted when unset. |
| `NOVAFABRIC_INFERENCE_ENGINE_VERSION` | — | Inference-determinism contract: serving-engine version string. |
| `NOVAFABRIC_INFERENCE_TP_SIZE` | — | Inference-determinism contract: tensor-parallel size (integer ≥ 1). |
| `NOVAFABRIC_INFERENCE_PP_SIZE` | — | Inference-determinism contract: pipeline-parallel size (integer ≥ 1). |
| `NOVAFABRIC_INFERENCE_DTYPE` | — | Inference-determinism contract: compute/storage dtype, e.g. `bfloat16`, `float16`, `fp8`. |
| `NOVAFABRIC_INFERENCE_BATCH_SIZE` | — | Inference-determinism contract: max/observed batch size (integer ≥ 1); batch invariance affects determinism. |
| `NOVAFABRIC_INFERENCE_ATTENTION_BACKEND` | — | Inference-determinism contract: attention backend identifier. |
| `NOVAFABRIC_INFERENCE_SEED` | — | Inference-determinism contract: inference RNG seed (integer). |
| `NOVAFABRIC_INFERENCE_DETERMINISTIC` | — | Inference-determinism contract: `1`/`true`/`yes`/`on` if the engine ran in a documented deterministic mode. |
| `NOVAFABRIC_POSTGRES_DSN` | — | Postgres DSN for `nova doctor --check-storage` and `nova migrate-to-postgres` |
| `NOVA_DSN` | — | Postgres DSN for `nova server start` (ADR-0029 resolution order) |
| `NOVA_BACKEND` | `sqlite` | Storage backend for `nova server start` |
| `NOVA_SERVER_CONFIG` | — | Absolute path to `nova-server.yaml` (overrides default search paths) |
| `NOVAFABRIC_SERVER_HOST` | `127.0.0.1` | Bind address for `nova server start`. |
| `NOVAFABRIC_SERVER_PORT` | `8000` | TCP port for `nova server start`. |
| `NOVAFABRIC_SERVER_BACKEND` | `sqlite` | Storage backend for `nova server start`: `sqlite` or `postgres`. |
| `NOVAFABRIC_SERVER_DB_PATH` | `$NOVAFABRIC_HOME/registry.db` | SQLite DB path for `nova server start` when `NOVAFABRIC_SERVER_BACKEND=sqlite`. |
| `NOVA_OIDC_ENABLED` | `false` | Enable OIDC authentication on the server |
| `NOVA_OIDC_ISSUER_URL` | — | OIDC issuer URL (e.g. `https://keycloak.example.com/realms/nova`) |
| `NOVA_OIDC_CLIENT_ID` | — | OIDC client ID registered at the provider |
| `NOVAFABRIC_OFFLINE_KEY_PATH` | `~/.novafabric/keys/offline-key.pem` | Path to the ed25519 private key for offline tokens |
| `NOVA_ADMIN_TOKEN` | — | Bearer token with admin role, used by `nova server flush-jwks-cache` |
| `NOVAFABRIC_CLUSTER_ID` | — | Cluster identifier for collector and HPC hub binaries |
| `NOVAFABRIC_HUB_ADDRESS` | — | NATS hub address (e.g. `nats://hub:4222`) for HPC hub binary |
| `NOVASEAL_KMS_ENDPOINT` | — | NovaSeal KMS endpoint URL for collector batch signing |
| `NOVAFABRIC_KMS_LOCAL_WAL` | `0` | Set to `1` to use local dev key (dev only; never in production) |
| `NOVAFABRIC_ENV` | — | Set to `production` to block LocalWAL key usage |
| `NOVAFABRIC_SPOOL_BASE` | `/tmp/novafabric` | Base directory for per-job HPC spool (Slurm Prolog) |
| `NOVAFABRIC_SPOOL_DIR` | `$NOVAFABRIC_HOME/spool` | Local event spool drained by `novafabric-spool-forwarder`; written by `nova capture --emit-spool` (ADR-0092 slice C, experimental) |
| `NOVAFABRIC_SPOOL_STREAM` | `NOVA_EVIDENCE` | JetStream stream the spool forwarder publishes to (`novafabric-spool-forwarder --stream`) |
| `NOVAFABRIC_SPOOL_SUBJECT` | `nova.evidence` | JetStream subject prefix for the spool forwarder; per-run subject is `<prefix>.<run_id>` |
| `NOVA_BYPASS_NOTIFY_FILE` | — | Path to JSONL file for bypass event notifications (v0.24.0) |
| `NOVA_BYPASS_NOTIFY_WEBHOOK` | — | HTTP(S) URL for bypass event webhook notifications (v0.24.0) |
| `NOVA_EVENTS_LOG` | — | Path of the local append-only lifecycle-event log (`events.jsonl`); enables the file sink (ADR-0137, experimental) |
| `NOVA_EVENTS_WEBHOOK` | — | Lifecycle-event webhook URL(s), comma-separated; user-configured only, no default destination (ADR-0137, experimental) |
| `NOVA_EVENTS_MAX_RETRIES` | `2` | Bounded webhook retry count for lifecycle-event delivery (ADR-0137) |
| `NOVA_EVENTS_TIMEOUT_S` | `5.0` | Per-request webhook timeout in seconds for lifecycle-event delivery (ADR-0137) |
| `NOVA_EVENTS_SIGN_SECRET` | — | HMAC-SHA256 shared secret; enables lifecycle-event signing. Never written to config, the log, or any payload (ADR-0137) |
| `NOVA_EVENTS_SIGN_KEYID` | `default` | Key identifier recorded in the lifecycle-event `signature.keyid` (ADR-0137) |
| `NOVA_INTEGRATION` | `0` | Set to `1` to enable integration-gated tests (metadata scale, JanusGraph) (v0.24.0) |
| `NOVAFABRIC_HPC_STORAGE` | — | Set to `spool` to use the JSONL spool as NATS leaf store (no local NVMe) |
| `NOVAFABRIC_EPILOG_FLUSH_TIMEOUT` | `50` | Slurm Epilog flush timeout in seconds (must stay below Slurm's `PrologEpilogTimeout`) |
| `NOVA_NATS_URL` | — | NATS server URL for `NATSJetStreamConsumer` (Evidence Fabric Tier 2); e.g. `nats://nats:4222`. Requires `pip install novafabric[nats]` (v0.29.0) |
| `NOVA_NATS_STREAM` | `nova-evidence` | NATS JetStream stream name for Evidence Fabric consumer (v0.29.0) |
| `NOVA_NATS_SUBJECT` | `nova.evidence.>` | NATS subject filter for Evidence Fabric consumer (v0.29.0) |
| `NOVA_NATS_CONSUMER` | `nova-evidence-consumer` | NATS durable consumer name for Evidence Fabric consumer (v0.29.0) |
| `NOVA_CLICKHOUSE_URL` | — | ClickHouse HTTP URL for `ClickHouseAccumulator` (Evidence Fabric Tier 2); e.g. `http://clickhouse:8123`. Requires `pip install novafabric[clickhouse]`. Also enables `nova cost report` (v0.29.0) |
| `NOVA_CLICKHOUSE_DB` | `nova` | ClickHouse database name used by `ClickHouseAccumulator` and `nova cost report`. |
| `NOVA_CLICKHOUSE_USER` | `default` | ClickHouse username for Evidence Fabric Tier 2 connection. |
| `NOVA_CLICKHOUSE_PASSWORD` | `` | ClickHouse password for Evidence Fabric Tier 2 connection. |
| `NOVA_COLLECTOR_TOKEN` | — | Bearer token required on every request to the HPC collector HTTP API. Unset = no auth (local-dev only). |
| `NOVA_COLLECTOR_HEALTH_FILE` | `$NOVAFABRIC_HOME/collector.health` | Path to the collector health-check file written by `nova serve --collector`. |
| `NOVA_CAP003_ENABLED` | `false` | Set to `true` to activate the dual-object-store erasure path (cap-003 compliance). Requires S3 GOVERNANCE Object Lock. |
| `NOVA_DLQ_DIR` | — | Directory for the dead-letter queue. When set, events that fail forwarding are written here instead of dropped. |
| `NOVA_LIBSPOOL_PATH` | — | Absolute path to `libspool.so` for the CFFI collector spool. Auto-discovered from `NOVA_LIBSPOOL_PATH`; falls back to the bundled .so. |
| `NOVAFABRIC_REPLAY_QUEUE_PATH` | — | Socket/FIFO path used by the mocked replay engine to inject synthetic events into a running subprocess. Set automatically by `nova replay --mode mocked`. |
| `NOVAFABRIC_EVIDENCE_DIR` | `$NOVAFABRIC_HOME/evidence` | Override directory for compliance evidence bundles (cap-001/002/004/005). Used by `nova assure` and serve endpoints. |
| `NOVAFABRIC_TOOL_PERMISSION_DB_PATH` | — | SQLite path for the tool-permission policy DB. Defaults to in-memory when unset (permissions are not persisted across restarts). |
| `NOVAFABRIC_GAIA_OCI_DIGEST` | — | OCI image digest pin for the GAIA eval container. Unset = default published digest. Override to use a private mirror or a specific version. |
| `NOVAFABRIC_GAIA_OCI_IMAGE` | — | OCI image reference for the GAIA eval container. Override to use a private registry. |
| `NOVAFABRIC_AGENTBENCH_OCI_DIGEST` | — | OCI image digest pin for the AgentBench eval container. |
| `NOVAFABRIC_AGENTBENCH_OCI_IMAGE` | — | OCI image reference for the AgentBench eval container. |
| `NOVAFABRIC_MMLU_OCI_DIGEST` | — | OCI image digest pin for the MMLU eval container. |
| `NOVAFABRIC_MMLU_OCI_IMAGE` | — | OCI image reference for the MMLU eval container. |
| `NOVAFABRIC_SWE_BENCH_OCI_DIGEST` | — | OCI image digest pin for the SWE-bench eval container. |
| `NOVAFABRIC_SWE_BENCH_OCI_IMAGE` | — | OCI image reference for the SWE-bench eval container. |

---

## Policy and governance (v0.8)

### nova policy test

Run the Rego unit test suite on the built-in policy bundle (requires `opa` installed).

```bash
nova policy test
nova policy test --bundle /path/to/custom-bundle
```

Options:
- `--bundle PATH` — override the default policy bundle path (also: `NOVAFABRIC_POLICY_BUNDLE_PATH` env var)

Returns exit code 0 if all tests pass, 1 if any test fails or `opa` is not found.

The built-in bundle ships reference gates including `promote_gate.rego`,
`regression_gate.rego` (v0.9), and `budget_gate.rego` — the **cost/energy budget
gate** ([ADR-0136](./decisions.md), *experimental*).

**Budget gate (`budget_gate.rego`, experimental).** Denies promotion when the
capsule's *already-recorded* cost/energy/token rollup exceeds declared ceilings —
a promotion gate over sealed evidence, never a live spend alert. The rollup is
assembled with `novafabric.policy.budget_block_from_capsule(capsule_dir)` (reads
`nova.cost` from `model-calls.jsonl`, `measured_joules` from
`energy-receipts.jsonl`, and `gen_ai.usage.*` token counts) and passed as
`input.resource.budget`; ceilings go in `input.context.budget_ceilings`
(`total_cost` / `cost_per_run` / `energy_kwh` / `tokens`). Missing evidence is
never treated as zero: absent ceilings or absent data pass with an explicit
"no data" reason (`skip_unmeasured`, the default), while
`input.context.missing_evidence: "require_measured"` fail-closes on a declared
ceiling with no recorded evidence. Spec: `design/spec/budget-gate-v0.md`.
The `nova policy budget set|list|show` authoring commands and the budget-gate
verdict record are **future design** (ADR-0136 P2/P3) — not yet implemented.

---

### nova policy explain \<decision-id\>

Replay a past policy decision from the audit log, showing the event type, actor, resource, and details.

```bash
nova policy explain 3f8a1b2c-...
```

The dashboard Policy Explain panel auto-completes decision IDs via
`GET /api/policy/recent-decisions?limit=50` (serve REST API), which returns up to 50
recent IDs from `~/.novafabric/dashboard-audit.jsonl` in most-recent-first order.

---

### nova policy list

List Rego policy files in the built-in bundle and any signed promotion policies stored in the PolicyStore.

```bash
nova policy list
nova policy list --namespace staging
nova policy list --bundle /path/to/custom-bundle
nova policy list --db ~/.local/share/novafabric/merkle.db
```

Options:
- `--bundle PATH` — Rego bundle directory to scan (default: built-in bundle; also: `NOVAFABRIC_POLICY_BUNDLE_PATH`)
- `--db PATH` — PolicyStore SQLite DB path (default: `NOVAFABRIC_HOME/promote/policy.db`, falling back to `~/.local/share/novafabric/merkle.db`)
- `--namespace / -n TEXT` — filter signed policies to this namespace (default: show all namespaces)

Output has two sections:

1. **Rego Bundle** — table of `.rego` files with file name (relative to bundle root) and size.
2. **Signed Promotion Policies** — table of stored policy versions with version number, namespace, and creation timestamp. If no DB exists yet, a note is printed and the command exits cleanly.

---

### nova approve \<name@version\>

Approve an asset that is in the `pending_approval` lifecycle state, recording the approver and note in the audit log.

```bash
nova approve my-model@v2
nova approve my-model@v2 --approver alice --note "Reviewed eval results"
```

Options:
- `--approver TEXT` — name or ID of the approver (default: current OS user)
- `--note TEXT` — optional approval note recorded in the audit log

Exits with code 1 if the asset is not found or not in `pending_approval` state.

---

### nova hold create \<registry\>

Place a legal hold on a registry, suspending all capsule deletion (ADR-0031).

```bash
nova hold create my-registry --reason "SEC examination 2026-Q2"
nova hold create my-registry --reason "SEC examination 2026-Q2" --duration-days 365
```

Options:
- `--reason TEXT` — required; reason for the hold (recorded in audit log)
- `--duration-days N` — hold duration in days; omit for indefinite hold

Holds are stored in `.novafabric/registries/<registry>/holds.jsonl` (append-only).

---

### nova hold list \<registry\>

List active (unreleased) legal holds on a registry.

```bash
nova hold list my-registry
```

---

### nova hold release \<hold-id\>

Release a legal hold by ID. Records the release in the audit log.

```bash
nova hold release hold-a1b2c3d4
```

Exits with code 1 if the hold ID is not found or already released.

---

### nova capsule delete \<capsule-id\>

Delete a capsule record, subject to retention policy and legal holds.

```bash
nova capsule delete cap-01JXXXXX --registry my-registry
nova capsule delete cap-01JXXXXX --registry my-registry --age-days 1826
nova capsule delete cap-01JXXXXX --registry my-registry --force
```

Options:
- `--registry TEXT` — required; the registry name owning this capsule
- `--age-days N` — age of the capsule in days (used for retention window check; default: 0)
- `--force` — skip retention and hold checks (dangerous; use only for emergency recovery)

Deletion is blocked when:
- One or more active legal holds exist for the registry (even without a `retention-policy.yaml`)
- A `retention-policy.yaml` exists and the capsule is within the retention window
- `deletion_mode: prohibited` is set in the policy

On success, a signed `capsule.delete` entry is appended to the audit log.

---

## Retention scheduler (ADR-0134)

Applies the [ADR-0031](./decisions.md) retention
windows *over time*: a WORM-aware, crypto-shred-integrated, audited sweep.
Bindings live in a new **optional** `bindings:` block inside the existing
`.novafabric/registries/<registry>/retention-policy.yaml`
(schema: `schemas/retention-binding.schema.json`); a registry with no bindings
is swept for nothing. NovaFabric embeds **no daemon** — run the sweep manually
or wire `nova retention apply --yes` into `cron`/`systemd`.

```yaml
# retention-policy.yaml (ADR-0031 fields unchanged; bindings are additive)
bindings:
  - id: mifid-trade-5y
    match: {tag: trade-record, deployment_environment: production}
    window: P1825D          # ISO-8601 duration from capsule created_at, or YYYY-MM-DD
    action: purge           # expire-metadata | purge | crypto-shred
  - id: gdpr-subject-pii
    match: {tag: contains-pii}
    window: P180D
    action: crypto-shred    # dispatches to the ADR-0069 DEK destruction
```

Matching reads capsule manifests under the capsule directory:
`metadata.tags` (comma-separated), `metadata.deployment_environment`,
`metadata.pii_subject_id` (crypto-shred subject), and `alias` (matched by the
`asset` predicate, glob allowed).

Safety invariants (fixed, not configurable): a **WORM/legal hold always wins**
— a WORM-retained capsule is never purged or shredded before its `locked_until`
(recorded `skipped: worm_hold`); `purge` is refused under
`deletion_mode: prohibited`; a crypto-shred inside the Art.17(3)(b) window is
`deferred`. Every decision — including every skip — appends one hash-chained
`retention.action` audit entry (`schemas/retention-action-record.schema.json`):
deletion is itself evidence.

### nova retention plan

Dry-run (the default posture): list what WOULD be affected. Touches nothing,
writes no audit records, and uses the identical due-computation code path as
`apply`.

```bash
nova retention plan --registry my-registry
nova retention plan --registry my-registry --json
```

### nova retention apply

Apply due retention actions. Confirm-gated: prompts unless `--yes` (or
`--dry-run`). Idempotent and fail-safe — re-runs are no-ops; a per-item
failure is recorded `error` and the sweep continues.

```bash
nova retention apply --registry my-registry --dry-run          # preview
nova retention apply --registry my-registry --yes              # cron/CI
nova retention apply -r my-registry --action crypto-shred --limit 100 --yes
```

Options:
- `--dry-run` — preview only (identical planner, no action, no audit entries)
- `--yes / -y` — skip the confirmation prompt (for cron/CI)
- `--capsule-dir PATH` — capsule directory to sweep (default: `$NOVAFABRIC_HOME/capsules`)
- `--worm-db PATH` — local WORM adapter DB consulted for `locked_until`
  (default: `.novafabric/registries/<registry>/worm.db`)
- `--action NAME` — only consider bindings with this action
- `--limit N` — bound the number of applied actions per pass (the rest carries over)
- `--principal TEXT` — acting identity recorded in evidence entries (default: `cli-user`)
- `--retention-months N` — Art.17(3)(b) minimum window for crypto-shred (default: 6)
- `--json` — machine-readable output

### nova retention status

Read-only: per binding, how many items are due now, how many are held
(WORM/legal), and the next due date.

```bash
nova retention status --registry my-registry --json
```

### nova retention explain \<capsule-id\>

Read-only: which bindings match this capsule, its computed due date, and its
current hold state.

```bash
nova retention explain cap-01JXXXXX --registry my-registry
```

---

## Local dashboard (experimental, v0.7)

### nova serve --experimental

Start the experimental local dashboard. Read-only browsing of capsules, registry, and lineage; Layer-B compute-only mutations (register, eval, promote, forensic replay, redact, export evidence). Per [ADR-0027](./decisions.md).

**v0.11 additions** — the following dashboard capabilities are now available in `nova serve`:

| Endpoint | What it does |
|---|---|
| `POST /api/evidence/{bundle_id}/verify` | Full cryptographic verification: Ed25519 DSSE + RFC 3161 TSR + NovaSeal Merkle log inclusion. Returns `{signature_ok, timestamp_ok, log_integrity_ok, seal_available, valid, errors[]}`. |
| `POST /api/runs/{run_id}/validate` | Capsule schema + required-file check. Returns `{valid, errors[], run_id}`. |
| `GET /api/runs/{run_id}/redaction-proof` | Returns the full `redaction-proof.json` for a capsule (404 if not yet scanned). |
| `GET /api/assets/{name}/diff` | Field-by-field diff of two asset spec versions (`?from_version=&to_version=`). Returns `{added, removed, changed, identical}`. |
| `GET /api/holds` | List all active legal holds across all registries. Returns `{total_active, registries[{name, holds[]}]}`. |
| `POST /api/holds` | Place a new hold. Body: `{registry, reason, duration_days?}`. Path-traversal guard on registry name. |
| `POST /api/holds/{hold_id}/release` | Release a hold by ID. Returns `{released, hold_id, registry}`. 404 if unknown or already released. |
| `POST /api/runs/{run_id}/replay/semantic` | Semantic similarity analysis: computes pairwise difflib similarity across model call responses. Returns `{similarity_score, matched_run_id, …}`. Read-only. |
| `POST /api/runs/{run_id}/replay/exact` | Exact replay eligibility check: validates `env.lock.lock_mode=deterministic` and `seed` on all model calls. Returns `{exact_eligible, exact_reasons[], exact_hash_count, …}`. Read-only. |
| `POST /api/runs/{run_id}/verify` | Full NovaSeal check on a capsule: DSSE signature + RFC 3161 timestamp + Merkle log inclusion (mirrors `nova verify`). Returns `{sealed, configured, signature_ok, timestamp_ok, log_integrity_ok, valid, errors[]}`. Returns `sealed=false` if capsule was not sealed; `configured=false` if NovaSeal profile missing. |
| `GET /api/lineage/{run_id}/emit-openlineage` | Emit OpenLineage events for a capsule run to the configured transport (mirrors `nova lineage emit-openlineage`). Returns `{ok, run_id, event_count, events[]}`. |
| `POST /api/assets/register-from-yaml` | Register an asset directly from a YAML spec string (mirrors `nova register`). Body: `{spec_yaml: "<yaml string>"}`. Returns `{ok, name?, error?}`. |
| `GET /api/reports/run-history` | Run history report; query params: `from`, `to` (ISO dates), `status` (`all\|ok\|error\|failed`), `agent` (name filter), `format` (`json\|csv`). Returns `{rows[], columns[], generated_at, report_type}`. |
| `GET /api/reports/eval-regression` | Eval regression report; params: `from`, `to`, `suite`. |
| `GET /api/reports/capsule-compare` | Side-by-side capsule diff report; params: `run_a`, `run_b`. |
| `GET /api/reports/cost-burn` | Cost burn over time; params: `from`, `to`, `model`. |
| `GET /api/reports/throughput` | Agent throughput; params: `from`, `to`, `resolution` (`1h\|1d\|1w`). |
| `GET /api/reports/evidence-inventory` | Evidence bundle inventory; params: `from`, `to`. |
| `GET /api/reports/policy-audit` | Policy decision audit log; params: `from`, `to`, `policy_id`, `result` (`pass\|fail\|warn`). |
| `GET /api/reports/seal-verification` | NovaSeal verification status for all capsules; params: `from`, `to`. |
| `GET /api/reports/executive-summary` | Management summary across key metrics; params: `from`, `to`. |
| `GET /api/reports/release-comparison` | Side-by-side release diff; params: `version_a`, `version_b`. |
| `GET /api/kg/entity-queue` | List all pending `ReviewItem` records from the Tier-3 human review queue. Returns `{ok, count, items[]}`. |
| `GET /api/kg/entity-queue/stats` | Return pending/approved/rejected counts for the review queue. Returns `{ok, pending, approved, rejected}`. |
| `POST /api/kg/entity-queue/{item_id}/approve` | Approve a review item. Body: `{canonical, resolved_by}`. Returns `{ok, item_id, canonical}`. |
| `POST /api/kg/entity-queue/{item_id}/reject` | Reject a review item. Body: `{resolved_by?}`. Returns `{ok, item_id, status}`. |
| `GET /api/compliance/audit/map` | Return compliance coverage matrix for all audit profiles (`nist-ai-rmf`, `eu-ai-act-high-risk`, `gdpr`, `soc2-type2`, `iso42001`, `scientific-reproducibility`). Mirrors `nova audit map --profile`. |
| `GET /api/compliance/erasure/status` | List persisted GDPR erasure requests from `$NOVAFABRIC_HOME/erasure.db` (ADR-0210, **experimental**). Params: `?subject_id=` (exact filter), `?limit=`. Returns `{cap003_enabled, requests[{request_id, subject_sha256, state, reason, requested_at, executed_at, capsule_ids[], receipt, receipt_sha256, error_class}]}`, newest first. |
| `GET /api/runs/{run_id}/children` | Return parent capsule metadata and list of child capsule summaries for a distributed/parent-child run. Returns `{ok, run_id, parent_status, child_count, children[{run_id, status, edge_type, exit_code}]}`. |

**v0.34.0 additions** — regulatory compliance export endpoints:

| Endpoint | What it does |
|---|---|
| `GET /api/compliance/euaiact/status` | EU AI Act Art.12 configuration — `high_risk`, `provider_mode`, `retention_months`, `deadline`. Mirrors `nova euaiact status`. Dashboard: **GovernanceTab → EU AI Act Art.12 Status**. |
| `POST /api/compliance/euaiact/export` | Art.12 structured log export. Body: `{from_date?, to_date?}` (ISO 8601 UTC). Returns `{ok, records[], count, retention_months, mode}`. Mirrors `nova euaiact export`. Dashboard: **GovernanceTab → EU AI Act Export**. |
| `POST /api/compliance/export/rocrate` | RO-Crate v1.1 ZIP export for a run ID. Body: `{run_id}`. Returns `{ok, filename, zip_base64, size_bytes, note}`. Mirrors `nova export-rocrate`. Dashboard: **ComplianceTab → RO-Crate Export** (one-click browser download). |
| `POST /api/lineage/export-prov` | W3C PROV-JSON lineage document for a run ID. Body: `{run_id}`. Returns `{ok, run_id, document, note}`. Mirrors `nova lineage export-prov`. Dashboard: **LineageTab → PROV-JSON Export**. |
| `POST /api/compliance/export/c2pa` | C2PA v2.3 manifest for a run ID. Body: `{run_id, include_training_mining?}`. Returns `{ok, run_id, manifest, note}`. Mirrors `nova export-c2pa`. Dashboard: **ComplianceTab → C2PA Export**. |
| `POST /api/compliance/export/ropa` | GDPR Art.30 RoPA entry for a run ID. Body: `{run_id, controller_name?, controller_contact?}`. Returns `{ok, run_id, document, completeness, missing_fields, note}`. Mirrors `nova export-ropa`. Dashboard: **ComplianceTab → GDPR Art.30 RoPA Export**. |
| `POST /api/compliance/export/aibom` | CycloneDX 1.7 AI-SBOM for a run ID. Body: `{run_id}`. Returns `{ok, run_id, bom_format, serial_number, component_count, components, generated_at, note}`. Mirrors `nova export-aibom`. Dashboard: **ComplianceTab → AI-SBOM Export**. |
| `POST /api/compliance/export/nist-rmf` | NIST AI RMF 1.0 quantitative risk report for a run ID. Body: `{run_id}`. Returns `{ok, run_id, overall_score, risk_level, metrics, missing_evidence, generated_at, note}`. Mirrors `nova export-nist-rmf`. Dashboard: **ComplianceTab → NIST AI RMF Report**. |
| `GET /api/aibom/status` | CRA SBOM compliance coverage across all capsules. Returns `{ok, regulation, cra_deadline, spec_version, capsule_directory, total_capsules, capsules_with_aibom, capsules_missing_aibom, coverage_status}`. Mirrors `nova aibom status`. Dashboard: **ComplianceTab → AI-SBOM Coverage Status**. |

**v0.12.8 fixes** — eval results panel:

- **Null score → `—`** — when an eval suite stores `{"score": null}` (binary pass/fail, no numeric score), the panel now shows a muted `—` instead of `0.00`. Prevents false-zero interpretation.
- **Empty suite name → `(unknown suite)`** — if `suite_name` is blank in the database, the row label falls back to italic `(unknown suite)`.

**v0.12.7 additions** — dashboard UI coverage Phase 1:

- **Infra tab** — 10 Phase 0–6 cluster-scale component status cards (NovaSeal, Collector, Object Store, Metadata DB, Lineage at Scale, Parent/Child, Server Mode, Eval Suites, Policy Gates, Run Capsule) with `shipped / partial / placeholder / planned` badges.
- **Commands tab (35 builders)** — expanded from 13 to 35 command builders across 4 journey tracks (Debug & Replay, Govern & Approve, Audit & Lineage, Infrastructure & Scaling). Live preview + copy for every builder.
- **Lineage QueryPanel** — interactive provenance / blast-radius / replay-chain query panel in the Lineage tab. Live CLI equivalent preview.
- **Context-aware autocomplete** — every database-reference input (Lineage ref, Diff run A/B, Holds registry, Policy resource ref) shows live-filtered suggestions from loaded data. Extended in v0.30.3 to full 100% coverage: `deploymentId` / `incidentId` (localStorage MRU via `useLocalMru`); `subjectId` panels; `runId` in AssurancePanel and StorageOpsCard (live-fetched).
- **`Makefile` `bundle` target** — `make bundle` rebuilds the web dashboard and rsyncs to `src/novafabric/serve/static/`. `make serve-local` rebuilds and starts the server.
- **`Makefile` `serve-topology-only` target** — `make serve-topology-only` starts `nova serve --experimental --topology` without rebuilding the SPA; useful on remote servers without npm/Node installed. Use `make serve-topology` on dev machines that need to rebuild the SPA first.

```bash
pip install 'novafabric[serve]'   # one-time, opt-in (FastAPI + uvicorn)
nova serve --experimental
nova serve --experimental --port 8080 --no-browser
nova serve --experimental --capsule-dir ~/runs/2026-04
```

Mandatory flag: `--experimental`. Without it the command prints the gate banner and exits cleanly.

| Flag | Default | Purpose |
| --- | --- | --- |
| `--experimental` | (required) | Acknowledges the experimental gate. |
| `--port` | `4321` | TCP port. |
| `--host` | `127.0.0.1` | Bind address. Refuses non-localhost without `--insecure`. |
| `--capsule-dir` | `$NOVAFABRIC_HOME/capsules/` | Where to look for capsules. |
| `--db-path` | `~/.novafabric/registry.db` | Registry/lineage SQLite path. |
| `--no-browser` | off | Don't auto-open a browser tab. |

**Authentication.** The server binds `127.0.0.1`, validates the `Host` header, and requires a
one-shot session token on every `/api/*` request (the probes `GET /api/health`, `/livez`, and
`/readyz` are open). The browser is launched with the token already in the URL; for scripts,
pass it either way — since **v0.97.0** an `Authorization: Bearer` header is accepted everywhere
`?token=` is, and when the header carries a Bearer credential it is the authoritative one:

```bash
curl -H "Authorization: Bearer $(cat ~/.novafabric/.serve-token)" \
  http://127.0.0.1:4321/api/stats            # preferred — keeps the secret out of shell
                                             # history, proxy logs, and Referer headers
curl "http://127.0.0.1:4321/api/stats?token=<token>"   # still supported (SPA, printed links)
```

Pin a stable token with `NOVAFABRIC_SERVE_TOKEN` for Docker/CI; otherwise it persists in
`$NOVAFABRIC_HOME/.serve-token` across restarts.

The CLI is the canonical interface. Every dashboard action surfaces the equivalent `nova` command and writes to `~/.novafabric/dashboard-audit.jsonl`.

---

### nova serve --topology

Start the experimental live topology dashboard alongside `nova serve`. Requires `novafabric[serve]`. Adds three topology endpoints to the running server and serves the `packages/nova-dashboard/` SPA from `/topology/`.

> **Note:** `--topology` implies `--tv5`. Passing `--topology` automatically activates the
> TV-5 3D topology REST + WebSocket endpoints without needing a separate `--tv5` flag.

```bash
nova serve --experimental --topology
nova serve --experimental --topology --port 8080
# On a remote server where the SPA is already built (no npm/Node required):
make serve-topology-only
```

**Additional flag:**

| Flag | Default | Purpose |
|---|---|---|
| `--topology` | off | Enable topology endpoints + SPA. Automatically implies `--tv5`. Requires `--experimental`. |

**Topology endpoints added when `--topology` is set:**

| Endpoint | Protocol | Description |
|---|---|---|
| `GET /topology/clusters` | HTTP + Apache Arrow IPC | Returns the full cluster layer (`ads.v1.cluster_layer`). Auth required. |
| `WS /topology/stream` | WebSocket — TDP v1 | Live graph delta stream. Requires `Sec-WebSocket-Protocol: nova-tdp-v1` header; rejects with code `4400` otherwise. Supports `subgraph_expand`, `subgraph_collapse`, and `resume_from` client messages. |
| `GET /metrics/stream` | SSE | Real-time metric frames (`ads.v1.metric_frame`). Supports `Last-Event-ID` for reconnect gap recovery. |

**Dependencies** (added automatically via `novafabric[serve]` if `--topology` is used): `duckdb`, `pyarrow`, `networkx`, `python-louvain`.

---

### nova serve --tv5

Enable the experimental TV-5 3D Topology View. Mounts `/api/tv5/` REST + WebSocket endpoints and adds a **3D View (TV-5)** tab in the nova-dashboard SPA.

```bash
nova serve --experimental --tv5
nova serve --experimental --topology --tv5
nova serve --experimental --tv5 --port 8080
```

**Additional flag:**

| Flag | Default | Purpose |
|---|---|---|
| `--tv5` | off | Enable TV-5 3D topology REST + WebSocket API. Requires `--experimental`. |

**TV-5 endpoints added when `--tv5` is set:**

| Endpoint | Protocol | Description |
|---|---|---|
| `GET /api/tv5/live` | HTTP JSON | Current topology state: `windowId`, `nodeCount`, `edgeCount`, `layoutAgeMs`. |
| `GET /api/tv5/windows` | HTTP JSON | List available snapshot windows. Accepts `from` and `to` Unix timestamp query params. |
| `GET /api/tv5/snapshot/{windowId}` | HTTP JSON or msgpack | Return topology snapshot (positions, node types, edge count). Window IDs validated to `^[a-z0-9_-]+$`; path traversal blocked. Returns msgpack if the optional `msgpack` package is installed, else JSON. |
| `WS /api/tv5/ws` | WebSocket | Pushes `{type: "snapshot", windowId}` on every new layout. Client sends `{type: "subscribe", topologyId}` to confirm. |

**Environment variables:**

| Variable | Default | Purpose |
|---|---|---|
| `TV5_SNAPSHOT_DIR` | `.nova/tv5_snapshots` | Directory for fine and coarse snapshot tiers. |
| `TV5_MAX_FINE_SNAPSHOTS` | `288` | Maximum number of fine-tier snapshots to retain. |
| `TV5_MAX_COARSE_SNAPSHOTS` | `168` | Maximum number of coarse-tier snapshots to retain. |

**Notes:**

- TV-5 is experimental (ADR-0068). Server-side layout uses `networkx.spring_layout` as an FA2 approximation (OQ-030: Python fa2 is blocked at 100K nodes).
- `msgpack` is an optional runtime dependency; the server falls back to JSON automatically.
- TV-5 does NOT require `--topology`, but `--topology` automatically implies `--tv5`. Both flags may be passed together without conflict.

**Environment variable:** `NOVA_DASHBOARD_DUCKDB_PATH` (default `~/.novafabric/dashboard.duckdb`) — path to the DuckDB database holding the cluster-layer topology.

The SPA at `http://localhost:4321/topology/` auto-fetches the cluster layer on mount, opens the TDP WebSocket stream, and renders the agent graph using Sigma.js 3 + Graphology 0.26 with FA2 incremental layout in a Web Worker.

**For the full capability matrix and limitations vs CLI, see [`dashboard.md`](dashboard.md).**

---

## Collector binaries (Phase 2 — cluster scale)

Phase 2 ships three Go binaries (`collector/`) that form the cluster-deployable
evidence ingestion tier. They are separate from the Python `nova` CLI. Install
them by building from source (Go 1.22+):

```bash
cd collector
go build -o bin/novafabric-collector ./cmd/novafabric-collector
go build -o bin/novafabric-verifier  ./cmd/novafabric-verifier
go build -o bin/novafabric-hpc-hub   ./cmd/novafabric-hpc-hub
```

These binaries complement the Python SDK: agents continue to emit events via the
existing `nova capture` / hook path; the collector tier signs and forwards those
events to Kafka at cluster scale.

---

### novafabric-collector

Start the OTel Collector with the `novaseal_batch_signer` processor pre-bundled.
Suitable for both K8s (Deployment) and bare-metal gateway deployments.

```bash
novafabric-collector --config collector.yaml
novafabric-collector --config /etc/novafabric/collector.yaml
```

The collector is built using the OpenTelemetry Collector Builder (OCB) with the
`novaseal_batch_signer` custom processor registered. It accepts standard OTel
Collector configuration YAML.

**Reference pipeline** (see `deploy/k8s/configmap-collector.yaml`):
```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: "0.0.0.0:4317"
      http:
        endpoint: "0.0.0.0:4318"
processors:
  batch:
    send_batch_size: 1000
    timeout: 5s
  novaseal_batch_signer:
    keystore_endpoint: "${NOVASEAL_KMS_ENDPOINT}"
    fail_open: false          # default: fail-closed — unsigned batches are refused
    cache_ttl: 5m
    key_rotation_interval: 1h
    local_wal: false          # true only in dev (sets NOVAFABRIC_KMS_LOCAL_WAL=1)
exporters:
  kafka:
    brokers: ["${KAFKA_BOOTSTRAP_SERVERS}"]
    topic: "nova.evidence"
service:
  pipelines:
    logs:
      receivers: [otlp]
      processors: [batch, novaseal_batch_signer]
      exporters: [kafka]
```

**`novaseal_batch_signer` processor options:**

| Option | Default | Description |
|---|---|---|
| `keystore_endpoint` | — | NovaSeal KMS mTLS endpoint (required unless `local_wal: true`) |
| `fail_open` | `false` | `false` = fail-closed (unsigned batches refused); `true` = continue without signature if KMS is down |
| `cache_ttl` | `5m` | How long a fetched key is cached |
| `key_rotation_interval` | `1h` | How often to rotate signing key |
| `local_wal` | `false` | **Dev only.** Use a local Ed25519 key from `~/.novafabric/dev-keys/`. Emits a WARN on every signing call. Refuses when `NOVAFABRIC_ENV=production`. |

The processor places each batch's signature and key ID in `Resource.attributes`:
- `nova.batch.signature` — base64-encoded Ed25519 signature (88 chars)
- `nova.batch.signing_key_id` — UUID of the signing key

Prometheus metrics exposed at `:8888/metrics`:
- `nova_batch_sign_latency_seconds` — histogram of per-batch signing time
- `nova_batch_sign_errors_total{reason="..."}` — counter by error category
- `nova_collector_forwarded_events_total` — events forwarded to Kafka

**K8s deployment:** see `deploy/k8s/` for a complete namespace, ConfigMap,
Deployment, DaemonSet (Fluent Bit node collector), and Service manifest set.

---

### novafabric-verifier

Verify a NovaSeal Ed25519 batch signature offline. Returns exit 0 if the
signature is valid, exit 1 if invalid.

```bash
# Verify with a PEM public key from the KMS
novafabric-verifier --batch batch.pb --pubkey kms-public.pem --key-id <key-uuid>

# Verify with the local dev key (matches what local_wal=true used to sign)
novafabric-verifier --batch batch.pb --local-wal
```

**Flags:**

| Flag | Required | Description |
|---|---|---|
| `--batch PATH` | yes | Path to a serialized OTLP `ResourceLogs` proto file |
| `--pubkey PATH` | one of | PEM-encoded Ed25519 public key file |
| `--key-id UUID` | with `--pubkey` | Key ID to verify against |
| `--local-wal` | one of | Use the dev key from `~/.novafabric/dev-keys/` |

**Output:**

```
# Success
OK key_id=a1b2c3d4-...

# Failure
INVALID key_id=a1b2c3d4-... error=signature mismatch
```

The verifier re-runs the same canonical-encoding pre-pass that the signer used
(ADR-001: strip `nova.batch.signature` + `nova.batch.signing_key_id`, sort
`Resource.attributes` by key, sort each `LogRecord.attributes` by key, marshal
with `proto.MarshalOptions{Deterministic: true}`). If the bytes differ between
signer and verifier, the signature will fail.

---

### novafabric-hpc-hub

Run the HPC cluster-hub NATS message handler. Subscribes to
`nova.<cluster_id>.aggregate`, signs each batch with NovaSeal, and publishes
signed batches to the Kafka regional topic.

```bash
# Production (uses NovaSeal KMS)
novafabric-hpc-hub \
  --cluster-id hpc01 \
  --hub nats://hub.cluster.local:4222 \
  --kms https://kms.internal:8443

# Development (uses local dev key)
novafabric-hpc-hub \
  --cluster-id dev \
  --hub nats://localhost:4222 \
  --local-wal
```

**Flags:**

| Flag | Env override | Default | Description |
|---|---|---|---|
| `--cluster-id` | `NOVAFABRIC_CLUSTER_ID` | — | **Required.** Cluster identifier |
| `--hub` | `NOVAFABRIC_HUB_ADDRESS` | — | NATS hub address (`nats://...`) |
| `--kms` | `NOVASEAL_KMS_ENDPOINT` | — | NovaSeal KMS endpoint (required unless `--local-wal`) |
| `--local-wal` | `NOVAFABRIC_KMS_LOCAL_WAL=1` | false | Dev only. Use local Ed25519 key. |
| `--job-id` | `SLURM_JOB_ID` | — | Slurm job ID (set automatically by Prolog) |
| `--spool` | — | — | Spool directory for this job |

The hub signs batches at the cluster boundary before forwarding to Kafka —
compute nodes (leaf NATS instances) never touch the NovaSeal keystore. This
preserves HPC air-gap security: only the hub node needs KMS network access.

**Signals:** `SIGTERM` / `SIGINT` trigger a clean shutdown.

---

### HPC Slurm integration

For Slurm cluster deployments, the collector provides Prolog/Epilog scripts that
initialize a per-job NATS leaf node and bounded-flush on job exit.

**Installation** (run on all Slurm compute nodes):
```bash
# 1. Copy scripts to compute nodes
cp deploy/hpc/prolog.sh /etc/novafabric/prolog.sh
cp deploy/hpc/epilog.sh /etc/novafabric/epilog.sh
chmod 755 /etc/novafabric/prolog.sh /etc/novafabric/epilog.sh

# 2. Add to slurm.conf (requires Slurm admin)
Prolog=/etc/novafabric/prolog.sh
Epilog=/etc/novafabric/epilog.sh
PrologFlags=Alloc
```

**Per-job lifecycle:**
1. **Prolog** — creates `/tmp/novafabric/$SLURM_JOB_ID/`, starts `novafabric-hpc-hub` as a leaf NATS instance bound to that directory. Exits 0.
2. **Job runs** — agents write events to the spool via SDK hooks; the leaf node buffers and forwards to the cluster hub.
3. **Epilog** — calls `nats stream flush --timeout=${NOVAFABRIC_EPILOG_FLUSH_TIMEOUT:-50}s` to drain the spool, stops the leaf process. **Always exits 0** to prevent Slurm from draining the node.

**Lustre/NFS safety:** The spool uses `rename(2)` as its only atomic commit primitive. No `flock`, `mmap`, or `fcntl` calls are used — these are unsafe on shared network filesystems. On nodes without local NVMe, set `NOVAFABRIC_HPC_STORAGE=spool` to use the JSONL spool as the NATS leaf store directory.

**Reference:** `deploy/hpc/README.md`, `deploy/hpc/ansible/`, ADR-0028, ADR-0043.

---

## Governance commands (v0.16)

### nova classify run

Classify an AI system's risk tier against EU AI Act Annex III, NIST AI RMF, and OMB M-24-10.

```bash
nova classify run --system system.yaml
nova classify run --system system.yaml --vocabulary nist-ai-rmf/1.0.0
nova classify run --system system.yaml --format json
```

**Options:**
- `--system PATH` — YAML file describing the AI system (`AISystemRecord` schema)
- `--vocabulary TEXT` — vocabulary ID (default: `eu-ai-act/2024.1.0`). List with `nova classify list-vocabularies`.
- `--format [text|json]` — output format (default: `text`)

**Exit codes:** `0` = classified; `1` = prohibited tier detected.

**AISystemRecord fields:** `name`, `description`, `use_cases` (list of strings), `deployment_context`, `data_subjects` (list), `automated_decision_making` (bool), `biometric_processing` (bool), `critical_infrastructure` (bool).

### nova classify list-vocabularies

List all available classification vocabularies.

```bash
nova classify list-vocabularies
```

### nova classify from-capsule

Infer an AI system record from a captured run capsule and classify it.

```bash
nova classify from-capsule .novafabric/runs/01HXAY7M/
```

**Reference:** `src/novafabric/governance/`, `src/novafabric/cli/classify.py`, ADR-0056.

---

## Compliance audit commands (v0.16)

### nova audit map

List all evidence checkers for a compliance profile.

```bash
nova audit map --profile nist-ai-rmf
nova audit map --profile eu-ai-act-high-risk
nova audit map --profile gdpr
```

**Available profiles:** `nist-ai-rmf`, `eu-ai-act-high-risk`, `gdpr`, `soc2-type2`, `iso42001`, `scientific-reproducibility`.

### nova audit report

Run a compliance audit against a capsule and print the result.

```bash
nova audit report --capsule .novafabric/runs/01HXAY7M/ --profile nist-ai-rmf
nova audit report --capsule .novafabric/runs/01HXAY7M/ --profile gdpr --format json
```

**Options:**
- `--capsule PATH` — path to a captured run capsule
- `--profile TEXT` — compliance profile (see `nova audit map`)
- `--format [text|json]` — output format (default: `text`)

### nova audit verify

Assert that a capsule meets a minimum compliance coverage threshold. Exits 1 if the coverage is below the threshold.

```bash
nova audit verify --capsule .novafabric/runs/01HXAY7M/ --profile nist-ai-rmf --min-coverage 0.8
```

**Options:**
- `--min-coverage FLOAT` — minimum required coverage score [0.0, 1.0] (default: `0.7`)

### nova audit bundle

Export a signed audit bundle (ZIP) containing the compliance report and evidence artifacts.

```bash
nova audit bundle --capsule .novafabric/runs/01HXAY7M/ --profile eu-ai-act-high-risk --output audit.zip
```

### nova audit coverage

Print a numeric coverage summary for all profiles against a capsule.

```bash
nova audit coverage --capsule .novafabric/runs/01HXAY7M/
```

**Reference:** `src/novafabric/compliance/audit/`, `src/novafabric/cli/audit.py`.

---

## Examiner exporter commands (v0.16)

### nova export-examiner bagit

Export a capsule as a RFC 8493 BagIt archive with SHA-256 checksums.

```bash
nova export-examiner bagit .novafabric/runs/01HXAY7M/ --output run.bagit.zip
```

**Output:** BagIt ZIP containing `bagit.txt`, `bag-info.txt`, `manifest-sha256.txt`, and the full capsule payload under `data/`.

### nova export-examiner pccp

Export a capsule as an FDA 21 CFR Part 11 PCCP (Predetermined Change Control Plan) package.

```bash
nova export-examiner pccp .novafabric/runs/01HXAY7M/ --output pccp.zip
nova export-examiner pccp .novafabric/runs/01HXAY7M/ --output pccp.zip --format json
```

**Output:** ZIP containing protocol document, change manifest, training documentation, and validation records.

### nova export-examiner iso42001

Export a capsule as an ISO/IEC 42001 AI Management System package.

```bash
nova export-examiner iso42001 .novafabric/runs/01HXAY7M/ --output iso42001.zip
```

**Output:** ZIP containing system profile, risk register, monitoring plan, and improvement log.

**Reference:** `src/novafabric/compliance/export/examiner.py`, `src/novafabric/cli/export_examiner.py`.

---

## HPC runner commands (v0.16)

### PBSRunner (Python API)

Run a capsule job on a PBS/Torque cluster:

```python
from novafabric.runners import PBSRunner
from novafabric.runners.spec import RunnerSpec

runner = PBSRunner(RunnerSpec(image=None, command=["python", "agent.py"]))
result = runner.run(capsule_dir=Path(".novafabric/runs/01HXAY7M"))
```

The runner submits via `qsub`, polls via `qstat`, and cancels via `qdel` on timeout or signal. Job script is injected at `PBS_JOBSCRIPT`.

### LSFRunner (Python API)

Run a capsule job on an IBM Spectrum LSF cluster:

```python
from novafabric.runners import LSFRunner

runner = LSFRunner(RunnerSpec(image=None, command=["python", "agent.py"]))
result = runner.run(capsule_dir=Path(".novafabric/runs/01HXAY7M"))
```

The runner submits via `bsub`, polls via `bjobs`, and cancels via `bkill`. Job script is injected at `LSF_JOBSCRIPT`.

---

## Evidence Fabric v1.0 commands (ADR-0066)

Requires `pip install novafabric[scale]` for full infrastructure support.

### nova schema list

List all canonical capsule event type names (cap-001, ADR-0066). The
vocabulary is 25 baseline types plus 8 extended span-taxonomy types
(ADR-0082, gap-011): `StateTransition`, `MemoryOperation`,
`GuardrailEvaluated`, `EvaluatorScored`, `RerankerApplied`,
`VectorRetrievalStarted`, `VectorRetrievalCompleted`,
`VectorRetrievalFailed` — 33 in total.

```bash
nova schema list
# RunStarted
# RunCompleted
# RunFailed
# ... (33 types total)
```

The extended span-taxonomy events have matching Pydantic models in
`novafabric.capture.events` (`StateTransitionEvent`, `MemoryOperationEvent`,
`GuardrailEvent`, `EvaluatorEvent`, `RerankerEvent`, `VectorRetrievalEvent`).
Identifiers, digests, scores, and counts are captured by default; raw content
payloads are opt-in (ADR-0021).

### nova cost report

Print a per-run LLM cost report for a tenant (cap-002, requires ClickHouse).

```bash
nova cost report --tenant acme --period 24h
```

Set `NOVA_CLICKHOUSE_URL` to enable ClickHouse integration.

**Token usage-type accounting (ADR-0132 — works today).** `nova capture`
automatically records the full provider-reported token usage breakdown on each
model-call record as an additive, optional `nova.usage` block — `cached_tokens`
(prompt-cache read), `cache_write_tokens`, `reasoning_tokens`,
`audio_input_tokens`/`audio_output_tokens`, `image_input_tokens`/`image_output_tokens`,
`total_tokens`, plus an open `extra` map for provider usage types NovaFabric does
not yet name. Values are copied verbatim from the provider payload (never
re-tokenized locally); an absent field means *not reported*, never zero. At
capsule finalize the per-type sums are rolled up into an optional `usage_totals`
block in `capsule.yaml` — all offline, no server required. The ClickHouse-backed
`/api/cost/report` totals and per-model rows additionally include `cached_tokens`.
Per-usage-type *pricing* is provided by the local pricing catalog (ADR-0133 —
works today; see `nova pricing` and `nova cost estimate` below).

### nova cost estimate (experimental, ADR-0133)

Offline cost for one capsule's model calls — no ClickHouse, no server, no
network. Each call's recorded `nova.cost` block is reported verbatim
(`basis=recorded`; it is never overwritten or recomputed). Calls without a
recorded cost are priced from the merged local pricing catalog and labeled
`basis=estimated`; models absent from every catalog layer stay `unpriced`
(cost 0.0, exactly the pre-catalog behavior).

```bash
nova cost estimate ~/.novafabric/capsules/<run_id>
nova cost estimate ./capsule --pricing-catalog ./pricing.yaml --format json
nova cost estimate ./capsule --at 2026-03-02      # price with the rate in force then
```

The output carries the merged catalog's `sha256:` digest so the estimated
figures are reproducible against the exact pricing that produced them.
Estimated amounts are derived from user-asserted catalog prices — estimates,
never billing records (ADR-0066 wording stands).

### nova cost attribute (experimental, ADR-0146)

Read-only. Splits recorded spend into **productive** vs **wasted** outcomes with a
per-status breakdown. Reads a JSON document
`{runs: [{run_id, status, cost}], productive_statuses?}` (a run's `status` is
productive when it is in `productive_statuses`, default `["success"]`; everything
else is wasted). This is **descriptive evidence, never a verdict** — there is no
threshold, quota, or over-budget field; whether the wasted spend was acceptable is
the operator's call.

```bash
nova cost attribute runs.json
nova cost attribute runs.json --json
```

Exit codes: `0` rendered; `2` the input is missing or malformed. This is the CLI
half of NF-148; a collector that derives per-run cost + status from the records a
capsule already holds is a documented follow-on.

### nova cost fairness (experimental, ADR-0146)

Read-only. Loads per-agent resource totals per dimension (`cost` / `energy` /
`calls`) from a JSON document `{totals: {dimension: {agent: total}}}` and prints a
fairness statistic per dimension — each agent's share, the Gini coefficient, and
the max/mean ratio. **Descriptive evidence, never a verdict**: no threshold, quota,
or pass/fail.

```bash
nova cost fairness totals.json
nova cost fairness totals.json --json
```

Exit codes: `0` rendered; `2` the input is missing or malformed. This is the CLI
half of NF-150; a collector that derives the per-agent totals from a capsule is a
documented follow-on.

### nova cost usage-breakdown (experimental, ADR-0132)

Read-only. Reports the **composition** of a capsule's token volume — each usage
type's share of the counted tokens — plus the cached-read ratio and the factual
`has_reasoning_tokens` / `is_multimodal` flags. It accepts a capsule
`manifest.json` (reading its `usage_totals`) or a bare `usage_totals` object. It
reports **token composition only**: no cost/dollars (pricing is ADR-0133) and no
efficient/within-budget verdict. It honours the ADR-0132 "absent != zero" rule — a
usage type that was never reported is absent from the composition, never
zero-filled.

```bash
nova cost usage-breakdown my-capsule/manifest.json
nova cost usage-breakdown usage.json --json
```

Exit codes: `0` rendered; `2` the input is missing or malformed.

### nova pricing list|show|add (experimental, ADR-0133)

Local, user-extensible model-pricing catalog for offline cost accounting of
self-hosted, fine-tuned, and private models. A catalog is a single local
YAML/JSON file merged over the built-in price table; layers (lowest to
highest precedence): built-in `PRICE_TABLE` < user
(`~/.config/novafabric/pricing.yaml`, honors `$XDG_CONFIG_HOME`) < project
(`./.novafabric/pricing.yaml`) < `--pricing-catalog PATH`. A higher layer's
entries fully replace a lower layer's for the same `model_id`. Fully offline:
no remote registry, no price fetch — NovaFabric never ships live vendor
prices as truth; the built-in table is a convenience default you can
override.

```bash
# Show the whole merged catalog and where each entry came from
nova pricing list
nova pricing list --json | jq .pricing_catalog_digest

# Resolve the effective price for one model (optionally as of a date)
nova pricing show mistral-7b-local
nova pricing show claude-opus-4 --at 2026-03-02

# Price a self-hosted model (writes ./.novafabric/pricing.yaml)
nova pricing add mistral-7b-local --input 0.10 --output 0.30 --unit per_1m \
  --source "internal GPU chargeback 2026-Q3"

# Effective-dated price history (idempotent per model_id + effective-from)
nova pricing add claude-opus-4 --input 0.012 --output 0.060 \
  --effective-from 2026-07-01 --source "negotiated enterprise rate H2 2026"
```

Prices are keyed by the ADR-0132 usage types (`--input`, `--output`,
`--cached`, `--reasoning`, `--audio`, `--image`) with per-`unit` math
(`per_1k`, `per_1m`; images are `per_image`), an ISO-4217 `--currency`
(default USD, never converted), and an optional `--effective-from` date —
the resolver picks the entry in force at the capsule's capture time.
`nova capture` consults the merged catalog automatically when estimating
`cost_usd_estimated`, so self-hosted models get real figures instead of 0.0;
a malformed catalog is skipped with a warning and never fails a capture.
Catalog file schema: `schemas/pricing-catalog.schema.json`
(`schema_version 0.1.0`).

### nova storage inspect

Show audit and PII object info for a run (cap-003).

```bash
nova storage inspect --run-id 01HXAY7MZPQRSTUVWXYZ
```

PII object is only written when `NOVA_CAP003_ENABLED=true`.

### nova storage validate

Validate that an S3 backend supports Object Lock COMPLIANCE mode (cap-009).

```bash
nova storage validate --endpoint http://minio:9000 --bucket nova-capsules
# or via env vars:
NOVA_S3_ENDPOINT_URL=http://minio:9000 nova storage validate
```

Exits 0 on success, 1 if Object Lock is absent or disabled.

### nova erasure request

Queue a GDPR erasure request to delete a run's PII payload (cap-003).

```bash
nova erasure request --run-id 01HXAY7MZPQRSTUVWXYZ
```

Requires S3 GOVERNANCE mode Object Lock and `NOVA_CAP003_ENABLED=true`.

### nova erasure status

Check the status of a pending GDPR erasure request.

```bash
nova erasure status --request-id req-abc123
```

### nova policy capture-level get

Print the current capture level (reads `NOVA_CAPTURE_LEVEL` env var, default `standard`).

```bash
nova policy capture-level get
# Current capture level: standard
```

### nova policy capture-level set

Print instructions for setting a new capture level (restart required).

```bash
nova policy capture-level set --level forensic
# Set NOVA_CAPTURE_LEVEL=forensic (restart required)
```

Valid levels: `minimal`, `standard`, `forensic`, `air_gapped`.

| Level | Description |
|---|---|
| `minimal` | Run metadata only; no model IDs, tool names, or any PII-adjacent fields |
| `standard` | Run + model call metadata; redacted PII (default) |
| `forensic` | Full capture including prompts and responses |
| `air_gapped` | Full capture; no external network calls permitted |

**Reference:** `src/novafabric/runners/_pbs.py`, `src/novafabric/runners/_lsf.py`.

---

## nova kg — Capsule Knowledge Graph (v0.17.0, ADR-0067)

Requires: `pip install 'novafabric[scale-kg]'` (kuzu>=0.11.3).

The KG is a **secondary derived artifact** — separate from the per-run lineage graph —
that aggregates observed entity relationships across all captured capsules:
which agents called which models (`CALLS`), which tools they used (`USES_TOOL`),
and which inference endpoints they routed to (`ROUTES_TO`).

**Environment variable:** `NOVA_KG_PATH` — override the default KuzuDB path.

### nova kg init

Initialise the KG schema at the given path.  Idempotent (safe to run multiple times).

```
nova kg init [--path PATH]
```

| Flag | Default | Description |
|---|---|---|
| `--path` | `.nova/kg/nova_kg.kuzu` | KuzuDB path (also reads `NOVA_KG_PATH`) |

```bash
nova kg init --path /data/nova/kg/nova_kg.kuzu
# KG schema initialised at /data/nova/kg/nova_kg.kuzu
```

### nova kg status

Show KG store health, total edge count, and **per-type node counts** in a Rich text
panel.

```
nova kg status [--path PATH]
```

```bash
nova kg status
# KG store health: ok
#   db_path:    .nova/kg/nova_kg.kuzu
#   edge_count: 1247
#   ┌─ Node counts per layer ──────────────┐
#   │ Node type         │ Count            │
#   │ Agent             │     12           │
#   │ Model             │      4           │
#   │ Tool              │     31           │
#   │ MCPServer         │      3           │
#   │ InferenceEndpoint │      2           │
#   └──────────────────────────────────────┘
```

### nova kg ingest

Ingest events from a capsule directory (or all capsule directories) into the KG.

```
nova kg ingest [CAPSULE_DIR] [--all] [--source local-dir|nats] [--capsule-dir DIR]
               [--path PATH] [--verified/--no-verified]
               [--nats-url URL] [--nats-subject SUBJECT]
```

| Argument / Flag | Description |
|---|---|
| `CAPSULE_DIR` | Path to a single capsule directory (local-dir mode). Omit when using `--all` or `--source nats`. |
| `--all` | Scan and ingest **all** subdirectories under `--capsule-dir` (or `$NOVAFABRIC_CAPSULE_DIR` / `$NOVAFABRIC_HOME/capsules`). |
| `--source local-dir\|nats` | Ingest from capsule files (default) or drain a NATS JetStream subject once and exit. |
| `--capsule-dir DIR` | Base directory to scan when using `--all`. Defaults to `$NOVAFABRIC_CAPSULE_DIR` or `$NOVAFABRIC_HOME/capsules`. |
| `--path` | KuzuDB path (default: `.nova/kg/nova_kg.kuzu`). |
| `--verified` | Mark all events as NovaSeal-verified (sets confidence=1.0). |
| `--nats-url URL` | NATS server URL (used with `--source nats`). |
| `--nats-subject SUBJECT` | NATS JetStream subject to consume (used with `--source nats`). |

**`--source nats` model/tool-call coverage (2026-07-30, [ADR-0220](./decisions.md) follow-up):**
`capture/orchestrator.py`'s real producer now re-emits each locally-captured
model/tool call as a `ModelCallCompleted`/`ModelCallFailed`/
`ToolCallCompleted`/`ToolCallFailed` spool event once the run finishes (no
`*Started` variant — the source data has one record per completed/failed
call, never a separate start event), so this ingestion path produces real
`CALLS`/`USES_TOOL` edges from real NATS traffic — verified end-to-end by
`tests/kg/test_kg.py::test_real_producer_to_kg_pipeline_end_to_end`.
`EndpointRouted` is still never emitted by any producer. `local-dir` and
`--all` read the same underlying `model-calls.jsonl`/`tool-calls.jsonl`
files directly and were unaffected either way.

```bash
# Single capsule
nova kg ingest .novafabric/runs/01HXAY7M --path /data/nova/kg/nova_kg.kuzu
# Ingested 42 events (0 skipped) → wrote 7 KG edges to /data/nova/kg/nova_kg.kuzu

# All capsules in the default capsule directory
nova kg ingest --all

# All capsules in a specific directory (e.g. after a nova-testbench run)
nova kg ingest --all --capsule-dir ~/novafabric-data/capsules
# Bulk ingest complete: 57 capsule(s) scanned · 1423 events ingested · 38 KG edges written · 0 skipped · 0 failed

# Drain a NATS JetStream subject once and exit (see known gap above)
nova kg ingest --source nats --nats-url nats://localhost:4222
```

Supported event types (read from `model-calls.jsonl`, `tool-calls.jsonl`, or `events.jsonl`):

| `event_type` | Edge created |
|---|---|
| `ModelCallCompleted`, `ModelCallStarted` | `CALLS` (Agent → Model) |
| `ToolCallCompleted`, `ToolCallStarted` | `USES_TOOL` (Agent → Tool); if `tool_name` contains `:` (MCP format), also `SERVED_BY` (Tool → MCPServer) |
| `EndpointRouted` | `ROUTES_TO` (Agent → InferenceEndpoint) |

`nova serve` automatically ingests new capsules at the interval set by
`NOVA_KG_INGEST_INTERVAL` (default `60` seconds) without requiring manual
`nova kg ingest` calls.  Use `nova kg ingest --all` (or the dashboard
**Re-ingest All** button) to trigger an immediate bulk ingest without waiting
for the next auto-ingest tick.  Already-ingested directories are tracked in a
SQLite sidecar (`ingest_tracker.db`) that persists across server restarts.

### nova kg build-provenance

**Experimental** (SPKG, [ADR-0111](./decisions.md); requires
`pip install novafabric[spkg]`). Map a capsule's `lineage.jsonl` to a W3C **PROV-O** RDF graph — the
canonical semantic layer of the Security & Provenance Knowledge Graph — and SHACL-validate it on the way
out (ADR-0111 R11: invalid provenance facts are rejected). This is the provenance-graph *export* step; to
populate the operational store used by `nova kg detect`, `attack-path`, and `blast-radius`, use
`nova kg build`.

```
nova kg build-provenance CAPSULE_DIR [-o OUTPUT] [--format turtle|nt|json-ld] [--validate/--no-validate]
```

| Argument / Flag | Description |
|---|---|
| `CAPSULE_DIR` | Path to a capsule directory (reads its `lineage.jsonl`). |
| `--output, -o PATH` | Write the RDF graph to this file (default: stdout). |
| `--format` | RDF serialization: `turtle` (default), `nt`, or `json-ld`. |
| `--validate / --no-validate` | SHACL-validate the graph (default on). Exit `1` if invalid facts are found (R11 ingest gate). |

Exit codes: `0` (built, and SHACL-valid when `--validate`), `1` (SHACL validation failed, or the `[spkg]`
extra is not installed).

```bash
nova kg build-provenance .novafabric/runs/01HXAY7M
# ✓ SHACL-valid: 7 PROV-O triples from 01HXAY7M
# @prefix nf: <https://novafabric.io/ns/spkg#> . …

nova kg build-provenance .novafabric/runs/01HXAY7M -o prov.ttl --format turtle
```

Findings emitted by `nova kg detect` must map to a MITRE ATT&CK technique and/or a D3FEND
countermeasure — a raw anomaly score alone is rejected by the `nf:FindingShape` SHACL constraint
(ADR-0111 R2).

### nova kg build

**Experimental** (SPKG, [ADR-0111](./decisions.md); requires
`pip install novafabric[spkg]`). Build **both** SPKG layers for a capsule: the canonical W3C **PROV-O**
RDF (SHACL-gated) and the operational **KùzuDB labeled-property graph** (LPG) that powers attack-path /
blast-radius traversal. The canonical layer is validated first (ADR-0111 R11) — on failure nothing is
written to the operational store; the LPG is then rebuilt from the same capsule, so it holds no state not
derivable from a capsule (R4). Where `build-provenance` *exports* the RDF, `build` *populates the stores*.

```
nova kg build CAPSULE_DIR [--path KG_PATH] [--validate/--no-validate]
```

| Argument / Flag | Description |
|---|---|
| `CAPSULE_DIR` | Path to a capsule directory (reads its `lineage.jsonl`). |
| `--path` | KùzuDB path for the SPKG operational graph, separate from the Capsule KG (default: `.nova/kg/spkg.kuzu`; env `NOVA_SPKG_PATH`). |
| `--validate / --no-validate` | SHACL-gate the canonical layer before writing the LPG (default on). Exit `1` if invalid facts are found (R11). |

Exit codes: `0` (both layers built), `1` (SHACL validation failed, or the `[spkg]` extra is not installed).

```bash
nova kg build .novafabric/runs/01HXAY7M
# ✓ SPKG built from 01HXAY7M (SHACL-valid): 7 PROV-O triples · 3 LPG edges → .nova/kg/spkg.kuzu
```

### nova kg detect

**Experimental** (SPKG, [ADR-0111](./decisions.md)). Rank a
capsule's most **anomalous lineage edges** with an unsupervised, label-free structural outlier detector:
it learns the fleet's own edge distribution (from `--baseline` capsules, or the target itself) and flags
edges with high combined surprisal (rare edge-type / entity / kind-triple). Every reported edge carries a
MITRE ATT&CK technique (ADR-0111 R2 — never a bare score). This is the dependency-free SP-2 baseline; the
PyGOD/TGN GNN detector is a later, resource-gated upgrade. **Needs no optional extra** (the detector is
pure standard-library).

```
nova kg detect CAPSULE_DIR [--baseline DIR ...] [--top/-k K] [--json] [-o OUTPUT]
```

| Argument / Flag | Description |
|---|---|
| `CAPSULE_DIR` | Capsule directory to score (reads its `lineage.jsonl`). |
| `--baseline DIR` | Capsule dir(s) to learn "normal" from — repeatable. Default: self-baseline on the target. |
| `--top, -k` | Number of most-anomalous edges to report (default 5). |
| `--json` | Emit schema-valid `AnomalyFinding` records instead of the table. |
| `--output, -o PATH` | Write findings JSON here (with `--json`). |

Exit code `0` (an anomaly scan is informational — a finding is not a failure).

```bash
nova kg detect .novafabric/runs/01HXAY7M -k 10
# SPKG anomaly scan — 01HXAY7M (top 10, self-baseline)  → ranked table with ATT&CK column

nova kg detect suspect/ --baseline normal-week-1/ --baseline normal-week-2/ --json -o findings.json
```

### nova kg attack-path

**Experimental** (SPKG, [ADR-0111](./decisions.md); requires
`pip install novafabric[spkg]`). Build the operational graph from a capsule's lineage, then run a bounded
**shortest-path** query between two entities (UC2 lateral-movement). Entities are `kind:ref` pairs, e.g.
`run:attacker`, `dataset:aws_credentials`. Informational — exit `0` whether or not a path exists.

```
nova kg attack-path CAPSULE_DIR --from KIND:REF --to KIND:REF [--max-depth N]
```

| Argument / Flag | Description |
|---|---|
| `CAPSULE_DIR` | Capsule directory whose `lineage.jsonl` builds the SPKG graph. |
| `--from KIND:REF` | Source entity, e.g. `run:attacker`. Required. |
| `--to KIND:REF` | Target entity, e.g. `dataset:aws_credentials`. Required. |
| `--max-depth N` | Maximum path length to search (default: `6`). |

```bash
nova kg attack-path .novafabric/runs/01HXAY7M \
  --from run:attacker --to dataset:aws_credentials
# ⚠ Attack path found: run:attacker → … → dataset:aws_credentials in 3 hop(s)

nova kg attack-path .novafabric/runs/01HXAY7M \
  --from agent:a1 --to artifact:report.md --max-depth 4
# ✓ No attack path from agent:a1 to artifact:report.md within 4 hop(s)
```

### nova kg blast-radius

**Experimental** (SPKG, [ADR-0111](./decisions.md); requires
`pip install novafabric[spkg]`). Build the operational graph from a capsule's lineage, then traverse the
**impact / blast radius** of an entity (UC3 supply-chain propagation). `--downstream` (default) lists
everything reachable *from* the entity — e.g. every run and artifact a poisoned model touched;
`--upstream` lists the entity's provenance instead. Prints a Rich table of affected entities (kind, ref).

```
nova kg blast-radius CAPSULE_DIR --entity KIND:REF [--downstream|--upstream] [--max-depth N]
```

| Argument / Flag | Description |
|---|---|
| `CAPSULE_DIR` | Capsule directory whose `lineage.jsonl` builds the SPKG graph. |
| `--entity KIND:REF` | Entity to analyse, e.g. `model:poisoned-model`. Required. |
| `--downstream / --upstream` | `--downstream` (default): what this entity affects (blast radius, UC3). `--upstream`: what influenced it (provenance). |
| `--max-depth N` | Maximum traversal depth (default: `6`). |

```bash
# What did the poisoned model touch? (downstream / impact)
nova kg blast-radius .novafabric/runs/01HXAY7M --entity model:poisoned-model

# Where did this artifact come from? (upstream / provenance)
nova kg blast-radius .novafabric/runs/01HXAY7M --entity artifact:report.md --upstream
```

### nova kg query

Query models and tools called by a specific agent.

```
nova kg query AGENT_ID [--path PATH] [--output json|text]
```

```bash
nova kg query my-agent --output json
# {
#   "agent_id": "my-agent",
#   "models": [
#     {"model_id": "gpt-4o", "provider": "openai", "call_count": 42, "confidence": 1.0}
#   ],
#   "tools": [
#     {"tool_id": "read_file", "tool_name": "read_file", "call_count": 18, "confidence": 0.0}
#   ],
#   "mcp_servers": [
#     {"server_id": "filesystem", "server_name": "filesystem", "call_count": 18}
#   ]
# }
```

The `mcp_servers` key lists MCPServer nodes reachable via the 2-hop path
`Agent → Tool → MCPServer`.  Empty when no MCP-namespaced tool calls were recorded.

**Reference:** `src/novafabric/kg/`, `design/adr/0067-capsule-knowledge-graph-v1.md`.

### nova kg audit

Check KG store health: node/edge counts, orphaned edges, and zero-call-count
anomalies.

```
nova kg audit [--path PATH] [--output json|text]
```

| Flag | Default | Description |
|---|---|---|
| `--path` | `.nova/kg/nova_kg.kuzu` | KuzuDB path (also reads `NOVA_KG_PATH`) |
| `--output`, `-o` | `text` | Output format: `text` (Rich) or `json` |

```bash
nova kg audit
# KG store health: ok
#   node_agent_count: 12
#   node_model_count: 4
#   edge_calls_count: 88
#   No issues detected.

nova kg audit --output json
```

Exit code 0 = no issues.  Exit code 1 = issues detected.

### GET /api/kg/topology (dashboard API)

Returns all KG nodes and edges for multi-layer topology visualization.  Requires `nova
serve` to be running with a KG store already initialised.

```
GET /api/kg/topology?max_nodes=500
Authorization: Bearer <token>
```

Response includes:
- `nodes` — array of `{id, type, name?, provider?, url?}` for all 5 node types (Agent, Model, MCPServer, Tool, InferenceEndpoint)
- `edges` — array of `{src, src_type, dst, dst_type, edge_type, call_count, confidence}` for CALLS / USES_TOOL / SERVED_BY / ROUTES_TO
- `node_counts` — per-type totals `{Agent: N, Model: N, MCPServer: N, Tool: N, InferenceEndpoint: N}`
- `edge_counts` — per-type totals

Used by the KGTab dashboard panel (Multi-Layer Topology section, lazy-loaded on demand).
Response is cached for 30 seconds per serve process.

### GET /api/kg/entity-queue (dashboard API)

Return all pending `ReviewItem` records from the Tier-3 human review queue. Requires `nova serve`.

```
GET /api/kg/entity-queue
GET /api/kg/entity-queue/stats
POST /api/kg/entity-queue/{item_id}/approve
POST /api/kg/entity-queue/{item_id}/reject
```

`GET /api/kg/entity-queue` returns `{ ok, count, items[] }` where each item has `item_id`, `candidate_name`, `entity_type`, `context_run_id`, `confidence`, `reason`, `status`, `created_at`.

`GET /api/kg/entity-queue/stats` returns `{ ok, pending, approved, rejected }`.

`POST .../approve` body: `{ "canonical": "resolved-entity-id", "resolved_by": "username" }`.
`POST .../reject` body: `{ "resolved_by": "username" }` (optional).

Used by the KGTab **Entity Review Queue** panel in the dashboard (v0.31.0).

### GET /api/kg/aliases (dashboard API)

List or register KG alias-table entries. Requires `nova serve` to be running.

```
GET  /api/kg/aliases[?canonical=<id>]   # list all; optional canonical filter
POST /api/kg/aliases                     # register an alias
```

POST body: `{ "alias": "gpt4", "canonical": "openai/gpt-4", "entity_type": "model" }`
Optional POST fields: `confidence` (float, default `1.0`), `registered_by` (string, default `"api"`).

Used by the KGTab **KG Alias Management** panel in the dashboard.

### nova kg alias list

List all aliases registered for a canonical entity name in the Tier-2 alias
table (SQLite-backed, no KuzuDB dependency required).

```
nova kg alias list CANONICAL [--alias-db PATH] [--output json|text]
```

| Argument / Flag | Default | Env var | Description |
|---|---|---|---|
| `CANONICAL` | _(required)_ | — | Canonical entity name to look up |
| `--alias-db` | `.nova/kg/alias.db` | `NOVA_KG_ALIAS_DB` | SQLite path for the alias table |
| `--output`, `-o` | `text` | — | Output format: `text` (Rich table) or `json` |

```bash
nova kg alias list gpt-4o
# Aliases for 'gpt-4o':
#  Alias          Entity Type  Confidence  Source   Created At
#  openai/gpt-4o  model        1.000       manual   2026-05-10T…
```

### nova kg alias register

Manually register an alias → canonical mapping in the Tier-2 alias table.

```
nova kg alias register ALIAS CANONICAL [--type model|agent|tool|endpoint|mcp_server]
    [--confidence FLOAT] [--alias-db PATH]
```

| Argument / Flag | Default | Description |
|---|---|---|
| `ALIAS` | _(required)_ | Alias form seen in capsule event payloads |
| `CANONICAL` | _(required)_ | Canonical entity name to map to |
| `--type`, `-t` | `model` | Entity type: `model`, `agent`, `tool`, `endpoint`, or `mcp_server` |
| `--confidence`, `-c` | `1.0` | Confidence score (0.0–1.0) |
| `--alias-db` | `.nova/kg/alias.db` | SQLite path for the alias table |

```bash
nova kg alias register "openai/gpt-4o" "gpt-4o" --type model --confidence 1.0
# Registered: 'openai/gpt-4o' → 'gpt-4o' (type=model, confidence=1.000)

nova kg alias register "filesystem" "nova-mcp-filesystem" --type mcp_server
# Registered: 'filesystem' → 'nova-mcp-filesystem' (type=mcp_server, confidence=1.000)
```

### nova kg entity-queue list

List all pending review items in the Tier-3 entity human review queue
(SQLite-backed).

```
nova kg entity-queue list [--queue-db PATH] [--output json|text]
```

| Flag | Default | Env var | Description |
|---|---|---|---|
| `--queue-db` | `.nova/kg/review_queue.db` | `NOVA_KG_QUEUE_DB` | SQLite path for the review queue |
| `--output`, `-o` | `text` | — | Output format: `text` (Rich table) or `json` |

```bash
nova kg entity-queue list
# Entity Review Queue (2 pending)
#  Item ID  Alias       Type   Suggested Canonical  Confidence  Created At
#  uuid-…   my-agent    agent  my-agent-v2          0.720       2026-05-15T…
```

### nova kg entity-queue approve

Approve a review item and assign its canonical name.

```
nova kg entity-queue approve ITEM_ID --canonical NAME [--by REVIEWER]
    [--queue-db PATH]
```

| Argument / Flag | Default | Description |
|---|---|---|
| `ITEM_ID` | _(required)_ | UUID of the review item to approve |
| `--canonical`, `-c` | _(required)_ | Canonical name to assign |
| `--by` | `cli` | Reviewer identifier (name or user ID) |
| `--queue-db` | `.nova/kg/review_queue.db` | SQLite path for the review queue |

```bash
nova kg entity-queue approve e3f9… --canonical "my-agent-v2" --by alice
# Approved: e3f9… → canonical='my-agent-v2' (resolved_by='alice')
```

### nova kg entity-queue reject

Reject a review item without assigning a canonical name.

```
nova kg entity-queue reject ITEM_ID [--by REVIEWER] [--queue-db PATH]
```

| Argument / Flag | Default | Description |
|---|---|---|
| `ITEM_ID` | _(required)_ | UUID of the review item to reject |
| `--by` | `cli` | Reviewer identifier (name or user ID) |
| `--queue-db` | `.nova/kg/review_queue.db` | SQLite path for the review queue |

```bash
nova kg entity-queue reject e3f9… --by alice
# Rejected: e3f9… (resolved_by='alice')
```

### nova kg entity-queue stats

Show pending / approved / rejected counts for the entity review queue.

```
nova kg entity-queue stats [--queue-db PATH] [--output json|text]
```

| Flag | Default | Env var | Description |
|---|---|---|---|
| `--queue-db` | `.nova/kg/review_queue.db` | `NOVA_KG_QUEUE_DB` | SQLite path for the review queue |
| `--output`, `-o` | `text` | — | Output format: `text` or `json` |

```bash
nova kg entity-queue stats
# Entity Review Queue Stats
#   pending:  3
#   approved: 18
#   rejected: 2

nova kg entity-queue stats --output json
# {"pending": 3, "approved": 18, "rejected": 2}
```

---

## Framework Adapters (v0.26.0)

Drop-in capture adapters for four additional AI frameworks. Each adapter uses the
SDK's own native extensibility interface (ADR-0078) rather than wrapping the executor.
All framework packages are optional extras.

### OpenAI Agents SDK adapter (E-5)

```python
# Install: pip install 'novafabric[openai-agents]'
from novafabric.adapters.openai_agents import register

# Call once at startup before any agent runs
register()

# All subsequent Runner.run() calls are captured as nova capsules
result = await Runner.run(agent, "hello")
```

The adapter registers a `NovaCapsuleTracingProcessor` via `add_trace_processor()`.
Each trace produces one capsule in `$NOVAFABRIC_HOME/capsules/`. `capture_mode` is
`adapter-openai-agents`.

Top-level alias: `from novafabric.adapters import register_openai_agents`

### Google ADK adapter (E-6)

```python
# Install: pip install 'novafabric[google-adk]'
from novafabric.adapters.google_adk import make_plugin
from google.adk.runners import Runner

runner = Runner(
    agent=my_agent,
    session_service=svc,
    plugins=[make_plugin()],
)
```

The adapter implements `before_run_callback` and `after_run_callback` on a
`NovaAdkPlugin` instance. `capture_mode` is `adapter-google-adk`.

Top-level alias: `from novafabric.adapters import make_google_adk_plugin`

### AWS Bedrock AgentCore adapter (E-7)

```python
# Install: pip install 'novafabric[bedrock-agentcore]'
import boto3
from novafabric.adapters.bedrock_agentcore import wrap_client

client = wrap_client(
    boto3.client("bedrock-agent-runtime", region_name="us-east-1")
)
response = client.invoke_agent(
    agentId="my-agent", agentAliasId="TSTALIASID",
    sessionId="session-1", inputText="hello"
)
for event in response["completion"]:
    if "chunk" in event:
        print(event["chunk"]["bytes"].decode())
```

The adapter wraps `invoke_agent()` and parses the EventStream for
`orchestrationTrace`, `preProcessingTrace`, `postProcessingTrace` chunks (written to
`bedrock-traces.jsonl`). The capsule is finalized when the stream is exhausted.
`capture_mode` is `adapter-bedrock-agentcore`.

All non-`invoke_agent` methods are delegated transparently to the underlying client
(`__getattr__` passthrough).

Top-level alias: `from novafabric.adapters import wrap_bedrock_agentcore`

### A2A SDK adapter (E-8)

```python
# Install: pip install 'novafabric[a2a]'
from novafabric.adapters.a2a import make_interceptor
from a2a.client import A2AClient

client = A2AClient(
    base_url="http://my-agent:8080",
    interceptors=[make_interceptor()],
)
result = await client.send_message(agent_card=card, message=msg)
```

The adapter implements `before()` and `after()` on a `NovaA2AInterceptor` instance.
Only `send_message` and `send_message_streaming` calls are captured; other methods
pass through unchanged. `capture_mode` is `adapter-a2a`. Task envelopes are written
to `a2a-tasks.jsonl` inside the capsule.

This implements RFC-0002 §Q4 (full A2A protocol-aware capture), deferred until A2A SDK
reached 1.0 stability.

Top-level alias: `from novafabric.adapters import make_a2a_interceptor`

**Reference:** `src/novafabric/adapters/`, `design/adr/0078-ecosystem-adapters.md`.

## Accountability Spine (experimental, ADRs 0093–0095)

Three research-grounded features for tamper-evident ex-post evidence. All additive and
opt-in; none is a third top-level format (ADR-0034). See
`design/architecture/accountability-spine.md`.

### nova energy probe

Report which energy counters this host can actually read (the ground truth the forgery
guard checks against). On hardware that cannot attribute per-action energy, receipts are
honestly marked `unavailable` rather than fabricated.

```bash
nova energy probe
```

### nova energy attest

Write one `EnergyReceipt` per action into `energy-receipts.jsonl` for a captured capsule.
The degrade-safe default emits honest `unavailable` receipts; Slurm `sacct ConsumedEnergyRaw`
yields the measured per-job class.

```bash
nova energy attest ./my-capsule
```

### nova energy verify

Re-check receipt integrity (payload-hash) and honesty/forgery consistency, then the energy
conservation status. Forgery-guard exit codes: `3` integrity failure (e.g. a `measured`
receipt with no available counter), `4` conservation diverged, `0` OK.

```bash
nova energy verify ./my-capsule
```

### nova energy report

Tabulate the receipts in a capsule (`--format table|json`), showing source, confidence,
and joules per action.

```bash
nova energy report ./my-capsule --format table
```

**Reference:** `src/novafabric/energy/`, `design/adr/0093-energy-anchored-action-receipts.md`.

### nova ledger anchor

Build per-stream sidecar hash-chains over a capsule's jsonl event streams (the `.jsonl`
files are never mutated) and write a DSSE-signed checkpoint. Requires a signing key.

```bash
nova ledger anchor ./my-capsule --key ~/.config/novafabric/keyring/ed25519.pem
```

### nova ledger verify

Verify the chains against the signed checkpoint. Detects content edits, reordering, and
truncation even after a host compromise. Seal-style exit codes (`3` tamper, `4` reorder,
`5` truncation, `6` bad signature, `10` no checkpoint).

```bash
nova ledger verify ./my-capsule
```

### nova ledger status

Show the current chain heads and checkpoint state for a capsule.

```bash
nova ledger status ./my-capsule
```

**Reference:** `src/novafabric/trust/ledger/`, `design/adr/0094-adversary-anchored-ledger-and-replay-attestation.md`.

### nova safety-case build

Compile a Claims-Arguments-Evidence safety case from a capsule's real artifacts (evals,
seals, criterion bindings, replay attestations) against a template. Backing states are
driven by inter-judge κ and Wilson confidence intervals; naked (unsupported) claims are a
schema error.

```bash
nova safety-case build ./my-capsule --template clymer-generic-v0 --output case.json
```

### nova safety-case verify

Re-verify a safety case: artifact-reference hashes, the recomputed `case_hash`, and the
no-naked-claims invariant (I1). Requires the source capsule for artifact re-hashing.

```bash
nova safety-case verify case.json --capsule ./my-capsule
```

### nova safety-case export

Render a safety case as JSON, Markdown, or a regulatory safety-case document
(`--format json|markdown|annex-iv|nist-rmf`). The `annex-iv` renderer (experimental,
ADR-0095) binds to the same 15 EU AI Act Annex IV element ids as
`compliance/export/annex_iv_mapping.yaml`; `nist-rmf` renders the NIST AI RMF view.
Honesty is structural: a CONTESTED claim renders its reason, an UNSUPPORTED claim is never
laundered to "compliant", and a not-quantified residual risk is never fabricated.

```bash
nova safety-case export case.json --format markdown
nova safety-case export case.json --format annex-iv --output annex-iv.md
nova safety-case export case.json --format nist-rmf --output nist-rmf.md
```

### nova evidence bind-custody

Build the FRE-902(14) court-admissibility block (chain-of-custody + self-authentication) for a
capsule, from the hash-chained audit log + capsule Merkle root. Invariant I3: unwitnessed fields
are `null` + `operator_declared`, never fabricated. With `--key`, the capsule hash is signed and
verified.

```bash
nova evidence bind-custody 01HX... --custodian alice@corp --provenance oidc --key ed25519.pem -o custody.json
```

### nova evidence check-admissibility

Re-run the five-point FRE-902(14) gate on a custody block and exit non-zero (3) unless the result
is `self-authenticating`. The checker supplies independent timestamp evidence via `--timestamp-ok`.

```bash
nova evidence check-admissibility custody.json --timestamp-ok
```

**Reference:** `src/novafabric/safetycase/`, `design/adr/0095-evidence-grounded-safety-case-and-admissible-evidence.md`.

### GET /api/runs/{id}/energy (dashboard API)

Return the energy receipts and conservation status for a run (experimental, ADRs 0093/0094/0095).
Token-gated, read-only. Requires `nova serve` to be running.

```
GET /api/runs/{id}/energy
Authorization: Bearer <token>
```

Response includes the per-action `receipts` (source, confidence grade, joules) plus the signed
energy `conservation` status (`conserved` / `diverged` / `unmeasurable`). The React EnergyTab
panel that renders this is the remaining frontend follow-up (**not yet built**).

### GET /api/runs/{id}/ledger (dashboard API)

Return the Adversary-Anchored Ledger verify status for a run (experimental, ADR-0094).
Token-gated, read-only.

```
GET /api/runs/{id}/ledger
Authorization: Bearer <token>
```

Response includes the per-stream chain verify result and the tamper-taxonomy exit code
(content edit / reorder / truncation / bad signature / no checkpoint).

### GET /api/runs/{id}/safety-case (dashboard API)

Return the compiled Claims-Arguments-Evidence safety-case tree for a run (experimental,
ADR-0095). Token-gated, read-only. The optional `template` query parameter selects the CAE
skeleton.

```
GET /api/runs/{id}/safety-case?template=clymer-generic-v0
Authorization: Bearer <token>
```

Response is the compiled CAE tree with backing states (SUPPORTED / UNSUPPORTED / CONTESTED /
UNKNOWN). The React SafetyCaseTab panel that renders this is the remaining frontend
follow-up (**not yet built**).

---

## Summary and next steps

The `nova` CLI is a complete, self-hosted toolkit: capture a run, replay it,
diff two runs, trace lineage, and export signed evidence — all locally, offline,
with no accounts. A typical first pass through the five primitives looks like:

```bash
# 1. Capture — turn any command into a Run Capsule
nova capture python agent.py

# 2. Validate — confirm the capsule is schema-valid and secret-redacted
nova validate .novafabric/runs/<ulid>/

# 3. Replay — re-run against the recorded LLM/tool calls (default: mocked)
nova replay .novafabric/runs/<ulid>/

# 4. Diff — structurally compare two runs; fail CI on any regression
nova diff .novafabric/runs/<ulid-a>/ .novafabric/runs/<ulid-b>/ --assert-no-regressions

# 5. Trace — see what a run consumed and what it would impact
nova lineage provenance <ulid>
nova lineage blast-radius <ulid>

# 6. Prove — build a signed Evidence Bundle an auditor can verify offline
nova export-evidence .novafabric/runs/<ulid>/ --key ~/.novafabric/keys/ed25519.pem \
  --output evidence.zip
```

The resulting `evidence.zip` verifies with only `sha256sum` and an ed25519
verifier — no NovaFabric runtime required. The vendored schemas make it a time
capsule: it still verifies years later against the same schemas it was built
with.

### Where to go from here

- **Concepts and the five-primitive model** — see the project README and
  [`docs/`](.) guides.
- **Writing a capture hook plugin** — [`docs/integrations/writing-a-hook-plugin.md`](integrations/writing-a-hook-plugin.md).
- **NovaSeal configuration** (signature + RFC 3161 timestamp + Merkle log) —
  [`docs/novaseal-configuration.md`](novaseal-configuration.md).
- **Server deployment** (Postgres, OIDC, RBAC) —
  [`docs/ops/server-deployment.md`](ops/server-deployment.md).
- **Dashboard capabilities and CLI-parity matrix** — [`docs/dashboard.md`](dashboard.md).

### A note on what is planned versus shipped

Several sections above (the NovaSeal signing **service**, the cluster-scale
collector binaries, parent/child capsules, the production RLS MetadataStore, the
object capsule store, lineage-at-scale, and the live topology dashboard) describe
**planned or experimental** cluster-scale direction. They are documented here for
completeness, but the local-first core — capture, validate, replay, diff,
lineage, signed Evidence Bundles, the SQLite registry, and OPA/Rego promotion
gates — is what works today and never depends on any of them.

When in doubt, run `nova <command> --help`: the terminal is always the
authoritative description of the installed version.
