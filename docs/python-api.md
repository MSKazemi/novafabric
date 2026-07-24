# Python API Reference

NovaFabric is a CLI-first toolkit, but every capability the `nova` command
exposes is also reachable as a typed Python API. This reference documents the
public symbols for programmatic **capture, replay, diff, lineage, registry,
spec validation, reporting, and signing** — so you can embed NovaFabric in a
test harness, a CI script, an internal tool, or a notebook without shelling out.

## What you will learn

- How to capture a run in-process with the `@agent` decorator, or capture an
  arbitrary command with `CaptureOrchestrator`.
- How to replay a capsule in any of the four honest modes (`forensic`,
  `mocked`, `semantic`, `exact`) and read back a structured `ReplayResult`.
- How to structurally diff two capsules and serialise the report for a CI gate.
- How to build and query the lineage graph (`provenance`, `blast_radius`,
  `replay_chain`).
- How to register, list, fetch, and promote assets through the six-state
  lifecycle.
- How to validate asset specs and generate registry reports.
- How to sign, timestamp, and verify capsules with the NovaSeal signing core.

Everything below maps one-to-one onto a `nova` subcommand; where a CLI
equivalent exists it is noted so you can cross-check behaviour with
`nova <command> --help`.

> **Stability.** All public symbols listed here are importable today and covered
> by tests. Most v0.x surfaces carry **experimental** maturity: they work, but
> interfaces may change before the v1.0 schema freeze. The Run Capsule and
> Evidence Bundle on-disk formats are **not yet frozen** — expect additive,
> backward-compatible schema changes until v1.0.

---

## Table of contents

