# Python API Reference

NovaFabric exposes a Python API for programmatic capture, replay, diff,
lineage, and registry operations. All public symbols listed here are stable
within the `0.x` series unless marked otherwise.

> **Format stability:** Run capsule and evidence bundle schemas are not yet
> frozen. Expect schema changes until v1.0.

---

## SDK decorator

```python
from novafabric.sdk.agent import agent
```

### `@agent(name, version, capsule_dir=None)`

Wraps an agent function with capture hooks. LLM calls made inside the
function are recorded into a capsule.

**Parameters**

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | Asset name (matches registered asset, if any) |
| `version` | `str` | Asset version (semver) |
| `capsule_dir` | `Path \| str \| None` | Directory to write the capsule. If `None`, OTel spans only — no capsule written. |

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
- On exception: writes capsule with `status: failure` and re-raises.
- Thread-safe: each call writes to its own capsule directory.

---

## Capture

```python
from novafabric.capture.orchestrator import CaptureOrchestrator, CaptureResult
```

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
```

### `ReplayFlags`

Controls replay mode and safety settings.

```python
@dataclass
class ReplayFlags:
    mode: Literal["mocked", "forensic", "semantic", "exact"] = "mocked"
    dry_run: bool = False
    allow_readonly: bool = False
    allow_mutating: bool = False
    allow_external_side_effects: bool = False
    allow_unknown_mutation: bool = False
    output_dir: Path | None = None
```

Modes: `forensic` — read-only inspection; `semantic` — pairwise similarity
analysis of model call responses (returns `similarity_score`); `exact` —
eligibility check for byte-exact replay (returns `exact_eligible`); `mocked` —
re-executes command with LLM calls served from cache.

### `ReplayEngine(capsule_dir, flags, base_dir=None)`

| Parameter | Type |
|---|---|
| `capsule_dir` | `Path` |
| `flags` | `ReplayFlags` |
| `base_dir` | `Path \| None` — defaults to `.novafabric/replays/` |

### `ReplayEngine.run() → ReplayResult`

Replays the capsule according to the flags. Writes
`replay_result.yaml` in the output directory.

```python
from pathlib import Path
from novafabric.replay import ReplayEngine, ReplayFlags

flags = ReplayFlags(mode="forensic")
engine = ReplayEngine(
    capsule_dir=Path(".novafabric/runs/01HXAY7M5JZ8R7K4P9DPBYK2WX"),
    flags=flags,
)
result = engine.run()
print(result.status)           # "success" | "failure" | "aborted" | "dry_run"
print(result.model_calls_mocked)  # int
print(result.env_warnings)    # list of {field, original, current}
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
    # exact mode
    exact_eligible: bool | None = None      # set when mode="exact"
    exact_hash_count: int | None = None
    exact_reasons: list[str] | None = None
```

---

## Diff

```python
from novafabric.diff import DiffEngine, DiffReport
```

### `DiffEngine.compare(capsule_a, capsule_b) → DiffReport`

Structurally compares two capsule directories.

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

text = format_text(report)
json_str = format_json(report)
annotations = format_github_annotations(report)
```

---

## Lineage

```python
from novafabric.lineage import (
    LineageWriter,
    LineageStore,
    import_capsule_dir,
    index_capsule_lineage,
)
```

### `LineageWriter(capsule_dir, run_id, version="0.4.0")`

Infers lineage edges from a capsule and writes `lineage.jsonl`.

```python
from pathlib import Path
from novafabric.lineage import LineageWriter

writer = LineageWriter(
    capsule_dir=Path(".novafabric/runs/01HX.../"),
    run_id="01HXAY7M5JZ8R7K4P9DPBYK2WX",
)
edges = writer.infer()   # list[LineageEdge]
path = writer.write(edges)  # writes lineage.jsonl, returns Path
```

### `LineageStore(db_path=None)`

SQLite-backed graph store. Default: `~/.novafabric/registry.db`.

```python
from novafabric.lineage import LineageStore

store = LineageStore()

# What did this run depend on?
ancestors = store.provenance(
    ref="run:01HXAY7M5JZ8R7K4P9DPBYK2WX",
    kind=None,
    depth=3,
)

# What runs consume this asset?
dependents = store.blast_radius(
    ref="registry:my-dataset@1.0.0",
    kind=None,
    depth=5,
)

# Replay ancestry
chain = store.replay_chain(run_id="01HXREPLAY...")
```

Each result item is a dict: `{"kind": "run|asset|artifact", "ref": "..."}`.

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
# version=None returns the latest registered version
```

Raises `AssetNotFoundError` if not found.

### `promote_asset(name, version, to, actor, force=False) → dict`

```python
from novafabric.registry.service import promote_asset
from novafabric.spec.models import AssetStatus

asset = promote_asset(
    name="my-model",
    version="1.0.0",
    to=AssetStatus.staging,
    actor="ci-bot",
)
```

Raises:
- `InvalidLifecycleTransitionError` — transition not permitted
- `PromotionBlockedError` — eval gate not satisfied (agent only)

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

Raises `SpecValidationError` with Pydantic error list and a hint string on
validation failure.

### Asset type models

```python
from novafabric.spec.models import (
    AssetType,    # Enum: model agent prompt tool dataset evaluation deployment
    AssetStatus,  # Enum: development staging production archived
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

---

## Report generation

```python
from novafabric.report.generator import generate_report

markdown = generate_report(format_="markdown")
json_str = generate_report(format_="json")
```

---

## NovaSeal signing (v0.10)

```python
from novafabric.trust.novaseal import NovaSeal, KeyConfig

# Instantiate with local ECDSA P-256 key
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
# bundle.tsr            → bytes  (RFC 3161 DER; b"" if TSA skipped)
# bundle.log_entry      → dict   (leaf_index, root_hash, tree_size, …)
# bundle.capsule_id     → str    (SHA-256 hex of the signed payload)

# Verify an existing .seal/ directory
result = seal.verify(
    capsule_id=bundle.capsule_id,
    seal_dir=".novafabric/runs/01HX.../.seal",
)
# result.valid           → bool
# result.signature_ok    → bool
# result.timestamp_ok    → bool
# result.log_integrity_ok → bool
# result.errors          → list[str]
assert result.valid, str(result)

# Rotate the signing key (appends a rotation event to the Merkle log)
from novafabric.trust.novaseal import KeyConfig
new_config = KeyConfig(profile="local", key_path="new.key", cert_path="new.crt")
receipt = seal.rotate_key(new_config)
# receipt.old_key_fingerprint, receipt.new_key_fingerprint, receipt.rotation_log_entry
```

Config can also be loaded from `~/.novafabric/novaseal.yaml` or `NOVAFABRIC_SEAL_CONFIG`:

```python
from novafabric.trust.novaseal.config import load_signing_profile

profile = load_signing_profile()  # None if not configured
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

---

## Utility

```python
from novafabric.capture import new_ulid, new_span_id

run_id = new_ulid()    # str — ULID (26 chars)
span_id = new_span_id()  # str — 16-char hex
```
