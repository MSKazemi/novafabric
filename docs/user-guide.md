# NovaFabric User Guide

This guide is the working reference for the shipped `nova` command surface:
what each command does, how to invoke it, and when to reach for it. It picks up
where the [getting started guide](getting-started.md) leaves off — that guide
walks you from install to your first capsule; this one is what you return to
once you are capturing, replaying, diffing, and auditing real runs.

## What you will learn

- How to **capture** any command as a portable, schema-valid, secret-redacted
  [Run Capsule](concepts.md) — with no changes to your application code.
- How to **inspect and validate** a capsule, and how to gate CI on capsule
  integrity and on the secret scanner's findings.
- How to **replay** a captured run in each of the four honest modes
  (`forensic`, `semantic`, `exact`, `mocked`) and **diff** two runs
  structurally as a regression gate.
- How to query the **lineage graph** — provenance, blast-radius, replay-chain,
  time-travel — and emit OpenLineage events to a data catalog.
- How to use the **trust layer** — redaction and signed Evidence Bundles an
  auditor can verify offline with only `sha256sum` and an ed25519 verifier.
- How to capture **non-Python clients** through the transparent MCP and API
  proxies, and how to manage assets through the local **Asset Registry**.
- Which capabilities are **shipped and work today**, and which are **planned**
  design intent that you should not depend on yet.

NovaFabric is local-first: every command below runs entirely inside your own
infrastructure — laptop to cluster, online or air-gapped, with no accounts and
no telemetry. The five primitives it is built on — **Asset Registry**,
**Run Capsule**, **Replay**, **Lineage**, and **Evidence Bundle** — map
directly onto the workflow areas in this guide.

The guide is organized by workflow area, not alphabetically. Use the table of
contents to jump to what you need.

> **Both `nova` and `novafabric` refer to the same binary.** All examples use `nova`.

