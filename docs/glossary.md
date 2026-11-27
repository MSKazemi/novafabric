# Glossary

Every product term used across the NovaFabric docs, in one or two sentences, with
the page that explains it and its maturity label.

**How to read the labels.** Each term carries exactly one of **works today**
(implemented, tested, described as built), **experimental** (implemented; the
interface may change), **planned** (on the [ROADMAP](../ROADMAP.md) with a target)
or **future design** (documented intent, no implementation). The label is the one the
linked page gives; where it gives none, the [README status](../README.md#status) and
the [ROADMAP](../ROADMAP.md) decide. NovaFabric is beta: even **works today** formats
are not frozen until the v1.0 schema freeze. Longer explanations are in
[Concepts](concepts.md); the questions people hit in practice are in the [FAQ](faq.md).

[Capture](#capture) · [Replay](#replay) · [Diff and diagnosis](#diff-and-diagnosis) ·
[Lineage](#lineage) · [Registry and governance](#registry-and-governance) ·
[Trust and evidence](#trust-and-evidence) · [Deployment](#deployment)

---

## Capture

| Term | Definition | Maturity | Explained in |
|---|---|---|---|
| **Run Capsule** (capsule) | A ULID-named directory holding every observable fact of one execution — manifest, model and tool calls, spans, environment, output, secret-scan proof. It is the source of truth; every index is rebuilt from it. | works today | [Run Capsule anatomy](architecture/run-capsule.md) |
| **Run ID** / **ULID** | The 26-character, time-sortable identifier that names a capsule (and a replay), so a directory listing is already chronological. | works today | [Concepts › ULID](concepts.md#ulid) |
| **`capsule.yaml`** (manifest) | The run manifest: command, timing, status, exit code, references to the streams, and `evidence_digests` (SHA-256 and size of every other file). Its schema is closed at the top level. | works today | [Run Capsule anatomy](architecture/run-capsule.md#the-manifest-capsuleyaml) |
| **`model-calls.jsonl`** | One record per LLM call, keyed with OpenTelemetry GenAI attributes. It stores the full request messages and response text at every capture level. | works today | [Concepts](concepts.md#model-callsjsonl) |
| **Transport record** / **logical record** | A call made through the OpenAI or Anthropic SDK leaves one *logical* record (from the SDK hook) and one *transport* record per HTTP attempt beneath it. Every count reads logical calls only. | works today | [Model-call record roles](architecture/run-capsule.md#model-call-record-roles) |
| **`tool-calls.jsonl`** | One record per tool invocation (MCP, `record.tool` functions, framework tools, proxies). | works today | [Run Capsule anatomy](architecture/run-capsule.md) |
| **`env.lock`** | The environment snapshot: Python, installed packages, host, and the lock-file hash. It is `deterministic` only for a hash-pinned `uv.lock`, otherwise `best-effort`. | works today | [Concepts › Environment Lock](concepts.md#environment-lock-envlock) |
| **`capture-health.json`** | Written only when events were dropped or the workload tampered with the capsule; counts drops per stream and source. Its absence means nothing was dropped. | works today | [Run Capsule anatomy](architecture/run-capsule.md#the-files-in-write-order) |
| **Tampered capsule** (`capture_tampered`) | A capsule in which the workload planted a link, FIFO or special file during capture. Capture removes the entry and records it; `nova verify` and `nova validate` then fail the capsule. | works today | [Concepts](concepts.md#links-inside-a-capsule-are-never-followed-works-today) |
| **Capture** (`nova capture`) | Running any command under NovaFabric, with no code change, so it leaves a Run Capsule — on success and on failure. | works today | [Concepts › Capture Hook Mechanism](concepts.md#capture-hook-mechanism) |
| **Capture hook** (per-SDK hook, wire-level hook) | Monkey-patches a `sitecustomize.py` loader installs in the workload: per-SDK hooks (OpenAI, Anthropic, MCP) record structured calls, and wire-level hooks (`httpx`, `requests`, `aiohttp`, `urllib3`) catch anything that bypasses them. A hook never changes what the call does. | works today | [Concepts › Capture Hook Mechanism](concepts.md#capture-hook-mechanism) |
| **URL registry** | The list of model-provider endpoints the wire-level hooks classify as model calls; you can extend it with `~/.novafabric/url_registry.yaml`. | works today | [Concepts](concepts.md#capture-hook-mechanism) |
| **Capture level** (`NOVA_CAPTURE_LEVEL`) | `minimal`, `standard` (default), `forensic` or `air_gapped`. It changes only what adapter, tool-hook and `record` paths keep (digests vs payloads); `minimal` behaves like `standard`, `air_gapped` like `forensic`. No level keeps prompts out of a capsule. | experimental | [Concepts › Capture levels](concepts.md#capture-levels--what-a-capsule-stores-experimental) |
| **Secret scanner** / **rule pack** | Pattern-based scanning of every capsule file against `gitleaks-core-v0` (18 rules); matches are redacted in place as `[REDACTED:rule-id]`. It finds known key formats only — capsules are *secret-scanned*, not secret-free. | works today | [What the scanner detects](architecture/run-capsule.md#what-the-scanner-detects-and-what-it-does-not) |
| **Redaction proof** (`redaction-proof.json`) | The scan record: files scanned with before/after hashes, each finding's rule and offset (never the secret), and a `chain_hash`. It records what was scanned and redacted, not that no secret remains. | works today | [The redaction proof](architecture/run-capsule.md#the-redaction-proof-redaction-proofjson) |
| **Masker** (`nova capture --masker`) | A pluggable PII masker that runs after the built-in scanner; every mask is attributed in the redaction proof. | experimental | [CLI reference › nova capture](cli-reference.md#nova-capture-cmd) |
| **Runner** (`--runner`) | Where the captured workload executes: `local` (default), or `docker`, `kubernetes`, `slurm`, `lsf`, `pbs`. | works today (`local`); experimental (the others) | [Concepts › Runners](concepts.md#runners--where-the-captured-workload-executes) |
| **Framework adapter** | Capture inside a framework call (LangGraph, OpenAI Agents SDK, Google ADK, smolagents, …) that writes a capsule with `capture_mode: sdk-decorator`. | experimental | [Capsules written inside a framework call](architecture/run-capsule.md#capsules-written-inside-a-framework-call) |
| **`@agent` decorator** (SDK decorator) | `novafabric.sdk.agent.agent`: in-process capture of one function into a capsule store, or OTel spans only when no `capsule_dir` is given. | experimental | [Concepts › SDK Decorator](concepts.md#sdk-decorator) |
| **`record.tool`** | A decorator (`novafabric.capture.record.tool`) that declares a Python function as a tool, so it is recorded and can be served by mocked replay. Payloads are kept only at `forensic` / `air_gapped`. | experimental | [Concepts › mocked mode](concepts.md#mocked-mode) |
| **API proxy** / **MCP proxy** | `nova api-proxy` (HTTP) and `nova mcp-proxy` (stdio MCP) record traffic from clients NovaFabric cannot hook, in any language. | experimental | [Concepts](concepts.md#capture-hook-mechanism) |
| **Session** | A content-addressed `session.json` that orders N independent capsules (turns of a conversation, stages of a workflow); `nova session` creates, exports and replays it. | experimental | [Concepts › Sessions vs parent/child](concepts.md#sessions-vs-parentchild-two-different-groupings) |
| **Parent/child capsule** | One distributed execution recorded as a PARENT capsule plus N WORKER capsules under one `global_run_id` (`nova run`). The ROADMAP calls it a prototype: not yet validated at target scale. | experimental | [Parent and child capsules](architecture/run-capsule.md#parent-and-child-capsules-prototype) |
| **Evidence facet** | An optional, first-party evidence object under the capsule's `facets` map (A2A messages, settlement, embodied, science, memstore, retrieval). The registry of names is closed. Not the same as an OpenLineage facet. | experimental | [Concepts › Evidence facets](concepts.md#evidence-facets-experimental) |
| **Extensions** | The capsule's open container for third-party vendor data, keyed by reverse-DNS prefix. | works today | [Concepts › Run Capsule](concepts.md#run-capsule) |
| **Warm capture daemon** | A resident prefork daemon (`nova daemon`) that pays the Python import cost once, so each capture forks instead of cold-starting. Linux only. | experimental | [Warm capture daemon](warm-capture-daemon.md) |

## Replay

| Term | Definition | Maturity | Explained in |
|---|---|---|---|
| **Replay** (`nova replay`) | Re-running or inspecting a capsule in one of five explicit modes. Each writes `replays/<ulid>/replay_result.yaml`. | works today | [Replay modes](architecture/replay-modes.md) |
| **`forensic` mode** | Read-only inspection: no subprocess, no network. | works today | [Replay modes › The modes](architecture/replay-modes.md#the-modes) |
| **`mocked` mode** (default) | Re-runs the recorded command and serves recorded model responses and intercepted tool results from the capsule; everything else runs live. Python workloads, supported surfaces only. | works today | [The mocked-replay contract](architecture/replay-modes.md#the-mocked-replay-contract-adr-0300-adr-0304) |
| **`semantic` mode** | A text-similarity score (`difflib`) over the capsule's recorded responses. It does not re-execute and calls no model. | works today | [Replay modes › The modes](architecture/replay-modes.md#the-modes) |
| **`exact` mode** | An eligibility check for a byte-exact re-run (deterministic `env.lock`, a seed on every model call, no tool-schema drift). It does not re-execute. | works today | [Replay modes › The modes](architecture/replay-modes.md#the-modes) |
| **`intervention` mode** | Counterfactual replay: substitutes one recorded event as an `InterventionSpec` says, re-runs downstream under mocked semantics, and writes a counterfactual capsule (`replay_mode: intervention`) you can `nova diff`. | experimental | [Concepts › intervention mode](concepts.md#intervention-mode-experimental) |
| **Fail closed** / **divergence** | A strict mocked replay marks itself `failure` with a `divergence_reason` on an extra, unmatched or unserved call, even if the workload caught the error. `--permissive` only reports. | works today | [The mocked-replay contract](architecture/replay-modes.md#the-mocked-replay-contract-adr-0300-adr-0304) |
| **`unserved_network`** | The divergence a strict mocked replay records when the replayed process opened a connection past loopback — after the fact, so the side effect has happened. | works today | [Concepts › mocked mode](concepts.md#mocked-mode) |
| **Replayability report** (`replayability.json`) | Written at capture: what a strict mocked replay of this capsule would do (`replayable`: `full` · `partial` · `none`), with per-surface counts and reason codes. `nova replay` never reads it. | experimental | [Replayability at capture time](architecture/replay-modes.md#replayability-at-capture-time-experimental) |
| **Fidelity verdict** | The `fidelity` block of a mocked replay result: whether the replay *reproduced* the recording (exit code, stdout, files written, call sequence, no divergence, no live call), as distinct from how the process exited. `--require-faithful` gates on it. | experimental | [Fidelity verdict](architecture/replay-modes.md#fidelity-verdict-adr-0321-experimental) |
| **Hermetic replay** (`--hermetic`) | Mocked replay inside a `bwrap` sandbox: no network beyond its own loopback, a read-only host filesystem except the working and result directories. Linux only. | experimental | [Hermetic replay](architecture/replay-modes.md#hermetic-replay---hermetic-experimental-adr-0322) |
| **Safety ladder** | Side-effect classes (`read-only` … `unknown`) and the `--allow-*` flags that let an unmatched or overridden intercepted tool call run live, each behind an audited policy gate. | experimental | [Concepts › Safety ladder](concepts.md#safety-ladder-mocked-replay) |
| **Tool override** | A per-tool `allow: true / false` in the capsule's `replay.yaml`. A restriction is trusted; a permission needs the operator's ladder flag. | experimental | [Concepts › Safety ladder](concepts.md#safety-ladder-mocked-replay) |
| **Tool-result echo** | A report-only check that the result an undeclared function sends back to the model matches the recording. | experimental | [Concepts › mocked mode](concepts.md#mocked-mode) |

## Diff and diagnosis

| Term | Definition | Maturity | Explained in |
|---|---|---|---|
| **Structural diff** (`nova diff`) | A field-by-field comparison of two capsules (model and tool calls, environment, outputs). `--assert-no-regressions` exits 1 on any change, which makes it a CI gate. | works today | [Concepts › Structural Diff](concepts.md#structural-diff) |
| **First divergence** | Two related reports: in `nova diff`, the earliest model or tool call where two runs stop agreeing; in `nova verify`, the first evidence file the seal no longer matches (file granularity). | works today | [CLI reference](cli-reference.md#replay-commands-v03) |
| **Diagnose** (`nova diagnose`) | Structural, no-LLM attribution of a failed run to its most likely responsible step; `--intervene` / `--search-root-cause` confirm a hypothesis by intervention replay. | experimental | [Concepts › Diagnose](concepts.md#diagnose-causal-graph-attribution-and-counterfactual-root-cause-search-experimental) |
| **Offline analytics** (`nova query`, `nova view`, `nova trend`) | Read-only filter, group-by and trend reports over the metrics already recorded in local capsules. | experimental | [Concepts](concepts.md#offline-analytics-over-capsules-experimental) |
| **Drift** (`nova drift`) | Offline two-sample drift and silent-failure checks over samples from sealed capsules. | experimental | [Drift detection](drift-gate.md) |

## Lineage

| Term | Definition | Maturity | Explained in |
|---|---|---|---|
| **Lineage graph** | A directed provenance graph indexed from each capsule's `lineage.jsonl` into `registry.db`; it can always be rebuilt from the capsules. | works today | [Lineage graph](architecture/lineage-graph.md) |
| **Lineage edge kinds** | Capture writes `consumed`, `produced_by` and `replayed_from`; the registry writes `depends_on`; `nova migrate` writes `migration_source`. `derived_from`, `evaluated_by` and `attested_by` are reserved. Distributed runs and sessions add `contains`, `spawned`, `delegated_to` and `member_of_session`; `wrote_memory` / `read_memory` are derived on demand. | works today | [Concepts › Edge types](concepts.md#edge-types) |
| **Confidence** (`observed` · `inferred` · `declared`) | Where an edge came from: recorded while the run executed (`consumed`, from your own `record_asset_consumption()` call), derived from capsule structure (`produced_by` from `outputs`), or stated by a record or spec (`replayed_from`, `depends_on`). NovaFabric does not watch file or dataset access. | works today | [Concepts › Confidence levels](concepts.md#confidence-levels) |
| **Provenance** / **blast radius** | The two main traversals: what a run or artifact depended on, and what depends on an asset (`nova lineage provenance`, `nova lineage blast-radius`). | works today | [Lineage graph › Traversals](architecture/lineage-graph.md#traversals) |
| **Replay chain** / **time travel** | `nova lineage replay-chain` follows `replayed_from` back to the original run; `nova lineage time-travel` lists the edges directly incident to a node as of time T (one hop). | works today | [Lineage graph › Traversals](architecture/lineage-graph.md#traversals) |
| **OpenLineage emission** | OpenLineage 2.0.2 `START` / `COMPLETE` / `FAIL` events, at capture when `OPENLINEAGE_URL` is set or on demand with `nova lineage emit-openlineage`. | works today | [Concepts › OpenLineage Integration](concepts.md#openlineage-integration) |
| **At-scale lineage backends** | Kuzu, Postgres, Apache AGE and JanusGraph implementations of the lineage store, usable from Python; no `nova` command selects them. | experimental | [Lineage graph › Storage backends](architecture/lineage-graph.md#storage-backends) |
| **Lineage federation** | Cross-cluster federation of lineage and capsules. | future design | [Deployment topologies](architecture/deployment-topologies.md#cluster-scale-experimental-partial) |

## Registry and governance

| Term | Definition | Maturity | Explained in |
|---|---|---|---|
| **Asset** (AI asset) | A versioned artifact — `model`, `agent`, `prompt`, `tool`, `dataset`, `evaluation` or `deployment` — described by a YAML spec and addressed as `name@version`, pinned to a git SHA. | works today | [Concepts › AI Asset](concepts.md#ai-asset) |
| **Asset Registry** (registry) | The local SQLite database (`registry.db`, override `NOVAFABRIC_DB_PATH`) of assets, eval results, approvals, proposals and status history. | works today | [Concepts › Asset Registry](concepts.md#asset-registry) |
| **Lifecycle status** | `development` → `validated` → `pending_approval` → `staging` → `production` → `archived`, with skips allowed and no way back from `archived`. A status is governance metadata only: promotion never restarts or redeploys anything. | works today | [Concepts › Asset Lifecycle](concepts.md#asset-lifecycle) |
| **Promotion** / **protected stage** | Changing an asset's status (`nova promote direct`). `production` is a protected stage: a single actor needs an explicit `promote_direct` policy allow, so it is normally reached by `nova promote propose` + `nova promote approve`. | works today | [Concepts](concepts.md#what-actually-changes-when-you-promote-an-asset) |
| **Maker-checker** | Two-person sign-off: one identity proposes, another approves with its own key — for promotions (`nova promote propose` / `approve`) and for NovaSeal signatures (`nova seal propose` / `approve`). | works today (promotion); experimental (NovaSeal) | [Sealing › Maker-checker](architecture/sealing-and-verification.md#maker-checker-signing-experimental) |
| **Eval-gated promotion** | An `agent` reaches `staging` or `production` only when its latest eval passed (`nova eval agent`), unless `--force`, which is recorded. | works today | [Concepts › Eval-Gated Promotion](concepts.md#eval-gated-promotion) |
| **Policy gate** | An OPA/Rego allow/deny decision, written to the audit log, before a governed action (promotion, export, a write-permitting replay). | works today | [CLI reference › Policy and governance](cli-reference.md#policy-and-governance-v08) |
| **Prompt asset** / **deployment label** | `nova prompt register` stores a prompt as an immutable, content-addressed version; `nova label` points a mutable name such as `production` at one version. | experimental | [Concepts › AI Asset](concepts.md#ai-asset) |
| **Legal hold** (`nova hold`) | A hold on a registry that suspends every capsule deletion (retention, `nova capsule delete`, server and dashboard deletes) until released. | experimental | [CLI reference](cli-reference.md#nova-hold-create-registry) |
| **WORM storage** | Write-once object storage adapters (S3 Object Lock, Azure immutable blob, GCS Bucket Lock) for retained capsules. | experimental | [Concepts › Evidence Bundle](concepts.md#evidence-bundle) |

## Trust and evidence

| Term | Definition | Maturity | Explained in |
|---|---|---|---|
| **Evidence Bundle** | A signed ZIP built by `nova export-evidence`: a copy of the whole capsule, its lineage edges, the JSON schemas, Ed25519-signed in-toto DSSE attestations and a manifest with every file's SHA-256. A recipient verifies it offline without NovaFabric. | works today | [What is in the bundle](architecture/sealing-and-verification.md#what-is-in-the-bundle) |
| **NovaSeal** / **seal** | Opt-in capsule sealing at capture: a DSSE envelope over the manifest (ECDSA P-256 by default) in `.seal/`, a Merkle log entry, and an RFC 3161 timestamp when a `tsa_url` is set. A default capture is unsealed. | experimental | [Sealing and verification](architecture/sealing-and-verification.md) |
| **`nova seal init`** | Creates a local, self-asserted ECDSA P-256 sealing identity and a managed `novaseal.yaml` with no network access. Separate from the Ed25519 key `nova init` writes for Evidence Bundles. | experimental | [Turning sealing on](architecture/sealing-and-verification.md#turning-sealing-on) |
| **Signing profile** | Where the NovaSeal key lives: `local` (PEM on disk), `aws_kms`, `azure_kv` or `gcp_kms`. | experimental | [NovaSeal configuration](novaseal-configuration.md) |
| **Merkle log** | NovaSeal's append-only local log of seals; `nova verify` checks a capsule's inclusion in it. | experimental | [The Merkle log](architecture/sealing-and-verification.md#the-merkle-log) |
| **RFC 3161 timestamp** / **`timestamp_trust`** | A token from a timestamp authority over the seal, requested only when configured. `timestamp_trust` is `anchored` only when the TSA chain was checked against a CA bundle you supplied. | experimental | [RFC 3161 timestamp](architecture/sealing-and-verification.md#rfc-3161-timestamp) |
| **`nova verify`** | Checks a sealed capsule: signature, timestamp, Merkle inclusion, manifest binding and every file's digest; exits 1 for an unsealed or tampered capsule. | experimental | [Verification](architecture/sealing-and-verification.md#verification-nova-verify-capsule) |
| **`nova validate`** | Checks that a capsule is well-formed (schemas of `capsule.yaml`, `env.lock`, `redaction-proof.json`; no links or special files; no recorded tampering). Needs no key. | works today | [FAQ](faq.md#what-is-the-difference-between-nova-validate-and-nova-verify) |
| **Evidence cart** | In the dashboard, a collection of runs exported together as one signed Evidence Bundle. | experimental | [User guide](user-guide.md) |
| **Air-gap bundle** | One tar whose members are inventoried in a DSSE-signed `airgap-manifest.json`, verifiable with no network. | experimental | [Air-gapped install](ops/air-gapped-install.md#signed-bundle-format-adr-0249-experimental--first-slice) |
| **Trust surfaces** | Human-readable projections of a capsule's trust evidence: `nova merkle-tree`, `nova trust-radar`, `nova redaction-xray`, `nova passport`. | experimental | [Trust surfaces](trust-surfaces.md) |
| **Accountability Spine** | Energy receipts (`nova energy`), a sidecar hash-chain ledger (`nova ledger`) and a structured safety case (`nova safety-case`) — record-only evidence beside the seal. | experimental | [Concepts › Accountability Spine](concepts.md#accountability-spine-experimental) |
| **Compliance exporters** | `nova export-compliance` and the `nova export-*` family: projections of captured evidence into regulator-shaped artifacts (EU AI Act, ISO 42001, NIST AI RMF, GDPR, …). They render facts, never a compliance verdict. | experimental | [Concepts › Evidence Bundle](concepts.md#evidence-bundle) |
| **Assurance case** | A machine-checkable argument graph bound to sealed evidence (`nova assure-case`). | experimental | [Assurance cases](assurance-cases.md) |

## Deployment

| Term | Definition | Maturity | Explained in |
|---|---|---|---|
| **Local mode** | The `nova` CLI on one machine with capsules under `$NOVAFABRIC_HOME` (default `~/.novafabric`) and a SQLite registry. No server, account or network. | works today | [Concepts › Local mode and server mode](concepts.md#local-mode-and-server-mode) |
| **`nova serve`** (dashboard) | The single-user web dashboard over local capsules, bound to `127.0.0.1`; needs `--experimental` and the `serve` extra. | experimental | [Dashboard](dashboard.md) |
| **Server mode** (`nova server start`) | The multi-user REST API: SQLite or Postgres, OIDC / offline-token / API-key auth, RBAC roles `reader`, `writer`, `admin` and `auditor`. | experimental | [Deployment topologies › Server mode](architecture/deployment-topologies.md#server-mode-experimental) |
| **Organization** / **workspace** | Server-mode scopes for the registry tier and usage attribution. They do not partition capsule bytes, so the server refuses to start with more than one organization unless you acknowledge the shared store. | experimental | [CLI reference](cli-reference.md#storage-and-server-commands-v07) |
| **Spool** (node spool) | A bounded local buffer on a compute node that `nova capture --emit-spool` writes Event Envelope v1 records to, so the hot path never writes to a database. | experimental | [Deployment topologies › Cluster-scale](architecture/deployment-topologies.md#cluster-scale-experimental-partial) |
| **Spool forwarder** | The Go binary `novafabric-spool-forwarder` that drains a node spool to the NATS JetStream stream `NOVA_EVIDENCE`. | experimental | [Deployment topologies › Cluster-scale](architecture/deployment-topologies.md#cluster-scale-experimental-partial) |
| **Collector** | The cluster-scale tier: the Go `novafabric-collector` and a custom OpenTelemetry Collector build with a NovaSeal processor, plus `nova collector` buffer operations. The collector image is not published. | experimental | [Deployment topologies › Cluster-scale](architecture/deployment-topologies.md#cluster-scale-experimental-partial) |
| **Event Envelope v1** | The JSON Schema / proto3 record format the spool, forwarder and consumers exchange. | experimental | [Deployment topologies › Cluster-scale](architecture/deployment-topologies.md#cluster-scale-experimental-partial) |
| **HPC hub** | A central signing service for HPC clusters; the binary is a stub that exits "not implemented". | future design | [Deployment topologies › Cluster-scale](architecture/deployment-topologies.md#cluster-scale-experimental-partial) |
| **Object capsule store** / **metadata DB** | Capsule storage on object stores with a manifest chain, and the Postgres metadata index with row-level security, for server and cluster deployments. | experimental | [Deployment topologies](architecture/deployment-topologies.md) |
| **Extra** | An optional dependency group (`pip install 'novafabric[serve]'`); `nova doctor --check-extras` lists which are installed. | works today | [Installation](installation.md) |
