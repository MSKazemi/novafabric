# CLI Reference

Both `nova` and `novafabric` refer to the same binary.

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
- `--mark-provenance` — write a C2PA synthetic-content provenance marker (`c2pa-manifest.json`, with the `c2pa.ai.generated: true` EU AI Act Art.50 disclosure) into the capsule when the run produces model output. The marker is written before NovaSeal so it is covered by the capsule signature (ADR-0074). Opt-in; non-blocking. Example: `nova capture --mark-provenance python agent.py`
- `--fast-emit` — install capture hooks **lazily** in the workload subprocess (ADR-0092 slice B). The default path imports every present SDK (`openai`, `mcp`, `requests`, …) at startup purely to patch it — measured at ~717 ms for `openai`, ~340 ms for `mcp`, paid even if the workload never calls them. `--fast-emit` patches each SDK only if/when the workload itself imports it, so unused SDKs are never imported by capture. **Measured (warm-fs, orchestrator):** a compute-only workload **2068 ms → 464 ms (−78 %)**; an `import openai` workload **2223 ms → 1509 ms (−32 %)** — the saving scales inversely with SDK usage. Fidelity is unchanged. Runs in-process (not delegated to the warm daemon). Example: `nova capture --fast-emit python agent.py`
- `--emit-spool` — **experimental** (ADR-0092 slice C). Also write run-boundary EventEnvelope v1 records (`run.start`, `capsule.finalize`) to the local event spool (`$NOVAFABRIC_SPOOL_DIR`, default `$NOVAFABRIC_HOME/spool`) so the resident `novafabric-spool-forwarder` can drain and forward them to the collector tier over NATS JetStream. Off by default; fail-open; **edge-keyless** — signing happens at the hub, not here (hub-sign default). Runs in-process (not delegated to the warm daemon). Example: `nova capture --emit-spool python agent.py`

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
for the contract; the strategic context is [RFC-0001](../design/governance/RFC-0001-multi-vendor-strategy.md).

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

### nova api-proxy (v0.6.4)

Transparent HTTP proxy that sits between a non-Python LLM client and an
upstream provider, recording every request/response pair as a
`model-calls.jsonl` entry. Implements [ADR-0026](../design/adr/0026-api-proxy-promotion.md).

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
(realizes SI-2; extends [ADR-0020](../design/adr/0020-cluster-scale-low-overhead-capture.md)).
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
`--runner`, `--runner-option`, `--timeout`, `--asset`, `--mark-provenance`, or
`--output-dir` runs in-process so those flags are honored.

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
- `--edge-types TEXT` — comma-separated filter: `contains,spawned,delegated_to,replayed_from`
- `--output, -o text|json` — output format (default: `text`)

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
```

Options:
- `--output-format {text,json,github-annotation}` — output format (default: `text`). Tab-completion available via `nova --install-completion`.
- `--assert-no-regressions` — exit 1 if any structural changes detected; useful as CI gate

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

### nova lineage emit-openlineage

```
nova lineage emit-openlineage <path> [--output TARGET]
```

Emit capsule runs as OpenLineage 2.0.2 events.

- `<path>` — a single capsule directory or a parent `runs/` directory (all capsules inside are emitted)
- `--output, -o TARGET` — destination: `-` for stdout, an `http://...` URL, or a file path

If `--output` is omitted, the target is resolved from `OPENLINEAGE_URL` → `OPENLINEAGE_FILE` → stdout.

```bash
# Stdout
nova lineage emit-openlineage .novafabric/runs/01HX.../  --output -

# HTTP endpoint (Marquez, Atlan, etc.)
nova lineage emit-openlineage .novafabric/runs/ --output http://marquez:5000

# Environment variable
OPENLINEAGE_URL=http://marquez:5000 nova lineage emit-openlineage .novafabric/runs/
```

**Dashboard equivalent:** Lineage tab → Export OpenLineage Events panel (returns JSON preview + copy button).

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
| `--image-tag` | `latest` | Docker image tag |

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

### nova export-evidence \<capsule\> --output \<bundle.zip\>

