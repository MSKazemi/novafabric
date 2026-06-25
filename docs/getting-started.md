# Getting Started with NovaFabric

NovaFabric turns any command — a training script, an AI agent, a notebook cell
runner — into a **run capsule**: a structured, secret-redacted, self-contained
directory containing every observable fact about that execution. Capsules can be
validated, replayed, diffed against each other, and fed into a queryable lineage
graph, all without modifying the code you are running.

This guide takes you from a fresh install to your first captured run, validated
capsule, and lineage query. Allow about 10–15 minutes.

---

## What you need

- Python 3.12 or later
- `pip` or `uv` (either works; examples use both)
- Any command you want to capture (a script, a one-liner, an agent — anything)

---

## Step 1: Install

```bash
pip install novafabric
```

Or, if you prefer `uv`:

```bash
uv add novafabric
```

Confirm the install:

```bash
nova --version
# novafabric 0.45.0
```

Both `nova` and `novafabric` are the same binary. The examples throughout this
guide use `nova`.

> **Optional one-time setup.** `nova init` pre-creates the data directories
> (`capsules/`, `keys/`, `replays/`) under `~/.novafabric` and generates a local
> signing keypair. It is optional — `nova capture` creates what it needs on first
> use — but handy if you want the directory tree in place up front. Re-running it
> is safe; use `nova init --force` to regenerate the keypair.

---

## Step 2: Capture your first run

Wrap any command with `nova capture`. The simplest possible example:

```bash
nova capture python -c "print('hello from nova')"
```

You should see:

```
✓ Capsule written: .novafabric/capsules/01HXAY7M5JZ8R7K4P9DPBYK2WX  (run_id=01HXAY7M5JZ8R7K4P9DPBYK2WX)
```

The ULID in the path is unique to your run. Every capture produces a fresh one.

NovaFabric works by injecting capture hooks into the subprocess via Python's
import system. No changes to your code are required. The hooks are removed
automatically when the command finishes.

### What was written

```
.novafabric/capsules/01HXAY7M5JZ8R7K4P9DPBYK2WX/
  capsule.yaml          ← run manifest: id, status, command, timing
  trace.jsonl           ← execution spans (OpenTelemetry-compatible)
  model-calls.jsonl     ← LLM API calls, one record each (OTel GenAI semconv)
  tool-calls.jsonl      ← tool invocations (MCP or plugin hooks)
  assets.jsonl          ← asset references declared by the run
  env.lock              ← frozen environment: Python, packages, OS, GPU
  redaction-proof.json  ← cryptographic proof that secrets were scanned
  replay.yaml           ← replay constraints and policy
  lineage.jsonl         ← lineage edges emitted by this run
  inputs/
  outputs/
    stdout.txt
    stderr.txt
```

The capsule captures both success and failure. If your command exits non-zero,
the capsule is still written with `status: failure`, `exit_code: N`, and an
`error` block. NovaFabric's exit code mirrors the wrapped command's exit code.

Take a look at a few of these files:

```bash
RUN=.novafabric/capsules/$(ls .novafabric/capsules/ | head -1)

# The manifest — run id, status, timing, command
cat $RUN/capsule.yaml

# What was printed
cat $RUN/outputs/stdout.txt

# Environment snapshot — Python version, all installed packages, OS, hardware
cat $RUN/env.lock
```

---

## Step 3: Validate the capsule

`nova validate` checks the capsule directory against its JSON schemas and
confirms all required files are present:

```bash
nova validate $RUN
# ✓ Valid capsule: 01HXAY7M5JZ8R7K4P9DPBYK2WX  status=success
```

Validation covers `capsule.yaml`, `env.lock`, `redaction-proof.json`, and
`lineage.jsonl`. A capsule without `redaction-proof.json` is invalid — the
secret scan must have run for the capsule to be trusted.

> **Running many captures on one machine?** An optional, opt-in **warm capture
> daemon** removes the per-run startup cost (`nova daemon start`, then `novacap
> <cmd>`). It's experimental and Linux-only; see the
> [warm capture daemon guide](warm-capture-daemon.md).

---

## Step 4: Capture a real LLM call

The simple `print` example produces an empty `model-calls.jsonl` because there
were no LLM API calls. Let us capture a real one.

The `examples/minimal-agent-run/` directory in the repository contains a ready-made
example. If you have an Anthropic API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
pip install anthropic

