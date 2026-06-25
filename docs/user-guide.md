# NovaFabric User Guide

This guide covers every shipped top-level `nova` command with context,
examples, and practical guidance. It is a reference you return to after
the [getting started guide](getting-started.md) has given you a working first capsule.

The guide is organized by workflow area, not alphabetically. Use the table of
contents to jump to what you need.

**Both `nova` and `novafabric` refer to the same binary.** All examples use `nova`.

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
6. [Non-Python client capture](#non-python-client-capture)
   - [nova mcp-proxy (experimental)](#nova-mcp-proxy-experimental)
   - [nova api-proxy (experimental)](#nova-api-proxy-experimental)
7. [Asset registry](#asset-registry)
   - [nova register](#nova-register)
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

---

## Capture

### nova capture

The core capture command. Wraps any command and records its execution as a
replayable run capsule. No changes to your code are required.

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
exits 1 will cause `nova capture` to exit 1 — but the capsule is still written
with `status: failure` and the full artifacts.

On success:

```
✓ Capsule written: .novafabric/capsules/01HXAY7M5JZ8R7K4P9DPBYK2WX  (run_id=01HXAY7M5JZ8R7K4P9DPBYK2WX)
```

**What gets captured.** The following transports are hooked automatically. If
the library is not installed, the hook is silently skipped:

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
`presence_penalty`, `response.id`, and `finish_reasons`.

---

### SDK decorator (in-process capture)

Use `@agent` when you own the entry point and want to capture without a
subprocess wrapper. The decorator installs capture hooks before calling the
function and removes them after (even on exception).

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

See `examples/minimal-agent-run/agent.py` for a complete working example.

---

### Runners

`nova capture` delegates subprocess execution to a **runner**. Choose one with
`--runner`:

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

Pass runner-specific options with `--runner-option key=value` (repeatable).

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
The job manifest enforces `privileged: false`, `hostNetwork: false`, `backoffLimit: 0`.

**`slurm`.** Runs the workload as a SLURM batch job via `sbatch`. Required option:
`partition`. Optional: `account`, `qos`, `time`, `nodes`, `gres`, `mem`,
`constraint`. The capsule directory **must be on a shared filesystem** (NFS,
Lustre, GPFS) visible from every compute node — the runner trusts the shared FS
rather than rsyncing artifacts.

```bash
export NOVAFABRIC_SLURM_SHARED_DIR=/home/vagrant  # shared path
nova capture --runner slurm \
  --runner-option partition=gpu \
  --output-dir $NOVAFABRIC_SLURM_SHARED_DIR/runs \
  python train.py
```

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

---

## Inspect and validate

### nova validate

Validates a capsule directory, an asset YAML spec, or a replay result directory
against their JSON schemas.

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

The command routes automatically based on what `path` contains.

Use in CI to gate on capsule integrity:

```bash
nova capture python agent.py
nova validate .novafabric/capsules/$(ls -t .novafabric/capsules/ | head -1)
```

---

### nova scan-secrets

Read-only inspection of a capsule's `redaction-proof.json`. Reports what the
secret scanner found during capture. Does not modify the capsule.

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

---

## Replay and diff

### nova replay

Replay a captured run from its capsule. Four modes available.

**Forensic mode (recommended as a first step):**

```bash
nova replay .novafabric/capsules/01HX.../ --mode forensic
```

No subprocess is launched, no network calls are made. NovaFabric reads the
manifest, traces, and model calls directly from the capsule. Safe to run in
any context — including on capsules received from colleagues or downloaded from
an artifact store.

**Semantic mode** (read-only similarity analysis):

```bash
nova replay .novafabric/capsules/01HX.../ --mode semantic
```

Computes pairwise text similarity across model call responses using
`difflib.SequenceMatcher`. Returns a `similarity_score` (0.0–1.0). No
subprocess, no network. Useful for checking whether a run's responses are
internally consistent or vary significantly across calls.

**Exact mode** (eligibility check):

```bash
nova replay .novafabric/capsules/01HX.../ --mode exact
```

Checks whether the capsule meets the requirements for byte-exact replay:
`env.lock.lock_mode=deterministic` and a `seed` field on every model call.
Returns `exact_eligible` (bool) and a list of `exact_reasons` if not eligible.
No subprocess, no network. Remote LLMs are almost never exact-eligible.

**Mocked mode:**

```bash
nova replay .novafabric/capsules/01HX.../ --mode mocked
```

The original command is re-spawned as a subprocess. All LLM calls are
intercepted and served from the capsule cache in the order they were recorded.
Tool calls are denied by default; enable them explicitly with safety flags:

```bash
nova replay .novafabric/capsules/01HX.../ --mode mocked \
  --allow-readonly             # permit read-only tool calls
  --allow-mutating             # also permit idempotent-write and non-idempotent-write
  --allow-external-side-effects  # also permit external-side-effect tools
  --allow-unknown-mutation     # also permit unclassified tools
```

**Dry-run** (see what would happen without running):

```bash
nova replay .novafabric/capsules/01HX.../ --dry-run
```

Results land in `.novafabric/replays/<replay-ulid>/replay_result.yaml`. Use
`--output-dir` to change the base directory.

**Mode comparison:**

| Mode | Re-executes command | LLM calls | Tool calls | Output |
|---|---|---|---|---|
| `forensic` | No | From capsule (read-only) | Not re-executed | Inspection report |
| `semantic` | No | Read-only analysis | Not re-executed | `similarity_score` (0–1.0) |
| `exact` | No | Read-only analysis | Not re-executed | `exact_eligible` + `exact_reasons[]` |
| `mocked` | Yes | From capsule cache | Gated by safety ladder | Replay result |

---

### nova diff

Structurally compare two run capsules. Aligned field-by-field: model calls
(by span id), tool calls (by tool name + argument hash), environment, and
output files (by content hash).

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

---

## Lineage graph

Every `nova capture` run automatically writes `lineage.jsonl` into the capsule.
Three edge types are inferred:

| Edge type | Source | Meaning |
|---|---|---|
| `consumed` | `assets.jsonl` entries | The run used this asset |
| `produced_by` | Output files in `capsule.yaml` | This artifact was produced by the run |
| `replayed_from` | Replay metadata | This run is a replay of another |

The edges are also indexed into a local SQLite graph at
`~/.novafabric/registry.db` (same file as the asset registry). The database
is a rebuildable cache — if you lose it, run `nova lineage import <runs-dir>`.

---

### nova lineage provenance

What did this run (or asset, or artifact) depend on?

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

```bash
nova lineage blast-radius local:datasets/training-set@1.0.0
nova lineage blast-radius <run-id> --depth 2
```

This is the primary "impact analysis" query. Run it before updating a shared
dataset to see what downstream runs depend on it.

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
reconstructing what was known at a point in time, for audits or incident reviews.

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

---

### nova lineage emit-openlineage

Emit capsule runs as [OpenLineage](https://openlineage.io) 2.0.2 events.
Enables integration with data catalog tools such as Marquez, Atlan, and
OpenMetadata.

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

---

## Trust layer

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
pass `--allow-unsafe-skips`.

---

### nova export-evidence

Build a signed Evidence Bundle ZIP from a capsule. The bundle is a
self-contained, verifiable archive: it embeds the capsule, the lineage
subgraph, in-toto attestation statements, signatures, and all JSON schemas.
A reviewer can verify the bundle with nothing but `sha256sum` and an ed25519
verifier — no NovaFabric runtime required.

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

Note: `--sigstore` (keyless Sigstore signing) is planned but not implemented.
Passing `--sigstore` exits 1 with an explanation.

---

### nova verify (v0.10)

Verify a capsule's cryptographic seal — DSSE signature, RFC 3161 timestamp, and
Merkle log inclusion. Requires NovaSeal configuration (ADR-0041). **experimental** (v0.10+)

**One-time setup** — generate a local signing key and create `~/.novafabric/novaseal.yaml`:

```bash
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 \
  -out ~/.novafabric/seal.key
openssl req -new -x509 -key ~/.novafabric/seal.key -days 365 \
  -out ~/.novafabric/seal.crt -subj "/CN=NovaSeal-Local"
```

```yaml
# ~/.novafabric/novaseal.yaml
profile: local
key_path: ~/.novafabric/seal.key
cert_path: ~/.novafabric/seal.crt
tsa_url: https://freetsa.org/tsr   # empty string to skip timestamping
merkle_db: ~/.novafabric/novaseal-merkle.db
```

**Sealed capture** — once `novaseal.yaml` exists, every `nova capture` automatically
seals the resulting capsule:

```bash
nova capture python my_agent.py
# ✓ Capsule written: .novafabric/capsules/01HXAY7M5JZ8R7K4P9DPBYK2WX (run_id=01HXAY7M5JZ8R7K4P9DPBYK2WX)
# Sealing runs silently in the background; a warning is shown if signing fails.
```

**Verify a sealed capsule:**

```bash
nova verify .novafabric/capsules/01HXAY7M5JZ8R7K4P9DPBYK2WX/
```

Output (all checks pass):

```
NovaSeal verification: 01HXAY7M5JZ8R7K4P9DPBYK2WX
  ✓ Signature (DSSE ECDSA P-256): OK
  ✓ Timestamp (RFC 3161): OK
  ✓ Merkle log inclusion: OK

signature_ok=True, timestamp_ok=True, log_integrity_ok=True
```

Exit 0 on full pass, 1 on any failure. Unsigned capsules exit 1 with a clear
"not sealed" message — this is informational, not an error.

**Options:**
- `--seal-config PATH` — path to `novaseal.yaml` (env: `NOVAFABRIC_SEAL_CONFIG`)

**What the `.seal/` bundle contains:**

| File | Contents |
|---|---|
| `manifest.dsse` | DSSE envelope: base64url(capsule JSON) + ECDSA P-256 signature + embedded cert |
| `manifest.dsse.tsr` | RFC 3161 TSR from the TSA, raw DER binary (empty if TSA skipped) |
| `log-entry.json` | Merkle log entry: `leaf_index`, `leaf_hash`, `root_hash`, `tree_size` |

**v0.1 scope:** local ECDSA P-256 key only. Sigstore keyless and cloud KMS are **planned** for v0.2. See [ADR-0041](../design/adr/0041-novaseal-cryptographic-core-adoption.md).

---

## Non-Python client capture

For agents that do not run in Python — Claude Code, Cursor, Continue.dev,
Node.js, Go, Rust — NovaFabric provides two transparent proxy commands that
capture LLM API calls without modifying the client.

### nova mcp-proxy (experimental)

A transparent proxy that sits between an MCP client and an upstream MCP server,
recording every `tools/call` request/response pair into a capsule.

Two transport modes are supported:

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

The asset registry is a local SQLite database at `~/.novafabric/registry.db`
(override: `NOVAFABRIC_DB_PATH`). It tracks AI assets — models, agents,
prompts, tools, datasets, evaluation suites, and deployment endpoints — with
lifecycle statuses and promotion history.

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

Analyze captured run capsules and suggest assets to register. Inverts the onboarding
workflow: capture first, then let NovaFabric propose what to register from observed
evidence (models seen, tools called, agent command).

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

# Stale asset detection (v0.12)
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

`nova promote` is a sub-group with three commands (v0.13.0). Valid lifecycle
transitions:

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

#### nova promote propose + nova promote approve

Maker-checker two-step flow for regulated deployments. Requires two
cryptographically distinct identities (different Ed25519 keypairs):

```bash
# Step 1 — maker proposes (signs with their keypair)
nova promote propose fraud-detection-model@1.2.0 --to staging

# Step 2 — checker approves (must be a different identity/key)
nova promote approve fraud-detection-model@1.2.0 --identity checker-alice
```

SoD is enforced at the cryptographic level: the approver's key fingerprint
must differ from the proposer's. Attempting to self-approve raises
`SoDViolationError`. Ed25519 keypairs are auto-generated at
`~/.novafabric/keys/{identity}.ed25519` on first use.

**Opt-in Rego gate.** Load `maker_checker_gate.rego` to block
`nova promote direct` to `staging`/`production` and require the two-step
flow. See [ADR-0058](../design/adr/0058-maker-checker-dual-approval.md).

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

---

### nova report

Generate an asset inventory report. Defaults to Markdown on stdout.

```bash
nova report                        # Markdown to stdout
nova report --format json          # JSON to stdout
nova report --output report.md     # write to file
```

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
lineage graph. The CLI remains the canonical interface; the dashboard is a
read-oriented satellite. All dashboard mutations display the equivalent `nova`
command and are logged to `~/.novafabric/dashboard-audit.jsonl`.

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

The terminal prints a URL with an embedded one-shot session token:

```
Dashboard: http://127.0.0.1:4321/?token=<token>
API docs:  http://127.0.0.1:4321/api/docs?token=<token>
```

Click the URL to open the dashboard in your browser. The token is also
written to `~/.novafabric/.serve-token` (mode 0600) and rotated on every
restart.

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--experimental` | (required) | Acknowledges the experimental gate |
| `--port` | `4321` | TCP port |
| `--host` | `127.0.0.1` | Bind address; localhost only without `--insecure` |
| `--capsule-dir` | `./.novafabric/runs/` | Where to look for capsules |
| `--db-path` | `~/.novafabric/registry.db` | Registry/lineage SQLite path |
| `--no-browser` | off | Do not auto-open a browser tab |

**Capsule index (Scale-S3, v0.36.0):**

`nova serve` keeps a `runs_cache` SQLite index so the Runs tab stays fast
regardless of how many capsule directories are on disk. The index is built on
startup and refreshed every 2 s by `CapsuleWatcher`. If capsules were added
while the server was stopped, force a full re-index:

```bash
nova ingest-capsule --all                       # re-index everything
nova ingest-capsule <run_id>                    # index a single capsule
nova ingest-capsule --watch                     # foreground watcher loop
```

Two watcher backends: `PollingBackend` (default, zero extra deps) and
`WatchdogBackend` (`pip install novafabric[watch]`; uses inotify/FSEvents).
Override with `NOVA_WATCHER_BACKEND=watchdog` and `NOVA_WATCHER_INTERVAL=<seconds>`.

**What the dashboard covers (v0.13):**

- **Runs tab** — list, search, and filter capsules; status filter pill-bar; hover copy-run-ID; inspect file tree; validate schema; view secret scan results; replay (forensic / dry-run); redact; export evidence. Multi-select up to 5 for N-run diff.
- **Registry tab** — list and inspect registered assets; register; run evals; promote (`direct` sub-command); bulk-promote checkbox; compare two spec versions (diff table); eval trend sparkline.
- **Evidence tab** — list signed bundles; in-browser ed25519 verify; full server-side cryptographic verify (DSSE + RFC 3161 + NovaSeal Merkle).
- **Holds tab** — place and release legal holds on registries; view all active holds with duration and reason; sidebar count badge.
- **Lineage tab** — interactive DAG rendered with React Flow; provenance, blast-radius, and replay-chain highlight modes; ancestry breadcrumb; click or double-click a node to select it (double-click no longer zooms).
- **Diff tab** — structural diff; 2-run or N-run (up to 5) comparison; stacked collapsible cards in N-run mode; URL-persistent `?run_ids=a,b,c` for sharing.
- **Audit tab** — unified mutation audit log (every dashboard write action with equivalent `nova` command); action-type filter.
- **Policy tab** — interactive OPA/Rego policy tester; ALLOW/DENY badge; explain toggle for full OPA trace output.
- **Infra tab** — 10 component status cards (NovaSeal, Collector, Object Store, Metadata DB, Lineage, Parent/Child, Server, Eval, Policy, Capture).
- **Commands tab** — 35 live command builders across 4 journey tracks with copy buttons.
- **Home tab** — staleness indicator (amber border on resume cards > 24 h).
- **Capture tab** — recent capsules panel with "Open folder" links for local paths.

**What requires the CLI.** Some operations are intentionally CLI-only:
`nova report`, lineage time-travel, OpenLineage emission, `nova mcp-proxy`,
`nova api-proxy`, cluster runners, mocked replay, semantic/exact replay,
`nova promote propose/approve` (maker-checker). See
[docs/dashboard.md](dashboard.md) for the full capability matrix.

**Security model:**
- Localhost only by default
- One-shot session token required on every `/api/*` request
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
| `NOVAFABRIC_CAPSULE_DIR` | — | Set by `nova capture` in the subprocess; used by hook loader and proxy commands |
| `NOVAFABRIC_SPAN_ID` | — | Root OTel span id injected into the subprocess by the orchestrator |
| `NOVAFABRIC_SLURM_SHARED_DIR` | — | Shared filesystem path for SLURM live tests (ops / CI use) |
| `OPENLINEAGE_URL` | — | HTTP endpoint for automatic OpenLineage emission at capture time |
| `OPENLINEAGE_FILE` | — | File path for OpenLineage emission (fallback if `OPENLINEAGE_URL` not set) |
| `NOVAFABRIC_DASHBOARD_AUDIT_FILE` | `$NOVAFABRIC_HOME/dashboard-audit.jsonl` | Audit log destination for dashboard mutations. Overrides `NOVAFABRIC_HOME` for this path only. |
| `NOVAFABRIC_CAPSULE_DIR` | — | Capsule storage directory. Set to redirect captures to a shared path (e.g. a shared NFS mount). |

---

## See also

- [Getting Started](getting-started.md) — narrative walkthrough from install to first capsule
- [Concepts](concepts.md) — capsule structure, replay modes, lineage edge types
- [Local Dashboard](dashboard.md) — full capability matrix vs CLI, security model
- [Python API](python-api.md) — programmatic usage
- [Architecture](../design/architecture/overview.md) — how the subsystems fit together
- [Writing a hook plugin](integrations/writing-a-hook-plugin.md) — extend capture to new transports
- [Tutorials](tutorials/README.md) — all tutorials: getting started, why NovaFabric, capture internals, multi-agent, cluster scale, Langfuse comparison
- [ROADMAP.md](../ROADMAP.md) — what is planned for v0.7 and beyond
