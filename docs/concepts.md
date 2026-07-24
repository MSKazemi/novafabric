# Concepts

This page is the conceptual reference for NovaFabric. It explains the nouns you
will meet everywhere else in the docs and on the `nova` command line — what a
Run Capsule is, how capture works without touching your code, the four replay
modes, structural diff, lineage, the Asset Registry and its lifecycle, and how
signed evidence is produced.

**What you will learn**

- The **five primitives** NovaFabric is built on — Asset Registry, Run Capsule,
  Replay, Lineage, and Evidence Bundle — and the strategic verb chain that
  connects them: **Capture → Seal → Replay → Diff → Audit**.
- The exact on-disk anatomy of a **Run Capsule** and why it is the source of
  truth (everything else is a rebuildable index).
- How **zero-code-change capture** works via a `sitecustomize.py` loader and
  per-SDK plus wire-level hooks.
- The **four replay modes** — `forensic`, `mocked`, `semantic`, `exact` — and
  what each one honestly promises.
- How **structural diff** becomes a CI regression gate, and how **lineage**
  answers provenance and blast-radius questions.
- How the **Asset Registry lifecycle** works and why promotion is governance
  metadata only — it never restarts or redeploys anything.
- What is **shipped today** versus **planned** (clearly labeled throughout).

> **Maturity note.** NovaFabric is local-first and in beta (v0.59.0). Nearly
> all shipped surfaces carry `experimental` maturity: they work today and are
> tested, but on-disk formats are **not frozen** until the v1.0 schema freeze.
> Anything labeled **PLANNED** or **FUTURE DESIGN** below is documented design
> intent (in the ADRs) and is **not implemented** — never treat it as shipped.

---

## The five primitives at a glance

NovaFabric is framed around exactly five primitives, each with a public spec and
JSON Schema. Cryptographic sealing is part of the Evidence Bundle / trust layer,
not a sixth primitive.

| # | Primitive | What it is | Since |
|---|---|---|---|
| 1 | **Asset Registry** | Local SQLite registry of versioned AI assets (`name@version`), pinned to a git SHA, with a six-state lifecycle | v0.1 |
| 2 | **Run Capsule** | The fundamental unit of capture: a ULID-named directory holding every observable fact of one execution | v0.2 |
| 3 | **Replay** | Re-execute or inspect a capsule with external calls controlled, in four honest modes | v0.3 |
| 4 | **Lineage** | A directed provenance graph (SQLite cache) with mechanical edge types and OpenLineage emission | v0.4 |
| 5 | **Evidence Bundle** | A signed, self-contained ZIP an auditor can verify offline with only `sha256sum` + an ed25519 verifier | v0.4 |

The design invariant behind all five: **the capsule is the source of truth; the
registry, metadata DB, and lineage graph are derived, rebuildable indexes.**
Only two top-level formats exist — Run Capsule and Evidence Bundle — and
introducing a third requires an accepted ADR.

---

## Run Capsule

A **Run Capsule** is the fundamental unit of capture in NovaFabric. It is a
directory containing all observable facts about a single command execution:
the command, timing, environment, every LLM call made, every tool invoked,
stdout/stderr, and a proof that no secrets escaped.

