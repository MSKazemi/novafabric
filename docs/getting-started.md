# Getting Started with NovaFabric

NovaFabric turns any command — a training script, an AI agent, a notebook cell
runner — into a **Run Capsule**: a structured, secret-redacted, self-contained
directory containing every observable fact about that execution. Capsules can be
validated, replayed, diffed against each other, exported as signed **Evidence
Bundles**, and fed into a queryable lineage graph — all without modifying the
code you are running.

NovaFabric is **local-first**: everything in this guide runs entirely on your own
machine. No accounts, no hosted backend, no telemetry, no network required for
the core workflow (capture, validate, replay, diff, lineage). What you produce
is not a trace row in someone else's database — it is a portable, replayable
capsule you own.

## What you will learn

By the end of this guide you will have, on your own machine:

1. Installed NovaFabric and confirmed the `nova` CLI works.
2. Captured a command into a Run Capsule with **zero code changes**.
3. Validated the capsule against its JSON schemas.
4. Captured a real LLM call recorded in OpenTelemetry GenAI semantic conventions.
5. Inspected the secret-scan redaction proof.
6. Replayed a capsule in read-only **forensic** mode.
7. Detected a regression between two runs with structural **diff** — the same
   check you can wire into CI.
8. Queried the **lineage** graph for blast radius and provenance.
9. Exported a signed **Evidence Bundle** an auditor can verify offline.

Allow about 10–15 minutes.

These nine steps exercise all five NovaFabric primitives — **Asset Registry, Run
Capsule, Replay, Lineage, and Evidence Bundle** — end to end. For the concepts
behind each, see [docs/concepts.md](concepts.md).

---

## What you need

- **Python 3.12 or later**
- `pip` or `uv` (either works; examples show both)
- Any command you want to capture (a script, a one-liner, an agent — anything)

No API keys are required for Steps 1–3 and 6–9. Step 4 (a real LLM call) needs an
Anthropic API key, but you can skip it and the rest of the guide still works.

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
# novafabric 0.99.0
```

Both `nova` and `novafabric` are the same binary. The examples throughout this
guide use `nova`.

> **Optional extras.** The base install covers everything in this guide.
> Narrow extras exist for optional integrations (e.g. `pip install
> "novafabric[serve]"` for the experimental dashboard, or `"novafabric[all]"`
> for everything) — see [docs/operator-guide.md](operator-guide.md#package-installation).

> **Changed in v0.99.0 — the default install is much smaller (412 MB → 113 MB).**
> `duckdb`, `pyarrow`, `python-louvain` and `clickhouse-connect` are no longer
> installed by default; they moved to the extras that actually use them
> (ADR-0222). Everything in this guide still works unchanged. If you relied on
> importing any of them after a plain `pip install novafabric`, use
> `pip install 'novafabric[all]'` or the narrower extra you need — see
> [docs/operator-guide.md](operator-guide.md#package-installation).

> **Optional one-time setup.** `nova init` pre-creates the data directories
> (`capsules/`, `keys/`, `replays/`) under `~/.novafabric` and generates a local
> signing keypair. It is optional — `nova capture` creates what it needs on first
> use — but handy if you want the directory tree in place up front. Re-running it
> is safe; use `nova init --force` to regenerate the keypair.

> **Maturity.** NovaFabric is in beta (v0.99.0). Most surfaces work today but
> carry `experimental` maturity: interfaces may change before the v1.0 schema
> freeze. On-disk formats are **not** frozen until v1.0. See
> [ROADMAP.md](../ROADMAP.md) for the sequencing.

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

The ID in the path is a **ULID** — a time-sortable identifier unique to your run.
Every capture produces a fresh one, so capsule directories sort chronologically.

NovaFabric captures by injecting hooks into the subprocess through Python's
import system (a `sitecustomize.py` placed on `PYTHONPATH`). **No changes to your
code are required.** The hooks are removed automatically when the command
finishes.

### What was written

```
.novafabric/capsules/01HXAY7M5JZ8R7K4P9DPBYK2WX/
  capsule.yaml          ← run manifest: id, status, command, timing
  trace.jsonl           ← execution spans (OpenTelemetry-compatible)
  model-calls.jsonl     ← LLM API calls, one record each (OTel GenAI semconv)
  tool-calls.jsonl      ← tool invocations (MCP or plugin hooks)
  assets.jsonl          ← asset references declared by the run
  env.lock              ← frozen environment: Python, packages, OS, GPU
  redaction-proof.json  ← proof that the secret scan ran (required to validate)
  replay.yaml           ← replay constraints and policy
  lineage.jsonl         ← lineage edges emitted by this run
  inputs/
  outputs/
    stdout.txt
    stderr.txt