python examples/minimal-agent-run/agent.py
```

Or capture it via the CLI instead of the `@agent` decorator:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
nova capture python examples/minimal-agent-run/agent.py
```

After the run, look at the model-calls record:

```bash
RUN=.novafabric/capsules/$(ls -t .novafabric/capsules/ | head -1)
cat $RUN/model-calls.jsonl | python -m json.tool | head -40
```

You will see a JSON record per LLM call. Each record follows the
[OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/):

```json
{
  "schema_version": "0.1.0",
  "gen_ai.system": "anthropic",
  "gen_ai.request.model": "claude-haiku-4-5-20251001",
  "gen_ai.request.temperature": 1.0,
  "gen_ai.request.max_tokens": 64,
  "gen_ai.request.messages": [...],
  "gen_ai.response.choices": [...],
  "gen_ai.response.finish_reasons": ["end_turn"],
  "gen_ai.usage.input_tokens": 22,
  "gen_ai.usage.output_tokens": 41,
  "status": "success",
  ...
}
```

This record is what `nova replay` uses to mock the call, and what `nova diff`
aligns when comparing two runs.

### What NovaFabric captures automatically

NovaFabric installs capture hooks for the following transports. You do not
configure anything — if the library is installed, the hook fires:

| Transport | What gets captured |
|---|---|
| `openai` SDK | `chat.completions.create` calls |
| `anthropic` SDK | `messages.create` calls |
| `httpx` (sync and async) | Requests to known LLM API URLs |
| `requests` | Same URL classification as `httpx` |
| `aiohttp` | Async requests to known LLM API URLs |
| `urllib3` | Low-level requests; covers `boto3`/Bedrock |
| `mcp` SDK | Every `call_tool` invocation |

The URL classification uses a vendored registry of known LLM provider hostnames
(OpenAI, Anthropic, Cohere, Together, Mistral, Replicate, AWS Bedrock, Ollama
on `localhost:11434`). You can extend it with `~/.novafabric/url_registry.yaml`
to cover private endpoints.

If none of these libraries are installed, the capsule is still written — you
get environment, stdout/stderr, and timing.

---

## Step 5: Inspect the secret scan

Every capsule includes a proof that no API keys or secrets leaked into the
artifacts:

```bash
nova scan-secrets $RUN
```

A clean run prints something like:

```
Capsule: 01HXAY7M5JZ8R7K4P9DPBYK2WX
  findings: 0
  files scanned: 4
  Status: CLEAN
```

The `redaction-proof.json` file is a cryptographic chain record — it proves the
scan ran and records which files were checked and how many findings were found.
If a key is detected, it is redacted in-place as `[REDACTED:rule-id]` before
the capsule is finalized.

---

## Step 6: Replay the capsule (read-only inspection)

`nova replay --mode forensic` gives you a read-only view of what happened in
the capsule without re-executing anything:

```bash
nova replay $RUN --mode forensic
```

Forensic mode reads and returns the manifest, traces, and model calls directly
from the capsule directory. No subprocess is launched, no network calls are
made. This is the safe way to inspect a run from a colleague or from a CI
artifact store.

Results are written to `.novafabric/replays/<replay-ulid>/replay_result.yaml`.

---

## Step 7: Detect regressions with diff

Capture two runs of the same command with a controlled difference, then have
NovaFabric show you what changed structurally:

```bash
# Capture a baseline
AGENT_MODE=baseline nova capture \
  --output-dir examples/replay-and-diff/runs \
  python examples/replay-and-diff/agent.py

# Capture a regressed variant
AGENT_MODE=regressed nova capture \
  --output-dir examples/replay-and-diff/runs \
  python examples/replay-and-diff/agent.py

# Compare them
RUNS=(examples/replay-and-diff/runs/*/)
nova diff "${RUNS[0]}" "${RUNS[1]}"
```

Output:

```
Diff: 01HX... → 01HY...
  changed=1  added=0  removed=0
Outputs:
  ~ outputs/stdout.txt
```

The `score: 0.85` → `score: 0.62` regression in stdout is detected structurally.
You did not have to read log files.

To use this as a CI gate:

```bash
nova diff "${RUNS[0]}" "${RUNS[1]}" --assert-no-regressions
# exits 1 if any changes detected
```

---

## Step 8: Query the lineage graph

Every captured run writes `lineage.jsonl` into the capsule directory, recording
which assets the run consumed and what it produced. NovaFabric also indexes
these edges into a local SQLite graph as it captures.