Capsules are identified by a [ULID](https://github.com/ulid/spec) — a
lexicographically sortable, time-prefixed unique identifier.

```
.novafabric/capsules/01HXAY7M5JZ8R7K4P9DPBYK2WX/
  capsule.yaml          ← run manifest
  trace.jsonl           ← execution spans (OTel)
  model-calls.jsonl     ← LLM API calls (OTel GenAI semconv)
  tool-calls.jsonl      ← tool invocations
  assets.jsonl          ← asset references
  env.lock              ← environment snapshot
  redaction-proof.json  ← secret scan proof
  replay.yaml           ← replay constraints
  lineage.jsonl         ← lineage edges
  inputs/
  outputs/
    stdout.txt
    stderr.txt
```

Capsules are written on success **and** failure. A failed run produces a
complete capsule with `status: failure` and an `error` block — the evidence of a
failure is just as durable as the evidence of a success.

Capsules support additive, optional `extensions:` blocks (for example `slurm`,
`kubernetes`, `ray`, `openlineage`) so the schema can grow without introducing a
new top-level format. Readers tolerate unknown fields; old capsules stay readable
forever.

### Sessions vs parent/child (two different groupings)

Two distinct, composable ways of relating capsules exist — do not conflate them:

- **Session (experimental, ADR-0122).** A *logical, multi-turn* grouping: N
  independent runs (e.g. the turns of a conversation or the stages of a
  workflow) referenced in order by a content-addressed `session.json` manifest
  (`nova session new/add/list/show`). Members are ordinary capsules, each with
  its own writer; the session copies no capsule data. `nova session replay`
  (experimental, ADR-0123) replays the members in sequence order.
- **Parent/child (prototype, ADR-0039).** A *physical, distributed-run*
  hierarchy: one logical execution fanned out across nodes (e.g. a Slurm DDP
  job) recorded as a PARENT capsule plus N WORKER capsules under a single
  `global_run_id`.

A session member may itself be a distributed-run PARENT. A capsule that belongs
to a session carries two additive optional back-reference fields (`session_id`,
`sequence`); absence means a standalone run, byte-identical to before.

> **Related:** [Replay Modes](#replay-modes) (a replay is itself a new capsule
> you can diff), [Lineage Graph](#lineage-graph), and
> [Evidence Bundle](#evidence-bundle).

---

## Evidence facets (experimental)

A capsule can carry optional, additive **evidence facets** — a `facets` map
(ADR-0196) whose keys are per-domain evidence objects, each owned by its own ADR.
A capsule without the `facets` key is valid and byte-identical to before, so
facets never change the base schema. `facets` holds *first-party,
NovaFabric-recorded evidence*; it is deliberately distinct from `extensions`,
which carries *third-party vendor data* — an auditor must be able to tell
NovaFabric-recorded evidence from a vendor annotation. The registry is **closed
on purpose** (ADR-0196 D2): only the registered names below are accepted, so a
typo cannot silently produce a capsule missing the evidence its author believed
it carried.

> **Not the same as OpenLineage facets.** The word "facet" is also used,
> unrelatedly, for the [OpenLineage run/dataset facets](#openlineage-integration)
> that `nova lineage emit-openlineage --with-facets` attaches to emitted lineage
> events. Those live in OpenLineage payloads, not on the capsule. This section is
> about the capsule `facets` container.

Each facet is populated by a **record-only** evidence module: it records what
another system did and never orchestrates, enforces, adjudicates, moves funds,
controls a device, or sits in a hot path. All are **experimental** Python APIs
today; none of them registers a `nova` CLI command.

| Module | Capsule facet | Records | ADR |
|---|---|---|---|
| `novafabric.a2a` | `a2a_messages` | Multi-agent A2A messages & handoffs — the wire between agents. Distinct from the `novafabric.adapters.a2a` capture-time SDK adapter, which is a separate subsystem. | ADR-0142 |
| `novafabric.settlement` | `settlement` | Agentic-commerce settlement provenance — mandate reconciliation, finality, and non-repudiation binding. Never processes payments or holds/moves funds. | ADR-0163 |
| `novafabric.embodied` | `embodied` | Embodied / cyber-physical agent evidence — declared sensor streams and actuation records. Stores references, digests, and counts only (never frames, point clouds, audio, or control credentials); never in a control path. | ADR-0162 |
| `novafabric.science` | `science_provenance` | Scientific-reproducibility / research-integrity provenance as a verifiable DAG of research steps. Never runs experiments or adjudicates validity. | ADR-0164 |
| `novafabric.memstore` | `memstore_mutation` | Persistent-knowledge / organisational-memory governance — an append-only ledger of who changed which shared-KB entry and when (store-external). Distinct from the [`nova memory`](cli-reference.md) capture feature. | ADR-0171 |
| `novafabric.retrieval` | `fetch_provenance` (+ source pinning) | Retrieval-source authority & knowledge provenance — what an external retriever or the agent fetched, and whether a pinned source still matches. Never fetches, crawls, or ranks. | ADR-0153 |
| `novafabric.context` | *(standalone artifacts)* | Context provenance — an ordered manifest of what entered the model's context window (`ContextManifest`) and the span→chunk support map a producer claimed (`GroundingMap`). Records `sha256:` digests only; never scores groundedness. Ships as standalone artifacts, not a registered capsule facet (ADR-0196 D2). | ADR-0143 (NF-112/113) |

---

## Capture Hook Mechanism

`nova capture <cmd>` works without modifying application code. It injects a
`sitecustomize.py` loader into the subprocess via `PYTHONPATH`. When Python
starts the subprocess, it imports this loader automatically, which installs
monkey-patches for:

- **OpenAI** — `openai.resources.chat.completions.Completions.create`
- **Anthropic** — `anthropic.resources.messages.Messages.create`
- **httpx** — `httpx.Client.send`, recording requests classified by the URL registry (`src/novafabric/capture/hooks/url_registry.yaml` + `~/.novafabric/url_registry.yaml` override). Default coverage: OpenAI, Anthropic, Cohere, Together, Mistral, Replicate, AWS Bedrock, Ollama (default port 11434). Non-default Ollama ports are detected automatically from `OLLAMA_BASE_URL` / `OLLAMA_HOST` at call time.
- **requests** — `requests.Session.send`, same URL-registry classification (v0.5; RFC-0001 Option C wire-level layer). Covers LangChain HTTP adapters, LlamaIndex REST clients, and any SDK that ships over `requests`.
- **aiohttp** — `aiohttp.ClientSession._request`, async wire-level capture (v0.6 / C-3.1). Catches LangChain async paths, FastAPI agents, streaming-first SDKs.
- **urllib3** — `urllib3.connectionpool.HTTPConnectionPool.urlopen`, lowest-tier wire-level capture (v0.6 / C-3.2). Sits below `requests` and `boto3`; transitively covers AWS Bedrock and any SDK that uses `urllib3` directly. Layered with the higher hooks via a context-local guard so a single `requests` call records exactly once.
- **MCP (Model Context Protocol)** — `mcp.client.session.ClientSession.call_tool`, recording every tool invocation as a `tool-calls.jsonl` entry with `transport=mcp` and a synthesized JSON-RPC envelope (v0.5; ADR-0015 §Primary).
- **Third-party plugins** — any class registered under the `novafabric.hooks` entry-point group is auto-discovered alongside the built-ins (v0.5.x experimental; RFC-0001 §Detailed design — Option C). Plugin authors: see [`docs/integrations/writing-a-hook-plugin.md`](integrations/writing-a-hook-plugin.md).

The layering is deliberate: **per-SDK hooks** capture rich, structured request
and response objects; **wire-level hooks** form a safety net beneath them so a
call that bypasses a known SDK (or uses a version whose surface changed) is still
recorded once at the transport layer. Because the hooks reach down to `urllib3`,
capture does not depend on your choice of HTTP client library.

Patches are removed after the run. If an SDK is not installed, its hook is
silently skipped. Capture works even if none of the AI SDKs are present — the
capsule is still written with environment, stdout/stderr, and timing.

**For uninstrumented agents** (Claude Desktop, Cursor, third-party SDKs that do not import the Python `mcp` package), `nova mcp-proxy` is a transparent stdio proxy that records the same `tool-calls.jsonl` schema by sitting between the client and an upstream MCP server. See [`docs/cli-reference.md`](cli-reference.md) and ADR-0015 §Secondary; experimental in v0.5.x.

**For non-Python clients over HTTP**, `nova api-proxy` is a transparent HTTP
proxy that classifies traffic against the same vendored provider registry — so a
client in any language can be captured without SDK hooks (v0.6).

### Runners — where the captured workload executes

The orchestrator delegates subprocess execution to a **runner** (ADR-0025).
Choose one with `nova capture --runner <name>`:

| Runner | Since | What it does | Key required options |
|---|---|---|---|
| **`local`** | v0.5.x | Runs the workload as a local subprocess. Default; no remote infrastructure needed. | — |
| **`docker`** | v0.6 | Runs the workload inside a Docker container. The image must already have NovaFabric installed. Capsule directory is mounted as a volume so artifacts land on the host. | `image` |
| **`kubernetes`** | v0.6.1 | Runs the workload as a Kubernetes `Job` via `kubectl` shell-out. Artifacts are pulled back via `kubectl cp`. | `image`, `namespace` |
| **`slurm`** | v0.6.1 | Runs the workload as a SLURM batch job via `sbatch` + `sacct`. Trusts a shared filesystem for artifacts (no rsync). | `partition` |

Runner details:

- **`docker`** — pass `--runner-option image=<ref>` (e.g. `pip install novafabric`
  in your Dockerfile). Other options: `network`, `workdir`, `user`,
  `extra_volumes`, `extra_env`. No privileged containers, no docker-socket
  mounts, no host namespaces by default — see ADR-0025 §Anti-patterns.
- **`kubernetes`** — optional: `service_account`, `node_selector`, `resources`.
  Anti-patterns are enforced in the Job manifest itself:
  `securityContext.privileged: false`, `hostNetwork: false`, `hostPID: false`,
  `hostIPC: false`, `backoffLimit: 0` (one shot, no retries). RBAC required:
  `create`/`get`/`watch`/`delete` on `jobs`+`pods`, `get` on
  `pods/log`+`pods/exec`. **No cluster-admin.**
- **`slurm`** — optional: `account`, `qos`, `time`, `nodes`, `gres`, `mem`,
  `constraint`. The capsule directory **must** be on a filesystem shared between
  submit and compute nodes (typically `/scratch`, `/lustre/...`, or the user's
  home). The runner does not rsync artifacts; it trusts the shared FS.

> **Note.** The single-node runner is the smallest case of a distributed run.
> The cluster-scale tiers shipped in v0.10+ with honest maturity labels:
> parent/child capsules (`nova run …`) are a **prototype** — implemented and
> tested, not yet validated at target scale — and the collector tier, object
> capsule store, and Postgres metadata DB are **experimental**. See
> [ROADMAP.md](../ROADMAP.md) for per-component labels; the federation and
> at-scale graph tiers remain **future design**.

---

## `model-calls.jsonl`

Every LLM API call intercepted by the capture hooks is recorded as one JSONL
line following the [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/):

```json
{
  "schema_version": "0.1.0",
  "semconv_version": "1.30.0",
  "model_call_id": "01HXAY7M6FN9TQGE0V0M7PAY1Q",
  "parent_span_id": "657bff2c61ddad1c",
  "started_at": "2026-05-09T12:34:56.123456Z",
  "finished_at": "2026-05-09T12:34:57.353456Z",
  "duration_ms": 1230,
  "status": "success",
  "gen_ai.system": "openai",
  "gen_ai.operation.name": "chat",
  "gen_ai.request.model": "gpt-4o",
  "gen_ai.response.model": "gpt-4o-2024-08-06",
  "gen_ai.response.id": "chatcmpl-abc123",
  "gen_ai.request.temperature": 0.7,
  "gen_ai.request.max_tokens": 1024,
  "gen_ai.request.top_p": 0.95,
  "gen_ai.request.seed": 42,
  "gen_ai.request.stop_sequences": ["END"],
  "gen_ai.request.messages": [...],
  "gen_ai.response.choices": [...],
  "gen_ai.response.finish_reasons": ["stop"],
  "gen_ai.usage.input_tokens": 42,
  "gen_ai.usage.output_tokens": 18,
  "endpoint": "https://api.openai.com/v1/chat/completions"
}
```

The full set of OTel "Required when applicable" fields is extracted by both
the per-SDK hooks (`_openai`, `_anthropic`) and the wire-level hooks
(`_httpx`, `_requests`, `_aiohttp`, `_urllib3`) when present in the request
body — so `nova replay --mode exact` has the determinism inputs it needs
(temperature, top_p, seed) regardless of which transport the captured call
took. See [`design/spec/model-call-v0.md`](../design/spec/model-call-v0.md) for the full
field reference.

This format is stable across providers and is the basis for two things:

- **`nova diff` call alignment** — aligned model calls are compared field by
  field (see [Structural Diff](#structural-diff)).
- **`nova replay` mock serving** — recorded calls are replayed from the capsule
  cache in `mocked` mode (see [Replay Modes](#replay-modes)).

---

## Environment Lock (`env.lock`)

The environment lock records the full execution environment at capture time:

- Python version and interpreter path
- All installed packages (up to 200)
- Safe environment variables (secrets are excluded)
- Host OS, CPU architecture, CPU count, total memory
- GPU presence
- Detected package manager (`uv.lock`, `poetry.lock`, `requirements.txt`, etc.)

`nova replay` uses `env.lock` to warn about environment mismatches before
re-executing the command. This is what makes the `exact` replay mode
*falsifiable*: eligibility for byte-exact replay depends on a matching,
deterministic environment plus a per-call seed, and `env.lock` is where that
determination starts.

---

## Secret Scanning and Redaction

Before a capsule is finalized, `SecretScannerV0` scans all JSONL artifacts
for 12 LLM provider key patterns (Anthropic, OpenAI, HuggingFace, Replicate,
Langfuse, and others). Detected values are redacted in-place as
`[REDACTED:rule-id]`.

After scanning, `redaction-proof.json` is written. It records:

- How many findings were detected
- Which files were scanned
- A cryptographic chain hash proving the scan ran

Capsules without `redaction-proof.json` are considered invalid by
`nova validate` and **cannot be exported** into an Evidence Bundle. Verifiable
redaction is therefore not optional — it is a precondition of every downstream
audit artifact.

---

## Replay Modes

A replay re-executes or inspects a capsule with all external calls controlled by
NovaFabric. There are **four honest, falsifiable modes**. A replay is itself a
new capsule, so you can diff a replay against the original run.

| Mode | Spawns subprocess? | Network? | Best for |
|---|---|---|---|
| **`forensic`** | No | No | Audit / post-incident inspection |
| **`mocked`** | Yes | LLM served from cache; tools gated by safety ladder | CI / regression |
| **`semantic`** | Yes | Yes (re-executes) | Drifting remote LLMs — judges *meaning*, not tokens |
| **`exact`** | Yes | Controlled | Local / on-prem / compliance byte-exact re-run |

> **Honesty note.** NovaFabric explicitly does **not** claim byte-exact replay of
> remote LLM calls. `exact` mode requires a deterministic environment and a
> per-call seed, which is realistic for local/on-prem models but not for a
> remote endpoint that can change under you. For remote LLMs that drift, use
> `semantic` mode, which scores similarity of meaning on a 0.0–1.0 scale.

### `forensic` mode

Read-only inspection. NovaFabric does not spawn a subprocess. It reads and
returns the manifest, traces, and model calls directly from the capsule.
No network access, no mutation.

Use forensic mode to inspect what happened without any risk of side effects.

### `mocked` mode

The original command is re-spawned as a subprocess. All LLM calls are
intercepted and served from the capsule cache in order
(`MockModelDispatcher`). Tool calls are mocked or denied according to the
**safety ladder** below.

### Safety ladder (mocked replay)

Mocked replay gates tool call re-execution behind explicit flags. By default,
**every tool call is denied** — you opt in, rung by rung, to exactly the level of
side effect you are willing to allow:

```
(none)               — deny all tool calls
  ↓  --allow-readonly
read-only            — permit read-only tool calls
  ↓  --allow-mutating
idempotent-write     — permit idempotent writes
non-idempotent-write
  ↓  --allow-external-side-effects
external-side-effect — permit calls with external effects
  ↓  --allow-unknown-mutation
unknown              — permit unclassified tool calls
```

Per-tool overrides can be specified in `replay.yaml`.

### `semantic` mode

Re-executes the command against live models and **judges meaning rather than
tokens**, returning a 0.0–1.0 similarity score. This is the honest answer to the
reality that remote LLMs drift: two runs weeks apart may produce different tokens
yet mean the same thing.

### `exact` mode

Byte-exact eligibility requiring a deterministic environment and a per-call seed.
This is the compliance-grade mode for local and on-prem models where determinism
is achievable.

---

## Structural Diff

`nova diff` compares two capsules field by field:

- **Model call alignment** — calls are matched by `span_id`, then by
  position + prompt hash. Aligned pairs are compared for model, token counts,
  and response content.
- **Tool call alignment** — calls are matched by `tool_name` + argument hash.
- **Environment diff** — Python version, OS, architecture, interpreter path.
- **Output diff** — stdout/stderr by content hash.

`DiffReport` has three counters: `changed`, `added`, `removed`. The
`--assert-no-regressions` flag exits 1 if any of these are non-zero, making
it suitable as a CI gate:

```bash
# Fail the pipeline if today's run diverges from a known-good baseline
nova diff baseline-capsule/ candidate-capsule/ --assert-no-regressions
```

This is the mechanism behind the "worked yesterday, fails today" flaky-agent
problem: wire `nova diff --assert-no-regressions` into CI and a structural
regression stops the merge.

### Offline analytics over capsules (experimental)

Because every metric a run produced — cost, tokens, latency, scores — is already
recorded inside its capsule, analytics never needs a server. The **experimental**
v0.59 surfaces read those recorded facts, offline and read-only: `nova query` is
a bounded filter → group-by → aggregate DSL over the local capsule directory,
`nova view` persists a named query as a small versionable file, and `nova trend`
buckets one metric over time or by asset into a JSON/HTML report. Nothing is
recomputed or fetched; the capsule directory is never written. See the
[CLI reference](cli-reference.md#offline-metrics-query-experimental-adr-0129).

---

## Lineage Graph

The lineage graph is a directed graph stored in SQLite — a rebuildable cache
derived from each capsule's `lineage.jsonl`. It has two table types:

- **`lineage_nodes`** — runs, assets, and artifacts. Each node has a
  deterministic ID (SHA-256 of `kind:ref`).
- **`lineage_edges`** — typed, directional edges between nodes.

Because the graph is derived from the capsules, it can always be rebuilt from
them — the capsules remain the source of truth. The SQLite backend is the
local-mode default and is crash-safe (WAL); it is well suited below roughly one
million edges. (A KuzuDB-backed v2 tier for larger graphs is **experimental** —
`nova lineage-store migrate`; billion-edge federation and the Postgres/Apache
AGE backends remain **FUTURE DESIGN**, not implemented.)

### Edge types

| Edge type | Meaning |
|---|---|
| `consumed` | The run read/used this asset or artifact |
| `produced_by` | This artifact was produced by the run |
| `replayed_from` | This run is a replay of the referenced run |

### Confidence levels

| Confidence | Meaning |
|---|---|
| `observed` | Directly recorded at runtime (e.g., explicit API call) |
| `inferred` | Derived from capsule structure (e.g., output file presence) |

### Graph queries

| Query | Direction | Use case |
|---|---|---|
| `provenance` | Backward (ancestors) | "What did this run depend on?" |
| `blast-radius` | Forward (descendants) | "What runs consume this asset?" |
| `replay-chain` | Backward via `replayed_from` | "What is the original run for this replay?" |
| `time-travel` | Backward, filtered by timestamp | "What was the lineage state as of time T?" |

---

### Graph analytics (experimental)

Traversal answers "what connects to this node?". A read-only **analytics layer**
(ADRs 0212–0215) answers "what does the whole graph mean?": centrality
(`nova lineage metrics` — hubs and single points of failure), node-level
root-cause ranking (`nova lineage root-cause`), interop export to
GraphML/GEXF/Cypher (`nova lineage export-graph`), and a synthesized
intelligence report (`nova insights`). All are descriptive rankings for
attention, not calibrated importance, and degrade honestly when a data source
(such as cost) is absent.

## OpenLineage Integration

NovaFabric emits [OpenLineage](https://openlineage.io) 2.0.2 events from
capsules. Events are typed as `START` and `COMPLETE` (or `FAIL`).

Events can be emitted:

- Automatically at capture time if `OPENLINEAGE_URL` is set
- On demand via `nova lineage emit-openlineage`

This enables integration with data-catalog tools such as Marquez, Atlan, and
OpenMetadata — so NovaFabric's run-level provenance can join the broader
data-pipeline lineage you already track.

**Custom run facets (experimental).** `emit-openlineage --with-facets` attaches
NovaFabric-specific facets to the COMPLETE event — the capsule id/hash, the eval verdict,
the promotion-policy decision, and reproducibility run params — and
`--otel-correlation` adds `trace_id`/`span_id` so a lineage node links to its OTel GenAI
spans. Facets are additive and schema-validated before emission, so a catalog that
ignores them still receives valid core OpenLineage events. Run facets, dataset provenance
cards, and benchmark-contamination checks are shown hands-on in the
[feature tour §17](tutorials/feature-tour.md#17-prove-supply-chain-provenance--eval-integrity).

> **Note:** these OpenLineage facets are unrelated to the capsule
> [`facets` container](#evidence-facets-experimental) (ADR-0196). Same word, two
> different places: OpenLineage facets live in emitted lineage events; evidence
> facets live on the capsule.

---

## AI Asset

An **AI asset** is any versioned artifact in an ML or LLM system. NovaFabric
tracks seven types:

| Type | What it represents |
|---|---|
| `model` | A trained ML/LLM model — framework, artifact path |
| `agent` | An AI agent — model ref, tools, prompts, policies, eval suites |
| `prompt` | A prompt template used by one or more agents |
| `tool` | A callable function exposed to an agent |
| `dataset` | A dataset used for training, fine-tuning, or evaluation |
| `evaluation` | An evaluation suite definition (test cases, scoring logic) |
| `deployment` | A running endpoint — base URL, environment |

Assets are described by YAML spec files and stored in the local Asset Registry.
Every registered asset is addressed as `name@version`, pinned to a git commit
SHA, and carries a lifecycle status that tracks where it is in your
development-to-production pipeline.

**Prompts as versioned assets (experimental, ADR-0112).** Beyond the YAML spec
route, `nova prompt register` turns a prompt into an **immutable,
content-addressed** registry version: every edit registers a new version (never
a mutation), and a run references it as `prompt:<id>@<version>+sha256:<hex>` —
so a capsule can be tied to exactly the prompt bytes that ran. Mutable
deployment labels (`nova label`, ADR-0113) point a name like `production` at one
immutable version, and prompt composition (`nova prompt compose`, ADR-0115)
snapshots pinned includes so an assembled prompt rebuilds byte-identically.
NovaFabric never renders or serves prompts — it versions and proves them.

---

## Asset Lifecycle

### The state machine

```
development ──► validated ──► pending_approval ──► staging ──► production ──► archived
      │                                                  ▲
      └──────────────────────────────────────────────────┘
           (direct skip: development → staging or production)
```

All six statuses in plain language:

| Status | Meaning |
|---|---|
| `development` | Work in progress. Default when registered. No constraints. |
| `validated` | Passed automated checks (`nova validate`). Spec is well-formed, secrets are clean. |
| `pending_approval` | Waiting for a human sign-off (`nova approve`). Useful in regulated environments. |
| `staging` | Cleared for pre-production use. **Agents require a passing eval to reach here.** |
| `production` | Live. **Agents require a passing eval to reach here.** |
| `archived` | Retired. No further promotion possible. Capsules and lineage are still readable. |

Direct skips (e.g. `development → production`) are allowed. Once archived, an
asset cannot be re-promoted. The `validated` and `pending_approval` steps are
optional — small teams often go straight from `development` to `staging`.

### What actually changes when you promote an asset

**Promoting an asset updates one database record.** That is all NovaFabric does.

```
nova promote direct my-agent@v1.2.0 --to production --actor alice
```

Internally this:

1. Checks the policy engine — allow or deny, written to the audit log.
2. For `agent` type going to `staging`/`production`: verifies a passing eval
   result exists. Blocks if not (unless `--force`).
3. Runs `UPDATE assets SET status = 'production', promoted_at = ..., promoted_by = 'alice'`.
4. Returns the updated record.

**Nothing else happens.** Your agent does not restart. Your prompt does not
reload. No traffic is rerouted. No container is restarted. The capture hooks
and runners are unchanged.

The status is **governance metadata** — a reliable signal your tooling can
read and act on. NovaFabric gives you the signal; your platform decides what
to do with it. (Policy checks and maker-checker approval are backed by OPA/Rego
and signed decision logs — see [Eval-Gated Promotion](#eval-gated-promotion).)

### How external tooling consumes the status

The pattern is: your CI/CD pipeline (or any script) reads the status from
NovaFabric and makes a deployment decision based on it.

```bash
# In a GitHub Actions deploy step or Argo CD sync hook:
STATUS=$(nova inspect my-agent@v1.2.0 | jq -r .status)

if [ "$STATUS" = "production" ]; then
  kubectl set image deployment/my-agent container=my-agent:v1.2.0
else
  echo "Asset not in production — deploy blocked (status=$STATUS)"
  exit 1
fi
```

NovaFabric doesn't own your deployment. It owns the evidence that the asset
is ready for deployment.

### Use cases

**Use case 1 — CI/CD gate for an LLM-powered feature**

You have a customer-facing summarisation agent. Your CI pipeline runs evals
on every PR. Only if evals pass and a team lead approves does the agent reach
`production`, which triggers your Helm chart to update the image tag.

```bash
# .github/workflows/deploy-agent.yml (simplified)
- run: nova eval summariser@${{ github.sha }} --suite regression
- run: nova promote direct summariser@${{ github.sha }} --to pending_approval --actor ci-bot
# ... human approves in dashboard or via CLI ...
- run: |
    nova promote direct summariser@${{ github.sha }} --to production --actor ${{ github.actor }}
    helm upgrade summariser ./chart --set image.tag=${{ github.sha }}
```

**Use case 2 — Safe prompt rollout without a full redeploy**

You update a system prompt template. You don't want to redeploy the entire
agent service — just swap the prompt version your agent reads at startup. The
registry tells your agent loader which version is `production`.

```python
# agent startup code
from novafabric.registry.service import get_asset

asset = get_asset("system-prompt-v2", version="latest")
if asset["status"] != "production":
    raise RuntimeError(f"Prompt not promoted to production (status={asset['status']})")

prompt_text = json.loads(asset["spec_json"])["spec"]["template"]
```

If the prompt has a bug and you archive it, the next agent restart fails fast
rather than silently loading a bad prompt.

**Use case 3 — Audit trail for a regulated environment**

Your company needs to demonstrate that no AI model reached production without
a human sign-off. Every promotion through `pending_approval` creates an
approval record (approver, timestamp, note) and an audit log entry. At audit
time:

```bash
nova inspect risk-scorer@v3.1.0 --format json | jq '{
  status,
  promoted_by,
  promoted_at,
  forced_promotion
}'
# → { "status": "production", "promoted_by": "alice", "promoted_at": "2026-03-15T10:22:01Z", "forced_promotion": false }
```

No manual tracking spreadsheet. The registry is the record.

**Use case 4 — Blast-radius control when an agent misbehaves**

An agent in production starts producing bad outputs after an upstream model
update. You archive it:

```bash
nova promote direct my-agent@v2.0.0 --to archived --actor on-call-eng
```

Your deployment hook detects the status change, rolls back to the previous
`production` version (`my-agent@v1.9.0`), and pages the team. The capsules
from the bad version are still fully intact for forensic replay — nothing
is deleted.

**Use case 5 — Multi-team handoff on an HPC cluster**

A research team develops and validates an agent on their workstation. When
they're ready for the ML engineering team to integrate it into the batch
pipeline, they promote it to `staging`. The ML team's Slurm job template only
launches agents whose status is `staging` or `production`:

```bash
# check_asset_status.sh — called from sbatch prolog
STATUS=$(NOVAFABRIC_DB_PATH=/shared/nova/registry.db nova inspect $AGENT_NAME@$AGENT_VERSION | jq -r .status)
if [[ "$STATUS" != "staging" && "$STATUS" != "production" ]]; then
  echo "PROLOG FAIL: $AGENT_NAME@$AGENT_VERSION is not ready (status=$STATUS)" >&2
  exit 1
fi
```

The registry on the shared filesystem (`/shared/nova/registry.db`) is the
handoff protocol between the two teams — no Slack messages, no wiki pages.

---

## Asset Registry

The registry is a local SQLite database at `~/.novafabric/registry.db`
(override: `NOVAFABRIC_DB_PATH`). It stores:

| Table | Contents |
|---|---|
| `assets` | Every registered asset — name, version, type, status, full spec JSON, git SHA, promotion history |
| `eval_results` | Pass/fail eval results per asset, per suite |
| `approvals` | Human sign-off records: approver, timestamp, note |

The registry is entirely local. There is no server, no credentials, and no
network dependency for local mode.

In server mode (`nova serve`, v0.7, experimental), the same data lives in
Postgres 16 and is accessible via the REST API at `/api/v1/assets`, guarded by
OIDC and an RBAC model (`reader < writer < admin`, plus an orthogonal
`auditor`). Server mode is strictly additive: **local mode never requires it.**

---

## Eval-Gated Promotion

`nova eval <agent@version>` discovers evaluation suites declared in the
agent spec (`spec.evals`) and resolves them via Python entry points in the
`novafabric.evals` group. Results are stored in the `eval_results` table.

Promotion to `staging` or `production` queries `eval_results` for a passing
result. If none exists, promotion is blocked unless `--force` is used. A
forced promotion is recorded as such (`forced_promotion = true`) and appears
in `nova inspect` output and the audit log — the escape hatch is visible,
not silent.

Standard eval suites ship OCI-pinned for reproducibility: **GAIA, SWE-bench,
AgentBench, MMLU, and a Smoke suite** (v0.9). Promotion can be Rego-gated to
block on a regression against a prior result.

---

## Evidence Bundle

An **Evidence Bundle** is the compliance primitive: a signed, self-contained ZIP
built by `nova export-evidence` that embeds

- the Run Capsule,
- a lineage subgraph,
- in-toto DSSE attestations,
- ed25519 signatures, and
- vendored JSON schemas.

Its defining property is **offline verifiability**: a recipient can verify it
with only `sha256sum` plus an ed25519 verifier — **no NovaFabric runtime
required**. That is what makes the capsule a durable, portable artifact you own
rather than a row in someone else's database. A capsule missing its
`redaction-proof.json` cannot be exported (see
[Secret Scanning and Redaction](#secret-scanning-and-redaction)).

**Shipped trust-layer surfaces (v0.4+):** ed25519-signed Evidence Bundles,
in-toto DSSE attestations, secret scanning with verifiable redaction proofs,
OPA/Rego policy gates with maker-checker promotion, and WORM storage adapters
(S3 Object Lock / Azure immutable blob / GCS Bucket Lock, with legal holds).

> **Experimental — NovaSeal signing core.** The in-process NovaSeal core
> shipped in v0.10+ (`experimental`): DSSE signing (ECDSA P-256), best-effort
> **RFC 3161 trusted timestamps**, and an append-only SQLite Merkle log,
> verified offline by `nova verify` (`signature_ok` / `timestamp_ok` /
> `log_integrity_ok`), plus the maker-checker `nova seal propose/approve/verify`
> chain (ADR-0059). Still **PLANNED / FUTURE DESIGN** per
> [ADR-0041](../design/adr/0041-novaseal-cryptographic-core-adoption.md): the
> dedicated network signing *service*, qualified timestamps, Sigstore-keyless
> signing as the default path, WORM-backed retention at the seal layer, and
> PQC/ML-DSA. The most stable portable proof today remains the ed25519 /
> in-toto DSSE Evidence Bundle described above.

---

## SDK Decorator

`@novafabric.agent` is an in-process alternative to `nova capture`. It wraps
an agent function with the same capture hooks used by the CLI, recording all
LLM calls into a capsule. Without `capsule_dir`, it emits OTel spans only —
this is the v0.1 observability mode.

---

## ULID

Run IDs and replay IDs are [Universally Unique Lexicographically Sortable
Identifiers](https://github.com/ulid/spec). They embed a millisecond timestamp
as the high bits, so capsule directories naturally sort chronologically without
a separate index.

---

## Summary and next steps

You now have the vocabulary NovaFabric is built on:

- **Run Capsule** — the ULID-named directory that is the source of truth for one
  execution, written on success and failure.
- **Capture** — zero-code-change instrumentation via a `sitecustomize.py`
  loader, per-SDK hooks, and a wire-level safety net down to `urllib3`.
- **Replay** — four honest modes (`forensic`, `mocked`, `semantic`, `exact`),
  each with a clear promise and each producing a diffable capsule.
- **Diff** — field-by-field structural comparison, wireable as a CI regression
  gate with `--assert-no-regressions`.
- **Lineage** — a rebuildable SQLite provenance graph answering `provenance`,
  `blast-radius`, `replay-chain`, and `time-travel`, with OpenLineage emission.
- **Registry + lifecycle** — versioned assets with a six-state lifecycle where
  promotion is governance metadata only.
- **Evidence Bundle** — offline-verifiable signed export; the NovaSeal signing
  core (DSSE + RFC 3161 timestamps + Merkle log, `nova verify`) is
  **experimental**, while the dedicated signing service and seal-layer WORM
  retention remain **PLANNED**.

Where to go next:

- **CLI reference** — every command and flag: [`docs/cli-reference.md`](cli-reference.md)
- **Writing a hook plugin** — [`docs/integrations/writing-a-hook-plugin.md`](integrations/writing-a-hook-plugin.md)
- **Model-call field reference** — [`design/spec/model-call-v0.md`](../design/spec/model-call-v0.md)
- **Runner design and anti-patterns** — ADR-0025
- **Trust-layer and sealing design intent** — [ADR-0041](../design/adr/0041-novaseal-cryptographic-core-adoption.md)
