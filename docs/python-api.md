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
| [Replay](#replay) | `novafabric.replay` | `nova replay` |
| [Diff](#diff) | `novafabric.diff` | `nova diff` |
| [Lineage](#lineage) | `novafabric.lineage` | `nova lineage` |
| [Asset Registry](#asset-registry) | `novafabric.registry.service` | `nova register` / `nova list` / `nova promote` |
| [Spec validation](#spec-validation) | `novafabric.spec` | `nova validate-spec` |
| [Report generation](#report-generation) | `novafabric.report.generator` | `nova report` |
| [NovaSeal signing](#novaseal-signing) | `novafabric.trust.novaseal` | `nova seal` / `nova verify` |
| [Utility](#utility) | `novafabric.capture` | — |

---

## SDK decorator

```python
from novafabric.sdk.agent import agent
```

### `@agent(name, version, capsule_dir=None)`

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