The `examples/lineage-chain/` example captures three runs that all depend on
the same upstream dataset:

```bash
RUNS=examples/lineage-chain/runs

nova capture --output-dir $RUNS python examples/lineage-chain/step.py train-v1
nova capture --output-dir $RUNS python examples/lineage-chain/step.py eval-v1
nova capture --output-dir $RUNS python examples/lineage-chain/step.py promote-v1
```

Now query the graph:

```bash
# "If this dataset changes, which runs do I need to re-evaluate?"
nova lineage blast-radius local:datasets/training-set@1.0.0
```

Output:

```
Blast radius of local:datasets/training-set@1.0.0
├── run:01K…  (train-v1)
├── run:01K…  (eval-v1)
└── run:01K…  (promote-v1)
```

```bash
# "What did this specific run depend on?"
nova lineage provenance <run-id>
```

If you ever lose the local SQLite index, you can rebuild it from the capsule
contents:

```bash
nova lineage import examples/lineage-chain/runs/
```

The lineage graph is fully derivable from the `lineage.jsonl` files — the
database is a rebuildable cache.

---

## Step 9: Seal and verify a capsule (optional, regulated environments)

If you work in a regulated environment or need tamper-evident capsule provenance,
NovaSeal cryptographically signs every capsule after capture.

**One-time key setup:**

```bash
mkdir -p ~/.novafabric
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 \
  -out ~/.novafabric/seal.key
openssl req -new -x509 -key ~/.novafabric/seal.key -days 365 \
  -out ~/.novafabric/seal.crt -subj "/CN=NovaSeal-Local"
```

Create `~/.novafabric/novaseal.yaml`:

```yaml
profile: local
key_path: ~/.novafabric/seal.key
cert_path: ~/.novafabric/seal.crt
tsa_url: https://freetsa.org/tsr
merkle_db: ~/.novafabric/novaseal-merkle.db
```

**Capture now seals automatically:**

```bash
nova capture python my_agent.py
```

**Verify the seal:**

```bash
nova verify .novafabric/capsules/01HXAY7M5JZ8R7K4P9DPBYK2WX/
# signature_ok=True, timestamp_ok=True, log_integrity_ok=True
```

Without `novaseal.yaml`, capture works exactly as before — NovaSeal is fully opt-in.

---

## Where to go next

You now have a captured run, a validated capsule, a forensic replay, a diff,
and a lineage query under your belt. The [user guide](user-guide.md) covers
every shipped `nova` command in detail.

A few directions to explore:

| You want to... | Where to go |
|---|---|
| Capture without wrapping in a subprocess (decorator pattern) | [User guide: SDK decorator](user-guide.md#sdk-decorator-in-process-capture) |
| Capture an MCP-using agent (Claude Desktop, Cursor) | [User guide: mcp-proxy](user-guide.md#nova-mcp-proxy-experimental) |
| Capture non-Python LLM clients (Node.js, Go, Claude Code) | [User guide: api-proxy](user-guide.md#nova-api-proxy-experimental) |
| Run inside a Docker container or Kubernetes | [User guide: runners](user-guide.md#runners) |
| Build a signed evidence bundle | [User guide: export-evidence](user-guide.md#nova-export-evidence) |
| Cryptographically sign and verify capsules (regulated environments) | [User guide: nova verify](user-guide.md#nova-verify-v010) |
| Register and lifecycle-manage AI assets | [User guide: asset registry](user-guide.md#asset-registry) |
| Browse capsules in a local web dashboard | [User guide: nova serve](user-guide.md#nova-serve-experimental) |
| Hands-on tour of every capability (proxies, providers, KG, compliance) | [tutorials/feature-tour.md](tutorials/feature-tour.md) |
| Why NovaFabric — plain-English value guide | [tutorials/why-novafabric.md](tutorials/why-novafabric.md) |
| How capture works under the hood | [tutorials/how-capture-works.md](tutorials/how-capture-works.md) |
| Capturing multi-agent systems | [tutorials/multi-agent-capture.md](tutorials/multi-agent-capture.md) |
| NovaFabric vs Langfuse | [tutorials/novafabric-vs-langfuse.md](tutorials/novafabric-vs-langfuse.md) |
| Cluster-scale and 1M agents | [tutorials/cluster-scale.md](tutorials/cluster-scale.md) |
| Read about the capsule format | [docs/concepts.md](concepts.md) |
| See what's planned | [ROADMAP.md](../ROADMAP.md) |