```

The capsule captures **both success and failure**. If your command exits
non-zero, the capsule is still written with `status: failure`, `exit_code: N`,
and an `error` block. NovaFabric's own exit code mirrors the wrapped command's
exit code, so it is safe to drop `nova capture` in front of any command in an
existing pipeline.

Take a look at a few of these files:

```bash
RUN=.novafabric/capsules/$(ls -t .novafabric/capsules/ | head -1)

# The manifest — run id, status, timing, command
cat $RUN/capsule.yaml

# What was printed
cat $RUN/outputs/stdout.txt

# Environment snapshot — Python version, installed packages, OS, hardware
cat $RUN/env.lock
```

`env.lock` records the Python version, up to 200 installed packages, safe
environment variables (secrets excluded), OS / arch / CPU / memory, and GPU
presence — enough to reason about whether a run is reproducible on another
machine.

---

## Step 3: Validate the capsule

`nova validate` checks the capsule directory against its JSON schemas and
confirms all required files are present:

```bash
nova validate $RUN
# ✓ Valid capsule: 01HXAY7M5JZ8R7K4P9DPBYK2WX  status=success
```

Validation covers `capsule.yaml`, `env.lock`, `redaction-proof.json`, and
`lineage.jsonl`. A capsule **without** `redaction-proof.json` is invalid — the
secret scan must have run for the capsule to be trusted, and (as you will see in
Step 9) an unredacted capsule cannot be exported as evidence.

> **Running many captures on one machine?** An optional, opt-in **warm capture
> daemon** removes the per-run startup cost (`nova daemon start`, then `novacap
> <cmd>`). It is experimental and Linux-only; see the
> [warm capture daemon guide](warm-capture-daemon.md).

---

## Step 4: Capture a real LLM call

The `print` example produced an empty `model-calls.jsonl` because there were no
LLM API calls. Let's capture a real one. *(Skip this step if you don't have an
API key — the rest of the guide does not depend on it.)*

The `examples/minimal-agent-run/` directory in the repository contains a
ready-made example. If you have an Anthropic API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
pip install anthropic

nova capture python examples/minimal-agent-run/agent.py
```