Build a signed Evidence Bundle ZIP per [ADR-0011](../design/adr/0011-evidence-bundle.md). Signs with a
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
```

Options:
- `--key PATH` — PEM-encoded ed25519 private key; when given, output is a DSSE-signed in-toto envelope (predicate types `novafabric.io/{completeness,criterion-binding,reperformance}/v0`). `attest-replay` always requires it.
- `--profile ID|PATH` — audit profile (`eu-ai-act-high-risk`, `gdpr`, `iso42001`, `nist-ai-rmf`, `scientific-reproducibility`, `soc2-type2`, or a YAML path).
- `--mode {mocked,forensic,semantic,exact}` — replay mode for `attest-replay` (default `mocked`); the verdict (`exact`/`semantic-match`/`mismatch`) is recorded separately from the mode. Exit 2 on `mismatch`.
- `--output, -o PATH` — output file.

---

### nova verify \<capsule\>

Verify a capsule's cryptographic seal: DSSE envelope signature, RFC 3161 timestamp
integrity, and Merkle log inclusion proof. Requires NovaSeal configuration
([ADR-0041](../design/adr/0041-novaseal-cryptographic-core-adoption.md)). **experimental** —
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

### nova incident (experimental, v0.50.0, ADR-0088)

First-class incident records with an EU AI Act Art. 73 deadline clock and
two export regimes (OECD AIM + NIS2). Local-first: the record lives in
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

## Compliance evidence commands (v0.15.0, BQ-005)

These commands require `pip install 'novafabric[compliance]'` for full functionality.

> **OQ-01 status:** `nova subject-proof` (GDPR Art.17 erasure proof) is in **LEGAL-HOLD DRAFT MODE** — PII is written to a `legal_hold/` staging area, not sealed capsules. The crypto-shredding strategy (DEK-based erasure per ADR-0069) is designed; implementation planned for v0.26.x.

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

Output fields include: system description, intended purpose, training data characteristics, human oversight measures, robustness metrics, and post-market monitoring plan. Fields are marked `complete`, `partial`, or `missing` based on capsule content.

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

**planned** — Show PII encryption status for a capsule — which fields are encrypted,
which subjects have active DEKs, and which have been erased.

```bash
nova pii status .novafabric/runs/01HX.../
```

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

Export the W3C PROV-JSON provenance graph from a capsule's `lineage.jsonl`.

**works today** — implemented in v0.32.0.

```bash
nova lineage export-prov .novafabric/runs/01HX.../
nova lineage export-prov .novafabric/runs/01HX.../ --output prov.json
```

Options:
- `--output, -o PATH` — path to write PROV-JSON (default: `<capsule_dir>/prov.json`)

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

```bash
nova promote direct my-agent@v1.1 --to staging                       # default gate
nova promote direct my-agent@v1.1 --to staging --significance-gate   # statistical gate
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

### nova diff \<name@v1\> \<name@v2\>

Show field-level differences between two registered versions of an asset.
Both arguments must contain `@` to trigger asset diff; otherwise capsule diff is used (see above).

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

### nova report [--format {markdown,json}] [--output FILE]

Generate an asset inventory report. Defaults to Markdown on stdout.
Use `--output` to write to a file. Valid `--format` values: `markdown` (default), `json`. Tab-completion available via `nova --install-completion`.

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

### nova doctor [--check-storage]

Run diagnostic checks on the NovaFabric installation.

```bash
nova doctor --check-storage
nova doctor --check-storage --backend postgres
nova doctor --check-storage --backend postgres --postgres-dsn "postgresql://..."
nova doctor --check-storage --db-path /path/to/custom.db
```

Without `--check-storage`, prints a hint and exits. With `--check-storage`:

- Reports the active backend (`sqlite` or `postgres`).
- Shows the Alembic schema version and migration status.
- Prints per-table row counts.
- Exits 0 on success, 1 on error.

Options:
- `--check-storage` — enable storage health report (ADR-0016)
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

One-time idempotent migration from a local SQLite registry to Postgres. Per [ADR-0016](../design/adr/0016-storage-backend-evolution.md).

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

Run `alembic upgrade head` against the configured MetadataStore backend.

```bash
# SQLite (reads NOVAFABRIC_DB_PATH, defaults to ~/.novafabric/metadata.db)
nova db upgrade

# Postgres (reads NOVAFABRIC_METADATA_DSN)
export NOVAFABRIC_METADATA_DSN="postgresql://nova:pass@host:5432/novafabric"
nova db upgrade --backend postgres
```

