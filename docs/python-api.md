# Python API Reference

NovaFabric is a CLI-first toolkit, but every capability the `nova` command
exposes is also reachable as a typed Python API. This reference documents the
public symbols for programmatic **capture, replay, diff, lineage, registry,
spec validation, reporting, and signing** — so you can embed NovaFabric in a
test harness, a CI script, an internal tool, or a notebook without shelling out.

## What you will learn

- How to capture a run in-process with the `@agent` decorator, or capture an
  arbitrary command with `CaptureOrchestrator`.
- How to replay a capsule in any of the five honest modes (`forensic`,
  `mocked`, `semantic`, `exact`, and the experimental `intervention`) and read
  back a structured `ReplayResult`.
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
| [Spec validation](#spec-validation) | `novafabric.spec` | `nova validate` |
| [Report generation](#report-generation) | `novafabric.report.generator` | `nova report` |
| [NovaSeal signing](#novaseal-signing) | `novafabric.trust.novaseal` | `nova seal` / `nova verify` |
| [Score submission](#score-submission) | `novafabric.scores` | `nova score submit` |
| [REST client](#rest-client) | `novafabric.client` | — (server mode) |
| [Compliance-export provenance](#compliance-export-provenance-experimental--adr-0197) | `novafabric.compliance.export.provenance` | — |
| [Utility](#utility) | `novafabric.capture` | — |

The top-level `novafabric` package exports only `__version__` (the installed
distribution version, a `str`); every symbol on this page is imported from the
submodule named in its section. `tests/docs/test_python_api_matches_the_code.py`
fails when a name this page imports cannot be imported, or when a name in the
`__all__` of one of the modules above is missing from this page.

---

## SDK decorator

```python
from novafabric.sdk.agent import agent
from novafabric.sdk import agent  # the same function, re-exported (`novafabric.sdk.__all__`)
```

### `@agent(name, version, capsule_dir=None, deployment_environment=None, variant=None, session_id=None, session_sequence=None) → Callable[[F], F]`

Wraps an agent function with capture hooks. LLM calls made inside the
function are recorded into a capsule. This is the in-process alternative to
`nova capture` — use it when you cannot (or do not want to) wrap the whole
process, e.g. a single function inside a larger long-lived service.

**Parameters**

| Parameter | Type | Description |
|---|---|---|
| `name` | `str` | Asset name (matches a registered asset, if any) |
| `version` | `str` | Asset version (semver) |
| `capsule_dir` | `Path \| str \| None` | Capsule store, like `nova capture --output-dir`: each call writes its own capsule to `<capsule_dir>/<run_id>/`. If `None`, OTel spans only — no capsule is written. |
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

Every call to `summarise()` writes a new capsule under `capsules/<run_id>/` (a
fresh ULID per call, logged as `sdk_capsule_started`). Releases up to v0.106.0
wrote every call into `capsule_dir` itself, so a second call overwrote
`capsule.yaml` while the JSONL streams appended: one capsule, several runs.

**Behaviour**

- Installs every built-in capture hook whose target SDK is importable (OpenAI,
  Anthropic, OpenAI Agents, Google ADK, LangChain, MCP, httpx, requests, aiohttp)
  plus registered third-party hooks, before calling the function.
- Removes hooks after the function returns (including on exception).
- On exception: writes a capsule with `status: failure` and re-raises. `sys.exit(n)`
  records `exit_code: n` (`sys.exit()`/`sys.exit(0)` is a success, a message is
  `1`) and Ctrl-C records `exit_code: 130`; both are re-raised untouched.
- `async def` agents are supported: the decorated function stays a coroutine
  function, and the capture stays open until the coroutine finishes, so model and
  tool calls after an `await` are recorded. Releases up to v0.107.0 finalised the
  capsule before an async agent ran and recorded `success, 0 model calls`.
- **Fail-open:** capture never replaces your result or exception. If the capsule
  store is unwritable, the call runs uncaptured (logged as
  `sdk_capsule_not_started`). If a teardown step fails (for example, ENOSPC
  writing `env.lock`), the capsule is still scanned and written but is **not
  sealed**, and `metadata.finalization_error` names the failed step, e.g.
  `capture incomplete: env.lock: OSError`. Invalid *explicit* tagging arguments
  still raise before the function runs.
- Thread-safe: each call writes to its own capsule directory.

> The capsule this produces has the same shape as one produced by
> `nova capture` — you can `nova validate`, `nova diff`, and `nova replay
> --mode forensic|semantic` it the same way. It records `capture_mode:
> sdk-decorator` and an `@agent:<name>@<version>` label instead of a command,
> so `nova replay --mode mocked` re-runs it only from the process invocation it
> also records (`facets.process_invocation`), when every replay guard passes
> (experimental, ADR-0306 Q7); otherwise it refuses it (`CapsuleNotReplayable`).

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

### `CaptureOrchestrator(base_dir=None, runner=None, ...)`

| Parameter | Type | Default |
|---|---|---|
| `base_dir` | `Path \| None` | `./.novafabric/runs/` (relative to the working directory) |
| `runner` | `RunnerSpec \| None` | `None` — run locally; a `RunnerSpec` dispatches to docker/kubernetes/slurm/lsf/pbs like `nova capture --runner` |
| `pii_gate` | `Any \| None` | `None` |
| `legal_hold` | `bool` | `False` — only acts with a `pii_gate`: the gate scans the manifest and, when it redacts anything, also writes the redacted copy to `<base_dir>/../legal_hold/<run_id>/`; a scan error exits 3. With a gate but no `legal_hold`, the scan is skipped. There is no `nova capture` flag for it |
| `mark_provenance` | `bool` | `False` |
| `fast_emit` | `bool` | `False` |
| `emit_spool` | `bool` | `False` |
| `spool_dir` | `Path \| None` | `None` |
| `masking_pipeline` | `Any \| None` | `None` |
| `capture_media` | `bool` | `False` |

Only `base_dir` and `runner` are covered by examples here; the other keyword
arguments mirror `nova capture` options — see `nova capture --help` and the
constructor's docstring.

The constructor creates `base_dir` at once and raises `CapsuleDirectoryError`
(`novafabric.capture.orchestrator`) when it cannot, before any workload starts.

### `CaptureOrchestrator.run(command, runner_options=None, timeout_s=None, ...) → CaptureResult`

Captures a command execution as a capsule. Only `command` is required; every
other parameter is optional and maps onto a `nova capture` flag.

| Parameter | Type | Default | `nova capture` flag |
|---|---|---|---|
| `command` | `list[str]` | — (required) | the command after `--` |
| `runner_options` | `dict[str, Any] \| None` | `None` | `--runner-option key=value` (passed to the runner uninterpreted) |
| `timeout_s` | `float \| None` | `None` — the runner's own default | `--timeout` |
| `asset_ref` | `str \| None` | `None` | `--asset` |
| `require_asset_statuses` | `list[str] \| None` | `None` | `--require-asset-status` |
| `warn_if_asset_statuses` | `list[str] \| None` | `None` | `--warn-if-asset-status` |
| `require_registered` | `bool` | `False` | `--require-registered` |
| `registry_db_path` | `Path \| None` | `None` — the default registry | — |
| `environment` | `str \| None` | `None` | `--environment` |
| `experiment`, `variant`, `variant_label`, `variant_source`, `variant_assigned_at` | `str \| None` | `None` | `--experiment`, `--variant`, `--variant-label`, `--variant-source`, `--variant-assigned-at` |
| `session_id` | `str \| None` | `None` | `--session-id` |
| `session_sequence` | `int \| None` | `None` | `--session-sequence` |

Pre-flight checks raise **before anything is written to disk**:
`AssetStatusCheckError` (`novafabric.capture.orchestrator`) when `asset_ref` is set
and fails an asset-status check you opted into, and a `ValueError` subclass for an
invalid explicit environment, variant or session value. A workload that fails is
not an exception: the capsule is written with `status: failure` and `exit_code`
carries the code. A `KeyboardInterrupt` or `SystemExit` during the run is
re-raised; when the runner reports that it stopped the workload, the capsule is
first finalised with `status: aborted`.

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

The examples on this page pass capsules written this way, so their paths start with
`.novafabric/runs/`. That is the SDK default, not the CLI's: `nova capture` writes to
the capsule store, `~/.novafabric/capsules/<run_id>/` by default
(`novafabric._paths.default_capsule_dir()`, which honours `NOVAFABRIC_CAPSULE_DIR`
and `NOVAFABRIC_HOME`). For a capsule from `nova capture`, pass that directory instead.

### `CaptureResult`

```python
@dataclass
class CaptureResult:
    run_id: str
    capsule_dir: Path
    exit_code: int
    runner_status: str | None = None   # e.g. "failed_setup": the workload never started
    runner_error: str | None = None    # the runner's own reason, when it gave one
    sealed: bool | None = None         # was the capsule sealed; None if finalization never ran
    unsealed_reason: str | None = None # why a sealable capsule was not sealed
    replayability: dict[str, Any] | None = None  # the capsule's replayability.json, if written
```

`exit_code` alone cannot tell *the workload ran and failed* from *the workload
never started* (a missing command and a program that exits 127 both give 127);
only `runner_status == "failed_setup"` identifies the latter. `None` means the
runner did not say, not "completed".

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
wire-level hooks and `nova seal propose` own those streams, and a second
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

### `record.tool(fn=None, *, name=None, mutation_class="unknown", version=None, ignore=())`

**Status: experimental** (ADR-0306 slice 1, since v0.105.0). Declares a
Python function as a tool boundary that `nova capture` records and mocked
`nova replay` serves, so the tool does not run again during replay.

```python
from novafabric.capture import record

@record.tool(mutation_class="external-side-effect", ignore=("ctx",))
def send_invoice(ctx, customer_id: str, amount_cents: int) -> dict: ...

@record.tool  # name = fn.__name__, mutation_class = "unknown"
async def lookup_order(order_id: str) -> dict: ...
```

- **Outside capture and replay** the wrapper reads one global and calls the
  function; nothing is recorded.
- **Under `nova capture`** the body runs, then one record is written to
  `tool-calls.jsonl` with `transport: "python"`; exceptions propagate unchanged
  and recording never raises into your code. Arguments and the result are kept
  **only at the `forensic`/`air_gapped` capture level** (`NOVA_CAPTURE_LEVEL`).
  At the default level the record keeps the name, an argument digest and a result
  digest, and replay refuses it with a re-capture hint.
- **Under mocked `nova replay`** the recorded result is returned **before the body
  runs**, matched by tool name and the arguments bound to the signature (defaults
  applied, `ignore` names dropped), one record per call in recorded order. An
  unmatched call raises `ReplayToolUnmatchedError`; a record that cannot be
  served raises `ReplayToolRecordNotServableError` with the reason. Either way the
  replay fails, even if your code catches the exception. With `--permissive` an
  unmatched call runs live only if a `--allow-*` ladder flag permits its declared
  `mutation_class`. A capsule's `replay.yaml` `tool_overrides` entry naming the
  function is enforced too (ADR-0306 slice 2): `allow: false` is never run live,
  `allow: true` re-executes the body only with the ladder flag for its declared
  class.
- **Served values**: JSON-native results only (`dict`, `list`, `str`, numbers,
  `bool`, `None`), up to `NOVAFABRIC_TOOL_RESULT_MAX_BYTES` (default 1 MiB).
  Tuples, sets, pydantic models and other objects are recorded as not servable.
  Arguments may also be pydantic models or dataclasses (dumped to JSON); any
  other non-JSON argument must be listed in `ignore` (methods need
  `ignore=("self",)`). A recorded builtin exception (e.g. `ValueError`) is raised
  again as itself; any other class becomes `ReplayRecordedToolError`.
- **Nested boundaries** (since v0.106.0 — ADR-0306 slice 4,
  experimental): a decorated function that itself calls a model, an MCP tool or
  another decorated function is served like any other; the records it wrote at
  capture (marked `io.novafabric.within_tool_call_id`) are consumed as *covered*
  (`replay_contract.model_calls_covered` / `tool_calls_covered`), so the calls
  after it still get their own recorded answers. A nested call made from a raw
  thread is not marked; its record stays unconsumed and the replay fails closed.
  Capsules captured by v0.105.0 are served too, without re-capture.
- **Not supported**: generator functions (rejected at decoration).
- **Digests and secrets.** At the default capture level the record keeps an
  argument digest (and, for results up to 64 KiB, a result digest) instead of the
  values. Both are computed **after** the capsule's secret rules have masked every
  detected secret as `[REDACTED:<rule>]`, so a detected secret leaves no digest
  derived from its value; replay redacts the live arguments the same way before
  matching. ⚠ A secret the rules do **not** detect — a short password, a PIN, a
  customer number — still feeds the digest, and a digest of a low-entropy value can
  be confirmed offline by guessing. **Do not decorate a tool that takes credentials
  or other low-entropy secrets at the default capture level**; pass them through an
  `ignore=` parameter or leave the function undecorated. Redaction costs roughly
  150 µs per KiB of argument text per call at that level. MCP `call_tool`
  arguments are matched the same way since slice 4 (v0.106.0).
- **Undeclared functions** are not served, but since slice 4 (v0.106.0,
  experimental) mocked replay compares the result such a function sends
  back to the model (the `role: "tool"` message, or an Anthropic `tool_result`
  block) with the recorded one, by `tool_call_id`, and reports a difference under
  `replay_contract.tool_result_echo` — report-only, it never fails the replay.

---

## Replay

```python
from novafabric.replay import ReplayEngine, ReplayFlags
from novafabric.replay._result import ReplayResult
```

Replay re-executes or inspects a capsule with external calls controlled. A
replay writes a `replay_result.yaml` report, not a capsule — only
`intervention` mode also writes a minimal counterfactual Run Capsule
(`capsule.yaml` marked `replay_mode: intervention` plus the model/tool call
streams) that you can `nova diff` against its source.

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
    intervention_file: Path | None = None
    required_environment: str | None = None
    permissive: bool = False   # mocked mode only (ADR-0300): warn instead of fail
    hermetic: bool = False     # mocked mode only, experimental (ADR-0322): sandbox the replayed process
    hermetic_backend: str = "auto"  # "auto" (bwrap, then Landlock + seccomp) | "bwrap" | "landlock"

    def permits(self, mutation_class: str) -> bool: ...  # does the allow_* ladder let this class run?
    def active_flag_names(self) -> list[str]: ...         # the CLI flags these settings correspond to
    @property
    def divergence_policy(self) -> Literal["fail", "warn"]: ...  # "warn" iff permissive
    @property
    def permits_writes(self) -> bool: ...  # True if any flag may let a tool beyond read-only run
```

`hermetic=True` (**experimental**, `nova replay --hermetic`) runs the replayed
process tree in a sandbox with no network beyond its own loopback and a read-only
host filesystem. When no sandbox backend is available the replay is refused before
the command runs: `status: "aborted"` with `error["type"] == "HermeticUnavailable"`.
The result's `hermetic` field describes the isolation used.

**Modes**

| Mode | What it does | Typical use |
|---|---|---|
| `forensic` | Read-only inspection. No subprocess, no network. | Audit / post-incident |
| `mocked` | Re-spawns the command (Python workloads). Serves recorded responses for OpenAI `chat.completions.create` and `responses.create` / Anthropic `messages.create` — **sync or async, streamed or not** (ADR-0304) — and recorded **MCP** `ClientSession.call_tool` results, one per call, plus — experimental — results of functions declared with [`record.tool`](#extended-event-recording-experimental) and of Google ADK tools run with `novafabric.adapters.google_adk.make_tool_plugin()` and OpenAI Agents function tools passed through `novafabric.adapters.openai_agents.wrap_tools()` (both shipped in v0.106.0; the latter relies on an undocumented SDK seam), and — new in v0.109.0 — LangChain / LangGraph tools passed through `novafabric.adapters.langchain_tools.wrap_tools()` (a generated subclass of each tool, not a LangChain API; other LangChain tools are recorded and, when the capsule holds their results — capture level `forensic` — served too; a strict replay of a capsule whose observed records cannot be served refuses to start, `ToolSurfaceNotServed`, and each one that runs live is counted in `tool_calls_live`). **Other tools run live** (HTTP, shell, files, other framework-native tools, undeclared functions); `replay_contract.network_connections_live` reports their connections. **Fails closed** (ADR-0300): an extra or unmatched call, an unsupported model surface (`parse`, legacy completions, `with_raw_response`), or an unconsumed recording makes the result `failure` with a `divergence_reason`; `permissive=True` only reports them. | CI / regression |
| `semantic` | **Does not re-execute.** Scores how similar the capsule's *recorded* model responses are to each other (mean pairwise text similarity, 0.0–1.0); no live model is called; returns `similarity_score`. | Consistency check of recorded responses |
| `exact` | **Does not re-execute.** Eligibility check for byte-exact replay (`exact_eligible` + reasons): deterministic env.lock, per-call seed, no tool-schema drift. | Local / on-prem / compliance |
| `intervention` | Re-executes with a spec-driven intervention overlay (experimental, ADR-0086). | What-if / counterfactual analysis |

> NovaFabric explicitly does **not** claim byte-exact replay of remote LLM
> calls. `exact` mode reports *eligibility*, not a guarantee.

The `allow_*` flags form a safety ladder over the capsule's recorded tool calls
(`allow_readonly` < `allow_mutating` < `allow_external_side_effects` <
`allow_unknown_mutation`). They drive the `dry_run` report, and `allow_mutating`
triggers an audited policy gate before the replay starts. Inside the replayed
process they gate only the intercepted tool surfaces (ADR-0306, experimental):
with `permissive=True` an unmatched MCP, `record.tool`, ADK or Agents SDK tool call runs live only if
they permit its class (MCP calls count as `unknown`), and a `replay.yaml`
`allow: true` override re-executes a tool only if they permit its class. A
strict replay whose `replay.yaml` has an `allow: false` override on a tool it
cannot intercept returns `status: "aborted"` with `error["type"] ==
"ToolOverrideUnenforceable"`. Leaving them `False` does not
sandbox tools that `mocked` mode does not intercept — run such replays in a
sandbox or against test credentials.

### `ReplayEngine(capsule_dir, flags, base_dir=None, actor="cli")`

| Parameter | Type |
|---|---|
| `capsule_dir` | `Path` |
| `flags` | `ReplayFlags` |
| `base_dir` | `Path \| None` — defaults to `$NOVAFABRIC_HOME/replays/` (`_paths.default_replays_dir()`; `./.novafabric/replays/` under the current directory up to v0.106.0) |
| `actor` | `str` — the identity the `replay_mutating` policy gate evaluates (`subject.user`, consulted when a write-permitting flag is set) and the audit log records; default `"cli"` |

### `ReplayEngine.run() → ReplayResult`

Replays the capsule according to the flags. Writes `replay_result.yaml` in the
output directory.

A replay that diverges or is refused is reported in the result (`status`
`"failure"` or `"aborted"`, with `divergence_reason` or `error`), not raised.
Before a replay starts, `run()` raises: `ValueError` when `capsule_dir` has no
`capsule.yaml` (`CapsuleRequiredFileError` when it is a link, FIFO or directory);
`ReplayEnvironmentMismatchError`
(`novafabric.replay.environment_gate`) when `flags.required_environment` is set and
the capsule recorded a different (or no) deployment environment; and
`PolicyDeniedError` (`novafabric.policy`) when a write-permitting flag is set and
the `replay_mutating` policy gate denies `actor`.

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

**Example — a mocked replay gate in CI (fail-closed, ADR-0300)**

```python
result = ReplayEngine(capsule_dir=cap, flags=ReplayFlags(mode="mocked")).run()
if result.status != "success":
    # e.g. "model_queue_exhausted: no recorded openai response left for call #3 ..."
    raise SystemExit(result.divergence_reason or result.error)
print(result.model_calls_mocked, "of", result.model_calls_available, "model calls served")
print(result.tool_calls_mocked, "MCP tool results served;",
      result.replay_contract["tool_calls_not_interceptable"], "recorded tools not intercepted")
```

The replayed process raises a `ReplayDivergenceError` subclass
(`novafabric.replay._errors`: `ReplayQueueExhaustedError` and its subclass
`ReplayProviderMismatchError`, `ReplayOrderMismatchError`,
`ReplayUnsupportedSurfaceError`, `ReplayRecordMalformedError`,
`ReplayWireRequestMismatchError`, `ReplayToolUnmatchedError`,
`ReplayToolRecordNotServableError`, `AdapterRunCountMismatchError`,
`ReplayRecordedErrorUnreconstructableError`) at the call that diverged. These
are raised inside the replayed process, not by `ReplayEngine.run()`, which
reports the divergence in the returned `ReplayResult`.

**Example — how alike a capsule's recorded responses are (`semantic`)**

`semantic` executes nothing and calls no model: it scores the recorded responses
within the capsule against one another (mean pairwise `difflib` ratio), a text
similarity, not a comparison with a live model.

```python
flags = ReplayFlags(mode="semantic")
result = ReplayEngine(capsule_dir=cap, flags=flags).run()
if result.similarity_score is not None and result.similarity_score < 0.9:
    print(f"recorded responses differ from one another: {result.similarity_score:.3f}")
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
    model_calls_mocked: int = 0          # mocked: responses actually served
    tool_calls_mocked: int = 0           # mocked: MCP results actually served
    tool_calls_available: int | None = None  # recorded on a servable surface (MCP)
    tool_calls_recorded: int | None = None   # every recorded tool call
    # mocked mode, ADR-0300 (all optional)
    model_calls_available: int | None = None
    model_calls_unmatched: int | None = None
    model_calls_not_servable: int | None = None  # ADR-0319: recorded on a surface mocked replay cannot serve
    tool_calls_live: int | None = None
    tool_calls_unmatched: int | None = None
    queues_fully_consumed: bool | None = None
    divergence_reason: str | None = None
    replay_contract: dict | None = None  # policy, surfaces, divergences, ...
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
    # tool-call schema drift findings (ADR-0128; additive, optional)
    schema_drift: list[dict] | None = None
    # mocked mode, ADR-0321 (additive, optional): outcome equivalence with the
    # recorded run ({"verdict": ..., "checks": ...}); `status` still says how the
    # replayed process ended, so a faithful replay of a failed run is
    # status "failure" with fidelity["verdict"] == "faithful"
    fidelity: dict | None = None
    replay_capsule_ref: str | None = None  # the replay capsule beside this result (a `nova diff` input)
    # mocked --hermetic only (ADR-0322): how the replayed process tree was isolated
    hermetic: dict | None = None

    def as_dict(self) -> dict: ...
```

---

## Diff

```python
from novafabric.diff import CapsuleFileError, DiffEngine, DiffReport
```

`DiffEngine` structurally compares two capsules — aligning their model calls,
tool calls, environment, and outputs — and reports what changed, was added, or
was removed. This is the engine behind `nova diff --assert-no-regressions`.
`DiffEngine()` takes no arguments.

### `DiffEngine.compare(capsule_a, capsule_b) → DiffReport`

Raises `CapsuleFileError` (a `ValueError` subclass, constructed as
`CapsuleFileError(path, reason)`) when a `capsule.yaml` or `env.lock` exists but
cannot be read, is not UTF-8, is not valid YAML, or is not a mapping (also when
a call-stream file is a link or special file, which is never followed), so the two
capsules cannot be compared. A missing or empty file is not an error, and
malformed lines in the JSONL call streams do not raise; they are counted (see
below).

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
print(report.has_changes)    # bool: any changed, added or removed entry
print(report.is_complete)    # bool: False if any record line was skipped as malformed
print(report.as_dict())      # serialisable dict
```

**Example — use the diff as a CI regression gate**

```python
import sys
report = DiffEngine().compare(baseline_capsule, candidate_capsule)
if not report.is_complete:
    print(report.skipped_malformed_lines)
    sys.exit(2)   # cannot compare: a record line was unreadable
if report.has_changes:
    print(report.as_dict())
    sys.exit(1)   # fail the build on any structural change
```

> `has_changes` is the one definition of "any difference". The
> `nova diff --assert-no-regressions` CLI exits `1` on it, and the text and
> GitHub-annotation formatters read it too, and `as_dict()` carries it as a
> top-level `has_changes` key — reach for the CLI directly in shell-based
> pipelines. Gating on `changed_count` alone misses a call that was added or
> removed.
>
> `has_changes` is computed over the records that parsed. A non-blank line of
> `model-calls.jsonl` or `tool-calls.jsonl` that is not UTF-8, not JSON, or not a
> JSON object is skipped and counted in `skipped_malformed_lines` (per side `a`/`b`,
> per file `model_calls`/`tool_calls`; also a top-level key of `as_dict()`), and
> `is_complete` turns False. The CLI gate then exits `2`, "cannot compare", before
> it looks at `has_changes` (ADR-0303 Amendment 1).

### `DiffReport`

```python
@dataclass
class DiffReport:
    run_a_id: str
    run_b_id: str
    # every list/dict field defaults to empty (field(default_factory=...))
    env_changes: list[dict]
    model_call_pairs: list[dict]
    tool_call_pairs: list[dict]
    output_changes: list[dict]
    skipped_malformed_lines: dict[str, dict[str, int]]  # {"a"|"b": {"model_calls", "tool_calls"}}
    first_divergence: dict | None = None  # earliest changed/added/removed model or tool call; None if the call streams agree (also a top-level key of `as_dict()`)

    @property
    def changed_count(self) -> int: ...
    @property
    def added_count(self) -> int: ...
    @property
    def removed_count(self) -> int: ...
    @property
    def has_changes(self) -> bool: ...
    @property
    def malformed_line_count(self) -> int: ...
    @property
    def is_complete(self) -> bool: ...
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
`index_capsule_lineage` (one capsule) or `import_capsule_dir` (a directory of
capsules) to index them, and `LineageStore` to query the graph.

### `LineageWriter(capsule_dir, run_id, version=None)`

Infers lineage edges from a capsule and writes `lineage.jsonl`. Each edge's
`emitter.version` is `version`, defaulting to the installed `novafabric.__version__`.
`produced_by` edges are read from the manifest's `outputs` — Artifact objects (keyed by
their `path`, with `name`/`content_hash`/`media_type` carried into `artifact_ref`; an
inline Artifact is keyed `inline:<name>`) or legacy path strings — and from `produced.jsonl`.

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

SQLite-backed graph store. Default: `$NOVAFABRIC_HOME/registry.db` (`~/.novafabric/registry.db`
unless `NOVAFABRIC_HOME` is set; `NOVAFABRIC_DB_PATH` overrides the file).

```python
from novafabric.lineage import LineageStore

store = LineageStore()

# What did this run depend on? (ancestors)
# A run node's ref is the bare run id; pass kind="run" to disambiguate.
ancestors = store.provenance(
    ref="01HXAY7M5JZ8R7K4P9DPBYK2WX",
    kind="run",
    depth=3,
)

# What runs consume this asset? (descendants / impact)
# An asset node's ref is "<registry>:<name>@<version>" (registry "local" by default).
dependents = store.blast_radius(
    ref="local:my-dataset@1.0.0",
    kind="asset",
    depth=5,
)

# Replay ancestry
chain = store.replay_chain(run_id="01HXREPLAY...")
```

Signatures: `provenance(ref, kind=None, depth=5, edge_type=None)` and
`blast_radius(ref, kind=None, depth=5, edge_type=None)`, where `edge_type` is one
edge type or a collection of them (`None`, `""` or an empty collection means no
filter) and restricts the whole traversal to those edges; `replay_chain(run_id)`
follows `replayed_from` edges (at most 100 steps).

Each result item is a node row: a dict with `node_id`, `kind`
(`"run" | "asset" | "artifact"`), `ref` and `payload` (the node's stored JSON
payload, as a string); `replay_chain` items also carry `step`. An unknown `ref`
returns `[]`, not an error.

`time_travel(ref, asof, kind=None, limit=5000)` returns the edges touching `ref`
created at or before the `asof` timestamp, oldest first, for point-in-time graph
reconstruction. `all_nodes(*, limit=250000)` and `all_edges(*, limit=1000000)`
read the whole graph and raise `LineageGraphTooLargeError`
(`novafabric.lineage._store`, a `RuntimeError` subclass) above `limit` instead of
truncating.

### Pluggable lineage backends (experimental)

```python
from novafabric.lineage import (
    AbstractLineageStore,    # ABC: insert(edge), provenance(run_id, depth),
                             #      blast_radius(run_id, max_depth), replay_chain(run_id)
    SqliteLineageStore,      # SqliteLineageStore(db_path, site_id="local"); db_path is required
    PostgresLineageStore,    # PostgresLineageStore(dsn, site_id="local"); needs psycopg
    AGELineageStore,         # AGELineageStore(dsn, site_id="local"); Postgres with Apache AGE
    JanusGraphLineageStore,  # JanusGraphLineageStore(gremlin_endpoint="ws://localhost:8182/gremlin", site_id="local")
)
```

These implement the backend interface `AbstractLineageStore`, which is narrower
than `LineageStore` above (its traversals take a run id and a depth). `LineageStore`
is the SQLite store the CLI uses and does not subclass it. `SqliteLineageStore`
raises `ValueError` when `db_path` is `None`, so a test never writes into the
developer database by accident. The Postgres-based backends import `psycopg` when
constructed, and `JanusGraphLineageStore` opens its Gremlin connection on first use.

### `index_capsule_lineage(capsule_dir, db_path=None, *, run_id=None) → ImportResult`

Index one capsule's `lineage.jsonl` into the graph store. The run is keyed by
`run_id` when given, else by the directory name (a `nova capture` capsule
directory is named after its run id). A capsule without `lineage.jsonl` returns
`skipped=True`; a `lineage.jsonl` that is present but unreadable (a symlink,
FIFO or directory) raises instead of reporting zero edges.

```python
from pathlib import Path
from novafabric.lineage import index_capsule_lineage

result = index_capsule_lineage(Path("~/.novafabric/capsules/01HX...").expanduser())
print(result.edges_indexed)  # int
print(result.nodes_indexed)  # int
```

### `import_capsule_dir(runs_dir, db_path=None) → list[ImportResult]`

Index every capsule directly under `runs_dir` (each subdirectory that holds a
`capsule.yaml`), returning one `ImportResult` per capsule.

```python
from pathlib import Path
from novafabric.lineage import import_capsule_dir

for result in import_capsule_dir(Path("~/.novafabric/capsules").expanduser()):
    print(result.capsule_run_id, result.edges_indexed, result.skipped)
```

### `ImportResult`

```python
@dataclass
class ImportResult:
    capsule_run_id: str
    edges_indexed: int
    nodes_indexed: int
    skipped: bool
    warning: str | None = None
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

| Function | Returns | Notes |
|---|---|---|
| `compute_graph_metrics(store, *, top_n=20, betweenness_sample_threshold=2000, seed=0)` | `GraphMetricsReport` | hubs and articulation points as `NodeMetric` rows; an empty graph gives an empty report |
| `rank_root_causes(store, run_id, *, depth=5, capsule_dir=None, half_life_hours=24.0)` | `RootCauseReport` | upstream `SuspectNode`s for a failed run; raises `UnknownLineageRunError` (a `KeyError` subclass) when `run_id` has no lineage node |
| `build_insights_report(store, *, top_n=10, seed=0, cost_db=None)` | `InsightsReport` | node/edge counts, hubs, communities, orphans, cost hot spots |
| `to_graphml(nodes, edges)`, `to_gexf(nodes, edges)`, `to_cypher(nodes, edges)` | `str` | serialise `store.all_nodes()` / `store.all_edges()` |

`GraphMetricsReport`, `RootCauseReport`, `InsightsReport`, `NodeMetric` and
`SuspectNode` are dataclasses importable from `novafabric.lineage.analytics`.
Scores are relative rankings for attention, not calibrated importance.

---

## Asset Registry

```python
from novafabric.registry.service import (
    register_asset,
    list_assets,
    get_asset,
    promote_asset,
    promote_asset_direct,
)
from novafabric.spec.models import AssetStatus
```

The registry is a local SQLite store (`$NOVAFABRIC_HOME/registry.db`, overridable with
`NOVAFABRIC_DB_PATH`) of versioned
assets addressed as `name@version`. Promotion is **governance metadata only** —
it updates one DB record and does not deploy, restart, or redeploy anything.

### `register_asset(spec, spec_path, db_path=None, *, actor="") → dict`

```python
from pathlib import Path
from novafabric.spec.validator import validate_spec
from novafabric.registry.service import register_asset

spec = validate_spec(Path("my-model.yaml"))
asset = register_asset(spec, spec_path=Path("my-model.yaml"))
print(asset["id"])   # UUID
```

`actor` is the identity recorded for the registration in the audit trail. A new
asset is always registered in `development`.

Raises (all importable from `novafabric.registry.service`):
- `DuplicateAssetError` — `name@version` is already registered.
- `VersionReuseError` (a `DuplicateAssetError` subclass) — `name@version` was in
  `production` before; its version number can never be reused.
- `RegistrationStatusError` — the spec declares a status other than `development`.

### `list_assets(asset_type, status, db_path=None) → list[dict]`

`asset_type` and `status` are required positional-or-keyword arguments with no
default; pass `None` to leave a filter off. Results are newest first.

```python
from novafabric.registry.service import list_assets

agents = list_assets(asset_type="agent", status="production")
for a in agents:
    print(a["name"], a["version"], a["status"])
```

### `get_asset(name, version, db_path=None) → dict`

```python
from novafabric.registry.service import get_asset

asset = get_asset("my-model", "1.0.0")
# version is required; pass None (or "latest") for the latest registered version
```

When the exact version is not registered, the normalised semver form is tried
(`1.0` → `1.0.0`, `v1` → `v1.0.0`). Raises `AssetNotFoundError` if not found.

### `promote_asset(name, version, to_status, actor, force=False, db_path=None, significance_gate=False, ...) → dict`

Advances an asset through the six-state lifecycle and returns the updated asset
record. The `to_status` argument is an `AssetStatus` enum member. The remaining
parameters tune the opt-in significance gate (ADR-0080): `sig_p0=0.9`,
`sig_p1=0.7`, `sig_alpha=0.05`, `sig_beta=0.05`, `sig_scores_path=None` (read the
sequence from a `scores.jsonl` instead of the registry's eval results) and
`sig_scores_metric="task_pass"`.

> **Use `promote_asset_direct` for a single-actor promotion.** `promote_asset` is
> the ungated core: it runs the lifecycle, `promote` policy, eval and significance
> checks, but **not** the protected-stage gate. `promote_asset_direct(name, version,
> to_status, actor, force=False, db_path=None, *, policy_subject=None, **gate_kwargs)`
> is the one gate every single-actor route uses (`nova promote direct`,
> `nova prompt promote`, the `nova serve` promote endpoints, `PUT
> /v0/assets/{id}/promote`): a target in `PROTECTED_STAGES` (today `production`)
> needs an explicit allow from the `promote_direct` policy action before
> `promote_asset` runs — `force` does not bypass it and a no-OPA `NoopEngine`
> decision is not an allow (ADR-0312 D1). It raises `DirectPromotionDeniedError`
> (a `PromotionBlockedError` subclass) when refused.

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
- `AssetNotFoundError` — `name@version` is not registered.
- `InvalidLifecycleTransitionError` — the transition is not permitted by the
  lifecycle state machine (`force` does not override this).
- `PromotionBlockedError` — a gate refused and `force=False`: the `promote`
  policy denied the promotion (any asset type), an `agent` promoted to `staging`
  or `production` has no passing evaluation (its most recent eval must pass; with
  `significance_gate` the whole recent sequence is judged instead), or the opt-in
  significance gate found a significant regression (agents only). With
  `force=True` these gates are overridden; a policy denial is still audited and
  alerted.

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

Raises `SpecValidationError(message, errors=None, hint=None)` when the file does
not exist, is not valid YAML, does not hold a mapping at the top level, or fails
model validation. `str(exc)` is the message; `exc.errors` is the Pydantic error
list (empty for the first three cases) and `exc.hint` a suggested fix. The
returned object is the concrete spec model for the file's `asset_type`
(`ModelSpec`, `AgentSpec`, ...).

`novafabric.spec` re-exports the models below, plus `validate_asset_spec` (the
same function as `validate_spec`) and `AssetSpec`, the discriminated union of the
seven spec models keyed on `asset_type` (a type annotation, for use with
Pydantic's `TypeAdapter`):

```python
from novafabric.spec import AssetSpec, validate_asset_spec
```

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
lifecycle states listed above. Both are `str` enums, so a member compares equal
to its value (`AssetStatus.staging == "staging"`). Every spec model rejects keys
it does not define; vendor or user data belongs in the `extensions` block or in
`tags` / `external_refs`.

---

## Report generation

```python
from novafabric.report.generator import generate_report

markdown = generate_report(format_="markdown")
json_str = generate_report(format_="json")
```

`generate_report(format_="markdown", db_path=None) → str` renders the current
registry state. This is the engine behind `nova report`. `format_` is
`"markdown"` (an inventory grouped by asset type), `"json"` (every asset record
without its `spec_json`) or `"html"`; any other value falls back to Markdown.

---

## NovaSeal signing

```python
from novafabric.trust.novaseal import NovaSeal, KeyConfig
```

NovaSeal is the cryptographic signing core for capsule manifests: a **DSSE
envelope (ECDSA P-256 / SHA-256)**, an optional **RFC 3161 trusted timestamp**,
and an entry appended to a **SQLite-backed append-only Merkle log**. The
`local` key profile is the default; cloud-KMS profiles (`aws_kms`, `azure_kv`,
`gcp_kms`) are also supported by the config loader. A cloud profile has no local
private key, so `NovaSeal` needs a signing `backend=` for it, built with
`novafabric.trust.novaseal.config.build_signing_backend()`.

`NovaSeal(config, tsa_url, db_path, tsa_urls=None, backend=None)`:
`tsa_url=""` disables timestamping (no network call); `tsa_urls` is an ordered
fallback list of TSA URLs, tried in turn; `db_path` is a SQLite path or a
Postgres DSN for the Merkle log; `backend` is required only for the cloud
profiles.

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
    tsa_url="",  # "" = no RFC 3161 timestamp, no network call; or your TSA's URL
    db_path="~/.novafabric/novaseal-merkle.db",
)

# Sign a capsule manifest (dict)
bundle = seal.seal(capsule_manifest)
# bundle.dsse_envelope  → bytes  (DSSE JSON)
# bundle.tsr            → bytes  (RFC 3161 DER; b"" if TSA skipped/unavailable)
# bundle.log_entry      → dict   (leaf_index, leaf_hash, root_hash, tree_size,
#                                  root_version, inclusion_proof, entry)
#                         root_version 2 = RFC 6962 root (new logs); absent or 1 =
#                         legacy root, meaningful only with tree_size (ADR-0313)
# bundle.capsule_id     → str    (SHA-256 hex of the signed payload)
```

A leading `~` in `key_path`, `cert_path` and a file `db_path` is expanded to the home directory
(`MerkleLog` and `open_merkle_log` expand it too). A Postgres DSN `db_path` is used exactly as
given. Up to v0.106.0 `NovaSeal(...)` and `MerkleLog(...)` did not expand `~`, so the example above
created a literal `./~` directory in the working directory.

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
# result.log_integrity_ok → bool  (True only when inclusion was actually checked)
# result.log_inclusion    → "local-log" | "carried-proof" | "not-checked" | "failed"
# result.pae_encoding     → "dsse-v1" | "legacy-le64" (sealed through v0.102.x)
# result.ca_chain_ok      → bool
# result.errors           → list[str]
assert result.valid, str(result)

# Independent verification — no key, no config, no Merkle log needed:
from novafabric.trust.novaseal import verify_seal_dir

result = verify_seal_dir(".novafabric/runs/01HX.../.seal")  # merkle_log=None
```

Signatures: `seal.verify(capsule_id, seal_dir, *, previous_logs=(), tsa_anchors_pem=None)`
and `verify_seal_dir(seal_dir, merkle_log=None, *, previous_logs=(), tsa_anchors_pem=None)`.
`verify()`'s `capsule_id` is unused and kept for compatibility: the capsule id is
re-derived from the signed payload. `previous_logs` are this host's earlier Merkle
logs (`previous_merkle_dbs` in `novaseal.yaml`), consulted like `merkle_log`.
`tsa_anchors_pem` is operator TSA CA certificates (PEM): with it the token's TSA
signature and chain are verified (`result.timestamp_trust` is `"anchored"` or
`"invalid"`, and a missing token fails); without it a present, intact token is
`"unanchored"`. `result.sealer_log` reports what the supplied logs said about
this signer (one of the `SEALER_LOG_*` values below).

`log_inclusion` is `not-checked` (and `valid` stays True) when the capsule carries no
inclusion proof and no log holding its entry was supplied — a capsule sealed by v0.102.x
or earlier, verified away from the sealer's log. Any inconsistency (an entry that names
another capsule, a proof that does not recompute its root) is `failed`.

`seal.verify()` passes the sealer's own log, and `verify_seal_dir(..., merkle_log=log)`
any log you open. The log is checked at the index `log-entry.json` records, so a manifest
sealed twice (the same leaf twice in the log) verifies at each index. When the supplied log
holds other seals by the same signer (the entry's `keyid`, or the SHA-256 fingerprint of the
certificate of **any** signature that verifies) under the record's root version, it is that
sealer's log: an entry
missing from it is `failed`, even if the carried proof verifies. A log with no seals by that
signer, a fresh log, or a log of the other root version (after starting a new v2 log,
ADR-0313) is not the sealer's, and absence there keeps `carried-proof`, with a note in
`log_notes`.

`timestamp_ok` is `False` for a token that cannot be cryptographically verified. A token
the strict DER parser rejects is checked on a lenient-encoding path (`timestamp_strict`
False) that still requires a verifying TSA signature, a signed `messageDigest` equal to the
hash of the TSTInfo, and that TSTInfo's imprint equal to the envelope hash.

`verify()` reads `manifest.dsse`, an optional `manifest.dsse.tsr`, and
`log-entry.json` from the `.seal/` directory. An **absent or empty** TSR is
reported as absent — `timestamp_ok=None` (ADR-0301; it was `True` before) — and does
not invalidate the seal; a present but invalid TSR fails verification
(`timestamp_ok=False`). `result.identity_trust` is `"self-asserted"` when the
signature verifies (no CA bundle is consulted here) and `"none"` otherwise;
`result.signer_subject` is the embedded certificate's subject, an unverified claim.
`result.ca_chain_ok` is `False` unless a CA bundle validated the chain
(`nova verify --ca-bundle`).

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

Configuration is read from `NOVAFABRIC_SEAL_CONFIG` when set (a missing file is an
error), else from `$NOVAFABRIC_HOME/novaseal.yaml` (`~/.novafabric/novaseal.yaml` by
default):

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

### Errors, timestamp helpers and result constants

Everything below is in `novafabric.trust.novaseal.__all__` (**experimental**, like
the rest of NovaSeal).

| Name | What it is |
|---|---|
| `SealError` | Raised by `seal()` when DSSE signing or the Merkle-log append fails. A TSA failure is not a `SealError`: the capsule is sealed without a timestamp. |
| `TSAUnavailableError` | Raised by `request_timestamp()` when the TSA cannot be reached. |
| `SealBundle`, `VerificationResult`, `RotationReceipt`, `KeyConfig`, `SigningIntent` | The dataclasses and enum used above. `SigningIntent` values: `authored`, `reviewed`, `approved`, `witnessed`, `verified`. |
| `verify_with_profile(seal_dir, profile) → VerificationResult` | Verify with what a `novaseal.yaml` profile provides: its `merkle_db` and `previous_merkle_dbs` (opened only if they exist) and `tsa_ca_certs`. `profile=None` verifies from the capsule's own files. Raises `OSError` when a configured TSA CA file cannot be read. |
| `unanchored_claims(result) → list[str]` | What a passed result still rests on without a trusted anchor (`identity-self-asserted`, `timestamp-unanchored`, `log-head-unanchored`, `log-inclusion-not-checked`, `sealer-log-other-version`). `nova verify --strict` fails when it is non-empty. |
| `timestamp_trust_for(timestamp_ok, tsa_chain_ok) → str` | The `TIMESTAMP_TRUST_*` value for a token check and an optional anchored check. |
| `request_timestamp(dsse_bytes, tsa_url, *, tsa_urls=None, nonce_store_path=None, offline_mode=False, verify_chain=True, tsa_cert_max_depth=4) → bytes` | Request an RFC 3161 token (DER) for an envelope, with a random nonce recorded in a `NonceStore` and an optional TSA chain check. |
| `verify_timestamp(tsr_bytes, dsse_bytes) → bool` | Is the token a valid timestamp of these envelope bytes. |
| `verify_tsa_cert_chain(tsr_bytes, max_depth=4) → CertChainResult` | Walk the TSA certificate chain embedded in a token. Never raises; failures are in `CertChainResult.error`. |
| `CertChainResult` | `valid`, `depth`, `subject`, `issuer`, `eku_present`, `self_signed`, `chain_subjects`, `error`. |
| `NonceStore(path=None, *, offline_mode=False)` | SQLite store of RFC 3161 request nonces, under `$NOVAFABRIC_HOME` by default; `offline_mode=True` makes it a no-op. |
| `LOG_INCLUSION_LOCAL`, `LOG_INCLUSION_PROOF`, `LOG_INCLUSION_NOT_CHECKED`, `LOG_INCLUSION_FAILED` | The `log_inclusion` values: `"local-log"`, `"carried-proof"`, `"not-checked"`, `"failed"`. |
| `SEALER_LOG_NO_LOG`, `SEALER_LOG_ENTRY_FOUND`, `SEALER_LOG_NOT_SEALERS`, `SEALER_LOG_OTHER_VERSION`, `SEALER_LOG_MISSING`, `SEALER_LOG_NOT_CONSULTED` | The `sealer_log` values: `"no-log"`, `"entry-found"`, `"not-sealer-log"`, `"unverifiable-other-version"`, `"missing"`, `"not-consulted"`. |
| `TIMESTAMP_TRUST_ANCHORED`, `TIMESTAMP_TRUST_UNANCHORED`, `TIMESTAMP_TRUST_ABSENT`, `TIMESTAMP_TRUST_INVALID` | The `timestamp_trust` values: `"anchored"`, `"unanchored"`, `"absent"`, `"invalid"`. |

`MerkleLog(db_path, *, root_version=None, read_only=False)` and
`open_merkle_log(uri)` (a SQLite path or a Postgres DSN) are importable from
`novafabric.trust.novaseal` too, but are not in its `__all__`.

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

Every parameter after `capsule_dir` is keyword-only. `value_type` is a
`ScoreValueType` or its string (`boolean`, `categorical`, `numeric`). Remaining
parameters: `source` (a `ScoreSource` or its string; `"code"` default, or `human` /
`heuristic` / `judge`), `subject_kind` (`"span"` default), `run_id`,
`significance` (an ADR-0080 `SignificanceBlock`), and `db_path` (registry
override for score-config lookup). `eval_card_digest` defaults to `None` in the
signature but is required: omitting it raises `SubmissionInvalidError`.

Validation is **fail-closed** — on any rejection nothing is written, and a named
exception is raised (`SubmissionInvalidError`, `CapsuleNotFoundError`,
`CapsuleUnusableError`, `SubjectNotFoundError`, `SupersedesNotFoundError`,
`IdempotencyConflictError`, `ScoreConfigViolation` — all importable from
`novafabric.scores`). All but `ScoreConfigViolation` subclass
`ScoreSubmissionError`, so catch both to handle every rejection. Corrections never
edit a prior record: submit a *new* score whose `supersedes` names the prior
`score_id`; both lines stay in the log. CLI equivalent: `nova score submit`.

| Name | What it is |
|---|---|
| `submit_request(capsule_dir, request, *, db_path=None) → SubmitResult` | The shared core `submit()` calls: validate a `ScoreSubmissionRequest` against the capsule and append it. |
| `ScoreSubmissionRequest` | The submission envelope (the HTTP POST body and the SDK argument shape), a Pydantic model. |
| `SubmitResult` | `score`, `idempotent_replay`, `config_bound` (see the example). |
| `Score` | One evidence-grade score, one `scores.jsonl` line. |
| `ScoreSource`, `ScoreValueType` | `str` enums: `human` / `heuristic` / `code` / `judge`; `boolean` / `categorical` / `numeric`. |
| `SignificanceBlock` | Optional ADR-0080 significance context carried on a score. |
| `ScoreSubmissionError` | Base class of the rejections above. Nothing was written. |
| `CapsuleUnusableError` | The capsule holds a symlink, or a file swapped for a FIFO or device. |

---

## REST client

**Maturity: experimental (ADR-0202 P1, shipped 2026-07-24).** A typed, sync
httpx client for the multi-tenant `nova server` REST API (`/v0`,
`api/openapi.yaml`). This is **server-mode tooling**: every local-first
feature above works without it, and importing it performs no I/O — the client
sends no request other than the ones you invoke (no telemetry, no update
checks). It mirrors the TypeScript SDK's contract (ADR-0194) and adds one
operation TS does not have yet: **capsule upload**.

### `novafabric.client.NovaFabricClient(base_url=None, *, api_key=None, token=None, timeout=None, retries=None, transport=None)`

`base_url=None` falls back to `NOVAFABRIC_SERVER_URL` (see *Config resolution*
below). The client is a context manager; outside a `with` block call `close()`.

| Method | Returns |
|---|---|
| `health()` | `ApiResult[ServerHealth]` |
| `upload_capsule(source: Path \| bytes)` | `ApiResult[CapsuleSummary]` |
| `get_capsule(run_id)` | `ApiResult[CapsuleDetail]` |
| `list_capsules(*, limit=None, cursor=None)` | `ApiResult[Page[CapsuleSummary]]` |
| `iter_capsules(*, limit=None)` | `Iterator[CapsuleSummary]` |
| `get_asset(asset_id)` | `ApiResult[AssetDetail]` |
| `list_assets(*, limit=None, cursor=None, asset_type=None, status=None)` | `ApiResult[Page[AssetSummary]]` |
| `iter_assets(*, limit=None, asset_type=None, status=None)` | `Iterator[AssetSummary]` |
| `submit_score(run_id, score: ScoreSubmission \| dict)` | `ApiResult[ScoreSubmissionResult \| None]` |
| `close()` | `None` |

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

Contract points (normative spec: `design/spec/python-client-v0.md` (private)):

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

Other names in `novafabric.client.__all__`:

| Name | What it is |
|---|---|
| `ApiResult`, `ResponseMeta` | `ApiResult(data, meta)`; `ResponseMeta` is the per-response metadata described above. |
| `Page` | One page of a cursor listing: `items`, `next_cursor`, `total`. |
| `CapsuleSummary`, `CapsuleDetail`, `AssetSummary`, `AssetDetail`, `ServerHealth` | Response models (Pydantic v2, `extra="allow"`). |
| `ScoreSubmission`, `ScoreSubmissionResult` | The score POST body and its `201` body. |
| `RetryConfig(max_attempts=3, backoff_base=0.5, backoff_factor=2.0, backoff_max=8.0, jitter="full", retry_statuses=(429, 502, 503, 504))` | The retry policy; `max_attempts=1` disables retrying. |
| `TokenProvider` | `Callable[[], str]`, the type of a refreshing `token=`. |
| `NovaFabricClientError` | Base class of every error the client raises, including `NovaFabricConfigError`, `NovaFabricAPIError` and `NovaFabricTransportError`. |
| `AuthenticationError`, `AuthorizationError`, `NotFoundError`, `ConflictError`, `PreconditionFailedError`, `ValidationFailedError`, `RateLimitedError`, `ServerError`, `NovaFabricTimeout` | The typed errors listed under *Errors*. |
| `reset_deprecation_warnings()` | Test helper: clear the warn-once registry for `Deprecation` headers. |

An async twin (`AsyncNovaFabricClient`) and artifact streaming are **planned**
(ADR-0202 P2); SSE and an OTLP endpoint helper are **future design** (P3).

---

## Compliance-export provenance (experimental — ADR-0197)

Field-group compliance exporters tag each field-group with an `evidence_source`
marker so a consumer can tell how NovaFabric established each value. The shared
primitive lives in `novafabric.compliance.export.provenance`:

```python
from novafabric.compliance.export.provenance import (
    EvidenceSource,      # operator_asserted | capsule_verified | unverifiable
    EvidenceSourceRef,   # re-performable ref: capsule_id + content_digest (+ seal path)
    build_capsule_ref,   # hash a capsule dir into an EvidenceSourceRef
    mark,                # validate a (source, ref) pairing
    validate_marked,     # assert a set of field-groups is honestly marked
)

# capsule_verified MUST carry a re-performable reference (ADR-0197 I-3):
ref = build_capsule_ref(capsule_dir, verified_at="2026-07-24T00:00:00Z")
source, ref = mark(EvidenceSource.capsule_verified, ref=ref)

# Enforce on an export path — raises UnmarkedFieldGroupError / MissingReperformableRefError:
from novafabric.compliance.export import AnnexIVExporter
doc = AnnexIVExporter().build_annex_iv_document("dep-1", capsule_dir)
validate_marked(doc.completeness_summary())
```

Signatures: `build_capsule_ref(capsule_dir, verified_at, *, files=None,
seal_envelope_path=None) → EvidenceSourceRef | None` (`None` when the capsule has
no manifest, so nothing may be marked `capsule_verified`);
`mark(source, *, ref=None) → tuple[EvidenceSource, EvidenceSourceRef | None]`;
`validate_marked(entries, *, require=True) → None`; and
`source_for_status(status, *, gap_states=frozenset()) → EvidenceSource`, the
marker for a pure-projection field-group: a status naming a checked gap (such as
`missing` or `not_evidenced`) maps to `unverifiable`, every other status to
`operator_asserted`. `ProvenanceError` (a `ValueError` subclass) is the base class
of `UnmarkedFieldGroupError` and `MissingReperformableRefError`; `mark()` raises it
directly for a non-verified source that carries a `ref`.

`operator_asserted` and `unverifiable` must **not** carry a ref (a reference
would misrepresent them as a re-performed verification). The marker is additive
and optional on the wire — a pre-ADR-0197 document deserializes with
`evidence_source is None`. See `design/spec/evidence-source-provenance-marker.md` (private).

---

## Utility

```python
from novafabric.capture import new_ulid, new_span_id

run_id = new_ulid()      # str — ULID (26 chars, time-sortable)
span_id = new_span_id()  # str — 16-char hex
```

### Log-level helpers

```python
from novafabric.capture import (
    LOG_LEVELS,            # ("debug", "info", "warn", "error")
    LOG_LEVEL_SOURCES,     # ("framework", "span-status", "adapter", "user")
    InvalidLogLevelError,  # ValueError subclass
    normalize_log_level,
    resolve_log_level,
)

normalize_log_level("WARNING")   # "warn"; CRITICAL/FATAL → "error", TRACE → "debug"
resolve_log_level(framework="info", span_status="ERROR")  # the most severe source wins
```

`normalize_log_level(value) → str` is case-insensitive and raises
`InvalidLogLevelError` for any value that does not map onto the four levels.
`resolve_log_level(framework=None, span_status=None, adapter=None, user=None)`
returns a `ResolvedLogLevel` (the level and the source it came from) or `None` when
no source supplies a level; `span_status` is an OTel span status (`ERROR` / `OK` /
`UNSET`), the others are level names. On a tie the higher-priority source in
`LOG_LEVEL_SOURCES` order is recorded.

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
| Validate a spec | `validate_spec` | `nova validate` |
| Sign / verify a capsule | `NovaSeal` | `nova seal` / `nova verify` |
| Talk to a NovaFabric server from Python | `NovaFabricClient` (experimental) | — |

**Where to go next**

- **CLI reference** (`docs/cli-reference.md`) — every `nova` subcommand, flag,
  and default. Each Python API above has a CLI twin; when in doubt, run
  `nova <command> --help`.
- **Developer guide** (`docs/developer-guide.md`) — how to add asset types, CLI
  commands and report formats. Third-party capture hooks (**experimental**) register
  a `HookProtocol` class under the `novafabric.hooks` entry-point group; the
  developer guide does not cover them yet — the contract is the
  `novafabric.capture.hooks._plugin` module docstring (RFC-0001).
- **The five primitives** — Asset Registry, Run Capsule, Replay, Lineage, and
  Evidence Bundle. Each has a public JSON Schema; the capsule is the source of
  truth and the registry/lineage graph are rebuildable indexes derived from it.