The same example also demonstrates the in-process `@novafabric.agent` decorator
if you prefer capturing without wrapping in a subprocess — see the
[User guide: SDK decorator](user-guide.md#sdk-decorator-in-process-capture).

After the run, look at the model-calls record:

```bash
RUN=.novafabric/capsules/$(ls -t .novafabric/capsules/ | head -1)
cat $RUN/model-calls.jsonl | python -m json.tool | head -40
```

You will see one JSON record per LLM call, following the
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

This record is what `nova replay` uses to mock the call (Step 6), and what
`nova diff` aligns when comparing two runs (Step 7). Sampling parameters such as
`temperature`, `top_p`, and `seed` are captured because they are what an exact,
deterministic replay would need to reproduce.

### What NovaFabric captures automatically

NovaFabric installs capture hooks for the transports below. You do not configure
anything — if the library is installed, the hook fires:

| Transport | What gets captured |
|---|---|
| `openai` SDK | `chat.completions.create` calls |
| `anthropic` SDK | `messages.create` calls |
| `httpx` (sync and async) | Requests to known LLM API URLs |
| `requests` | Same URL classification as `httpx` |
| `aiohttp` | Async requests to known LLM API URLs |
| `urllib3` | Low-level requests; covers `boto3` / Bedrock |
| `mcp` SDK | Every `call_tool` invocation |

URL classification uses a **vendored registry** of known LLM provider hostnames
(OpenAI, Anthropic, Cohere, Together, Mistral, Replicate, AWS Bedrock, and Ollama
on `localhost:11434`). Extend it with `~/.novafabric/url_registry.yaml` to cover
private endpoints. Third-party plugins can also register hooks via the
`novafabric.hooks` entry-point group.

If none of these libraries are installed, the capsule is still written — you get
environment, stdout/stderr, and timing.

---

## Step 5: Inspect the secret scan

Every capsule includes a proof that no API keys or secrets leaked into the
artifacts:

```bash
nova scan-secrets $RUN
```

A clean run prints:

```
✓ 01HXAY7M5JZ8R7K4P9DPBYK2WX: no findings
```

If findings were detected, `nova scan-secrets` lists each one (with a severity
badge); add `--fail-on <severity>` to exit non-zero above a threshold, or
`--json` to print the full `redaction-proof.json` for scripting. The
`redaction-proof.json` file itself records that the scan ran, which files were
checked, and how many findings were found. If a secret is detected, it is
redacted **in place** as `[REDACTED:rule-id]` before the capsule is finalized.
This proof is what makes a capsule safe to share, archive, and export — and it is
generated on every capture, not as an afterthought.

---

## Step 6: Replay the capsule (read-only inspection)

Replay re-executes or inspects a capsule with external calls controlled. There
are four honest, falsifiable modes for reproducing or judging a run —
`forensic`, `mocked`, `semantic`, `exact` — plus a fifth, **experimental**
`intervention` mode for counterfactual root-cause analysis (see the table
below). We'll start with the safest — **forensic** — which reads the capsule
without re-executing anything:

```bash
nova replay $RUN --mode forensic
```

Forensic mode reads and returns the manifest, traces, and model calls directly
from the capsule directory. **No subprocess is launched and no network calls are
made** — this is the safe way to inspect a run handed to you by a colleague or
pulled from a CI artifact store.

Results are written to `.novafabric/replays/<replay-ulid>/replay_result.yaml`. A
replay is itself a new capsule, so you can diff a replay against the original.

The other modes each answer a different question:

| Mode | Re-executes? | Network? | Use it for |
|---|---|---|---|
| `forensic` | No | No | Audit / post-incident inspection |
| `mocked` | Yes | LLM calls served from the capsule cache | CI / regression |
| `semantic` | Yes | Yes | Judging *meaning* (0.0–1.0 score) against drifting remote LLMs |
| `exact` | Yes | Deterministic env only | Local / on-prem byte-exact compliance |
| `intervention` (experimental) | Yes, mocked semantics | No | Counterfactual root-cause: substitute one captured event per an `InterventionSpec` and see whether the outcome flips |

NovaFabric deliberately does **not** claim byte-exact replay of remote LLM calls
— that is why `semantic` mode exists. `intervention` mode is what
`nova diagnose --intervene` / `--search-root-cause` use under the hood to test
a failure hypothesis rather than just rank it — see
[User guide: replay](user-guide.md) for the details of each mode.

---

## Step 7: Detect regressions with diff

Capture two runs of the same command with a controlled difference, then let
NovaFabric show you what changed **structurally** — no reading log files:

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

`nova diff` aligns model calls, tool calls, and outputs between the two capsules,
so a `score: 0.85` → `score: 0.62` regression in stdout is detected structurally.

To use this as a **CI gate**:

```bash
nova diff "${RUNS[0]}" "${RUNS[1]}" --assert-no-regressions
# exits 1 if any regression is detected
```

This is the single most common production use of NovaFabric: wire
`nova diff --assert-no-regressions` into CI so a "worked yesterday, fails today"
agent fails the build with a portable capsule attached as evidence.

---

## Step 8: Query the lineage graph

Every captured run writes `lineage.jsonl` into the capsule directory, recording
which assets the run consumed and what it produced. NovaFabric also indexes these
edges into a local SQLite graph as it captures.

The `examples/lineage-chain/` example captures three runs that all depend on the
same upstream dataset:

```bash
RUNS=examples/lineage-chain/runs

nova capture --output-dir $RUNS python examples/lineage-chain/step.py train-v1
nova capture --output-dir $RUNS python examples/lineage-chain/step.py eval-v1
nova capture --output-dir $RUNS python examples/lineage-chain/step.py promote-v1
```

Now query the graph.

**Blast radius** — *"If this dataset changes, which runs must I re-evaluate?"*

```bash
nova lineage blast-radius local:datasets/training-set@1.0.0
```

```
Blast radius of local:datasets/training-set@1.0.0
├── run:01K…  (train-v1)
├── run:01K…  (eval-v1)
└── run:01K…  (promote-v1)
```

**Provenance** — *"What did this specific run depend on?"*

```bash
nova lineage provenance <run-id>
```

If you ever lose the local SQLite index, rebuild it from the capsule contents:

```bash
nova lineage import examples/lineage-chain/runs/
```

The lineage graph is **fully derivable** from the `lineage.jsonl` files — the
database is a rebuildable cache, never the source of truth. NovaFabric can also
emit these as OpenLineage 2.0.2 events (START / COMPLETE / FAIL) to Marquez,
Atlan, or OpenMetadata; see [User guide: lineage](user-guide.md).

---

## Step 9: Export a signed Evidence Bundle

When you need portable, tamper-evident proof of what a run did — for a colleague,
an archive, or an auditor — export the capsule as a signed **Evidence Bundle**:

```bash
nova export-evidence $RUN --output bundle.zip --key ~/.novafabric/keys/signing_key.pem
```

`--key` is required (the CLI errors with a clear message if it's omitted) — it
points at the Ed25519 private key `nova init` already generated for you in
Step 1. If you skipped `nova init`, generate one with
`python -m novafabric.evidence.signing` or any ed25519 tool.

An Evidence Bundle is a signed, self-contained ZIP that embeds the capsule, a
lineage subgraph, in-toto DSSE attestations, **ed25519** signatures, and the
JSON schemas it validates against. Its defining property is that it is verifiable
with **only `sha256sum` plus an ed25519 verifier — no NovaFabric runtime
required**, so an auditor can check it offline, air-gapped, years later.

A capsule can only be exported if its secret scan ran and produced a
`redaction-proof.json` (Step 5) — you cannot ship evidence that was never
checked for leaked secrets.

See [User guide: export-evidence](user-guide.md#nova-export-evidence) for the
full bundle layout and the offline verification procedure.

> **Experimental — cryptographic sealing (NovaSeal).** Beyond ed25519 Evidence
> Bundles, NovaFabric ships an **experimental, opt-in** in-process sealing core
> (v0.10+): DSSE ECDSA P-256 signatures, best-effort RFC 3161 trusted
> timestamps, and an append-only Merkle log, verified with `nova verify`
> (`signature_ok` / `timestamp_ok` / `log_integrity_ok`), driven by an optional
> `~/.novafabric/novaseal.yaml`. Its interfaces may change before the v1.0
> schema freeze; the dedicated, hardened NovaSeal signing *service* (network
> service, qualified timestamps, Sigstore-keyless by default — ADR-0041)
> remains **planned**. For portable proof with the most stable surface today,
> use the **Evidence Bundle** above. Sealing is fully opt-in: without a
> `novaseal.yaml`, capture and export behave exactly as in Steps 2–8.

---

## Where to go next

You now have a captured run, a validated capsule, a real LLM call recorded in
GenAI semconv, a forensic replay, a CI-gateable diff, a lineage query, and a
signed Evidence Bundle. The [user guide](user-guide.md) covers every shipped
`nova` command in detail.

A few directions to explore:

| You want to... | Where to go |
|---|---|
| Capture without wrapping in a subprocess (decorator pattern) | [User guide: SDK decorator](user-guide.md#sdk-decorator-in-process-capture) |
| Capture an MCP-using agent (Claude Desktop, Cursor) | [User guide: mcp-proxy](user-guide.md#nova-mcp-proxy-experimental) |
| Capture non-Python LLM clients (Node.js, Go, Claude Code) | [User guide: api-proxy](user-guide.md#nova-api-proxy-experimental) |
| Run inside a Docker container, Kubernetes, or Slurm | [User guide: runners](user-guide.md#runners) |
| Build a signed Evidence Bundle | [User guide: export-evidence](user-guide.md#nova-export-evidence) |
| Register and lifecycle-manage AI assets (eval-gated promotion) | [User guide: asset registry](user-guide.md#asset-registry) |
| Gate promotion with OPA/Rego policy and maker-checker approval | [User guide: nova promote](user-guide.md) |
| Browse capsules in a local read-only web dashboard | [User guide: nova serve](user-guide.md#nova-serve-experimental) |
| Hands-on tour of every capability (proxies, providers, KG, compliance) | [tutorials/feature-tour.md](tutorials/feature-tour.md) |
| Prove supply-chain provenance & eval integrity (dataset cards, contamination checks, SLSA-for-ML, OTel export) | [feature tour §17](tutorials/feature-tour.md#17-prove-supply-chain-provenance--eval-integrity) |
| Group runs into multi-turn sessions and replay them in order (experimental) | [CLI reference: nova session](cli-reference.md#nova-session-experimental-adr-0122) |
| Version prompts as immutable registry assets + deployment labels (experimental) | [CLI reference: nova prompt](cli-reference.md#prompt-versioning-commands-experimental-adr-0112) |
| Query cost / tokens / scores offline over your capsules (experimental) | [CLI reference: nova query](cli-reference.md#nova-query-experimental-adr-0129) |
| Attribute a failed run to its most likely cause, then replay-prove the hypothesis (experimental) | `nova diagnose <run-id> --search-root-cause` |
| Export EU AI Act / ISO 42001 / NIST / GPAI compliance evidence (experimental) | `nova export-compliance --help`, [docs/concepts.md](concepts.md#evidence-bundle) |
| Everything that shipped experimental in v0.59, in one list | [User guide: v0.59 summary](user-guide.md#what-shipped-experimental-in-v059) |
| Why NovaFabric — plain-English value guide | [tutorials/why-novafabric.md](tutorials/why-novafabric.md) |
| How capture works under the hood | [tutorials/how-capture-works.md](tutorials/how-capture-works.md) |
| Capturing multi-agent systems | [tutorials/multi-agent-capture.md](tutorials/multi-agent-capture.md) |
| NovaFabric vs Langfuse (complementary, not competing) | [tutorials/novafabric-vs-langfuse.md](tutorials/novafabric-vs-langfuse.md) |
| The five primitives and the capsule format | [docs/concepts.md](concepts.md) |
| What's shipped vs planned | [ROADMAP.md](../ROADMAP.md) |