> **Maturity.** Nearly all commands documented here ship today but carry
> `experimental` maturity: they work, but on-disk formats and interface details
> may change before the v1.0 schema freeze. Anything labeled **planned** or
> **future design** is documented intent only and is **not** implemented — do
> not build on it. See [ROADMAP.md](../ROADMAP.md) for sequencing, and
> [What shipped experimental in v0.59](#what-shipped-experimental-in-v059) for
> the newest command groups.

---

## Table of contents

1. [Capture](#capture)
   - [nova capture](#nova-capture)
   - [SDK decorator (in-process capture)](#sdk-decorator-in-process-capture)
   - [Runners](#runners)
   - [Customizing the URL registry](#customizing-the-url-registry)
2. [Inspect and validate](#inspect-and-validate)
   - [nova validate](#nova-validate)
   - [nova scan-secrets](#nova-scan-secrets)
   - [Searching run content (experimental)](#searching-run-content-experimental)
3. [Replay and diff](#replay-and-diff)
   - [nova replay](#nova-replay)
   - [nova diff](#nova-diff)
4. [Lineage graph](#lineage-graph)
   - [nova lineage provenance](#nova-lineage-provenance)
   - [nova lineage blast-radius](#nova-lineage-blast-radius)
   - [nova lineage replay-chain](#nova-lineage-replay-chain)
   - [nova lineage time-travel](#nova-lineage-time-travel)
   - [nova lineage import](#nova-lineage-import)
   - [nova lineage emit-openlineage](#nova-lineage-emit-openlineage)
5. [Trust layer](#trust-layer)
   - [nova redact](#nova-redact)
   - [nova export-evidence](#nova-export-evidence)
   - [Cryptographic sealing (experimental)](#cryptographic-sealing-experimental)
6. [Non-Python client capture](#non-python-client-capture)
   - [nova mcp-proxy (experimental)](#nova-mcp-proxy-experimental)
   - [nova api-proxy (experimental)](#nova-api-proxy-experimental)
7. [Asset registry](#asset-registry)
   - [nova register](#nova-register)
   - [nova suggest-register](#nova-suggest-register)
   - [nova list](#nova-list)
   - [nova inspect](#nova-inspect)
   - [nova promote](#nova-promote)
   - [nova rollback](#nova-rollback)
   - [nova capture — asset status gate](#nova-capture--asset-status-gate)
   - [nova eval](#nova-eval)
   - [nova report](#nova-report)
8. [Local dashboard](#local-dashboard)
   - [nova serve (experimental)](#nova-serve-experimental)
9. [Environment variables](#environment-variables)
10. [What shipped experimental in v0.59](#what-shipped-experimental-in-v059)
11. [What shipped in v0.75–v0.94](#what-shipped-in-v075v094)
12. [Summary and next steps](#summary-and-next-steps)

---

## Capture

Capture is the entry point to everything else. It turns a command — a script, an
agent, an HPC training run, a notebook cell — into a **Run Capsule**: a
directory identified by a time-sortable ULID that holds every observable fact of
one execution. A capsule is the unit you later replay, diff, trace, and export.

### nova capture

The core capture command. Wraps any command and records its execution as a
replayable Run Capsule. No changes to your code are required.

```bash
nova capture python train.py --lr 0.001
nova capture python my_agent.py
nova capture -- python -c "import sys; sys.exit(1)"
```

Capsules are written to `.novafabric/capsules/<ulid>/` by default. Use
`--output-dir` to change the base directory:

```bash
nova capture --output-dir /mnt/experiment-runs python train.py
```

Use `--` to separate `nova capture` flags from commands that contain their
own flags:

```bash
nova capture -- python script.py -v --config cfg.yaml
```

NovaFabric's exit code mirrors the wrapped command's exit code. A command that
exits 1 will cause `nova capture` to exit 1 — but **the capsule is still written
on failure**, with `status: failure`, an `error` block, and the full artifacts.
Capture never silently drops a run: success and failure are both evidence.

On success:

```
✓ Capsule written: .novafabric/capsules/01HXAY7M5JZ8R7K4P9DPBYK2WX  (run_id=01HXAY7M5JZ8R7K4P9DPBYK2WX)
```

**What gets captured.** The following transports are hooked automatically via a
`sitecustomize.py` injected over `PYTHONPATH` — no SDK decorator, no import in
your code. If the library is not installed, its hook is silently skipped, and a
capsule is still written even when no AI SDK is present at all:

| Transport | What is recorded |
|---|---|
| `openai` SDK | `chat.completions.create` calls → `model-calls.jsonl` |
| `anthropic` SDK | `messages.create` calls → `model-calls.jsonl` |
| `httpx` | Requests to URL-registry-classified hosts → `model-calls.jsonl` |
| `requests` | Same classification as `httpx`; covers LangChain, LlamaIndex, `boto3` |
| `aiohttp` | Async wire-level capture; covers async LangChain, FastAPI agents |
| `urllib3` | Lowest-tier wire-level capture; covers `boto3`/Bedrock |
| `mcp` SDK | `ClientSession.call_tool` invocations → `tool-calls.jsonl` |
| Plugins | Any class in the `novafabric.hooks` entry-point group (experimental) |

The `requests` and `urllib3` hooks use a shared layering guard so a single
`requests.post()` produces exactly one record even though `requests` calls
`urllib3` internally.

**OTel GenAI semantic conventions.** Every model-call record follows the
[OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/).
All "Required when applicable" fields are extracted: `temperature`, `top_p`,
`top_k`, `max_tokens`, `stop_sequences`, `seed`, `frequency_penalty`,
`presence_penalty`, `response.id`, and `finish_reasons`. The `temperature`,
`top_p`, and `seed` fields are what make a later `exact` replay eligibility
check possible (see [nova replay](#nova-replay)).

**Export the run as portable OTel spans (experimental, NF-032/033).** Because those
semconv attributes are already stored, `--emit-otel-genai` maps the finished capsule
*outward* to OTel GenAI `gen_ai.*` spans (a root `invoke_agent` span, a `chat` client
span per model call, an `execute_tool` span per tool call) written to
`<capsule>/otel-genai-spans.json`. Message content is **off by default** (ADR-0021); add
`--capture-content` to include request messages, routed through the same secret-redaction
gate as capture and size-bounded:

```bash
nova capture --emit-otel-genai python my_agent.py
```

**Secrets are redacted before the capsule is finalized.** A secret scanner
rewrites matches in place as `[REDACTED:rule-id]` and writes
`redaction-proof.json` before the capsule closes. A capsule that lacks this
proof is invalid to `nova validate` and cannot be exported as an Evidence
Bundle — redaction is a precondition of trust, not an afterthought.

---

### SDK decorator (in-process capture)

Use `@agent` when you own the entry point and want to capture without a
subprocess wrapper. The decorator installs capture hooks before calling the
function and removes them afterward — even if the function raises.

```python
from novafabric.sdk.agent import agent

@agent(name="research-agent", version="0.1.0", capsule_dir="capsules/")
def run():
    # openai, anthropic, httpx calls made here are auto-captured
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Hello"}],
    )
    return response

run()
```

The capsule structure is identical to `nova capture`. Without `capsule_dir`,
the decorator emits OTel spans only — no capsule is written. This is the
original v0.1 observability mode and is still useful if you only need traces.

**Optional record-only tags (experimental, v0.59).** The decorator also accepts
`deployment_environment=` (ADR-0126), `variant=` (ADR-0116, A/B attribution),
and `session_id=`/`session_sequence=` (ADR-0122, multi-turn sessions). Each is
recorded verbatim as an additive optional manifest field, never inferred; the
matching CLI flags and `NOVAFABRIC_*` env vars take precedence. See
[Python API: SDK decorator](python-api.md#sdk-decorator).

See `examples/minimal-agent-run/agent.py` for a complete working example.

**When to use which.** Prefer `nova capture` for anything you can invoke as a
command — it needs zero code changes and works for non-Python subprocesses.
Reach for the `@agent` decorator only when you own the Python entry point and
want capture to live inside the process (for example, inside a long-running
service where spawning a subprocess is awkward).

---

### Runners

`nova capture` delegates subprocess execution to a **runner**. The runner
abstraction is what lets the same capture command target a laptop, a container,
a Kubernetes cluster, or a SLURM batch queue without changing your workload.
Choose one with `--runner`:

```bash
nova capture --runner local python agent.py    # default: runs on this machine
nova capture --runner docker \
  --runner-option image=myorg/agent-runtime:latest \
  python agent.py
nova capture --runner kubernetes \
  --runner-option image=myorg/agent-runtime:latest \
  --runner-option namespace=ml-jobs \
  python agent.py
nova capture --runner slurm \
  --runner-option partition=gpu \
  python train.py
```

Pass runner-specific options with `--runner-option key=value` (repeatable). All
runners are non-privileged by design — see the enforcement notes per runner.

**`local` (default).** Runs the workload as a local subprocess. Equivalent to
the pre-v0.6 behavior. No additional setup required.

**`docker`.** Runs the workload inside a container. Required option: `image`.
The image must already have NovaFabric installed. The capsule directory is
mounted as a volume at `/novafabric/capsule`; artifacts land on the host
filesystem.

```bash
nova capture --runner docker \
  --runner-option image=myorg/agent-runtime:abc1234 \
  --runner-option network=bridge \
  --runner-option user=1000:1000 \
  python my_agent.py
```

Optional options: `network`, `workdir`, `user`, `extra_volumes`, `extra_env`.
The runner enforces no `--privileged`, no docker-socket mount, no host
namespace defaults.

**`kubernetes`.** Runs the workload as a Kubernetes `Job` via `kubectl`. Required
options: `image`, `namespace`. Optional: `service_account`, `node_selector`,
`resources`. Capsule artifacts are pulled back via `kubectl cp` after completion.
The job manifest enforces `privileged: false`, `hostNetwork: false`,
`backoffLimit: 0` — no root, no host namespace, no silent retries.

**`slurm`.** Runs the workload as a SLURM batch job via `sbatch`. Required option:
`partition`. Optional: `account`, `qos`, `time`, `nodes`, `gres`, `mem`,
`constraint`. The capsule directory **must be on a shared filesystem** (NFS,
Lustre, GPFS) visible from every compute node — the runner trusts the shared FS
rather than rsyncing artifacts. This is what makes the shared-filesystem
registry usable as a laptop↔cluster handoff protocol.

```bash
export NOVAFABRIC_SLURM_SHARED_DIR=/home/vagrant  # shared path
nova capture --runner slurm \
  --runner-option partition=gpu \
  --output-dir $NOVAFABRIC_SLURM_SHARED_DIR/runs \
  python train.py
```

> **Prototype.** Multi-node distributed runs recorded as a **parent/child
> capsule tree** under a single `global_run_id` shipped as a prototype in
> v0.10+ (ADR-0039; `nova run new-run-id / validate-distributed / show /
> lineage` — see the [CLI reference](cli-reference.md)). It is implemented and
> tested but not yet validated at target scale. Each single-node `nova capture`
> still produces one capsule with one writer; a one-node run is the degenerate
> case of the distributed model.

---

### Customizing the URL registry

The wire-level hooks (`httpx`, `requests`, `aiohttp`, `urllib3`) classify
outbound URLs against a vendored YAML registry. Default coverage includes
OpenAI, Anthropic, Cohere, Together, Mistral, Replicate, AWS Bedrock, and
Ollama on `localhost:11434`.

To add a private endpoint or a provider not in the default list, create
`~/.novafabric/url_registry.yaml`. A user-override file replaces the
vendored default entirely — copy any entries you still want from the default
before editing.

```yaml
schema_version: "0.1.0"
patterns:
  - match: "internal-llm.example.com"
    gen_ai_system: "internal-prod"
    transport: "http"
  - match: "api.openai.com"
    gen_ai_system: "openai"
    transport: "http"
  - match: "api.anthropic.com"
    gen_ai_system: "anthropic"
    transport: "http"
```

To override per-invocation:

```bash
NOVAFABRIC_URL_REGISTRY=/path/to/registry.yaml nova capture python agent.py
```

Because the registry is a local, editable file and never hardcodes a hosted
endpoint, you can point every classified call at a private mirror or proxy —
NovaFabric requires no connection to any NovaFabric-operated service.

---

## Inspect and validate

Once you have a capsule, the first thing to do is confirm it is well-formed and
free of leaked secrets. Both checks are CI-friendly and exit non-zero on
failure, so you can wire them into a pipeline.

### nova validate

Validates a capsule directory, an asset YAML spec, or a replay result directory
against their JSON schemas. The command routes automatically based on what
`path` contains.

**Capsule validation:**

```bash
nova validate .novafabric/capsules/01HXAY7M5JZ8R7K4P9DPBYK2WX/
# ✓ Valid capsule: 01HXAY7M5JZ8R7K4P9DPBYK2WX  status=success
```

Checks `capsule.yaml`, `env.lock`, `redaction-proof.json`, `lineage.jsonl`
(if present), and the presence of all required files and directories.

**Asset spec validation** (see [Asset registry](#asset-registry)):

```bash
nova validate my-model.yaml
# ✓ Valid model spec: my-model@1.0.0
```

**Replay result validation:**

```bash
nova validate .novafabric/replays/01HXB.../
# ✓ Valid replay result: 01HXB...  mode=forensic  status=success
```

**Use in CI to gate on capsule integrity:**

```bash
nova capture python agent.py
nova validate .novafabric/capsules/$(ls -t .novafabric/capsules/ | head -1)
```

---

### nova scan-secrets

Read-only inspection of a capsule's `redaction-proof.json`. Reports what the
secret scanner found during capture. Does not modify the capsule — to re-run the
scanner with different strategies, use [nova redact](#nova-redact).

```bash
nova scan-secrets .novafabric/capsules/01HX.../
```

Clean output:

```
✓ 01HXAY7M5JZ8R7K4P9DPBYK2WX: no findings
```

With findings:

```
! 01HXAY7M5JZ8R7K4P9DPBYK2WX: 2 findings (critical=1, high=1, medium=0, low=0)
rule_id          severity  target           strategy
openai-api-key   critical  model-calls.jsonl  mask
pii-email        high      outputs/stdout.txt  mask
```

Use `--fail-on <severity>` to gate CI on severity level:

```bash
nova scan-secrets <capsule> --fail-on critical    # exits 2 on critical findings
nova scan-secrets <capsule> --fail-on high        # exits 2 on critical or high
```

Exit codes: `0` (clean or below threshold), `1` (missing proof / bad input),
`2` (threshold exceeded).

Use `--json` to emit the full `redaction-proof.json` as parseable JSON to
stdout — useful for piping into `jq` or a CI reporting tool.

### Searching run content (experimental)

**Status: experimental** (ADR-0204, first slice). `nova search` finds runs by
what the agent actually said or did — not just by run id or command:

```bash
nova search "invoice INV-2291"        # which run mentioned this invoice?
nova search "rm -rf"                  # where did an agent run this? (literal)
nova search "429" --status failure    # failed runs that saw a rate limit
nova search --reindex                 # one-time backfill of older capsules
```

The index lives in the local registry DB (SQLite FTS5, zero new
dependencies) and only ever contains **post-redaction** capsule text — the
same four files the secret scanner redacts before a capsule is complete.
New capsules are indexed automatically at ingest (`nova serve` /
`nova ingest-capsule`); run `nova search --reindex` once to backfill
capsules captured before this feature. Capsules captured at `minimal`
capture level simply contribute fewer (or no) content rows. All query terms
must match (AND); a trailing `*` does prefix search; FTS5 operators like
`OR`/`NEAR`/`-` are matched literally. The same search is available in the
dashboard API via `GET /api/runs/search?scope=content`. Set
`NOVA_CONTENT_INDEX=off` to disable indexing. Postgres server-mode parity
is **planned**; indexing the extended event streams is **planned** (ADR-0204
P2 — the ADR-0209 scanner-coverage prerequisite has shipped; the corpus
stays pinned to the four v0 streams until the P2 slice bumps it). See the
[CLI reference](cli-reference.md#nova-search-experimental-adr-0204).

---

## Replay and diff

This is the core of "replayable AI infrastructure." Tracing tells you what
happened; replay tells you whether a past run can be re-executed under controlled
conditions, and diff tells you exactly what changed between two runs. A replay is
itself a new capsule — so you can diff a replay against its original.

### nova replay

Replay a captured run from its capsule, in one of **four honest, falsifiable
modes**. NovaFabric deliberately does **not** claim byte-exact replay of remote
LLM calls; the mode you choose reflects how much determinism the run actually
supports.

**Forensic mode (recommended as a first step):**

```bash
nova replay .novafabric/capsules/01HX.../ --mode forensic
```

No subprocess is launched, no network calls are made. NovaFabric reads the
manifest, traces, and model calls directly from the capsule. Safe to run in
any context — including on capsules received from colleagues or downloaded from
an artifact store. This is the mode for audit and post-incident review.

**Semantic mode** (read-only similarity analysis):

```bash
nova replay .novafabric/capsules/01HX.../ --mode semantic
```

Computes pairwise text similarity across model call responses using
`difflib.SequenceMatcher`. Returns a `similarity_score` (0.0–1.0). No
subprocess, no network. Useful for checking whether a run's responses are
internally consistent or vary significantly across calls — the pragmatic answer
when the upstream LLM is remote and non-deterministic.

**Exact mode** (eligibility check):

```bash
nova replay .novafabric/capsules/01HX.../ --mode exact
```

Checks whether the capsule meets the requirements for byte-exact replay:
`env.lock.lock_mode=deterministic` and a `seed` field on every model call.
Returns `exact_eligible` (bool) and a list of `exact_reasons` if not eligible.
No subprocess, no network. Remote LLMs are almost never exact-eligible; this
mode is for local / on-prem / compliance runs where determinism is controllable.

**Mocked mode:**

```bash
nova replay .novafabric/capsules/01HX.../ --mode mocked
```

The original command is re-spawned as a subprocess. All LLM calls are
intercepted and served from the capsule cache in the order they were recorded.
Tool calls are denied by default and gated by a five-rung safety ladder; enable
each rung explicitly:

```bash
nova replay .novafabric/capsules/01HX.../ --mode mocked \
  --allow-readonly             # permit read-only tool calls
  --allow-mutating             # also permit idempotent-write and non-idempotent-write
  --allow-external-side-effects  # also permit external-side-effect tools
  --allow-unknown-mutation     # also permit unclassified tools
```

This is the mode for CI and regression testing: re-run the recorded command
against its cached responses and diff the result.

**Dry-run** (see what would happen without running):

```bash
nova replay .novafabric/capsules/01HX.../ --dry-run
```

Results land in `.novafabric/replays/<replay-ulid>/replay_result.yaml`. Use
`--output-dir` to change the base directory.

**Mode comparison:**

| Mode | Re-executes command | LLM calls | Tool calls | Output | Primary use |
|---|---|---|---|---|---|
| `forensic` | No | From capsule (read-only) | Not re-executed | Inspection report | Audit / post-incident |
| `semantic` | No | Read-only analysis | Not re-executed | `similarity_score` (0–1.0) | Drift detection |
| `exact` | No | Read-only analysis | Not re-executed | `exact_eligible` + `exact_reasons[]` | Determinism / compliance check |
| `mocked` | Yes | From capsule cache | Gated by safety ladder | Replay result | CI / regression |

---

### nova diff

Structurally compare two Run Capsules. Aligned field-by-field: model calls
(by span id), tool calls (by tool name + argument hash), environment, and
output files (by content hash). This is what turns "it worked yesterday, fails
today" into a precise, mechanical answer.

```bash
nova diff .novafabric/capsules/01HX.../ .novafabric/capsules/01HY.../
```

Sample output:

```
Diff: 01HX... → 01HY...
  changed=1  added=0  removed=0
Outputs:
  ~ outputs/stdout.txt
```

**Use as a CI gate.** `--assert-no-regressions` exits 1 if any changes are
detected — wire it into CI to catch behavioral regressions before they reach
production:

```bash
nova diff cap-a/ cap-b/ --assert-no-regressions
```

**Output formats:**

```bash
nova diff cap-a/ cap-b/ --output-format json               # machine-readable
nova diff cap-a/ cap-b/ --output-format github-annotation  # for PR checks
```

**Asset spec diff.** If both arguments contain `@`, the command falls through
to the asset registry diff (see [nova inspect](#nova-inspect)):

```bash
nova diff my-model@1.0.0 my-model@1.1.0
```

**Group by recorded A/B variant (ADR-0116).** `--group-by variant` labels the
diff as cross-arm or within-arm using each capsule's recorded
`(experiment_id, variant_id)` — read-only, capsule paths only, text/json output
only:

```bash
nova diff --group-by variant runs/arm-a/ runs/arm-b/
```

**Statistical regression gate (experimental, NF-007).** `--significance` runs a
Wald SPRT test over stored boolean-outcome scores instead of a structural diff —
a distinct mode with no positional capsule refs. It reports Wilson confidence
intervals for both sides and exits non-zero on a statistically significant
regression:

```bash
nova diff --significance --baseline base/ --candidate cand/ --metric task_pass
```

**A common pattern** is capture → replay → diff: capture a baseline, replay it
in `mocked` mode after a code change, and diff the replay against the baseline
under `--assert-no-regressions`.

---

## Lineage graph

Replay and diff answer questions about individual runs; lineage answers
questions *across* runs — what a run consumed, what it produced, and what would
be affected if an upstream artifact changed.

Every `nova capture` run automatically writes `lineage.jsonl` into the capsule.
Three edge types are inferred:

| Edge type | Source | Meaning |
|---|---|---|
| `consumed` | `assets.jsonl` entries | The run used this asset |
| `produced_by` | Output files in `capsule.yaml` | This artifact was produced by the run |
| `replayed_from` | Replay metadata | This run is a replay of another |

The edges are also indexed into a local SQLite graph at
`~/.novafabric/registry.db` (same file as the asset registry). **The database
is a rebuildable cache** — if you lose it, run `nova lineage import <runs-dir>`.
The `lineage.jsonl` files inside the capsules are the source of truth; the graph
is a derived index.

---

### nova lineage provenance

What did this run (or asset, or artifact) depend on? Walks ancestors.

```bash
nova lineage provenance <run-id>
nova lineage provenance registry:my-dataset@1.0.0
nova lineage provenance <run-id> --depth 3
nova lineage provenance <run-id> --output json
```

Options:
- `--depth N` — maximum traversal depth (default: 5)
- `--kind run|asset|artifact` — filter to a specific node kind
- `--output text|json` — output format (default: text)

---

### nova lineage blast-radius

If this asset or artifact changes, which runs would need to be re-evaluated?
Walks descendants — the inverse of provenance.

```bash
nova lineage blast-radius local:datasets/training-set@1.0.0
nova lineage blast-radius <run-id> --depth 2
```

This is the primary "impact analysis" query. Run it before updating a shared
dataset to see what downstream runs depend on it, or during incident response to
scope what a bad artifact touched.

Options: `--depth`, `--kind`, `--output` (same as `provenance`).

---

### nova lineage replay-chain

Show the replay ancestry of a run, back to the original captured run.

```bash
nova lineage replay-chain <replay-run-id>
```

Useful when you have several generations of replays and want to trace which
original capture they all stem from.

Options: `--output text|json`.

---

### nova lineage time-travel

Show the lineage state of a node as of a specific timestamp. Useful for
reconstructing what was known at a point in time, for audits or incident reviews
("what did the dependency graph look like on the day of the incident?").

```bash
nova lineage time-travel <run-id> --asof 2026-05-01T00:00:00Z
nova lineage time-travel registry:my-dataset@1.0.0 --asof 2026-04-15T12:00:00Z
```

Options: `--kind`, `--output` (same as `provenance`).

---

### nova lineage import

(Re-)index lineage from capsule directories into the local SQLite graph. Use
this to backfill runs captured before the lineage graph was set up, or to
rebuild the index after moving the database.

```bash
nova lineage import .novafabric/capsules/01HX.../    # single capsule
nova lineage import .novafabric/capsules/            # all capsules in the directory
```

The graph is fully derivable from `lineage.jsonl` files in the capsule
directories — losing the SQLite index is not data loss.

> **Scale note.** The SQLite lineage graph is the local-mode default and is
> designed for graphs below roughly 1M edges. A KuzuDB-backed v2 tier for
> larger graphs shipped as **experimental** in v0.10+ (`nova lineage-store
> migrate` / `profile`; see the [lineage migration guide](lineage/migration-guide.md)).
> Postgres/Apache AGE backends and the billion-edge federation tier remain
> **future design**, not implemented.

---

### nova lineage emit-openlineage

Emit capsule runs as [OpenLineage](https://openlineage.io) 2.0.2 events
(START / COMPLETE / FAIL). Enables integration with data catalog tools such as
Marquez, Atlan, and OpenMetadata.

```bash
# Print to stdout
nova lineage emit-openlineage .novafabric/capsules/01HX.../ --output -

# Post to an HTTP endpoint
nova lineage emit-openlineage .novafabric/capsules/ --output http://marquez:5000/api/v1/lineage

# Write to a file
nova lineage emit-openlineage .novafabric/capsules/ --output events.jsonl
```

If `--output` is omitted, the target is resolved from `OPENLINEAGE_URL` →
`OPENLINEAGE_FILE` → stdout.

You can also trigger automatic emission at capture time by setting
`OPENLINEAGE_URL` before running `nova capture` — the orchestrator will post
events without an explicit `emit-openlineage` call.

**Custom run facets (experimental, NF-036/037).** Add `--with-facets` to attach
NovaFabric's custom facets to the COMPLETE event — `novafabric_capsule` (capsule/run id
and hash), `novafabric_eval` (verdict + suite + metrics), `novafabric_policy` (promotion
gate decision), and the standard `executionParameters` facet. Add `--otel-correlation`
(implies `--with-facets`) to also attach `novafabric_otel_correlation` (`trace_id` /
`span_id`) so a lineage node links to its OTel GenAI spans. Facets are additive and
schema-validated before emission, so a consumer that ignores them still sees unchanged
core OpenLineage events:

```bash
nova lineage emit-openlineage .novafabric/capsules/01HX.../ --with-facets --otel-correlation
```

> **Note:** these OpenLineage run facets are a different thing from the capsule
> `facets` container (ADR-0196, experimental) — the additive `facets.<domain>`
> evidence objects that record-only modules such as `settlement`, `embodied`, and
> `science` attach to a capsule. Same word, two unrelated places; see
> [Evidence facets](concepts.md#evidence-facets-experimental) in Concepts.

---

### Graph analytics (experimental — ADRs 0212–0215)

Beyond traversal, NovaFabric can turn the whole lineage graph into insight —
all read-only, local-first, and deterministic:

```bash
# Structurally critical nodes: hubs + single points of failure
nova lineage metrics --top 10

# Rank the upstream root-cause suspects for a failed run
nova lineage root-cause 01HX...

# Export the graph for Gephi / yEd / Neo4j
nova lineage export-graph --format cypher -o lineage.cypher

# One synthesized report (hubs, communities, orphans, health, cost)
nova insights --output markdown -o insights.md
```

`metrics` ranks by degree / PageRank / betweenness and flags articulation
points; `root-cause` combines error signals, recency, edge confidence, and
cross-run failure correlation and never fabricates a culprit when there is no
signal; `export-graph` emits byte-stable GraphML/GEXF/Cypher; `insights`
composes all of it plus seeded Louvain communities and best-effort cost, and
reports any unavailable data source as unavailable rather than inventing it.
See the [CLI reference](cli-reference.md#nova-lineage-metrics) for full flags.

### nova lineage consume (experimental)

Runs the JetStream `LineageConsumer` as a foreground daemon: it pulls
NovaFabric lineage events off a NATS subject and bulk-COPYs derived edges into
a KuzuDB graph on a size-or-time flush trigger. This is the cluster-scale
ingestion path alongside the local `nova lineage import` — local-mode capture
never requires it; use it only when events already flow through a NATS
deployment rather than sitting in capsule directories on a shared filesystem.
Requires `pip install 'novafabric[scale,scale-kg]'` (nats-py + kuzu) and a
running NATS JetStream server.

```bash
nova lineage consume --nats-url nats://nats:4222 --kuzu-path .nova/kg/lineage.kuzu
```

Options include `--subject` (default `novafabric.lineage.>`), `--batch-size`,
`--fetch-timeout`, `--flush-batch-size`, and `--flush-interval-s` (max seconds
between flushes even below the batch-size threshold). **Not exactly-once:** a
NATS message is acked once its edges are extracted, before the flush actually
persists them — see the
[CLI reference](cli-reference.md#nova-lineage-consume-experimental-cluster-scale)
for the full flag list and delivery-semantics detail.

## Trust layer

The trust layer is what makes a capsule shareable and auditable: verifiable
redaction of secrets, and a signed Evidence Bundle a reviewer can verify offline
with no NovaFabric runtime. This is the compliance-supporting surface of the
product — it produces evidence that *supports* compliance workflows; it does not
certify or guarantee compliance.

### nova redact

Re-scan a capsule and update its `redaction-proof.json`. Use this if you want
to re-run the scanner with a different strategy, or if you received a capsule
with potentially unscanned secrets.

**Re-scan with default strategies** (mask in-place):

```bash
nova redact .novafabric/capsules/01HX.../
```

**Re-scan with strategy overrides.** Choose `mask` (default), `hash`
(SHA-256 first-8 hex chars), or `drop` (remove the field entirely):

```bash
nova redact <capsule> --strategy-override openai-api-key:hash
nova redact <capsule> --strategy-override pii-email:drop
```

**Mark a finding as an acknowledged false positive:**

```bash
nova redact <capsule> --mark-unsafe-skip <finding-id> --rationale "test fixture — not a real key"
```

False positives recorded in `unsafe_skips` are preserved across future
re-scans unless you clear them:

```bash
nova redact <capsule> --clear-unsafe-skips
```

Capsules with `unsafe_skips` entries block `nova export-evidence` unless you
pass `--allow-unsafe-skips` — so an acknowledged false positive is always a
deliberate, recorded decision, never a silent one.

---

### nova export-evidence

Build a signed **Evidence Bundle** ZIP from a capsule. The bundle is a
self-contained, verifiable archive: it embeds the capsule, the lineage
subgraph, in-toto attestation statements, ed25519 signatures, and all JSON
schemas. A reviewer can verify the bundle with nothing but `sha256sum` and an
ed25519 verifier — **no NovaFabric runtime required.** This is the primitive
that makes a capsule portable audit evidence you own.

**Generate a signing key** (one time):

```python
from pathlib import Path
from novafabric.evidence.signing import generate_keypair
generate_keypair(Path("./keys"))
# writes keys/ed25519.pem (private) and keys/ed25519.pub.pem (public)
```

**Build the bundle:**

```bash
nova export-evidence <capsule> \
  --key ~/.novafabric/keys/ed25519.pem \
  --output evidence.zip
```

**Options:**

- `--key PATH` (required) — PEM-encoded ed25519 private key
- `--output PATH` (required) — output ZIP path
- `--allow-unsafe-skips` — export even if `redaction-proof.json` contains acknowledged false positives

**Bundle layout:**

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
│   ├── *.sig
│   └── *.cert
├── schemas/                            # all JSON schemas vendored
└── README.md                           # human-readable verification recipe
```

> **Note.** `--sigstore` (keyless Sigstore signing) is **planned** but not
> implemented. Passing `--sigstore` exits 1 with an explanation.

---

### Cryptographic sealing (experimental)

> **Status: experimental — shipped in v0.10+, interfaces may change before the
> v1.0 schema freeze.**
>
> The **NovaSeal** in-process signing core is implemented and tested: DSSE
> signing (ECDSA P-256), a best-effort
> [RFC 3161](https://www.rfc-editor.org/rfc/rfc3161) trusted timestamp, and an
> append-only SQLite Merkle log, verified by `nova verify <capsule>` — which
> reports `signature_ok`, `timestamp_ok`, and `log_integrity_ok`. Sealing is
> opt-in via `~/.novafabric/novaseal.yaml`; maker-checker seal flows are
> `nova seal propose / approve / verify` (ADR-0059). See the
> [NovaSeal configuration guide](novaseal-configuration.md) and the
> [CLI reference](cli-reference.md#nova-verify-capsule).

**Still planned** (ADR-0041 design intent, not shipped): the dedicated,
network-hardened NovaSeal signing *service*, qualified timestamps,
Sigstore-keyless signing as the default path, and WORM-backed retention at the
seal layer. For the most stable portable proof today, use the
[Evidence Bundle](#nova-export-evidence) above — ed25519-signed bundles with
in-toto DSSE attestations and verifiable redaction proofs, verifiable offline.

---

## Non-Python client capture

For agents that do not run in Python — Claude Code, Cursor, Continue.dev,
Node.js, Go, Rust — NovaFabric provides two transparent proxy commands that
capture LLM API calls without modifying the client. Both write into the same
capsule schemas as the in-process hooks.

### nova mcp-proxy (experimental)

A transparent proxy that sits between an MCP client and an upstream MCP server,
recording every `tools/call` request/response pair into a capsule.

Two transport modes are supported.

**Stdio mode (default):** wraps the upstream MCP server process.

```bash
NOVAFABRIC_CAPSULE_DIR=/path/to/.novafabric/runs/01HX...
nova mcp-proxy -- npx -y @modelcontextprotocol/server-filesystem /tmp
```

Or with an explicit flag:

```bash
nova mcp-proxy \
  --capsule-dir .novafabric/runs/01HX.../ \
  -- /usr/local/bin/my-mcp-server --flag
```

If `--capsule-dir` is omitted and `NOVAFABRIC_CAPSULE_DIR` is not set, a fresh
capsule directory is allocated automatically under `.novafabric/runs/`.

**Claude Desktop integration.** Replace the upstream server entry in
`claude_desktop_config.json`:

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

**HTTP mode (v0.6.3):** listens on a TCP port and forwards POST/SSE to an
upstream HTTP MCP server.

```bash
nova mcp-proxy \
  --listen 127.0.0.1:8765 \
  --upstream-url http://upstream-mcp.internal:9000/mcp
```

Stdio and HTTP modes are mutually exclusive per invocation. One proxy
invocation handles one upstream server.

Records use the same `tool-calls.jsonl` schema as the SDK hook, plus an
extension field `extensions["io.novafabric.capture_method"] = "proxy"`.

**Status: experimental.** The API may change.

---

### nova api-proxy (experimental)

A transparent HTTP proxy for non-Python LLM clients. The client points its
`*_BASE_URL` environment variable at the proxy; the proxy forwards to the
upstream LLM API and records every call into `model-calls.jsonl`.

```bash
# Terminal 1: start the proxy
nova api-proxy \
  --upstream-url https://api.openai.com \
  --listen 127.0.0.1:8765

# Terminal 2: point your LLM client at the proxy
export OPENAI_BASE_URL=http://127.0.0.1:8765
claude ...     # Claude Code
# or: cursor, continue, any Node/Go/Rust LLM client
```

For Anthropic:

```bash
nova api-proxy \
  --upstream-url https://api.anthropic.com \
  --listen 127.0.0.1:8765

export ANTHROPIC_BASE_URL=http://127.0.0.1:8765
```

**Options:**

| Flag | Default | Description |
|---|---|---|
| `--upstream-url` | (required) | The LLM API base URL to forward to |
| `--listen` | `127.0.0.1:8765` | Proxy listener address as `host:port` |
| `--capsule-dir` | auto-allocated | Where to write `model-calls.jsonl`; falls back to `NOVAFABRIC_CAPSULE_DIR` |
| `--upstream-timeout` | `60.0` | Wall-clock timeout per upstream call (seconds) |

Streaming responses (Server-Sent Events) are consumed by the proxy and merged
into a synthesized non-streaming envelope for the record — the record captures
the full content even when the client received it incrementally.

The proxy is base-URL override only. No TLS MITM, no local CA.

**Status: experimental.** ADR-0026.

---

## Asset registry

The Asset Registry is NovaFabric's identity layer: a local SQLite database at
`~/.novafabric/registry.db` (override: `NOVAFABRIC_DB_PATH`) that tracks AI
assets — models, agents, prompts, tools, datasets, evaluation suites, and
deployment endpoints — with lifecycle statuses and promotion history. Each asset
is addressed as `name@version` and pinned to a git SHA at registration.

> **Promotion is governance metadata only.** Promoting an asset updates one DB
> record. It does **not** restart, deploy, or redeploy anything — it records a
> lifecycle decision, and (for agents) gates on a passing eval.

### nova register

Register an asset from a YAML spec file.

```bash
nova register my-model.yaml
nova register research-agent.yaml
```

**Model spec example:**

```yaml
novafabric_spec_version: "1"
asset_type: model
name: fraud-detection-model
version: 1.2.0
status: development
description: "XGBoost fraud detection model trained on transactions dataset"
spec:
  framework: xgboost
  artifact_path: s3://ml-artifacts/fraud-detection/v1.2.0
```

**Agent spec example:**

```yaml
novafabric_spec_version: "1"
asset_type: agent
name: support-triage-agent
version: v1.0.0
status: development
description: "LLM agent that triages incoming support tickets"
spec:
  model:
    provider: anthropic
    name: claude-haiku-4-5-20251001
    temperature: 0.1
  tools:
    - search_knowledge_base
    - create_ticket
  prompts:
    system: "You are a support triage assistant. Classify incoming tickets by priority."
  evals:
    - triage_accuracy_suite
```

Supported asset types and their required `spec` fields:

| `asset_type` | Spec fields |
|---|---|
| `model` | `framework`, `artifact_path` |
| `agent` | `model`, `tools`, `prompts`, `policies`, `evals`, `memory` |
| `prompt` | `template` |
| `tool` | `entrypoint` |
| `dataset` | `path` |
| `evaluation` | `suite_name` |
| `deployment` | `endpoint` |

Exits 0 on success, 1 on validation error or duplicate. Use `nova validate
<spec.yaml>` to validate without registering.

---

### nova suggest-register

Analyze captured Run Capsules and suggest assets to register. This inverts the
onboarding workflow: capture first, then let NovaFabric propose what to register
from observed evidence (models seen, tools called, agent command).

```bash
# Interactive: scan 10 most recent capsules, prompt per suggestion
nova suggest-register

# Write draft YAML files without registering
nova suggest-register --draft-only --output-dir ./drafts/

# Auto-register high-confidence suggestions (models + tools, skip agents)
nova suggest-register --auto --min-confidence 0.8 --skip-types agent

# Analyze a specific run by ID
nova suggest-register 01HXAY7M5JZ8R7K4P9DPBYK2WX
```

**Confidence levels:** models = 100% (every observed call), tools = 80% (min 2 calls),
agents = 70%. Post-capture, a one-line hint is printed when unregistered assets are
detected. Disable with `NOVAFABRIC_SUGGEST=0`.

---

### nova list

List registered assets. Filter by type or lifecycle status. Use `--stale` to surface
assets with no promotion, consumption, or eval activity in the last N days.

```bash
nova list
nova list --type agent
nova list --type model --status staging
nova list --status production

# Stale asset detection
nova list --stale                          # assets inactive > 30 days
nova list --stale --stale-days 60          # custom threshold
nova list --stale --status production      # stale production assets only
```

`--stale` output adds `last_activity_at` and `days_stale` columns. Activity includes
any promotion, capsule consumption (`assets.jsonl`), or eval run.

---

### nova inspect

Show full metadata for an asset. If version is omitted, shows the latest.

```bash
nova inspect fraud-detection-model@1.2.0
nova inspect support-triage-agent          # latest version
```

Output includes the full spec YAML, lifecycle status, git commit at
registration, and eval results for agent assets.

---

### nova promote

`nova promote` is a sub-group with three commands. Valid lifecycle transitions:

```
development → staging → production → archived
```

Direct transitions (e.g. `development → production`) are permitted. Once
`archived`, no further promotion is possible.

#### nova promote direct

Single-actor promotion (original behaviour):

```bash
nova promote direct fraud-detection-model@1.2.0 --to staging
nova promote direct fraud-detection-model@1.2.0 --to production
nova promote direct fraud-detection-model@1.2.0 --to archived
```

**Eval gate.** Agent assets require a passing `nova eval` result before
promotion to `staging` or `production`. Use `--force` to override
(requires interactive confirmation; recorded as forced in the audit trail):

```bash
nova promote direct support-triage-agent@v1.0.0 --to staging --force
```

**SLSA provenance on promotion (experimental).** Add `--slsa-provenance` to emit a
DSSE-signed `slsa.dev/provenance/v1` attestation for the promotion, and
`--slsa-ml-profile` to emit the **SLSA-for-ML** profile instead — its `buildDefinition`
captures dataset versions/hashes and seeds, and its byproducts bind the promoted model to
the exact gating eval verdict (NF-057). Both verify with `nova verify-envelope` or stock
`cosign`:

```bash
nova promote direct my-model@1.0.0 --to staging \
    --slsa-provenance --slsa-ml-profile --slsa-out my-model.slsa.json
```

#### nova promote propose + nova promote approve

Maker-checker two-step flow for regulated deployments. Requires two
cryptographically distinct identities (different Ed25519 keypairs):

```bash
# Step 1 — maker proposes (signs with their keypair)
nova promote propose fraud-detection-model@1.2.0 --to staging

# Step 2 — checker approves (must be a different identity/key)
nova promote approve fraud-detection-model@1.2.0 --identity checker-alice
```

Separation of duties is enforced at the cryptographic level: the approver's key
fingerprint must differ from the proposer's. Attempting to self-approve raises
`SoDViolationError`. Ed25519 keypairs are auto-generated at
`~/.novafabric/keys/{identity}.ed25519` on first use.

**Opt-in Rego gate.** Load `maker_checker_gate.rego` to block
`nova promote direct` to `staging`/`production` and require the two-step
flow. See [ADR-0058](./decisions.md).

---

### nova rollback

Roll back an asset to its most recent previous production version in one command.
Atomically archives the current production version and restores the previous one.
Designed for incident response — no manual registry queries needed.

```bash
nova rollback my-agent --actor on-call-eng
nova rollback my-agent --to v1.8.0 --actor on-call-eng   # explicit target
```

Steps performed in a single DB transaction:
1. Find the current `production` version (error if none).
2. Find the most recent prior `production` version (auto, or use `--to`).
3. Archive the current production version.
4. Promote the prior version back to `production`.
5. Write an audit log entry with a `rollback_reason` field.

If the discovered prior version is archived, the command errors and asks you to
supply `--to` explicitly. `--actor` is required and recorded in the audit trail.

---

### nova capture — asset status gate

Before spawning the subprocess, `nova capture` can verify that a named asset is in
the required lifecycle status. Nothing is written to disk if the check fails.

```bash
# Hard block — exits non-zero if my-agent@v1 is not in staging or production
nova capture --asset my-agent@v1 --require-asset-status staging,production -- python agent.py

# Soft warn — logs a warning but does not block
nova capture --asset my-agent@v1 --warn-if-asset-status development -- python agent.py

# Block if the asset is not registered at all (default: warn only)
nova capture --asset my-agent@v1 --require-asset-status production --require-registered -- python agent.py
```

This is an opt-in gate. Without `--asset`, capture behaviour is unchanged.

---

### nova eval

Run evaluation suites declared in an agent's spec and store the results in
the registry. Suites are resolved via the `novafabric.evals` Python entry-point
group.

```bash
nova eval support-triage-agent@v1.0.0
```

After a passing eval, `nova promote direct <name@version> --to staging` (or `--to production`) will
succeed without `--force`.

The bundled **standard eval suites** (GAIA, SWE-bench, AgentBench, MMLU, Smoke)
run in OCI-pinned containers, and promotion can be Rego-gated to block on a
regression against a recorded baseline.

**Guard against benchmark contamination (experimental, NF-028).** Contamination
silently inflates scores, so NovaFabric records the dataset + split content hashes an
eval ran against. Check a capsule against a configurable registry of known-bad hashes:

```bash
nova eval contamination-check ./my-capsule --registry known-bad.json --json
```

It reports a status per dataset (`current` / `superseded` / `contaminated` / `unknown`)
and **exits `4`** when any dataset is contaminated or superseded, so CI can gate on it.
Detection only — no remediation. See the [feature tour](tutorials/feature-tour.md#17-prove-supply-chain-provenance--eval-integrity)
for the dataset provenance and SLSA-for-ML surfaces that pair with it.

---

### nova report

Generate an asset inventory report. Defaults to Markdown on stdout.

```bash
nova report                        # Markdown to stdout
nova report --format json          # JSON to stdout
nova report --output report.md     # write to file
nova report --format html --output inventory.html   # self-contained HTML + by-type chart (ADR-0201)
nova report --format pdf --output inventory.pdf      # requires the optional WeasyPrint extra
```

`--format pdf` requires `--output` and the optional `pip install 'novafabric[compliance]'`
extra (WeasyPrint); without it the command exits 1 with an install hint.

Sample Markdown output:

```markdown
# NovaFabric Asset Report

## Summary
| Type | Count |
|---|---|
| model | 3 |
| agent | 2 |
...
```

---

## Local dashboard

### nova serve (experimental)

An opt-in local HTTP dashboard for browsing capsules, registry assets, and the
lineage graph. **The CLI remains the canonical interface**; the dashboard is a
read-oriented satellite. Every dashboard mutation displays the equivalent `nova`
command and is logged to `~/.novafabric/dashboard-audit.jsonl`, so nothing the
dashboard does is invisible to the CLI-first audit trail.

**Install the optional extra** (one time):

```bash
pip install 'novafabric[serve]'
```

This adds FastAPI and uvicorn (both Apache-2.0 / MIT licensed).

**Start the server:**

```bash
nova serve --experimental
```

The `--experimental` flag is mandatory and acknowledges that the dashboard
API may change.

The terminal prints a URL with an embedded session token:

```
Dashboard: http://127.0.0.1:4321/?token=<token>
API docs:  http://127.0.0.1:4321/api/docs?token=<token>
```

Click the URL to open the dashboard in your browser. The token is also
written to `$NOVAFABRIC_HOME/.serve-token` (mode 0600) — the start-up panel
prints the resolved path, which is `~/.novafabric/.serve-token` only when
`NOVAFABRIC_HOME` is unset.

The token is **not** rotated on every restart. `nova serve` reuses an existing
`.serve-token` file so that a restart does not break open browser sessions, so
a URL you were given earlier keeps working after a restart — and the token
stays readable on disk (mode 0600) after the server exits. Delete the file if
you want the next start to mint a fresh token.

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--experimental` | (required) | Acknowledges the experimental gate |
| `--port` | `4321` | TCP port |
| `--host` | `127.0.0.1` | Bind address; localhost only without `--insecure` |
| `--capsule-dir` | `./.novafabric/runs/` | Where to look for capsules |
| `--db-path` | `~/.novafabric/registry.db` | Registry/lineage SQLite path |
| `--no-browser` | off | Do not auto-open a browser tab |

**Capsule index.** `nova serve` keeps a `runs_cache` SQLite index so the Runs
tab stays fast regardless of how many capsule directories are on disk. The index
is built on startup and refreshed every 2 s by `CapsuleWatcher`. If capsules
were added while the server was stopped, force a full re-index:

```bash
nova ingest-capsule --all                       # re-index everything
nova ingest-capsule <run_id>                    # index a single capsule
nova ingest-capsule --watch                     # foreground watcher loop
```

Two watcher backends: `PollingBackend` (default, zero extra deps) and
`WatchdogBackend` (`pip install novafabric[watch]`; uses inotify/FSEvents).
Override with `NOVA_WATCHER_BACKEND=watchdog` and `NOVA_WATCHER_INTERVAL=<seconds>`.

**Navigating.** The 29 tabs are grouped in the sidebar under seven headings:
Overview · Runs & Debug · Govern & Promote · Provenance & Trust · Compliance ·
Platform · Reports & Export. Since v0.98.3 the groups **start collapsed**, each
showing its tab count — click a heading to expand it. The group holding the
tab you are on is always expanded, so your current location is never hidden,
and whatever you expand or collapse is remembered in that browser. Every tab is
deep-linkable as `?tab=<id>`, and the
Compliance tab is itself a hub whose five panel groups (Frameworks · Audits ·
Privacy · Exports · Assurance) are deep-linkable as `?sub=<group>`. Navigation
shortcuts are mnemonic two-key sequences — press `g` then the tab's letter
(`g h` Home, `g r` Runs, `g c` Compliance); press `?` for the full map, and
`⌘K` / `Ctrl-K` for the command palette. (v0.97.0 replaced the earlier
positional `1`–`9` keys with these sequences; tab ids and `?tab=` links are
unchanged.)

**What the dashboard covers:**

- **Runs tab** — list, search, and filter capsules; status filter pill-bar; hover copy-run-ID; inspect file tree; validate schema; view secret scan results; replay (forensic / dry-run); redact; export evidence. Multi-select up to 5 for N-run diff.
- **Registry tab** — list and inspect registered assets; register; run evals; promote (`direct` sub-command); bulk-promote checkbox; compare two spec versions (diff table); eval trend sparkline.
- **Evidence tab** — list signed bundles; in-browser ed25519 verify; full server-side cryptographic verify of the bundle.
- **Holds tab** — place and release legal holds on registries; view all active holds with duration and reason; sidebar count badge.
- **Lineage tab** — interactive DAG rendered with React Flow; provenance, blast-radius, and replay-chain highlight modes; ancestry breadcrumb; click or double-click a node to select it.
- **Diff tab** — structural diff; 2-run or N-run (up to 5) comparison; stacked collapsible cards in N-run mode; URL-persistent `?run_ids=a,b,c` for sharing.
- **Audit tab** — unified mutation audit log (every dashboard write action with equivalent `nova` command); action-type filter.
- **Policy tab** — interactive OPA/Rego policy tester; ALLOW/DENY badge; explain toggle for full OPA trace output.
- **Commands tab** — live command builders across journey tracks with copy buttons.
- **Home tab** — staleness indicator (amber border on resume cards > 24 h).
- **Capture tab** — recent capsules panel with "Open folder" links for local paths.

> The dashboard also surfaces a component-status view covering the
> cluster-scale subsystems (NovaSeal, collector, object store, metadata DB,
> parent/child capsules). These shipped in v0.10+ as **experimental /
> prototype** tiers — implemented and tested, but not validated at target
> scale. Check each component's maturity label in [ROADMAP.md](../ROADMAP.md)
> rather than treating presence in the status view as production readiness.

**What requires the CLI.** Some operations are intentionally CLI-only:
`nova report`, lineage time-travel, OpenLineage emission, `nova mcp-proxy`,
`nova api-proxy`, cluster runners, mocked replay, semantic/exact replay,
`nova promote propose/approve` (maker-checker). See
[docs/dashboard.md](dashboard.md) for the full capability matrix.

**Security model:**
- Localhost only by default (binds `127.0.0.1`)
- One-shot session token required on every `/api/*` request — either as
  `?token=<token>` or as an `Authorization: Bearer <token>` header (when the
  header is present it is authoritative)
- DNS-rebinding defence (`Host` header validated)
- CORS restricted to `localhost` and `127.0.0.1` origins
- Every write action is confirm-gated and audit-logged

Stop the server with `Ctrl+C`. The session token is invalidated on stop.

**Status: experimental.** The dashboard API (endpoint shapes, field names)
may change between minor versions.

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `NOVAFABRIC_HOME` | `~/.novafabric` | Root directory for all internal NovaFabric data. Set this once to redirect all files (registry.db, .serve-token, dashboard audit log) to a shared location. |
| `NOVAFABRIC_DB_PATH` | `$NOVAFABRIC_HOME/registry.db` | SQLite path for asset registry and lineage graph. Overrides `NOVAFABRIC_HOME` for this path only. |
| `NOVAFABRIC_URL_REGISTRY` | (bundled) | Override path for the wire-level URL classification registry |
| `NOVAFABRIC_CAPSULE_DIR` | — | Capsule storage directory. Set by `nova capture` in the subprocess; used by hook loader and proxy commands. Set explicitly to redirect captures to a shared path (e.g. a shared NFS mount). |
| `NOVAFABRIC_SPAN_ID` | — | Root OTel span id injected into the subprocess by the orchestrator |
| `NOVAFABRIC_SLURM_SHARED_DIR` | — | Shared filesystem path for SLURM live tests (ops / CI use) |
| `NOVAFABRIC_SUGGEST` | — | Set to `0` to disable the post-capture `suggest-register` hint |
| `OPENLINEAGE_URL` | — | HTTP endpoint for automatic OpenLineage emission at capture time |
| `OPENLINEAGE_FILE` | — | File path for OpenLineage emission (fallback if `OPENLINEAGE_URL` not set) |
| `NOVAFABRIC_DASHBOARD_AUDIT_FILE` | `$NOVAFABRIC_HOME/dashboard-audit.jsonl` | Audit log destination for dashboard mutations. Overrides `NOVAFABRIC_HOME` for this path only. |
| `NOVA_WATCHER_BACKEND` | `polling` | `nova serve` capsule-index watcher backend (`polling` or `watchdog`) |
| `NOVA_WATCHER_INTERVAL` | — | Poll interval in seconds for the capsule-index watcher |
| `NOVA_CONTENT_INDEX` | on | Set to `off` to disable content indexing at ingest (experimental, ADR-0204) |
| `NOVA_CONTENT_INDEX_MAX_DOC_KB` | `64` | Per-doc text cap for the content index (operator tuning) |
| `NOVA_CONTENT_INDEX_MAX_DOCS` | `10000` | Per-capsule doc cap for the content index (operator tuning) |

---

## Cost analytics (experimental)

Beyond the offline `nova cost estimate` and the ClickHouse-backed `nova cost
report` (both covered in the [CLI reference](cli-reference.md#nova-cost-report)),
three additional `nova cost` subcommands report **descriptive** cost evidence over
records you already hold. Each is **experimental**, read-only, and never a verdict —
none applies a threshold, quota, or pass/fail; whether a figure is acceptable is
the operator's call. Each also takes `--json`.

- **`nova cost attribute <runs.json>`** (ADR-0146, NF-148) — splits recorded spend
  into productive vs wasted outcomes with a per-status breakdown, from a
  `{runs: [{run_id, status, cost}], productive_statuses?}` document.
- **`nova cost fairness <totals.json>`** (ADR-0146, NF-150) — per-agent fairness
  ledger (each agent's share, Gini coefficient, max/mean ratio) over per-dimension
  resource totals (`cost` / `energy` / `calls`) in a `{totals: {dimension: {agent:
  total}}}` document.
- **`nova cost usage-breakdown <manifest.json>`** (ADR-0132) — token usage-type
  composition of a capsule (each usage type's share, cached-read ratio, reasoning /
  multimodal flags). Composition only — no cost, no verdict; honours "absent !=
  zero".

See the [CLI reference](cli-reference.md#nova-cost-attribute-experimental-adr-0146)
for flags, input shapes, and exit codes.

---

## What shipped experimental in v0.59

v0.59.0 added a large cohort of **experimental** command groups — first slices of
the Langfuse-parity ADRs (0112–0141) plus interop and forensics surfaces. All are
additive, off unless you opt in, and their interfaces may change. This guide does
not duplicate them; each is fully documented in the
[CLI reference](cli-reference.md), and the grouped overview lives in the
[v0.59.0 release notes](releases/v0.59.0.md):

| Area | Commands / surfaces (all experimental) |
|---|---|
| Prompt lifecycle | `nova prompt register/get/list/history/diff/compose/tree`, `nova label` (deployment labels + protected maker-checker moves) |
| Evaluation & annotation | `nova eval score config`, `nova annotate`, `nova score submit` (+ `novafabric.scores.submit`), `nova comment`, `nova experiment run/compare` |
| Capture completeness | `nova session new/add/list/show/replay`, `nova graph agent`, `nova capture --capture-media` + `nova media list`, `--environment`, variant attribution (`--experiment`/`--variant`), observation log levels, `nova validate --schemas` |
| Offline analytics | `nova query`, `nova view`, `nova trend`, per-usage-type token accounting, `nova pricing` + `nova cost estimate` |
| Governance | `nova retention plan/apply/status/explain`, PII masking plugins (`--masker`), the budget promotion gate (Rego), `nova events` webhooks, SCIM 2.0 provisioning, partial SAML SSO (metadata + assertion-validation policy; live assertion consumption shipped opt-in in v0.73.0 and is off by default) |
| Portability & interop | `nova export --html`, `nova export-blob` + manifest `nova verify`, OTLP GenAI-span ingest (`POST /api/otlp/v1/traces`), `nova eval import-inspect/export-inspect`, `nova diagnose --intervene`, `nova pii status` |

---

## What shipped in v0.75–v0.94

A second large cohort landed after v0.59, mostly ADR-0101 (attribution/root-cause)
and a set of standalone trust primitives. All are **experimental**; some are
CLI-exposed today, others are **Python-API only** — no `nova` subcommand wraps
them yet, so build against the module, not a shipped command:

| Capability | Status | Where |
|---|---|---|
| Counterfactual root-cause search (ADR-0101 NF-018/020) | **CLI**, experimental | `nova diagnose <run-id> --search-root-cause [--max-interventions N]` — sweeps ranked causal-root candidates with bounded intervention replays until one confirms |
| Causal-graph back-trace (ADR-0101 NF-019/022) | Python API only | `diagnose.causal_root_candidates()` — used internally by `--search-root-cause`; no direct CLI verb |
| Span-level claim-grounding audit (ADR-0101 NF-021) | Python API only | `diagnose.audit_claims()` — flags a model-span claim `ungrounded` when no tool-span evidence precedes it; not wired into `nova diagnose` output yet |
| NATS lineage consumer (cap-006, ADR-0061/0066/0219) | **CLI**, experimental | [`nova lineage consume`](#nova-lineage-consume-experimental) |
| EU AI Act Art. 12 record-keeping export | **CLI**, experimental | `nova euaiact export` / `nova euaiact status` |
| Compliance cohort (ISO 42001/42005, Art.72 PMM, GPAI Art.53, NIST GenAI/CSA) | **CLI**, experimental | `nova export-compliance {iso42001,pmm,gpai53,genai-profile}` |
| Portable agent-passport projection (ADR-0149) | **CLI**, experimental | `nova passport issue` / `nova passport verify` |
| SAML SSO (ADR-0138) | **CLI**, experimental, partial | `nova server saml-metadata` emits SP metadata only; assertion-consumption is opt-in server config, not a CLI verb |
| x509 certificate-pinned signing identity (ADR-0055) | Python API only | `trust/novaseal/x509_identity.py` |
| Crypto-agility hybrid-signature envelope (ADR-0072) | Python API only | `trust/novaseal/hybrid_signature.py` |
| `did:key` + Verifiable Credentials (ADR-0075) | Python API only | `trust/did.py` |
| "Acted-as" delegation chains (ADR-0106) | Python API only | `trust/delegation.py` |
| Transparency-log witness cosigning (ADR-0097) | Python API only | `trust/novaseal/witness.py` |
| Jurisdiction sovereignty site-seals (ADR-0077) | Python API only | `compliance/sovereignty.py` |

The Python-API-only rows above have no `nova` command and no dashboard panel —
they are real, tested modules (see the corresponding ADR for design intent),
but there is no shipped end-user workflow around them yet. Do not follow a
tutorial that shows a `nova` command for them; none exists. See
[Python API](python-api.md) and [`docs/developer-guide.md`](developer-guide.md)
for the module-level detail.

---

## Summary and next steps

You now have the full shipped `nova` command surface, organized around the five
primitives:

| Primitive | Commands |
|---|---|
| **Run Capsule** | `nova capture`, `@agent` decorator, runners, `nova validate`, `nova scan-secrets` |
| **Replay** | `nova replay` (forensic / semantic / exact / mocked), `nova diff` |
| **Lineage** | `nova lineage provenance / blast-radius / replay-chain / time-travel / import / emit-openlineage` |
| **Evidence Bundle** | `nova redact`, `nova export-evidence` |
| **Asset Registry** | `nova register`, `nova suggest-register`, `nova list`, `nova inspect`, `nova promote`, `nova rollback`, `nova eval`, `nova report` |

The strategic verb chain across them is **Capture → Seal → Replay → Diff →
Audit** (sealing is opt-in and `experimental` — see
[Cryptographic sealing](#cryptographic-sealing-experimental)). Everything above
runs locally today, offline, with no accounts and no telemetry.

**A good next move, depending on your goal:**

- **Wire a CI regression gate** — capture a baseline, replay in `mocked` mode
  after a change, and run `nova diff --assert-no-regressions`. Add
  `nova scan-secrets --fail-on high` as a second gate.
- **Produce audit evidence** — `nova redact` then `nova export-evidence`, and
  hand the ZIP to a reviewer who verifies it with only `sha256sum` and an
  ed25519 verifier.
- **Understand impact before a change** — run `nova lineage blast-radius` on the
  asset you are about to update.

**Where to go from here:**

- [Getting Started](getting-started.md) — narrative walkthrough from install to first capsule
- [Concepts](concepts.md) — capsule structure, replay modes, lineage edge types
- [Local Dashboard](dashboard.md) — full capability matrix vs CLI, security model
- [Python API](python-api.md) — programmatic usage
- [Architecture](./architecture.md) — how the subsystems fit together
- [Writing a hook plugin](integrations/writing-a-hook-plugin.md) — extend capture to new transports
- [Tutorials](tutorials/README.md) — getting started, why NovaFabric, capture internals, multi-agent, cluster scale, Langfuse comparison
- [ROADMAP.md](../ROADMAP.md) — what is planned, including the NovaSeal seal layer and cluster-scale tiers