Options:
- `--backend TEXT` — `sqlite` (default) or `postgres`

Environment variables consumed:
- `NOVAFABRIC_DB_PATH` — SQLite database path (sqlite backend)
- `NOVAFABRIC_METADATA_DSN` — Postgres DSN (postgres backend)

Exit codes: `0` = success, `2` = config/connection error or alembic failure.

---

### nova server start

Start the multi-user REST API server. Per [ADR-0017](../design/adr/0017-server-api-protocol.md) and [ADR-0029](../design/adr/0029-server-config-schema.md).

```bash
nova server start
nova server start --backend postgres
nova server start --config /etc/novafabric/nova-server.yaml
nova server start --host 0.0.0.0 --port 7433
```

The server reads `~/.config/novafabric/nova-server.yaml` by default. CLI flags
override the config file. See [`docs/ops/server-deployment.md`](ops/server-deployment.md)
for config examples.

Options:
- `--config, -c FILE` — path to server YAML config (default: `~/.config/novafabric/nova-server.yaml`)
- `--backend TEXT` — `sqlite` (default) or `postgres`
- `--host TEXT` — bind address (default from config or `127.0.0.1`)
- `--port, -p INTEGER` — bind port (default from config or `7433`)

---

### nova server issue-token

Issue a signed offline JWT for airgapped or SLURM deployments. Per [ADR-0018](../design/adr/0018-auth-model.md).

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
active backend. Per [ADR-0018](../design/adr/0018-auth-model.md).

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

REST equivalent (per [ADR-0060](../design/adr/0060-role-management-http-surface.md)):
```bash
curl -X POST http://localhost:7433/v0/admin/roles \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"subject": "user@example.com", "role": "writer"}'
```

---

### nova server revoke-role \<user\> \<role\>

Revoke a role from a user. Per [ADR-0060](../design/adr/0060-role-management-http-surface.md);
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

### nova login

Authenticate with a NovaFabric server via Device Authorization Grant (RFC 8628,
[ADR-0018](../design/adr/0018-auth-model.md)). Credentials are stored in
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

## Local dashboard (experimental, v0.7)

### nova serve --experimental

Start the experimental local dashboard. Read-only browsing of capsules, registry, and lineage; Layer-B compute-only mutations (register, eval, promote, forensic replay, redact, export evidence). Per [ADR-0027](../design/adr/0027-nova-serve-experimental-dashboard.md).

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
| `GET /api/compliance/erasure/status` | Return status of a GDPR erasure request by `?request_id=`. Returns `{ok, request_id, status, subject_id, requested_at, completed_at?, pending_paths[]}`. Mirrors `nova erasure status`. |
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
nova kg ingest [CAPSULE_DIR] [--all] [--capsule-dir DIR] [--path PATH] [--verified/--no-verified]
```

| Argument / Flag | Description |
|---|---|
| `CAPSULE_DIR` | Path to a single capsule directory (local-dir mode). Omit when using `--all`. |
| `--all` | Scan and ingest **all** subdirectories under `--capsule-dir` (or `$NOVAFABRIC_CAPSULE_DIR` / `$NOVAFABRIC_HOME/capsules`). |
| `--capsule-dir DIR` | Base directory to scan when using `--all`. Defaults to `$NOVAFABRIC_CAPSULE_DIR` or `$NOVAFABRIC_HOME/capsules`. |
| `--path` | KuzuDB path (default: `.nova/kg/nova_kg.kuzu`). |
| `--verified` | Mark all events as NovaSeal-verified (sets confidence=1.0). |

```bash
# Single capsule
nova kg ingest .novafabric/runs/01HXAY7M --path /data/nova/kg/nova_kg.kuzu
# Ingested 42 events (0 skipped) → wrote 7 KG edges to /data/nova/kg/nova_kg.kuzu

# All capsules in the default capsule directory
nova kg ingest --all

# All capsules in a specific directory (e.g. after a nova-testbench run)
nova kg ingest --all --capsule-dir ~/novafabric-data/capsules
# Bulk ingest complete: 57 capsule(s) scanned · 1423 events ingested · 38 KG edges written · 0 skipped · 0 failed
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