| Section | Import root | CLI equivalent |
|---|---|---|
| [SDK decorator](#sdk-decorator) | `novafabric.sdk.agent` | — (in-process) |
| [Capture](#capture) | `novafabric.capture.orchestrator` | `nova capture` |
| [Extended event recording](#extended-event-recording-experimental) | `novafabric.capture.record` | — (in-process) |
| [Replay](#replay) | `novafabric.replay` | `nova replay` |
| [Diff](#diff) | `novafabric.diff` | `nova diff` |
| [Lineage](#lineage) | `novafabric.lineage` | `nova lineage` |
| [Asset Registry](#asset-registry) | `novafabric.registry.service` | `nova register` / `nova list` / `nova promote` |
| [Spec validation](#spec-validation) | `novafabric.spec` | `nova validate-spec` |
| [Report generation](#report-generation) | `novafabric.report.generator` | `nova report` |
| [NovaSeal signing](#novaseal-signing) | `novafabric.trust.novaseal` | `nova seal` / `nova verify` |
| [Score submission](#score-submission) | `novafabric.scores` | `nova score submit` |
| [REST client](#rest-client) | `novafabric.client` | — (server mode) |
| [Utility](#utility) | `novafabric.capture` | — |

---

## SDK decorator

```python
from novafabric.sdk.agent import agent
```

### `@agent(name, version, capsule_dir=None, deployment_environment=None, variant=None, session_id=None, session_sequence=None)`

Wraps an agent function with capture hooks. LLM calls made inside the
function are recorded into a capsule. This is the in-process alternative to
`nova capture` — use it when you cannot (or do not want to) wrap the whole
process, e.g. a single function inside a larger long-lived service.

**Parameters**

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | Asset name (matches a registered asset, if any) |
| `version` | `str` | Asset version (semver) |
| `capsule_dir` | `Path \| str \| None` | Directory to write the capsule. If `None`, OTel spans only — no capsule is written. |
| `deployment_environment` | `str \| None` | **Experimental (ADR-0126).** Delivery-lifecycle tag for the run (e.g. `production`, `staging`), recorded verbatim as the additive optional `deployment_environment` manifest field with `environment_source: sdk-arg`. The `nova capture --environment` flag and the `NOVAFABRIC_ENVIRONMENT` env var take precedence. |
| `variant` | `Mapping[str, Any] \| None` | **Experimental (ADR-0116, record-only).** Which A/B experiment/variant an *external* allocator had active: a mapping with `experiment_id`, `variant_id`, `assignment_source` (plus optional `variant_label`, `assigned_at`, `extensions`), copied verbatim into the capsule's optional `variant` block. NovaFabric never assigns variants; an incomplete explicit block raises before the workload runs. `NOVAFABRIC_VARIANT*` env vars take precedence. |
| `session_id` | `str \| None` | **Experimental (ADR-0122, record-only).** ULID of the session this run is one turn of (create with `nova session new`); recorded as an additive optional back-reference — the `session.json` manifest stays authoritative. `NOVAFABRIC_SESSION_ID` takes precedence. |
| `session_sequence` | `int \| None` | **Experimental (ADR-0122).** The run's turn number within the session; requires `session_id`. `NOVAFABRIC_SESSION_SEQUENCE` takes precedence. |

All four tagging parameters are optional and additive: leave them unset and the
capsule is byte-identical to previous releases. Invalid explicit values raise
**before** the wrapped function runs; ambient env-var values that are invalid
warn and are ignored rather than blocking the workload.

**Returns** — the decorated function's return value unchanged.

**Example**

```python
from novafabric.sdk.agent import agent
import openai

client = openai.OpenAI()

@agent(name="summariser", version="0.2.0", capsule_dir="capsules/")
def summarise(text: str) -> str:
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": f"Summarise: {text}"}],
    )
    return response.choices[0].message.content

result = summarise("Long document text here...")
```

Every call to `summarise()` writes a new capsule under `capsules/<ulid>/`.

**Behaviour**

- Installs OpenAI + Anthropic hooks before calling the function.
- Removes hooks after the function returns (including on exception).
- On exception: writes a capsule with `status: failure` and re-raises.
- Thread-safe: each call writes to its own capsule directory.

> The capsule this produces is identical in shape to one produced by
> `nova capture` — you can `nova validate`, `nova replay`, and `nova diff` it
> exactly the same way.

---

## Capture

```python
from novafabric.capture.orchestrator import CaptureOrchestrator, CaptureResult
```

`CaptureOrchestrator` is the programmatic equivalent of `nova capture <cmd>`.
It captures a subprocess as a Run Capsule with **no changes to the target
program** — hooks are injected via `sitecustomize.py` over `PYTHONPATH`. A
capsule is written on both success and failure (a failed run sets
`status: failure` and records an `error` block).

### `CaptureOrchestrator(base_dir=None)`

| Parameter | Type | Default |
|---|---|---|
| `base_dir` | `Path \| None` | `.novafabric/runs/` |

### `CaptureOrchestrator.run(command) → CaptureResult`

Captures a command execution as a capsule.

| Parameter | Type | Description |
|---|---|---|
| `command` | `list[str]` | Command + arguments |

```python
from pathlib import Path
from novafabric.capture.orchestrator import CaptureOrchestrator

result = CaptureOrchestrator(base_dir=Path(".novafabric/runs")).run(
    ["python", "agent.py", "--dataset", "data.csv"]
)
print(result.run_id)      # ULID string
print(result.capsule_dir) # Path to capsule directory
print(result.exit_code)   # int
```

### `CaptureResult`

```python
@dataclass
class CaptureResult:
    run_id: str
    capsule_dir: Path
    exit_code: int
```

---

## Extended event recording (experimental)

```python
from novafabric.capture import record
```

**Status: experimental** (ADR-0209 P1; the API surface may change until it
has survived one release unchanged). The stable public façade for emitting
the extended capture events of ADR-0082 — guardrails, evaluators, state
transitions, memory operations, rerankers, vector retrievals, file events —
into whatever run is currently being captured (any capture mode: `nova
capture`, adapters, `@agent`, in-process hooks).

Contract:

- **Outside a capture run every call is a silent no-op** returning `None` —
  you can instrument library code unconditionally and it stays inert until
  a capture is active. `record.active()` tells you which case you are in.
- Inside a run, each call appends one event to the run's dedicated JSONL
  stream (fail-open, thread-safe; `run_id`/`capsule_id` are injected by the
  run). All of these streams are covered by the capsule secret scanner, so
  free-text payloads are redacted at finalize like everything else.
- The façade passes your payload fields through at every capture level (you
  own your data). NovaFabric's own default-path wirings only attach payloads
  (state dicts, document text) at `forensic`/`air_gapped` capture level.

```python
record.active() -> bool

record.file_event(operation, path, *, size_bytes=None, success=True,
                  error=None, agent_id=None)
record.state_transition(step_index, state_digest_before, state_digest_after, *,
                        agent_id=None, state_before=None, state_after=None)
record.memory_operation(operation, memory_key, *, relevance_score=None,
                        freshness_seconds=None, agent_id=None, value=None,
                        origin_run_id=None, origin_memory_key=None,
                        origin_timestamp_utc=None)
record.guardrail(guardrail_name, outcome, *, category=None, score=None,
                 agent_id=None, details=None)
record.evaluator(evaluator_name, *, score=None, label=None, passed=None,
                 dataset_id=None, agent_id=None, rationale=None)
record.reranker(reranker_model, *, input_count=None, output_count=None,
                documents=None, agent_id=None)
record.vector_retrieval(vector_store, *, phase="completed", operation="query",
                        collection=None, top_k=None, returned_count=None,
                        duration_ms=None, error=None, documents=None,
                        agent_id=None)
```

`network_event` and `human_approval` are deliberately **not** exposed: the
wire-level hooks and `nova seal-propose` own those streams, and a second
producer would invite double-recording.

### `record.wrap_retriever(fn, *, vector_store, collection=None, operation="query")`

Wraps any sync callable retriever; emits `VectorRetrievalStarted` before the
call, `…Completed` (with `returned_count`, `duration_ms`) on return,
`…Failed` (with `error`) on exception — then re-raises. Document payloads
are recorded only at `forensic`/`air_gapped` capture level.

```python
# LangChain retriever
search = record.wrap_retriever(retriever.invoke,
                               vector_store="qdrant", collection="docs")
docs = search("what changed in v0.63?")

# DSPy
retrieve = dspy.Retrieve(k=8)
retrieve.forward = record.wrap_retriever(retrieve.forward,
                                         vector_store="chroma")
```

An async variant is future design. Default-path wirings that call this API
automatically (OpenAI Agents guardrail spans, LangGraph state transitions)
are documented in the capture tutorial; file/memory/evaluator/reranker
events have **no auto-capture** — they record only when you call the façade.

---

## Replay

```python
from novafabric.replay import ReplayEngine, ReplayFlags
from novafabric.replay._result import ReplayResult
```

Replay re-executes or inspects a capsule with external calls controlled. A
replay is itself a new capsule, so you can `diff` a replay against its source.

### `ReplayFlags`

Controls replay mode and safety settings.

```python
@dataclass
class ReplayFlags:
    mode: Literal["mocked", "forensic", "semantic", "exact", "intervention"] = "mocked"
    dry_run: bool = False
    allow_readonly: bool = False
    allow_mutating: bool = False
    allow_external_side_effects: bool = False
    allow_unknown_mutation: bool = False
    output_dir: Path | None = None
```

**Modes**

| Mode | What it does | Typical use |
|---|---|---|
| `forensic` | Read-only inspection. No subprocess, no network. | Audit / post-incident |
| `mocked` | Re-spawns the command; LLM calls served from the capsule cache; tool calls gated by a safety ladder (`allow_*` flags). | CI / regression |
| `semantic` | Re-executes and judges *meaning* not tokens; returns a `similarity_score` in `0.0–1.0`. | Drifting remote LLMs |
| `exact` | Eligibility check for byte-exact replay; returns `exact_eligible`. Requires a deterministic env and per-call seed. | Local / on-prem / compliance |
| `intervention` | Re-executes with a spec-driven intervention overlay (experimental, ADR-0086). | What-if / counterfactual analysis |

> NovaFabric explicitly does **not** claim byte-exact replay of remote LLM
> calls. `exact` mode reports *eligibility*, not a guarantee.

The `allow_*` flags form a safety ladder for `mocked`/`intervention` tool
execution — each opts into a broader class of side effect
(`allow_readonly` < `allow_mutating` < `allow_external_side_effects` <
`allow_unknown_mutation`). Leave them `False` for a fully sandboxed replay.

### `ReplayEngine(capsule_dir, flags, base_dir=None)`

| Parameter | Type |
|---|---|
| `capsule_dir` | `Path` |
| `flags` | `ReplayFlags` |
| `base_dir` | `Path \| None` — defaults to `.novafabric/replays/` |

### `ReplayEngine.run() → ReplayResult`

Replays the capsule according to the flags. Writes `replay_result.yaml` in the
output directory.

```python
from pathlib import Path
from novafabric.replay import ReplayEngine, ReplayFlags

flags = ReplayFlags(mode="forensic")
engine = ReplayEngine(
    capsule_dir=Path(".novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX"),
    flags=flags,
)
result = engine.run()
print(result.status)              # "success" | "failure" | "aborted" | "dry_run"
print(result.model_calls_mocked)  # int
print(result.env_warnings)        # list of {field, original, current}
```

**Example — semantic replay against a drifting remote LLM**

```python
flags = ReplayFlags(mode="semantic")
result = ReplayEngine(capsule_dir=cap, flags=flags).run()
if result.similarity_score is not None and result.similarity_score < 0.9:
    print(f"semantic drift detected: {result.similarity_score:.3f}")
```

**Example — exact-replay eligibility gate for a compliance run**

```python
flags = ReplayFlags(mode="exact")
result = ReplayEngine(capsule_dir=cap, flags=flags).run()
if not result.exact_eligible:
    print("not byte-exact reproducible:", result.exact_reasons)
```

### `ReplayResult`

```python
@dataclass
class ReplayResult:
    replay_id: str
    replay_of_run_id: str
    mode: str
    status: str
    start_time: str
    end_time: str
    duration_ms: int
    policy_flags_used: list[str]
    env_warnings: list[dict[str, str]]
    model_calls_mocked: int = 0
    tool_calls_mocked: int = 0
    exit_code: int | None = None
    error: dict | None = None
    # semantic mode
    similarity_score: float | None = None   # 0.0–1.0; set when mode="semantic"
    matched_run_id: str | None = None
    # intervention mode (experimental, ADR-0086)
    intervention: dict | None = None
    # exact mode
    exact_eligible: bool | None = None      # set when mode="exact"
    exact_hash_count: int | None = None
    exact_reasons: list[str] | None = None

    def as_dict(self) -> dict: ...
```

---

## Diff

```python
from novafabric.diff import DiffEngine, DiffReport
```

`DiffEngine` structurally compares two capsules — aligning their model calls,
tool calls, environment, and outputs — and reports what changed, was added, or
was removed. This is the engine behind `nova diff --assert-no-regressions`.

### `DiffEngine.compare(capsule_a, capsule_b) → DiffReport`

```python
from pathlib import Path
from novafabric.diff import DiffEngine

report = DiffEngine().compare(
    capsule_a=Path(".novafabric/runs/01HX.../"),
    capsule_b=Path(".novafabric/runs/01HY.../"),
)

print(report.changed_count)  # int
print(report.added_count)    # int
print(report.removed_count)  # int
print(report.as_dict())      # serialisable dict
```

**Example — use the diff as a CI regression gate**

```python
import sys
report = DiffEngine().compare(baseline_capsule, candidate_capsule)
if report.changed_count or report.removed_count:
    print(report.as_dict())
    sys.exit(1)   # fail the build on regression
```

> The `nova diff --assert-no-regressions` CLI applies the same rule and exits
> `1` on regression — reach for it directly in shell-based pipelines.

### `DiffReport`

```python
@dataclass
class DiffReport:
    run_a_id: str
    run_b_id: str
    env_changes: list[dict]
    model_call_pairs: list[dict]
    tool_call_pairs: list[dict]
    output_changes: list[dict]

    @property
    def changed_count(self) -> int: ...
    @property
    def added_count(self) -> int: ...
    @property
    def removed_count(self) -> int: ...
    def as_dict(self) -> dict: ...
    def write(self, output_path: Path) -> None: ...
```

### Format helpers

```python
from novafabric.diff._format import format_text, format_json, format_github_annotations

text = format_text(report)                    # human-readable diff
json_str = format_json(report)                # machine-readable
annotations = format_github_annotations(report)  # GitHub Actions annotations
```

---

## Lineage

```python
from novafabric.lineage import (
    LineageWriter,
    LineageStore,
    import_capsule_dir,
    index_capsule_lineage,
    ImportResult,
)
```

Lineage is a directed provenance graph in SQLite — a rebuildable cache derived
from each capsule's `lineage.jsonl`. Use `LineageWriter` to infer edges,
`import_capsule_dir` to index them, and `LineageStore` to query the graph.

### `LineageWriter(capsule_dir, run_id, version="0.4.0")`

Infers lineage edges from a capsule and writes `lineage.jsonl`.

```python
from pathlib import Path
from novafabric.lineage import LineageWriter

writer = LineageWriter(
    capsule_dir=Path(".novafabric/runs/01HX.../"),
    run_id="01HXAY7M5JZ8R7K4P9DPBYK2WX",
)
edges = writer.infer()      # list[LineageEdge]
path = writer.write(edges)  # writes lineage.jsonl, returns Path
```

### `LineageStore(db_path=None)`

SQLite-backed graph store. Default: `~/.novafabric/registry.db`.

```python
from novafabric.lineage import LineageStore

store = LineageStore()

# What did this run depend on? (ancestors)
ancestors = store.provenance(
    ref="run:01HXAY7M5JZ8R7K4P9DPBYK2WX",
    kind=None,
    depth=3,
)

# What runs consume this asset? (descendants / impact)
dependents = store.blast_radius(
    ref="registry:my-dataset@1.0.0",
    kind=None,
    depth=5,
)

# Replay ancestry
chain = store.replay_chain(run_id="01HXREPLAY...")
```

Each result item is a dict: `{"kind": "run|asset|artifact", "ref": "..."}`.
A `time_travel` query is also available for point-in-time graph reconstruction.

### `import_capsule_dir(capsule_dir, db_path=None) → ImportResult`

Index a capsule's `lineage.jsonl` into the graph store.

```python
from pathlib import Path
from novafabric.lineage import import_capsule_dir

result = import_capsule_dir(Path(".novafabric/runs/01HX.../"))
print(result.edges_indexed)  # int
print(result.nodes_indexed)  # int
```

### `ImportResult`

```python
@dataclass
class ImportResult:
    capsule_run_id: str
    edges_indexed: int
    nodes_indexed: int
    skipped: bool
    warning: str | None
```

> The lineage store also emits **OpenLineage 2.0.2** START/COMPLETE/FAIL events
> to a configured backend (Marquez, Atlan, OpenMetadata). See the CLI reference
> for `nova lineage` emission configuration.

### Graph analytics (experimental — ADR-0212..0215)

```python
from novafabric.lineage._store import LineageStore
from novafabric.lineage.analytics import (
    build_insights_report,            # ADR-0215 synthesized report
    compute_graph_metrics,            # ADR-0212 hubs / articulation points
    rank_root_causes,                 # ADR-0213 upstream suspect ranking
    to_cypher, to_gexf, to_graphml,   # ADR-0214 exports
)

store = LineageStore()
metrics = compute_graph_metrics(store, top_n=10)
report = build_insights_report(store)
graphml = to_graphml(store.all_nodes(), store.all_edges())
```

Read-only and deterministic; oversize graphs raise `LineageGraphTooLargeError`
instead of truncating. See the CLI reference for the command equivalents
(`nova lineage metrics|root-cause|export-graph`, `nova insights`).

---

## Asset Registry

```python
from novafabric.registry.service import (
    register_asset,
    list_assets,
    get_asset,
    promote_asset,
)
from novafabric.spec.models import AssetStatus
```

The registry is a local SQLite store (`~/.novafabric/registry.db`) of versioned
assets addressed as `name@version`. Promotion is **governance metadata only** —
it updates one DB record and does not deploy, restart, or redeploy anything.

### `register_asset(spec, spec_path, db_path=None) → dict`

```python
from pathlib import Path
from novafabric.spec.validator import validate_spec
from novafabric.registry.service import register_asset

spec = validate_spec(Path("my-model.yaml"))
asset = register_asset(spec, spec_path=Path("my-model.yaml"))
print(asset["id"])   # UUID
```

Raises `DuplicateAssetError` if `name@version` already exists.

### `list_assets(asset_type=None, status=None, db_path=None) → list[dict]`

```python
from novafabric.registry.service import list_assets

agents = list_assets(asset_type="agent", status="production")
for a in agents:
    print(a["name"], a["version"], a["status"])
```

### `get_asset(name, version=None, db_path=None) → dict`

```python
from novafabric.registry.service import get_asset

asset = get_asset("my-model", "1.0.0")
# version=None (or "latest") returns the latest registered version
```

Raises `AssetNotFoundError` if not found.

### `promote_asset(name, version, to_status, actor, force=False, db_path=None) → dict`

Advances an asset through the six-state lifecycle. The `to_status` argument is
an `AssetStatus` enum member.

```python
from novafabric.registry.service import promote_asset
from novafabric.spec.models import AssetStatus

asset = promote_asset(
    name="my-model",
    version="1.0.0",
    to_status=AssetStatus.staging,
    actor="ci-bot",
)
```

Raises:
- `InvalidLifecycleTransitionError` — the transition is not permitted by the
  lifecycle state machine.
- `PromotionBlockedError` — the eval gate is not satisfied (agents only).

> **Lifecycle:** `development → validated → pending_approval → staging →
> production → archived`. Promotion of `agent` assets is eval-gated; the OPA/Rego
> policy engine is consulted before the eval-score gate so decisions are always
> logged. See `nova promote --help` for the maker-checker CLI flow.

---

## Spec validation

```python
from novafabric.spec.validator import validate_spec, SpecValidationError
from novafabric.spec.models import AssetType, AssetStatus, BaseAssetSpec
```

### `validate_spec(yaml_path) → BaseAssetSpec`

```python
from pathlib import Path
from novafabric.spec.validator import validate_spec

spec = validate_spec(Path("my-agent.yaml"))
print(spec.name, spec.version, spec.asset_type)
```

Raises `SpecValidationError` with a Pydantic error list and a hint string on
validation failure.

### Asset type models

```python
from novafabric.spec.models import (
    AssetType,    # Enum: model agent prompt tool dataset evaluation deployment
    AssetStatus,  # Enum: development validated pending_approval staging production archived
    BaseAssetSpec,
    ModelSpec,
    AgentSpec,
    PromptSpec,
    ToolSpec,
    DatasetSpec,
    EvaluationSpec,
    DeploymentSpec,
)
```

`AssetType` has seven members (one per asset type). `AssetStatus` has the six
lifecycle states listed above.

---

## Report generation

```python
from novafabric.report.generator import generate_report

markdown = generate_report(format_="markdown")
json_str = generate_report(format_="json")
```

`generate_report(format_="markdown", db_path=None)` renders the current registry
state. This is the engine behind `nova report`.

---

## NovaSeal signing

```python
from novafabric.trust.novaseal import NovaSeal, KeyConfig
```

NovaSeal is the cryptographic signing core for capsule manifests: a **DSSE
envelope (ECDSA P-256 / SHA-256)**, an optional **RFC 3161 trusted timestamp**,
and an entry appended to a **SQLite-backed append-only Merkle log**. The
`local` key profile is the default; cloud-KMS profiles (`aws_kms`, `azure_kv`,
`gcp_kms`) are also supported by the config loader.

> **Maturity — experimental.** NovaSeal is present and covered by tests, but its
> interfaces (like the rest of the v0.x surface) may change before the v1.0
> schema freeze. Timestamping is best-effort: if the TSA is unreachable, the
> capsule is still sealed, without a timestamp.

### Sign a capsule manifest

```python
from novafabric.trust.novaseal import NovaSeal, KeyConfig

# Instantiate with a local ECDSA P-256 key
config = KeyConfig(
    profile="local",
    key_path="~/.novafabric/seal.key",
    cert_path="~/.novafabric/seal.crt",
)
seal = NovaSeal(
    config=config,
    tsa_url="https://freetsa.org/tsr",  # "" to skip timestamping
    db_path="~/.novafabric/novaseal-merkle.db",
)

# Sign a capsule manifest (dict)
bundle = seal.seal(capsule_manifest)
# bundle.dsse_envelope  → bytes  (DSSE JSON)
# bundle.tsr            → bytes  (RFC 3161 DER; b"" if TSA skipped/unavailable)
# bundle.log_entry      → dict   (leaf_index, root_hash, tree_size, …)
# bundle.capsule_id     → str    (SHA-256 hex of the signed payload)
```

`seal()` accepts an optional `intent` (a `SigningIntent`, default
`SigningIntent.AUTHORED`) recorded in the DSSE envelope to support FDA 21 CFR
§11.50 signing-intent semantics. Pass `intent=None` to omit it.

### Verify an existing `.seal/` directory

```python
result = seal.verify(
    capsule_id=bundle.capsule_id,
    seal_dir=".novafabric/runs/01HX.../.seal",
)
# result.valid            → bool
# result.signature_ok     → bool
# result.timestamp_ok     → bool
# result.log_integrity_ok → bool
# result.ca_chain_ok      → bool
# result.errors           → list[str]
assert result.valid, str(result)
```

`verify()` reads `manifest.dsse`, an optional `manifest.dsse.tsr`, and
`log-entry.json` from the `.seal/` directory. An **absent or empty** TSR is
treated as a deliberately skipped timestamp (`timestamp_ok=True`); a present but
invalid TSR fails verification.

### Rotate the signing key

```python
from novafabric.trust.novaseal import KeyConfig

new_config = KeyConfig(profile="local", key_path="new.key", cert_path="new.crt")
receipt = seal.rotate_key(new_config)
# receipt.old_key_fingerprint, receipt.new_key_fingerprint, receipt.rotation_log_entry
```

Rotation appends a `key_rotation` event to the Merkle log and switches
subsequent seals to the new key.

### Load a signing profile from config

Configuration can be discovered from `~/.novafabric/novaseal.yaml` or the
`NOVAFABRIC_SEAL_CONFIG` environment variable:

```python
from novafabric.trust.novaseal.config import load_signing_profile
from novafabric.trust.novaseal import NovaSeal, KeyConfig

profile = load_signing_profile()  # None if NovaSeal is not configured
if profile:
    seal = NovaSeal(
        config=KeyConfig(
            profile=profile.profile,
            key_path=str(profile.key_path),
            cert_path=str(profile.cert_path),
        ),
        tsa_url=profile.tsa_url,
        db_path=str(profile.merkle_db),
    )
```

> The dedicated, hardened **NovaSeal signing *service*** (network service,
> qualified timestamps, Sigstore-keyless mode) described in ADR-0041 is a
> **planned** capability. The `novafabric.trust.novaseal` library documented here
> is the in-process signing core.

---

## Score submission

**Maturity: experimental (ADR-0119).** The documented, supported way for an *external*
evaluation pipeline — a CI job, a third-party LLM-as-judge, a human tool — to submit an
already-computed score into a capsule's append-only `scores.jsonl`. Works offline, no
server, no model call.

### `novafabric.scores.submit(capsule_dir, *, name, value, value_type, evaluator_id, subject, ...) → SubmitResult`

```python
from novafabric.scores import submit

result = submit(
    "./capsules/run-01HX...",
    name="answer_correct", value=True, value_type="boolean",
    source="judge", evaluator_id="ci://acme/repo#judge@v3",
    subject="sha256:...",            # must exist in the target capsule
    eval_card_digest="sha256:...",   # required — the eval card reproducibility key
    score_id=None,                   # supply a client-minted ULID for idempotency
    supersedes=None,                 # prior score_id being corrected (append-only)
)
result.score.score_id     # the appended (or replayed) evidence-grade Score
result.idempotent_replay  # True → score_id already existed; no second line appended
result.config_bound       # True → an ADR-0117 ScoreConfig validated the value
```

Remaining keyword-only parameters: `source` (`"code"` default; `human` /
`heuristic` / `judge`), `subject_kind` (`"span"` default), `run_id`,
`significance` (an ADR-0080 `SignificanceBlock`), and `db_path` (registry
override for score-config lookup).

Validation is **fail-closed** — on any rejection nothing is written, and a named
exception is raised (`SubmissionInvalidError`, `CapsuleNotFoundError`,
`SubjectNotFoundError`, `SupersedesNotFoundError`, `IdempotencyConflictError`,
`ScoreConfigViolation` — all importable from `novafabric.scores`). Corrections never
edit a prior record: submit a *new* score whose `supersedes` names the prior
`score_id`; both lines stay in the log. CLI equivalent: `nova score submit`.

---

## REST client

**Maturity: experimental (ADR-0202 P1, shipped 2026-07-24).** A typed, sync
httpx client for the multi-tenant `nova server` REST API (`/v0`,
`api/openapi.yaml`). This is **server-mode tooling**: every local-first
feature above works without it, and importing it performs no I/O — the client
sends no request other than the ones you invoke (no telemetry, no update
checks). It mirrors the TypeScript SDK's contract (ADR-0194) and adds one
operation TS does not have yet: **capsule upload**.

### `novafabric.client.NovaFabricClient(base_url, *, api_key=None, token=None, timeout=None, retries=None, transport=None)`

```python
from pathlib import Path

from novafabric.capture import new_ulid
from novafabric.client import NovaFabricClient

with NovaFabricClient(
    "https://nova.example.com/v0",     # no default URL — include the /v0 prefix
    api_key="nvfk_...",                # or token="..." / token=lambda: fresh_jwt()
) as nc:
    print(nc.health().data.version)    # server version (skew diagnostics)

    # Capsules: upload a directory (packed to a deterministic ZIP), a .zip
    # path, or raw ZIP bytes; then read back.
    nc.upload_capsule(Path("./capsules/run-01HX..."))
    detail = nc.get_capsule("run-01HX...").data

    # Cursor pagination: one page, or a lazy iterator over all pages.
    page = nc.list_capsules(limit=100).data          # .items / .next_cursor / .total
    for capsule in nc.iter_capsules():               # one HTTP call per page
        print(capsule.run_id, capsule.status)

    # Assets mirror the same surface: get_asset / list_assets / iter_assets.

    # External scores (ADR-0119): 201 → stored record; a 200 idempotent
    # replay carries no body, so .data is None — inspect .meta.status.
    result = nc.submit_score("run-01HX...", {
        "name": "answer_correct", "value": True, "value_type": "boolean",
        "source": "judge", "evaluator_id": "ci://acme/repo#judge@v3",
        "subject": "sha256:...", "eval_card_digest": "sha256:...",
        "score_id": new_ulid(),        # client-minted ULID → safe to re-send
    })
```

Contract points (normative spec: `design/spec/python-client-v0.md`):

- **Config resolution** — constructor argument → environment variable
  (`NOVAFABRIC_SERVER_URL`, `NOVAFABRIC_API_KEY`, `NOVAFABRIC_TOKEN`) → error.
  A missing base URL raises `NovaFabricConfigError` at construction; so does
  passing both `api_key` and `token`. If both credential env vars are set,
  `NOVAFABRIC_API_KEY` wins with a `UserWarning`.
- **Auth** — exactly one bearer header. `api_key` takes an `nvfk_` key
  (ADR-0193); `token` takes an OIDC/offline/local token, as a string or a
  zero-arg callable your application refreshes. No OIDC flow is performed.
- **Results** — every call returns `ApiResult(data, meta)`; `meta` carries
  `status`, RFC 9745 `Deprecation` / RFC 8594 `Sunset` (also emitted once per
  endpoint as a `DeprecationWarning`; reset with
  `novafabric.client.reset_deprecation_warnings()`), the
  `X-NovaFabric-Quota-Warning` value, and a request id when the server sends
  one. Models are Pydantic v2 with `extra="allow"` — additive server fields
  never break an older client.
- **Errors** — every non-2xx raises a typed subclass of `NovaFabricAPIError`
  (`AuthenticationError` 401, `AuthorizationError` 403, `NotFoundError` 404,
  `ConflictError` 409, `PreconditionFailedError` 412, `ValidationFailedError`
  422, `RateLimitedError` 429 with `.retry_after`, `ServerError` 5xx) carrying
  the envelope `code` verbatim, with an `unknown_error` fallback for
  non-envelope bodies. Transport failures raise `NovaFabricTransportError`
  (`NovaFabricTimeout` for timeouts) — never an API error.
- **Retries** — bounded, **GET-only** (no POST is ever auto-retried), on
  connect errors and 429/502/503/504, honoring `Retry-After` (capped 30 s);
  tune or disable via `RetryConfig` (`max_attempts=1` disables). Default
  timeouts: connect 5 s, read/write 30 s, pool 5 s.
- **Pagination** — cursors are opaque strings; `iter_*` fetches lazily, one
  page ahead at most, and terminates on `next_cursor: null`.
- **Testing seam** — pass any `httpx.BaseTransport` as `transport=` (e.g.
  `httpx.MockTransport`) for hermetic tests.

An async twin (`AsyncNovaFabricClient`) and artifact streaming are **planned**
(ADR-0202 P2); SSE and an OTLP endpoint helper are **future design** (P3).

---

## Utility

```python
from novafabric.capture import new_ulid, new_span_id

run_id = new_ulid()      # str — ULID (26 chars, time-sortable)
span_id = new_span_id()  # str — 16-char hex
```

---

## Summary and next steps

| You want to… | Use | CLI equivalent |
|---|---|---|
| Capture one function in-process | `@agent` decorator | — |
| Capture an arbitrary command | `CaptureOrchestrator.run` | `nova capture` |
| Re-run or inspect a capsule | `ReplayEngine` + `ReplayFlags` | `nova replay` |
| Compare two runs / gate CI | `DiffEngine.compare` | `nova diff --assert-no-regressions` |
| Trace provenance / blast radius | `LineageStore` | `nova lineage` |
| Register / promote assets | `registry.service` | `nova register` / `nova promote` |
| Validate a spec | `validate_spec` | `nova validate-spec` |
| Sign / verify a capsule | `NovaSeal` | `nova seal` / `nova verify` |
| Talk to a NovaFabric server from Python | `NovaFabricClient` (experimental) | — |

**Where to go next**

- **CLI reference** (`docs/cli-reference.md`) — every `nova` subcommand, flag,
  and default. Each Python API above has a CLI twin; when in doubt, run
  `nova <command> --help`.
- **Developer guide** (`docs/developer-guide.md`) — how to add asset types, CLI
  commands, report formats, and capture hooks (the `novafabric.hooks`
  entry-point group).
- **The five primitives** — Asset Registry, Run Capsule, Replay, Lineage, and
  Evidence Bundle. Each has a public JSON Schema; the capsule is the source of
  truth and the registry/lineage graph are rebuildable indexes derived from it.
