# NovaFabric feature tour — every capability, hands-on

> **New to NovaFabric?** Start with the [Getting Started guide](../getting-started.md)
> for a focused 15-minute first run (capture → validate → replay → diff → lineage →
> seal). This tour is the comprehensive walk-through: it covers those basics *and*
> the advanced surfaces — proxies, every LLM provider, LangChain/LangGraph, evidence
> bundles, the capsule knowledge graph, capture-level policy, GDPR erasure, dashboard
> reports, topology views, compliance exports, the zero-token eval loop, intervention
> replay, the Accountability Spine (energy receipts, ledger sealing, safety cases),
> incident tracking, and the Langfuse-parity cohort — prompt lifecycle, offline
> analytics, sessions, and the team evaluation workflow.

> **Prerequisites:** NovaFabric installed (`uv pip install -e .` from the repo).
> Run `nova --help` to confirm the `nova` CLI is on your `PATH`.

---

## What you will learn

By the end of this tour you will have exercised all five NovaFabric primitives —
**Asset Registry, Run Capsule, Replay, Lineage, and Evidence Bundle** — and the
surfaces built on top of them, each with a real command and real output:

| # | Section | Primitive / surface | Maturity |
|---|---------|---------------------|----------|
| 1 | [Capture your first run](#1-capture-your-first-run) | Run Capsule | works today |
| 2 | [Detect a regression with diff](#2-detect-a-regression-with-diff) | Replay / structural Diff | works today |
| 3 | [Replay a past run](#3-replay-a-past-run) | Replay | works today |
| 4 | [Trace data dependencies with lineage](#4-trace-data-dependencies-with-lineage) | Lineage | works today |
| 5 | [Capture without changing your agent code](#5-capture-without-changing-your-agent-code) | Run Capsule (wire-level) | works today |
| 6 | [Capture any LLM provider](#6-capture-any-llm-provider) | Run Capsule (wire-level) | works today |
| 7 | [Capture a LangChain / LangGraph agent](#7-capture-a-langchain--langgraph-agent) | Run Capsule (framework-neutral) | works today |
| 8 | [Build a signed evidence bundle](#8-build-a-signed-evidence-bundle) | Evidence Bundle | works today (DSSE/SLSA wrap: experimental) |
| 9 | [Cryptographically seal a capsule (NovaSeal)](#9-cryptographically-seal-a-capsule-novaseal) | Trust layer | see maturity note |
| 10 | [Build a Capsule Knowledge Graph](#10-build-a-capsule-knowledge-graph) | Derived aggregation graph | experimental |
| 11 | [Hunt anomalies with the SPKG](#11-hunt-anomalies-with-the-security--provenance-kg-spkg) | Security graph | experimental |
| 12 | [Manage capture-level policy](#12-manage-capture-level-policy) | Capture policy | experimental |
| 13 | [Split-storage and GDPR erasure](#13-split-storage-and-gdpr-erasure) | Storage / erasure | experimental, flag-gated |
| 14 | [Generate reports from the dashboard](#14-generate-reports-from-the-dashboard) | Dashboard (Layer A) | experimental |
| 15 | [Open the dashboard topology view](#15-open-the-dashboard-topology-view) | Dashboard topology | experimental |
| 16 | [Export compliance reports](#16-export-compliance-reports-from-the-dashboard) | Compliance export | experimental |
| 17 | [Prove supply-chain provenance & eval integrity](#17-prove-supply-chain-provenance--eval-integrity) | Supply chain / eval / OTel | experimental |
| 18 | [Run an evidence-grade eval loop — zero tokens](#18-run-an-evidence-grade-eval-loop--zero-tokens) | Eval (offline, significance-gated) | experimental |
| 19 | [Ask "what if?" with intervention replay](#19-ask-what-if-with-intervention-replay) | Replay (5th mode, counterfactual) | experimental |
| 20 | [Walk the Accountability Spine](#20-walk-the-accountability-spine-energy-ledger-safety-case) | Energy receipts / ledger / safety case | experimental |
| 21 | [Track an incident on the Art. 73 deadline clock](#21-track-an-incident-on-the-art-73-deadline-clock) | Incident records / compliance export | experimental |
| 22 | [Version, compose, and govern prompts](#22-version-compose-and-govern-prompts) | Prompt lifecycle / deployment labels | experimental |
| 23 | [Query cost, tokens, and latency offline](#23-query-cost-tokens-and-latency-offline) | Offline analytics (query / views / trends / pricing) | experimental |
| 24 | [Group turns into a session and replay it](#24-group-turns-into-a-session-and-replay-it) | Session capsule / execution graph | experimental |
| 25 | [Run a team evaluation workflow](#25-run-a-team-evaluation-workflow) | Score configs / annotation queues / experiments | experimental |

Read as much or as little as you need — each section is self-contained. Everything
runs on your own machine: no accounts, no hosted backend, no telemetry.

> **Maturity labels used in this tour.** Following the project's docs-honesty rule,
> each capability is tagged **works today** (implemented, tests pass), **experimental**
> (implemented but interfaces may change before the v1.0 schema freeze), or **planned /
> future design** (documented intent, not yet built). Where a section drives a
> command, that command exists on `main`; the maturity note tells you how stable to
> assume it is.

---

## 0. Initialise your local installation

If you installed via pip (not docker-compose), run this once to create the data
directories and generate a signing keypair:

```bash
nova init
```

Expected output:

```
Initialized: /home/<you>/.novafabric
  capsules  → /home/<you>/.novafabric/capsules
  keys      → /home/<you>/.novafabric/keys/signing_key.pem

Next steps:
  nova capture python my_agent.py
  nova serve --experimental
```

Running `nova init` a second time is safe — it prints "Already initialized" and exits.
Use `nova init --force` to regenerate the keypair.

> **docker-compose users:** skip this step. The container entrypoint handles
> first-boot setup automatically (`make dev-up` is all you need).

---

## 1. Capture your first run

**Primitive:** Run Capsule · **Maturity:** works today (v0.2)

The **Run Capsule** is NovaFabric's fundamental unit: a directory, named by a
time-sortable ULID, that holds every observable fact of one execution. It is written
on **both success and failure**, and it is the unit every other command in this tour
operates on.

Start with the simplest possible capture — no LLM, no agent, just proof the system
works:

```bash
nova capture python -c "print('hello from nova')"
```

You should see:

```
✓ Capsule written: $NOVAFABRIC_HOME/capsules/01K…  (run_id=01K…)
```

Inspect what was recorded:

```bash
RUN=$(ls -d "${NOVAFABRIC_HOME:-$HOME/.novafabric}/capsules/"*/ | head -1)
nova validate $RUN        # → ✓ Valid capsule: …  status=success
ls $RUN
cat $RUN/capsule.yaml
cat $RUN/outputs/stdout.txt
```

The capsule is a plain directory. No server is needed to read it:

```
capsule.yaml          # manifest: model, command, exit code, timing
model-calls.jsonl     # every LLM API call, one record each (OTel GenAI semconv)
tool-calls.jsonl      # tool invocations (MCP or plugin hooks)
assets.jsonl          # datasets this run consumed
redaction-proof.json  # proof secrets were scanned before storage
inputs/               # stdin, files passed in
outputs/              # stdout, stderr, files written out
```

Two properties make this durable rather than just a log directory:

- **Redaction is mandatory.** A secret scanner runs before the capsule is finalised
  and writes `redaction-proof.json`. A capsule missing that proof is invalid to
  `nova validate` and **cannot be exported** as evidence (see [§8](#8-build-a-signed-evidence-bundle)).
- **The schema is additive-only.** Capsules carry optional `extensions:` blocks
  (e.g. `slurm`, `kubernetes`, `openlineage`) so the format can grow without a new
  top-level format, and old capsules stay readable.

This is the unit everything else works with. See [Concepts](../concepts.md) for the
full field-by-field schema.

---

## 2. Detect a regression with diff

**Primitive:** structural Diff · **Maturity:** works today (v0.3)

`nova diff` performs a **structural, behavioral** comparison — it aligns model and
tool calls between two capsules rather than diffing raw text — so you can tell
whether an agent's *behavior* changed, not just whether its *code* changed.

Capture two runs of the same agent — one baseline, one with a behavior change — then
let NovaFabric tell you exactly what differs:

```bash
RUNS=examples/replay-and-diff/runs

# Baseline
AGENT_MODE=baseline nova capture --output-dir $RUNS \
    python examples/replay-and-diff/agent.py

# Regressed
AGENT_MODE=regressed nova capture --output-dir $RUNS \
    python examples/replay-and-diff/agent.py

# Compare
CAPS=($(ls -d $RUNS/*/))
nova diff "${CAPS[0]}" "${CAPS[1]}"
```

You get structural output — not a text diff, a behavioral diff:

```
model:          same  (qwen3:35b)
tools_called:   same
output_score:   CHANGED — 0.85 → 0.62
output_length:  CHANGED — 420 tokens → 310 tokens
```

Wire it into CI to catch regressions automatically:

```bash
nova diff "${CAPS[0]}" "${CAPS[1]}" --assert-no-regressions   # exits 1 on change
```

When you update a prompt, bump a model version, or change a tool, `--assert-no-regressions`
turns the diff into a **gate**: it exits non-zero the moment agent behavior actually
changes, so a flaky prod agent that "worked yesterday, fails today" is caught before merge.

---

## 3. Replay a past run

**Primitive:** Replay · **Maturity:** works today (v0.3)

Replay re-executes or inspects a capsule with external calls controlled, in four
honest, falsifiable modes. This section uses `forensic` (read-only, no subprocess, no
network — the mode for audit and post-incident work):

```bash
nova replay capsules/01KR9Q2AD… --mode forensic
```

The agent sees the same log content, same model responses, and same tool results as
the original run. If it produces the same output, the run is reproducible. If it does
not, something drifted — a model update, a tool change, or non-determinism.

The four modes, and when to reach for each:

| Mode | What it does | Use for |
|------|--------------|---------|
| `forensic` | Read-only inspection; no subprocess, no network | Audit, post-incident |
| `mocked` | Re-spawns the command; LLM calls served from the capsule cache; tool calls gated by a safety ladder | CI / regression |
| `semantic` | Re-executes and judges *meaning* (0.0–1.0 similarity), not tokens | Drifting remote LLMs |
| `exact` | Byte-exact eligibility; requires a deterministic env and per-call seed | Local / on-prem / compliance |

> NovaFabric does **not** claim byte-exact replay of remote LLM calls — remote models
> drift, so `semantic` is the honest mode for them. See the
> [replay section of the user guide](../user-guide.md) for the full mode reference.

Because a replay is **itself a new capsule**, you can diff the original against the
replay to quantify exactly what drifted:

```bash
nova diff capsules/01KR9Q2AD… capsules/01KRB4F7…
```

---

## 4. Trace data dependencies with lineage

**Primitive:** Lineage · **Maturity:** works today (v0.4)

Lineage is a directed provenance graph — a rebuildable SQLite cache derived from each
capsule's `lineage.jsonl`. Declare what data an agent consumed during capture:

```python
# inside your agent, before it runs
from nova_assets import record_consumed
record_consumed("datasets/training-set@1.0.0")
```

Then run `nova lineage import` after capture to write the edges:

```bash
nova capture --output-dir runs python agent.py
nova lineage import runs/01K…
```

Now query the graph. **"Which runs depended on this dataset?"**

```bash
nova lineage blast-radius datasets/training-set@1.0.0
```

Output:

```
Blast radius of datasets/training-set@1.0.0
├── run:01K…  (train-v1)
├── run:01K…  (eval-v1)
└── run:01K…  (promote-v1)
```

**"What did this specific run depend on?"**

```bash
nova lineage provenance 01KR9Q2AD…
```

When a dataset is found corrupt or wrong, blast radius tells you exactly which runs to
re-validate — no manual tracking, no grep. The graph is built automatically from what
each run declared it consumed, with mechanical edge types (`consumed`, `produced_by`,
`replayed_from`) and two confidence levels (`observed` at runtime vs `inferred` from
structure).

NovaFabric also emits **OpenLineage 2.0.2** START/COMPLETE/FAIL events, so these edges
flow into Marquez, Atlan, or OpenMetadata without a NovaFabric-specific integration.

> **Dashboard shortcut:** Open the dashboard's **Lineage** tab — it renders the full
> blast-radius and provenance graph interactively. Select any node to inspect its
> edges, and use the **Export OpenLineage** panel to emit the lineage for a run as a
> standards-compliant OpenLineage event without touching the CLI.

---

## 5. Capture without changing your agent code

**Primitive:** Run Capsule (wire-level capture) · **Maturity:** works today (v0.5–v0.6)

NovaFabric captures at the HTTP layer, so you do not need to touch agent code at all.
Under the hood, `nova capture` injects a `sitecustomize.py` over `PYTHONPATH` and
installs monkey-patches on the HTTP stack; the patches are removed after the run.
There are four entry points, and **all four produce the same capsule format:**

**Subprocess wrap** — the default:

```bash
nova capture python your_agent.py
```

**LLM API proxy** — for agents already running as services, or non-Python agents:

```bash
nova api-proxy --port 9900 --upstream http://localhost:11434

# In another terminal — agent thinks it's talking to Ollama directly:
OLLAMA_HOST=http://localhost:9900 python your_agent.py
```

**MCP proxy** — for agents using MCP tools over stdio:

```bash
nova mcp-proxy -- python mcp_server.py
```

**In-process** — for notebooks or embedded agents:

```python
from novafabric.capture.hooks import install_hooks
install_hooks()
# now all requests/aiohttp calls in this process are captured
```

Wire-level hooks cover `httpx`, `requests`, `aiohttp`, `urllib3`, Bedrock, and MCP
(`ClientSession.call_tool`); per-SDK hooks cover OpenAI (`Completions.create`) and
Anthropic (`Messages.create`). Missing SDK hooks are silently skipped — a capsule is
still written even when no AI SDK is present. To capture a provider NovaFabric does
not ship a hook for, see [Writing a hook plugin](../integrations/writing-a-hook-plugin.md)
(hooks are auto-discovered via the `novafabric.hooks` entry-point group).

---

## 6. Capture any LLM provider

**Primitive:** Run Capsule (wire-level capture) · **Maturity:** works today (v0.6)

Because the hook lives at the HTTP layer, not inside any provider SDK, NovaFabric is
provider-agnostic. Ollama, OpenAI, Azure OpenAI, and Amazon Bedrock are all captured
identically.

**Azure OpenAI:**

```bash
export AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
export AZURE_OPENAI_API_KEY=<your-key>
export AZURE_OPENAI_DEPLOYMENT=<deployment-name>

nova capture python examples/azure-openai/agent.py
```

The capsule records `gen_ai.system = "openai"` (the SDK identifier) regardless of
whether the backend is Azure, a private proxy, or an on-prem gateway. A vendored URL
registry classifies known providers (OpenAI, Anthropic, Cohere, Together, Mistral,
Replicate, Bedrock, Ollama), and every model call is recorded as JSONL in OTel GenAI
semconv — including `temperature`, `top_p`, and `seed`, which is what makes `exact`
replay determinism possible on local models.

---

## 7. Capture a LangChain / LangGraph agent

**Primitive:** Run Capsule (framework-neutral) · **Maturity:** works today

NovaFabric records what agent frameworks do without any framework-specific
instrumentation — it is framework-neutral by design. No special configuration is
needed; just wrap with `nova capture`:

```bash
export ANTHROPIC_API_KEY=sk-…
uv pip install langgraph langchain-anthropic

nova capture python examples/langchain-agent/agent.py
```

Inspect the full tool-use chain:

```bash
RUN=$(ls -dt "${NOVAFABRIC_HOME:-$HOME/.novafabric}/capsules/"*/ | head -1)
cat $RUN/model-calls.jsonl | jq -c '{
  msg: .["gen_ai.request.messages"][-1].content,
  response: .["gen_ai.response.choices"][0].message
}'
```

You see two LLM round-trips: the model deciding to call `add_two_numbers(17, 25)`, and
then incorporating the result `42` into its final answer. The whole tool-use chain is
captured without any LangChain-specific hooks — the same mechanism records LangGraph,
AutoGen, CrewAI, DSPy, or the OpenAI Agents SDK.

---

## 8. Build a signed evidence bundle

**Primitive:** Evidence Bundle · **Maturity:** works today (v0.4); DSSE/SLSA outer
envelope is experimental

For compliance, audits, or any situation where you need to prove what an agent did —
and prove the record has not been altered:

```bash
nova export-evidence capsules/01KR9Q2AD… --output bundle.zip --key ed25519.pem
# → bundle.zip  (ed25519-signed)
```

> `--output`/`-o` and `--key` are both **required** — `--key` a PEM-encoded ed25519
> private key (generate one with `python -m novafabric.evidence.signing`). Running
> the command without them exits non-zero with "`--key` is required in v0.4
> (local-key signing)."

The ZIP is self-contained: it embeds the capsule, a lineage subgraph, in-toto DSSE
attestations, ed25519 signatures, and vendored JSON schemas. An auditor can verify it
with only `sha256sum` plus an ed25519 verifier — **no NovaFabric runtime required.**
That offline-verifiability is what makes the Evidence Bundle the compliance primitive.

To also publish the DSSE envelope to the Rekor transparency log (requires
`NOVA_REKOR_URL`):

```bash
export NOVA_REKOR_URL=https://rekor.sigstore.dev
nova export-evidence capsules/01KR9Q2AD… --output bundle.zip --key ed25519.pem --sigstore
# ✓ evidence bundle written: bundle.zip
# ✓ Rekor transparency log entry: 24296fb24b3c…
```

If `NOVA_REKOR_URL` is not set the step is silently skipped (exit 0) — nothing reaches
the network unless you point it there.

### Standard outer envelopes — DSSE / SLSA (experimental)

If your verifier already speaks the [in-toto](https://in-toto.io/) /
[SLSA](https://slsa.dev/) ecosystem (e.g. `cosign`), NovaFabric can wrap the bundle in a
**standard DSSE outer envelope** so it verifies with stock tooling — no NovaFabric
dependency on the verifier side. This is **experimental** (implemented on `main`;
opt-in; the bundle output is byte-for-byte unchanged without the flag):

```bash
# Emit <bundle>.dsse.json alongside the ZIP (wraps the bundle manifest)
nova export-evidence capsules/01KR9Q2AD… --output bundle.zip --key ed25519.pem --dsse

# Emit a SLSA v1 provenance attestation on a successful promotion
nova promote direct my-model@1.0.0 --to staging --slsa-provenance --slsa-out my-model.slsa.json

# Verify any of the above with the public key (same verdict as `cosign verify-blob-attestation`)
nova verify-envelope bundle.zip.dsse.json --key ed25519.pub.pem
# ✓ verified  payloadType=application/vnd.novafabric.bundle+json  keyid=…
```

The inner bundle bytes are the envelope payload verbatim — **wrap, don't replace.** See
the [CLI reference](../cli-reference.md) for the full flag set.

---

## 9. Cryptographically seal a capsule (NovaSeal)

**Maturity note — read this first (corrected 2026-07-30).** DSSE signing, RFC 3161
timestamping, and the append-only Merkle log are **implemented and tested**
(`src/novafabric/trust/novaseal/` — envelope/merkle/ratchet/timestamp/trust_chain,
ADR-0041/ADR-0070; `tests/seal/` including a p99 latency gate), not merely design
intent — an earlier draft of this note (and of `why-novafabric.md`) claimed
RFC-3161 timestamping itself was still planned; that was stale and is corrected
here. The `nova capture` / `nova verify` seal flow below is present on `main`
behind an opt-in `novaseal.yaml`; treat the whole NovaSeal surface as
**experimental** (interfaces may still change before v1.0), not "planned, not
built." Also shipped in this space: the ed25519-signed Evidence Bundle in
[§8](#8-build-a-signed-evidence-bundle), in-toto DSSE attestations, verifiable
redaction proofs, OPA/Rego policy gates with maker-checker promotion, WORM
storage adapters, a Merkle Mountain Range append-only accumulator (v0.79.0),
and checkpoint/witness cosigning (v0.75.0, ADR-0097).

For regulated environments that need DSSE signatures, RFC 3161 timestamps, and an
append-only Merkle log:

**One-time setup:**

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
tsa_url: https://freetsa.org/tsr
merkle_db: ~/.novafabric/novaseal-merkle.db
```

**Capture seals automatically once configured:**

```bash
nova capture python my_agent.py
nova verify "$NOVAFABRIC_HOME/capsules/01HXAY7M5JZ8R7K4P9DPBYK2WX/"
# signature_ok=True, timestamp_ok=True, log_integrity_ok=True
```

> **Dashboard shortcut:** Open the dashboard's **Seal** tab and use the **Capsule
> Integrity Verify** panel — enter the run ID, click **Verify capsule**, and see
> per-check pass/fail for signature, timestamp, and Merkle log inclusion without
> leaving the browser. Since v0.98.0 the same tab also carries the **Trust Radar**
> (which trust guarantees this capsule can actually evidence — an axis it cannot
> evidence is drawn hollow and excluded from the filled claim polygon, so it never
> reads as a failure) and the **Redaction X-Ray** (field paths and protection
> states only; the API never returns a field value). Both are `experimental`.

Sealing is completely opt-in. Without `novaseal.yaml`, capture is unchanged.

---

## 10. Build a Capsule Knowledge Graph

**Maturity:** experimental

Once you have captured a few runs, you can build a **cross-capsule knowledge graph**
that aggregates which agents called which models and used which tools. This graph is a
**derived aggregation artifact** — separate from the lineage graph in
[§4](#4-trace-data-dependencies-with-lineage) — and is safe to rebuild from capsule
replay at any time.

```bash
# One-time setup
pip install 'novafabric[scale-kg]'   # adds kuzu>=0.11.3
nova kg init                          # creates .nova/kg/nova_kg.kuzu

# Ingest a captured run (positional capsule directory, not --capsule/capsule.yaml)
nova kg ingest capsules/01KR9Q2AD…/

# Inspect what's in the graph
nova kg status
nova kg query agent-001 --output text
```

**MCP server auto-detection:** tool names containing `:` (e.g. `filesystem:read_file`,
`github:search_code`) are automatically split — the left part becomes an `MCPServer`
node, the right the `Tool` node, joined by a `SERVED_BY` edge. No configuration
required.

The KG schema has **5 node types** (`Agent`, `Model`, `Tool`, `MCPServer`,
`InferenceEndpoint`) and **4 relationship types** (`CALLS`, `USES_TOOL`, `SERVED_BY`,
`ROUTES_TO`). `nova serve` ingests both `model-calls.jsonl` and `tool-calls.jsonl`
automatically every 60 seconds, so MCP tool-call events are included without manual
ingestion.

Dashboard equivalent: open `nova serve --experimental` and visit the **KG** tab. The
**Multi-Layer Topology** panel shows per-layer node counts and edge breakdown.
Additional KG dashboard panels:

| Panel | CLI equivalent |
|---|---|
| **KG Query** | `nova kg query <entity>` — search for agents, models, or tools by name |
| **KG Audit** | `nova kg audit` — orphaned edges and zero-call-count anomalies |
| **Entity Queue** | `nova kg entity-queue list/approve/reject` — human review queue for ambiguous entities |
| **KG Aliases** | `nova kg alias list/register` — alias → canonical entity mapping management |

For the human review queue:

```bash
nova kg entity-queue list                         # pending review items
nova kg entity-queue approve ITEM_ID              # resolve to canonical entity
nova kg entity-queue reject ITEM_ID               # discard ambiguous candidate
nova kg alias register gpt4 openai/gpt-4 model    # register an alias
```

---

## 11. Hunt anomalies with the Security & Provenance KG (SPKG)

**Maturity:** experimental (ADR-0111)

The Capsule KG in [§10](#10-build-a-capsule-knowledge-graph) *aggregates* who-called-what.
The **SPKG** is a different, security-focused graph: it turns a capsule's lineage into a
provenance graph and answers *"which action here doesn't look like anything my fleet
normally does?"*, *"is there an attack path from X to Y?"*, and *"if this model is
poisoned, what did it touch?"* Every anomaly carries a **MITRE ATT&CK** technique, not a
bare score. It is opt-in and never blocks other commands.

```bash
# The anomaly detector needs NO extra (pure stdlib). RDF/graph commands need:
pip install 'novafabric[spkg]'        # rdflib + pyshacl + kuzu (all permissive)

# 1. Unsupervised anomaly scan — try the shipped example (planted shell exec):
python examples/spkg-anomaly-scan/make_fixture.py
nova kg detect examples/spkg-anomaly-scan/capsule/ -k 3
#   → row 1: run-evil → tool:/bin/shell   score 1.000   ATT&CK T1059.004

# 2. Build the SPKG for a real capsule (canonical PROV-O, SHACL-gated, + KùzuDB graph):
nova kg build path/to/capsule/

# 3. Attack-path query (lateral movement):
nova kg attack-path path/to/capsule/ --from run:attacker --to dataset:aws_credentials

# 4. Blast radius (what a poisoned model touched; --upstream for provenance):
nova kg blast-radius path/to/capsule/ --entity model:suspect-model
```

You can also learn "normal" from a corpus of known-good runs and scan a suspect
capsule, or export PROV-O RDF and machine-readable findings for a pipeline:

```bash
# Export a capsule's provenance as PROV-O RDF (SHACL-gated — invalid facts are rejected)
nova kg build-provenance path/to/capsule/ -o provenance.ttl

# Learn "normal" from known-good runs, then scan a suspect capsule
nova kg detect suspect/ --baseline normal-week-1/ --baseline normal-week-2/

# Emit machine-readable findings (each carries an ATT&CK technique) for a pipeline
nova kg detect suspect/ --json -o findings.json
```

A `tool:/bin/shell` or `dataset:aws_credentials` edge that never appears in the learned
distribution surfaces at the top of the scan, mapped to `T1059.004` (Unix shell) or
`T1078` (valid accounts). Every finding must map to a MITRE ATT&CK technique and/or
D3FEND countermeasure (ADR-0111 R2 — a bare anomaly score is SHACL-rejected).

> **Planned, not shipped:** the PyGOD/TGN GNN detectors, the 1M-edge scale tier, and a
> dashboard overlay are planned upgrades. The stdlib anomaly baseline and RDF/KùzuDB
> layers above ship today. Full guide:
> [`docs/security-knowledge-graph.md`](../security-knowledge-graph.md).

---

## 12. Manage capture-level policy

**Maturity:** experimental (cap-004)

`nova capture` records different field sets depending on the capture-level policy. Four
levels are available:

| Level | What's captured |
|---|---|
| `minimal` | metadata only (run_id, event_type, timestamp, exit_code, duration_ms) |
| `standard` | metadata + model_id, provider, token counts, tool name + result summary |
| `forensic` | everything (prompts, responses, tool args, full context snapshots) |
| `air_gapped` | everything + blocks external network calls in the runner |

```bash
nova policy capture-level get
# Current capture level: standard

# To change: set the env var and restart `nova serve` / re-invoke `nova capture`
export NOVA_CAPTURE_LEVEL=forensic
```

By default NovaFabric does not capture full prompts and responses — `standard` records
summaries, and `forensic` is the explicit opt-in to full context. Dashboard equivalent:
the **Policy** tab has a Capture Level panel with the same get/set semantics.

---

## 13. Split-storage and GDPR erasure

**Maturity:** experimental, flag-gated (cap-003)

The dual-object split keeps the PII payload (prompts, responses) in a separately
addressable S3 object so it can be erased without breaking the audit trail. **This is
gated on a feature flag** — `NOVA_CAP003_ENABLED=true` — until the legal review (OQ-01)
closes.

```bash
# Validate the S3 backend supports Object Lock COMPLIANCE
nova storage validate --endpoint https://s3.amazonaws.com --bucket nova-capsules

# Inspect the object split for a run (informational; full behaviour gated on flag)
nova storage inspect --run-id run_xyz

# Request erasure of a subject's PII payload
nova erasure request --run-id run_xyz
nova erasure status --request-id <id>
```

This supports a GDPR right-to-erasure workflow: the audit trail (hashes, signatures,
lineage) survives while the subject's PII payload is deleted. Dashboard equivalents: the
**Compliance** tab has a GDPR Erasure panel; the **Infra** tab has a Storage Operations
card.

---

## 14. Generate reports from the dashboard

**Maturity:** experimental (Layer A dashboard)

The **Reports** tab in `nova serve --experimental` provides a Catalog + Builder layout
covering four audience groups:

| Group | Report types |
|---|---|
| Developer | Run History, Eval Regression, Capsule Compare |
| Ops | Cost Burn, Throughput |
| Compliance | Evidence Inventory, Policy Audit, Seal Verification |
| Management | Executive Summary, Release Comparison |

Select a report on the left, set date-range and optional filters in the filter bar, then
export as **CSV**, **JSON**, or **PDF** (via browser print).

Each report maps to a `GET /api/reports/<type>` endpoint; you can also fetch them
directly:

```bash
# Run history for the past 7 days, CSV format
curl "http://127.0.0.1:4321/api/reports/run-history?token=<TOKEN>&from=2026-05-13&format=csv"

# Evidence inventory as JSON
curl "http://127.0.0.1:4321/api/reports/evidence-inventory?token=<TOKEN>"
```

> The dashboard is **local-only**: it binds `127.0.0.1`, requires a session token, and
> every page shows its CLI equivalent so anything you see can be reproduced in a
> pipeline. It is not a hosted UI. Reports are read-only (Layer A); the write actions
> the dashboard does expose (Layer B — register, eval, promote, forensic replay,
> redact, export evidence) are confirm-gated and audit-logged to
> `~/.novafabric/dashboard-audit.jsonl`.

---

## 15. Open the dashboard topology view

**Maturity:** experimental

NovaFabric ships two complementary topology views:

```bash
# 2D Sigma.js view — --experimental is mandatory (ADR-0027 graduation gate);
# omitting it just prints an "EXPERIMENTAL" notice and exits without starting anything
nova serve --experimental --topology

# 3D Three.js view (TV-5)
nova serve --experimental --tv5
```

Both views render the agent / model / tool call graph in real time from the ADS encoder
+ DeltaBuffer pipeline. The TV-5 3D view adds depth via per-tier Z-stacking using
`networkx.spring_layout`.

> Note: the **live cluster-scale topology dashboard** (`nova serve --topology` at
> 1,000,000 agents, ADS-schema-validated live streaming) is **planned design intent**
> and requires prototype spikes first — see [ROADMAP.md](../../ROADMAP.md). The 2D/3D
> views above render your local captured graph.

---

## 16. Export compliance reports from the dashboard

**Maturity:** experimental

Four compliance-export CLI commands have full dashboard equivalents in the **Compliance
tab** of `nova serve --experimental`. No command line is needed for compliance officers
or auditors.

| Panel | Sub-group | CLI equivalent | Endpoint | Regulation |
|---|---|---|---|---|
| GDPR Art.30 RoPA Export | Privacy | `nova export-ropa` | `POST /api/compliance/export/ropa` | GDPR Art.30 |
| AI-SBOM Export | Exports | `nova export-aibom` | `POST /api/compliance/export/aibom` | EU CRA (2026-09-11) |
| NIST AI RMF Report | Frameworks | `nova export-nist-rmf` | `POST /api/compliance/export/nist-rmf` | NIST AI 100-1 |
| AI-SBOM Coverage Status | Exports | `nova aibom status` | `GET /api/aibom/status` | EU CRA |

**Quick start — dashboard or CLI compliance export:**

```bash
# Start the dashboard
nova serve --experimental

# OR use the CLI directly for automation
nova export-ropa capsules/<run_id>/ \
  --output ropa.json \
  --controller-name "ACME Corp" \
  --controller-contact "dpo@acme.example"

nova export-aibom capsules/<run_id>/ --output aibom.json

nova export-nist-rmf capsules/<run_id>/ --output nist-rmf.json

nova aibom status   # shows coverage across all capsules
```

In the dashboard, open the **Compliance** tab and pick the sub-group from the segmented
control at the top — since v0.97.0 the tab is a hub over five groups (Frameworks ·
Audits · Privacy · Exports · Assurance) rather than one long scroll. Each group is
deep-linkable as `?tab=compliance&sub=<group>` (for example
`?tab=compliance&sub=privacy` for the RoPA panel), and the groups are also reachable
from the `⌘K` / `Ctrl-K` command palette. Each panel shows the equivalent CLI command
at the bottom so teams can reproduce the export in CI pipelines.

> **Honest framing:** these exports produce evidence that *supports* a compliance
> workflow — they do not certify or guarantee compliance. NovaFabric attests that a
> capsule is unmodified since signing; it does not vouch for content compliance with any
> regulation.

---

## 17. Prove supply-chain provenance & eval integrity

**Primitive:** Run Capsule / Evidence Bundle · **Maturity:** experimental (every command
below is on `main`, opt-in, and additive — omitting the flag leaves today's output
byte-for-byte unchanged)

Once you can capture and seal a run, the next question an auditor asks is *where did the
inputs come from, and can I trust the score?* This section chains five recent additions
that answer it — dataset provenance, benchmark-contamination detection, SLSA-for-ML
promotion provenance, portable OTel spans, and lineage run facets. Each is independent;
pick the ones your workflow needs.

### Record where a dataset came from (NF-058)

Emit a signed **dataset provenance card** — source, version, content hash, license, and a
content-addressed *transform history* (operation digests only, never raw values):

```bash
nova dataset provenance-card dataset:gaia@2026-05 \
    --source oci://reg/gaia:2026-05 --version 2026-05 --hash b17a... \
    --license CC-BY-4.0 --tlp TLP:CLEAR --sign --out card.json
```

Use `--from-capsule ./my-capsule` to derive the `transformHistory[]` from that capsule's
`lineage.jsonl` derivation edges. The card is Ed25519-signed over its canonical body
(reusing the same keyring as the Evidence Bundle), so **an unsigned card is
schema-invalid** — a signed card is evidence. It feeds the AI-BOM (`nova export-aibom`)
and the SLSA-for-ML attestation below.

### Flag a contaminated or superseded benchmark (NF-028)

Benchmark contamination silently inflates eval scores. NovaFabric records the dataset +
split content hashes an eval ran against, so a capsule can be checked against a registry
of known-bad hashes:

```bash
nova eval contamination-check ./my-capsule --registry known-bad.json --json
```

It reports a status per dataset (`current` / `superseded` / `contaminated` / `unknown`)
and **exits `4` when any dataset is contaminated or superseded** — so CI can gate on it.
The registry is configurable (no hardcoded URL); it can only *raise* a facet's severity,
never downgrade a recorded status. Detection only — no remediation.

### Attach SLSA-for-ML provenance to a promotion (NF-057)

[§8](#8-build-a-signed-evidence-bundle) showed the generic SLSA v1 attestation. Add
`--slsa-ml-profile` to emit the **SLSA-for-ML** profile instead — its `buildDefinition`
captures dataset versions/hashes and seeds, and its byproducts bind the promoted model to
the exact gating **eval verdict**:

```bash
nova promote direct my-model@1.0.0 --to staging \
    --slsa-provenance --slsa-ml-profile --slsa-out my-model.slsa.json
```

The result is DSSE-signed and verifies with `nova verify-envelope` (or stock `cosign`)
exactly like the generic attestation.

### Export the run as portable OTel GenAI spans (NF-032/033)

Map an already-captured capsule *outward* to OpenTelemetry GenAI `gen_ai.*` spans — a
root `invoke_agent` span, a `chat` client span per model call, and an `execute_tool` span
per tool call:

```bash
nova capture --emit-otel-genai python agent.py
# → writes <capsule>/otel-genai-spans.json
```

Message/choice **content is off by default** (ADR-0021). Add `--capture-content` to
include request messages — they are routed through the same ADR-0009 secret-redaction
gate as the sealer and size-bounded. Every span carries an honest `semconv_maturity`
(`stable` on LLM client spans, `development` on agent/tool spans, matching OTel's own
status in early 2026).

### Emit NovaFabric run facets to OpenLineage (NF-036/037)

[§4](#4-trace-data-dependencies-with-lineage) emits standard OpenLineage events. Add
`--with-facets` to attach NovaFabric's custom run facets (capsule id, eval verdict,
promotion-policy decision, reproducibility run params), and `--otel-correlation` to link
a lineage node to its OTel spans by `trace_id`/`span_id`:

```bash
nova lineage emit-openlineage ./my-capsule --with-facets --otel-correlation
```

Facets are additive and schema-validated before emission — a consumer that ignores custom
facets (Marquez, Atlan, OpenMetadata) still sees unchanged core OpenLineage events.

> **Honest framing:** these five surfaces are **experimental** — implemented and tested
> on `main`, but their on-disk shapes are not frozen until v1.0. They *produce and
> verify* provenance evidence; they do not certify compliance or vouch for the content of
> a dataset or model.

---

## 18. Run an evidence-grade eval loop — zero tokens

**Primitive:** Eval over Run Capsules · **Maturity:** experimental (NF-002/007/009/028,
ADR-0099, ADR-0108 — implemented and tested on `main`; shapes not frozen until v1.0)

[§17](#17-prove-supply-chain-provenance--eval-integrity) introduced the idea that a
score is evidence, not a number. This section runs the whole loop on your machine
with **zero model calls**: structural checks over an already-stored capsule, a signed
eval card, a statistically-gated regression diff, and a benchmark-contamination gate.

This section and the next use the stdlib-only agent in
[`examples/eval-and-intervention/`](../../examples/eval-and-intervention/). It performs
three deterministic "tool calls" (two word-counts — one of them a whitespace variant of
the other — and one lookup) and self-reports them into the live capsule's
`tool-calls.jsonl` via `NOVAFABRIC_CAPSULE_DIR`. In a real deployment the MCP hook,
`nova mcp-proxy`, or a hook plugin records tool calls for you; the self-report only
keeps the example free of third-party dependencies.

```bash
mkdir -p /tmp/nova-tour

# Consistent behavior
AGENT_MODE=baseline nova capture --output-dir /tmp/nova-tour \
    python examples/eval-and-intervention/agent.py

# Planted consistency regression (the whitespace variant is miscounted)
AGENT_MODE=regressed nova capture --output-dir /tmp/nova-tour \
    python examples/eval-and-intervention/agent.py

CAPS=($(ls -d /tmp/nova-tour/*/))
BASE=${CAPS[0]}   # baseline capsule
REG=${CAPS[1]}    # regressed capsule
```

### Zero-token structural checks (`nova eval offline`, NF-009)

The capsule is already on disk, so these checks are pure arithmetic over recorded
events — no tokens, no network:

```bash
# Did the run exercise every declared tool?  (reads tool-calls.jsonl)
nova eval offline --capsule $BASE --check coverage --declared-tools word_count,lookup
# tool_coverage = 1.0  (source=code, zero-token)

nova eval offline --capsule $BASE --check coverage --declared-tools word_count,lookup,search
# tool_coverage = 0.6666666666666666  (source=code, zero-token)

# Do recorded outputs satisfy a JSON-schema contract?
nova eval offline --capsule $BASE --check contract \
    --schema examples/eval-and-intervention/out.schema.json --field output
# output_contract = 1.0  (source=code, zero-token)

# Do equivalent inputs produce consistent outputs?  (declarative check-spec)
nova eval offline --capsule $BASE --check metamorphic \
    --spec examples/eval-and-intervention/check-spec.yaml --emit-score
# whitespace_consistency = True  (source=code, zero-token)
# Recorded 01KXK4ATC5… → /tmp/nova-tour/01KXK4AB…/scores.jsonl

nova eval offline --capsule $REG --check metamorphic \
    --spec examples/eval-and-intervention/check-spec.yaml
# whitespace_consistency = False  (source=code, zero-token)
```

The metamorphic check groups records whose *input* collapses to the same key under the
spec's `transform` (here `[lower, strip, collapse_whitespace]`, so `"Hello World"` and
`"  hello   world "` pair up) and asserts the spec's `invariant` over each pair's
*output*. The regressed run miscounts the whitespace variant, so the invariant fails —
a consistency regression caught without re-running anything.

> **Reality check:** `nova eval offline` exits `0` whenever the check *runs* — a
> `False` result is a recorded score, not a process failure (a malformed spec exits
> `2`). Gating happens downstream: `--emit-score` appends the score to
> `<capsule>/scores.jsonl`, where it is covered by the capsule Merkle root (any
> Evidence Bundle built from the capsule detects score tampering), and the
> significance diff below turns accumulated scores into a CI gate.

### A score is a signed record (`nova eval card`, NF-002/010)

Every score names its evaluator. An **eval card** pins the evaluator identity and is
Ed25519-signed with the local keyring (`nova init` created it):

```bash
nova eval card new --source code --card-id exact-match --name "Exact Match" --out card.json
nova eval card sign card.json        # Signed card.json  key_id=6eaaeb98…  digest=sha256:1c06ab9a…
nova eval card register card.json    # Registered eval-card:exact-match@0.1.0+sha256:1c06ab9a…
nova eval card verify exact-match@0.1.0
# signature_ok=True  calibration_present=True  digest=sha256:1c06ab9a…

nova eval score list --capsule $BASE     # the metamorphic score recorded above
```

### Significance-gated diff (`nova diff --significance`, NF-007)

A single-run dip should never fire a regression gate. `nova diff --significance`
compares two stored outcome histories by **statistical significance, not raw delta**
— a Wilson interval per side plus a sequential SPRT — and is again zero-token. The
example ships a generator that simulates the accumulated `scores.jsonl` history of a
boolean `task_pass` metric (47/50 baseline, 38/50 candidate, failures interleaved the
way real flaky-run records are):

```bash
python examples/eval-and-intervention/make_scores.py /tmp/nova-tour/scores

nova diff --significance \
    --baseline /tmp/nova-tour/scores/baseline/scores.jsonl \
    --candidate /tmp/nova-tour/scores/candidate/scores.jsonl \
    --metric task_pass
```

```
metric: task_pass
baseline:  47/50  wilson=[0.838, 0.979]
candidate: 38/50  wilson=[0.626, 0.857]
SPRT verdict: accept_h1  llr=3.10
```

Exit code **`3`** — a statistically significant regression; gate CI on it. With only
four candidate runs (one of them failing), the same command answers honestly that it
cannot tell yet:

```bash
head -4 /tmp/nova-tour/scores/candidate/scores.jsonl > /tmp/nova-tour/scores/one-dip.jsonl
nova diff --significance --baseline /tmp/nova-tour/scores/baseline/scores.jsonl \
    --candidate /tmp/nova-tour/scores/one-dip.jsonl --metric task_pass
# candidate: 3/4  wilson=[0.301, 0.954]
# SPRT verdict: continue  llr=0.34        (exit 0 — collect more evidence)
```

Three verdicts, three behaviors: `accept_h0` (no regression), `accept_h1` (block),
`continue` (defer — do not fire on noise). The SPRT is **sequential**: it walks the
outcome sequence in order, so ordering matters, and it assumes i.i.d. pass/fail
outcomes (see ADR-0080 for the known limitation with correlated cascades).

### Benchmark-contamination gate (`nova eval contamination-check`, NF-028)

Contaminated benchmarks silently inflate scores. Eval runs record the dataset + split
content hashes they ran against as additive `dataset_provenance` facets under
`extensions/dev.novafabric.dataset-provenance/`; the check resolves them against a
**configurable** known-bad registry (no hardcoded URL). Here we plant a facet by hand
so you can watch the gate fire:

```bash
mkdir -p $BASE/extensions/dev.novafabric.dataset-provenance
cat > $BASE/extensions/dev.novafabric.dataset-provenance/word-count-suite.json <<'EOF'
{"name": "word-count-suite", "version": "2026-05",
 "dataset_hash": "sha256:9c56cc51b374c3ba189210d5b6d4bf57790d351c96c47c02190ecf1e430635ab",
 "split_hash": "", "status": "unknown"}
EOF

cat > /tmp/nova-tour/known-bad.json <<'EOF'
{"contaminated": ["sha256:9c56cc51b374c3ba189210d5b6d4bf57790d351c96c47c02190ecf1e430635ab"],
 "superseded": []}
EOF

nova eval contamination-check $BASE                                     # unknown, exit 0
nova eval contamination-check $BASE --registry /tmp/nova-tour/known-bad.json
```

```
        Benchmark contamination check
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ Dataset          ┃ Version ┃ Status       ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ word-count-suite │ 2026-05 │ contaminated │
└──────────────────┴─────────┴──────────────┘
```

Exit code **`4`** — CI-gateable. The registry can only *raise* a facet's recorded
severity, never downgrade it, and the check is detection-only (no remediation).

---

## 19. Ask "what if?" with intervention replay

**Primitive:** Replay (5th mode) · **Maturity:** experimental (v0.50.0, ADR-0086)

The four replay modes in [§3](#3-replay-a-past-run) answer *"what happened?"*.
**Intervention replay** answers the counterfactual: *what if the model had answered
differently? What if the tool had returned something else?* You supply an
**InterventionSpec** — one target selector plus exactly one substitution — and the
engine replays the capsule with the substitution applied, re-executing downstream
steps under mocked semantics. The output is a new, minimal capsule hard-marked
`replay_mode: intervention` so it can never be mistaken for an original run.

The example ships a spec that asks: *what if the second word-count call had answered
3 instead of 2?*

```yaml
# examples/eval-and-intervention/intervention.yaml
target:
  stream: tool-calls        # or model-calls
  event_index: 1            # exactly one of event_index / span_id
mutate_payload:             # exactly one of replace_model_response /
  output: {value: 3}        #   replace_tool_result / mutate_payload
checks:
  - name: substitution_applied
    stream: tool-calls
    event_index: 1
    field: output.value     # dotted path into the event record
    equals: 3
```

Run it against the baseline capsule from [§18](#18-run-an-evidence-grade-eval-loop--zero-tokens):

```bash
nova replay $BASE --mode intervention \
    --intervention-file examples/eval-and-intervention/intervention.yaml \
    --output-dir /tmp/nova-tour/replays
# ✓ Replay written: /tmp/nova-tour/replays/01KXK4E4…  (replay_id=01KXK4E4…  mode=intervention)

CF=$(ls -d /tmp/nova-tour/replays/*/ | tail -1)
cat $CF/capsule.yaml
```

The counterfactual capsule is self-describing — selector, substitution, and check
outcomes are all recorded, and the source capsule is never touched:

```yaml
intervention:
  check_count: 1
  checks:
  - detail: 'output.value == 3: got 3'
    name: substitution_applied
    passed: true
  matched_event_index: 1
  selector: {event_index: 1}
  stream: tool-calls
  substitution: mutate_payload
replay_mode: intervention
replay_of_run_id: 01KXK4AB…
```

The mutated event carries an explicit `intervened: true` marker. Now close the loop
with the tools you already know — the counterfactual is **diffable** and
**evaluable** like any capsule:

```bash
# Structural diff: original vs counterfactual
nova diff $BASE $CF

# Zero-token consistency check over the counterfactual world
nova eval offline --capsule $CF --check metamorphic \
    --spec examples/eval-and-intervention/check-spec.yaml
# whitespace_consistency = False  (source=code, zero-token)
```

The intervention broke the metamorphic invariant — you just measured the downstream
behavioral consequence of a hypothetical change without ever re-running the model.

Honest notes on the current implementation:

- The spec allows **one substitution per replay** (`replace_model_response` for
  `model-calls`, `replace_tool_result` for `tool-calls`, or `mutate_payload` for
  arbitrary fields on either stream). Chained multi-step interventions are not a thing yet.
- Check-functions are named per-step assertions; failures are reported, never
  swallowed, and a check with `fatal: true` **aborts** the replay (status `aborted`).
- The output capsule is deliberately *minimal* (manifest + mutated event streams), so
  `nova diff` against the original also reports the environment fields the
  counterfactual does not carry, and a mutated tool call whose payload changed can
  align as added/removed rather than changed (alignment hashes include the payload).
  Read the `intervention:` block for the authoritative story.

---

## 20. Walk the Accountability Spine (energy, ledger, safety case)

**Maturity:** experimental (v0.55.0, ADRs 0093–0095). Three research-grounded surfaces
for tamper-evident ex-post evidence — energy receipts per action, per-stream ledger
sealing, and an evidence-grounded safety case. All additive and opt-in; none is a
third top-level format (ADR-0034).

This section keeps working on the `$BASE` capsule from
[§18](#18-run-an-evidence-grade-eval-loop--zero-tokens) — any capsule with recorded
model/tool calls works.

### Energy receipts (`nova energy`, ADR-0093)

One `EnergyReceipt` per recorded action, with a forgery guard that checks claims
against what the host can actually measure:

```bash
nova energy probe
# readable energy counters: none — per-action energy is unavailable on this host
# (receipts will be honestly marked 'unavailable').

nova energy attest $BASE
# wrote 3 energy receipt(s) to /tmp/nova-tour/01KXK4AB…/energy-receipts.jsonl

nova energy report $BASE --format table
```

```
action        source                confidence        joules
tool_call     unavailable           unknown                -
tool_call     unavailable           unknown                -
tool_call     unavailable           unknown                -
total: 3 receipt(s)
```

On a laptop without readable RAPL/NVML counters the receipts are **honestly
`unavailable`** — never fabricated (the degrade-safe default). On a Slurm cluster,
`sacct ConsumedEnergyRaw` yields the measured per-job class. The forgery guard makes
the honesty tamper-evident — edit a receipt to *claim* a measurement the host cannot
back:

```bash
nova energy verify $BASE
# OK: 3 receipt(s) consistent; conservation status=unmeasurable

# forge the first receipt: measurement_source→rapl_pkg, measured_joules→0.42 …
nova energy verify $BASE
# [01KXK4EX…] payload_hash mismatch: recorded sha256:973849c9…, recomputed sha256:c498bcdd…
# [01KXK4EX…] confidence='unknown' must have measured_joules=null
# FAIL: 1/3 receipt(s) failed integrity                          (exit 3)
```

Exit codes: `0` OK, `3` receipt-integrity failure, `4` energy conservation diverged.

### Per-stream ledger sealing (`nova ledger`, ADR-0094)

The ledger builds a sidecar hash-chain per capsule `.jsonl` stream and DSSE-signs a
checkpoint — the evidence streams themselves are **never modified**. Use the Ed25519
key `nova init` generated:

```bash
KEY=${NOVAFABRIC_HOME:-$HOME/.novafabric}/keys/signing_key.pem

nova ledger anchor $BASE --key $KEY
# ✓ Anchored 7 stream(s); checkpoint 01KXK4G3… → /tmp/nova-tour/01KXK4AB…/.ledger/checkpoint.json

nova ledger status $BASE
```

```
assets             seq_high=-1   records=0    status=none
energy-receipts    seq_high=2    records=3    status=none
lineage            seq_high=-1   records=0    status=none
model-calls        seq_high=-1   records=0    status=none
scores             seq_high=0    records=1    status=none
tool-calls         seq_high=2    records=3    status=none
trace              seq_high=0    records=1    status=none
```

Note what got anchored: the tool calls, the **eval scores from §18**, and the
**energy receipts from above** are now all covered by signed chains. Verification
detects content edits, reordering, and truncation with a named exit-code taxonomy —
try two tampers and watch each get the right verdict:

```bash
nova ledger verify $BASE                            # ✓ ledger OK              (exit 0)

sed -i 's/Paris/London/' $BASE/tool-calls.jsonl     # content edit
nova ledger verify $BASE
# ✗ TAMPER (exit 3)
#   - stream tool-calls: record-digest multiset diverges from sealed chain (content edit)

# …restore, then drop the last record instead (truncation)
nova ledger verify $BASE
# ✗ TRUNCATION (exit 5)
#   - stream tool-calls: record count 2 != sealed 3
```

Full taxonomy: `3` tamper, `4` reorder, `5` truncation, `6` bad signature, `10` no
checkpoint. Restore the original stream and verification returns to `✓ ledger OK`.

### Evidence-grounded safety case (`nova safety-case`, ADR-0095)

A safety case is a Claims-Arguments-Evidence tree compiled **from the capsule's real
artifacts** — a naked (unsupported-but-claimed) claim is a schema error, and an
unresolvable claim becomes an honest `UNSUPPORTED`, never a silent pass. On a bare
capsule that honesty is the whole story:

```bash
nova safety-case build $BASE --template clymer-generic-v0 --output /tmp/nova-tour/case.json
nova safety-case export /tmp/nova-tour/case.json --format markdown
# - C-record-integrity [UNSUPPORTED] — The recorded run is faithfully reproducible…
# - C-harm-avoidance   [UNSUPPORTED] — The agent avoids unsafe actions…
# - C-logging          [UNSUPPORTED] — The run's process logging is complete…
# - C-root             [UNSUPPORTED] — Deployment presents acceptable residual risk.
```

Claims flip to `SUPPORTED` only when evidence artifacts exist in the capsule. Two of
the three come straight from `nova evidence` (ADR-0087):

```bash
# C-logging ← completeness assertion (per-stream counts, capture level, time window)
nova evidence completeness $BASE -o $BASE/completeness.json

# C-record-integrity ← DSSE-signed re-performance attestation (replays the run)
mkdir -p $BASE/attestations
nova evidence attest-replay $BASE --key $KEY --mode mocked \
    -o $BASE/attestations/reperformance.intoto.json
# ✓ Re-performance attestation: … (mode=mocked, match=semantic-match)

# C-harm-avoidance ← a judge-aggregate eval result (from your eval harness)
mkdir -p $BASE/eval-results
cat > $BASE/eval-results/harm-avoidance.json <<'EOF'
{"judges": ["judge-a", "judge-b", "judge-c"], "inter_judge_agreement": 0.82,
 "consensus_verdict": "pass", "sample_size": 50, "successes": 47,
 "process_evidence_ref": "scores.jsonl"}
EOF
```

> **Integration seam (real, as of v0.59):** `nova safety-case build` reads a *raw*
> attestation JSON at `attestations/reperformance.json`, while `nova evidence
> attest-replay` emits a DSSE-*wrapped* envelope. Feed the compiler the envelope's
> payload predicate (one line of Python) or it will bind the envelope file and mark
> the claim `CONTESTED` with reason `Replay match == ''`:
>
> ```bash
> python3 -c "import base64, json; env = json.load(open('$BASE/attestations/reperformance.intoto.json')); \
> json.dump(json.loads(base64.b64decode(env['payload']))['predicate'], \
> open('$BASE/attestations/reperformance.json', 'w'), indent=2)"
> ```

Rebuild, verify, and export — including the regulatory renderers:

```bash
nova safety-case build $BASE --template clymer-generic-v0 --output /tmp/nova-tour/case.json
# - C-record-integrity [SUPPORTED] — evidence E-replay-1 (replay_attestation): attestations/reperformance.json sha256:68081594…
# - C-harm-avoidance   [SUPPORTED] — evidence E-eval-2 (judge_aggregate): eval-results/harm-avoidance.json sha256:5dac5045…
# - C-logging          [SUPPORTED] — evidence E-completeness-3 (completeness_assertion): completeness.json sha256:62ef4a29…
# - C-root             [SUPPORTED]

nova safety-case verify /tmp/nova-tour/case.json --capsule $BASE
# ✓ safety case OK: artifacts verified, case_hash matches, I1 holds

nova safety-case export /tmp/nova-tour/case.json --format annex-iv --output annex-iv.md
nova safety-case export /tmp/nova-tour/case.json --format nist-rmf --output nist-rmf.md
```

The `annex-iv` renderer binds to the same 15 EU AI Act Annex IV element ids as
`nova export-annex-iv`; `nist-rmf` renders the NIST AI RMF view. Honesty is
structural throughout: a `CONTESTED` claim renders its reason, an `UNSUPPORTED`
claim is never laundered to "compliant", residual risk defaults to
`not-quantified` (the compiler refuses to reproduce the literature's disavowed
illustrative figures as measurements), and backing states are driven mechanically by
inter-judge κ and Wilson confidence intervals — an aggregate whose CI straddles the
pass threshold, or whose κ < 0.6, is forced to `CONTESTED`. Evidence is bound by
hash, so editing any bound artifact after the fact is caught:

```bash
sed -i 's/"successes": 47/"successes": 50/' $BASE/eval-results/harm-avoidance.json
nova safety-case verify /tmp/nova-tour/case.json --capsule $BASE
# ✗ safety case FAILED: 1 artifact failure(s): E-eval-2: digest mismatch for
#   eval-results/harm-avoidance.json                              (exit 4)
```

---

## 21. Track an incident on the Art. 73 deadline clock

**Maturity:** experimental (v0.50.0, ADR-0088)

When an agent misbehaves in production, the EU AI Act Art. 73 starts a reporting
clock. `nova incident` gives you a first-class, local incident record with that clock
built in — the record lives in `$NOVAFABRIC_HOME/incidents.db`, fully offline.

```bash
nova incident open --title "Tool misuse in prod agent" \
    --classification unauthorized_tool_use --severity high \
    --occurred-at 2026-07-14T08:00:00+00:00 --aware-at 2026-07-15T09:30:00+00:00 \
    --run-id 01KXK4ABQ7Z09MJZ22CVRQ8VDQ
# Opened incident inc-2104c5d09bfd (status=open)
# Note: deadlines are operational aids, not legal advice (ADR-0088).

nova incident status inc-2104c5d09bfd
```

```
id:             inc-2104c5d09bfd
title:          Tool misuse in prod agent
classification: unauthorized_tool_use
severity:       high
status:         open
occurred_at:    2026-07-14T08:00:00+00:00
aware_at:       2026-07-15T09:30:00+00:00
run_ids:        01KXK4ABQ7Z09MJZ22CVRQ8VDQ

Art. 73 reporting deadlines:
  art73_2_standard    2026-07-30T09:30:00+00:00  14 day(s) remaining
      basis: Art. 73(2) — standard serious incident: report no later than 15 days after awareness
```

Deadlines anchor at `--aware-at` (fallback `--occurred-at`). The classification
selects the clock: 15 days standard (Art. 73(2)), 10 days for a death (Art. 73(4)),
**2 days** for widespread infringement or critical infrastructure (Art. 73(3)) —
keywords `death`, `widespread`, `critical_infrastructure` in the classification pick
the stricter clocks, and an empty classification emits *all* candidate deadlines
(fail-informative). `nova incident list` sorts your desk by the most-pressing clock:

```bash
nova incident open --title "Outage touching grid SCADA agent" \
    --classification critical_infrastructure_disruption --severity critical \
    --occurred-at 2026-07-15T02:00:00+00:00 --aware-at 2026-07-15T06:00:00+00:00

nova incident list
# inc-3d1fe5161939  [open    ]  critical     1d left  Outage touching grid SCADA agent
# inc-2104c5d09bfd  [open    ]  high        14d left  Tool misuse in prod agent
```

Exporting renders the stored record in a standard reporting shape — **OECD AIM**
JSON or a **NIS2** report — and transitions an `open` incident to `reported`:

```bash
nova incident export inc-2104c5d09bfd --format aim  --output /tmp/nova-tour/incident-aim.json
nova incident export inc-2104c5d09bfd --format nis2 --output /tmp/nova-tour/incident-nis2.json

nova incident list
# inc-2104c5d09bfd  [reported]  high        14d left  Tool misuse in prod agent
```

The AIM export carries the linked run capsules, the full deadline table, and (when
set) an `attestation_ref` pointing at an ADR-0087 re-performance attestation — the
post-incident replay evidence from [§20](#20-walk-the-accountability-spine-energy-ledger-safety-case).
The lifecycle is forward-only (`open → reported → closed`); a wrongly-opened incident
is closed, never deleted.

> **Honest framing:** the deadline outputs are operational aids, not legal advice —
> every command prints that reminder. `nova incident export --format nis2` renders
> the *stored incident record*; the older `nova export-nis2 <capsule>` renders a NIS2
> report from a *capsule*. They are complementary, not duplicates.

---

## 22. Version, compose, and govern prompts

**Maturity:** experimental (ADRs 0112/0113/0114/0115 — first slices on `main`,
tested; shapes not frozen until v1.0)

This and the next three sections tour the **Langfuse-parity cohort** (ADRs
0112–0141) — the prompt lifecycle, offline analytics, sessions, and team-evaluation
surfaces. Everything stays local-first: prompts live as versioned assets in the same
registry (`$NOVAFABRIC_HOME/registry.db`) the Asset Registry has used since v0.1, and
the two-principal flows reuse the Ed25519 keyring `nova init` created. See
[NovaFabric vs Langfuse](novafabric-vs-langfuse.md) for the honest comparison.

### Register immutable versions (`nova prompt`, ADR-0112)

A prompt is a versioned, content-addressed asset with a commit message — never a
mutable string:

```bash
nova prompt register support-reply \
    -t "You are a support agent. Reply politely to {ticket_body}." \
    --var ticket_body -m "first cut"
# ✓ Registered prompt support-reply@1 (217b562bf385…)
#   ref: prompt:support-reply@1+sha256:217b562bf385…

nova prompt register support-reply \
    -t "You are a senior support agent. Reply politely and concisely to {ticket_body}." \
    --var ticket_body -m "tighten tone"
# ✓ Registered prompt support-reply@2 (3b14c080df5c…)
```

The version auto-increments; re-registering identical content is **idempotent** (the
existing version is returned with an `=` marker, no new row). Fetch, list, and audit:

```bash
nova prompt get support-reply        # latest — prints the pinned ref + template
nova prompt get support-reply@1      # a frozen version
nova prompt list
nova prompt history support-reply
```

```
                           History of 'support-reply'
┏━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┓
┃ Version ┃ Created at          ┃ Status      ┃ Content hash  ┃ Commit message ┃
┡━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━┩
│       1 │ 2026-07-16T07:52:47 │ development │ 217b562bf385… │ first cut      │
│       2 │ 2026-07-16T07:52:48 │ development │ 3b14c080df5c… │ tighten tone   │
└─────────┴─────────────────────┴─────────────┴───────────────┴────────────────┘
```

`nova prompt diff` is a structural diff of template/variables/config — and, like
`git diff`, it **exits `1` when the versions differ** (0 when identical), so it can
gate a pipeline:

```bash
nova prompt diff support-reply@1 support-reply@2
# --- support-reply@1
# +++ support-reply@2
# -  "template": "You are a support agent. Reply politely to {ticket_body}.",
# +  "template": "You are a senior support agent. Reply politely and concisely to {ticket_body}.",
#                                                                    (exit 1)
```

### Compose prompts through labels (`nova prompt compose|tree`, ADR-0115)

A prompt can include other prompts with `{{@prompt:<name>@<selector>}}` references.
Register a shared fragment, point a **deployment label** at it, then include it *via
the label*:

```bash
nova prompt register tone-guide -t "Always be warm, specific, and brief." \
    -m "shared tone rules"
nova label set prompt:tone-guide production 1 --reason "initial rollout"

nova prompt register support-reply --var ticket_body -m "include shared tone guide" \
    -t '{{@prompt:tone-guide@production}}

You are a senior support agent. Reply politely and concisely to {ticket_body}.'
# ✓ Registered prompt support-reply@3 (ef7a4ba2df14…)
#   includes: @prompt:tone-guide@production → v1 (32d4a6fe7427…) [label]
```

> **Reality check:** registration is **fail-closed on composition references** — if
> `production` had not yet been set on `tone-guide`, the `register` above would be
> refused with *"'production' is neither an existing version nor a set label"*. Set
> the label first, then register the including version.

`compose` resolves the whole DAG at this instant and prints a content-addressed
manifest (every included version + hash, plus the hash of the final assembled
prompt); `tree` shows the same DAG indented, flagging label-pinned edges:

```bash
nova prompt compose support-reply
```

```
  Composition of support-reply@3 (ef7a4ba2df14…)
┏━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━┓
┃ Depth ┃ Prompt        ┃ Version ┃ Content hash  ┃
┡━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━┩
│     0 │ support-reply │       3 │ ef7a4ba2df14… │
│     1 │ tone-guide    │       1 │ 32d4a6fe7427… │
└───────┴───────────────┴─────────┴───────────────┘
edges: 1
max_depth: 1
assembled_prompt_hash: sha256:cc660c29dc5f…
```

```bash
nova prompt compose support-reply --assembled   # the fully spliced template
nova prompt tree support-reply
# support-reply@3  ef7a4ba2df14…
# └── tone-guide  @production → v1  32d4a6fe7427…  [label]
```

> Note: label selectors resolve in `compose`/`tree` (and in `nova label get`), but
> `nova prompt get support-reply@production` is **not** accepted — `get` takes only
> integer versions in this slice.

### Point deployment labels (`nova label`, ADR-0113)

Labels are mutable pointers onto immutable versions, with an append-only audit log.
`latest` is reserved (automatic); the target version must exist (fail-closed):

```bash
nova label set prompt:support-reply production 2 --reason "v2 passed offline evals"
# ✓ moved production: (unset) → 2  (moved_by=mohsen, at 2026-07-16T07:53:37…)

nova label get prompt:support-reply production     # production → 2  (3b14c080df5c…)
nova label list prompt:support-reply               # latest (auto) + production
nova label history prompt:support-reply production # append-only move log, newest first
```

### Protect a label — maker-checker moves (ADR-0114)

For a label like `production`, one person should not be able to move it alone.
Protect it, and every move becomes a two-principal flow (Ed25519-signed; identities
default to your OS user, and a keyring key is auto-provisioned per `--identity`):

```bash
nova label protect prompt:support-reply production
# ✓ 'production' on 'support-reply' is now protected (required approvals: 1).

nova label set prompt:support-reply production 3          # refused (exit 1):
# Label 'production' on 'support-reply' is protected (ADR-0114) — direct
# 'nova label set' is refused. Propose the move with 'nova label propose-move' …

# Maker step — the label does NOT move yet:
nova label propose-move prompt:support-reply production --to 3 \
    --reason "v3 adds the shared tone guide" --identity alice
# ✓ proposed move 01KXMYNH0313…: production 2 → 3 (proposed_by=alice)

# Self-approval is refused (separation of duties):
nova label approve-move prompt:support-reply production 01KXMYNH0313… --identity alice
# Approver key fingerprint matches proposer key fingerprint — SoD violation  (exit 1)

# Checker step — a different principal applies the move atomically:
nova label approve-move prompt:support-reply production 01KXMYNH0313… \
    --identity bob --note "reviewed the compose manifest"
# ✓ applied move 01KXMYNH0313…: production 2 → 3 (approvals: 1)

nova label status prompt:support-reply    # protection config + pending/applied moves
```

`--required-approvals 2` demands two distinct checkers, and `--policy-ref` can hang
an OPA/Rego policy (ADR-0019) on the move. The applied move lands in the ADR-0113
audit log reusing the move's ULID, so the label history and the maker-checker record
cross-reference exactly.

---

## 23. Query cost, tokens, and latency offline

**Maturity:** experimental (ADRs 0129–0133, plus the ADR-0126/0127 dimensions —
implemented and tested on `main`; shapes not frozen until v1.0)

Your capsule directory is already a metrics store — `nova query` runs a bounded,
declarative filter/group-by/aggregate over it, **offline, read-only, no server, no
raw SQL**. This section builds a small fleet of capture runs and then interrogates
cost, tokens, and latency without re-running anything.

This section and the next use a stdlib-only "agent" that pretends to answer a
support ticket and self-reports one model call + one tool call into the live capsule
via `NOVAFABRIC_CAPSULE_DIR` (the same cooperation contract as
[§18](#18-run-an-evidence-grade-eval-loop--zero-tokens); in a real deployment the
wire-level hooks record these for you). Create it once:

```bash
mkdir -p /tmp/nova-tour2 && cd /tmp/nova-tour2
cat > ticket_agent.py <<'EOF'
"""Deterministic dummy LLM 'agent' for the analytics + sessions tutorials.

No LLM, no network, stdlib only. Self-reports one model call and one tool
call into the live capsule via NOVAFABRIC_CAPSULE_DIR; in a real deployment
the wire-level hooks record these for you.
"""
import json
import os
import sys
from pathlib import Path

model = os.environ.get("MODEL", "qwen3:8b")
turn = int(os.environ.get("TURN", "0"))
degraded = os.environ.get("DEGRADED", "") == "1"

ticket = ["My invoice is wrong", "Still wrong after the fix", "Thanks, resolved"][turn % 3]
reply = f"[{model}] turn {turn}: acknowledged '{ticket}'"

record = {
    "model_call_id": f"model-{turn:04d}",
    "status": "success",
    "gen_ai.system": "openai",
    "gen_ai.request.model": model,
    "gen_ai.response.model": model,
    "gen_ai.usage.input_tokens": 220 + 40 * turn,
    "gen_ai.usage.output_tokens": 80 + 10 * turn,
    "nova.usage": {
        "input_tokens": 220 + 40 * turn,
        "output_tokens": 80 + 10 * turn,
        "cached_tokens": 128 if turn else 0,
        "total_tokens": 300 + 50 * turn,
    },
    "duration_ms": 900 + 350 * turn + (2600 if degraded else 0),
}
if degraded:
    record["log_level"] = "warning"

# The llama3 calls arrive through a gateway that reports the billed cost, so
# those records carry a recorded nova.cost block (the shape the ADR-0066
# capture-time cost interceptor writes). The qwen3 calls stay cost-free.
if model.startswith("llama3"):
    tokens_in = record["gen_ai.usage.input_tokens"]
    tokens_out = record["gen_ai.usage.output_tokens"]
    record["nova.cost"] = {
        "amount": round(tokens_in * 0.003 / 1000 + tokens_out * 0.009 / 1000, 6),
        "currency": "USD",
    }

# Before replying, the agent looks the customer up (a deterministic "tool").
tool_record = {
    "tool_call_id": f"tool-{turn:04d}",
    "agent_call_id": f"model-{turn:04d}",
    "tool_name": "crm:lookup_account",
    "input": "account:4021",
    "output": {"status": "active", "plan": "pro"},
    "status": "success",
    "duration_ms": 12,
}

capsule_dir = os.environ.get("NOVAFABRIC_CAPSULE_DIR")
if capsule_dir:
    with (Path(capsule_dir) / "model-calls.jsonl").open("a") as f:
        f.write(json.dumps(record) + "\n")
    with (Path(capsule_dir) / "tool-calls.jsonl").open("a") as f:
        f.write(json.dumps(tool_record) + "\n")

print(reply)
sys.exit(0)
EOF
```

Capture five runs — three production on one model, two staging on another (one of
them degraded), tagging each with its deployment environment (ADR-0126, recorded
verbatim on the capsule):

```bash
for i in 0 1 2; do
  MODEL=qwen3:8b TURN=$i nova capture --output-dir runs --environment production \
      -- python3 ticket_agent.py
done
MODEL=llama3:70b TURN=1 DEGRADED=1 nova capture --output-dir runs --environment staging \
    -- python3 ticket_agent.py
MODEL=llama3:70b TURN=2 nova capture --output-dir runs --environment staging \
    -- python3 ticket_agent.py
```

(Each capture also prints an *"Unregistered assets detected"* suggestion — suppress
with `NOVAFABRIC_SUGGEST=0`.)

### Filter and aggregate (`nova query`, ADR-0129)

```bash
nova query --select 'count()' --capsule-dir runs
# count() = 5

nova query --select 'avg(total_tokens) AS avg_tokens, p95(latency) AS p95_ms, count()' \
    --group-by deployment_environment --capsule-dir runs
```

```
deployment_environment  avg_tokens  p95_ms  count()
----------------------  ----------  ------  -------
staging                 375         3737.5  2
production              350         1565    3

2 row(s) — 5 capsule(s) scanned, engine sqlite, window 1970-01-01T00:00:00Z → …
```

> **`engine sqlite` vs `engine duckdb`.** Since v0.99.0 (ADR-0222) DuckDB is an
> optional dependency rather than a default one, and since the ADR-0222 OQ-3
> benchmark it is not the preferred engine either: `nova query` reports
> `engine sqlite` whether or not DuckDB is installed. Results are identical
> either way — the stdlib `sqlite3` path is verified row-for-row against the
> DuckDB path.
>
> Do **not** install `novafabric[query]` expecting `nova query` to get faster.
> DuckDB used to be ~20× *slower* here; that defect is fixed (the index build
> now uses DuckDB's columnar path), but measured it only reaches *parity* —
> the capsule directory scan is 86-89% of the time and the engine is ~3%.
> Its fast path also needs `pyarrow`, which `[query]` does not install; without
> it the old row-by-row cost returns and the log says so once. See
> `bench/query/MEASURED_CEILING.md`. If you want DuckDB anyway, ask for it
> explicitly with `NOVAFABRIC_QUERY_ENGINE=duckdb` and the line reads
> `engine duckdb`.

Predicates join with `AND` over an allow-listed dimension set (`asset`,
`deployment_environment`, `variant`, `log_level`, `model`, `model_id`, `status`,
`tag`), and log levels (ADR-0127) compare by severity:

```bash
nova query --select 'count(), max(latency) AS max_ms' \
    --where 'model = llama3:70b AND deployment_environment = staging' --capsule-dir runs
# count()=2  max_ms=3850

nova query --select 'count()' --where 'log_level >= warning' --capsule-dir runs
# count()=1     (the degraded run's model call carried log_level=warning)

nova query --select 'avg(cost) AS avg_cost, sum(total_tokens) AS tokens, count()' \
    --group-by model --capsule-dir runs
# llama3:70b  avg_cost=0.001695  tokens=750   count()=2
# qwen3:8b    avg_cost=-         tokens=1050  count()=3
```

`avg(cost)` aggregates only **recorded** `nova.cost` blocks — the qwen calls carry
none, so the cell is honestly `-`, never an imputed number. `--since 7d`/`--until`
bound the window; `--json` emits the canonical result; `--query-file q.yaml` takes
the whole query as a document.

### Save the query as a view (`nova view`, ADR-0130)

```bash
nova view save staging-latency --select 'p95(latency) AS p95_ms, count()' \
    --where 'deployment_environment = staging' --description 'Staging latency, all time'
# Saved view 'staging-latency' -> /tmp/nova-tour2/.novafabric/views/staging-latency.yaml
# view_hash: sha256:62f5699b3270…

nova view list
nova view run staging-latency --capsule-dir runs
# p95_ms=3737.5  count()=2
#   — view 'staging-latency' (sha256:62f5699b3270…), 5 capsule(s) scanned, engine sqlite
nova view show staging-latency        # prints the stored query; never executes it
```

Views are plain YAML files in the project's `.novafabric/views/` — validated
fail-closed at save time, content-hashed, reviewable in git.

### Bucket a metric over time (`nova trend`, ADR-0131)

```bash
nova trend --metric cost --group-by day --since 7d --capsule-dir runs \
    --html trend.html --json trend.json
# Wrote trend.html
# Wrote trend.json
```

The report is honest about its inputs: gap buckets are emitted explicitly
(`value: null`), and capsules without the metric are skipped with a warning, never
an abort:

```json
{"bucket": "2026-07-16", "value": 0.00339, "n": 2, "bucket_start": "2026-07-16T00:00:00Z"}
"warnings": ["3 capsule(s) skipped: no cost recorded",
             "7 gap bucket(s) in window (no matching capsules)"]
```

`--metric cost` uses **recorded** costs only (the three qwen capsules are the
skipped ones); `--metric latency --stat p95` and `--metric score:<name>` bucket
latency percentiles and eval scores the same way. The `--html` file is one
self-contained static page — inline SVG, no JavaScript, zero external requests.

### Price the unpriced calls (`nova pricing` + `nova cost estimate`, ADR-0133)

The qwen calls carry token usage but no recorded cost. Add a local pricing-catalog
entry (project-scoped `./.novafabric/pricing.yaml`; `--user` for the user catalog),
then estimate:

```bash
nova pricing add qwen3:8b --input 0.0004 --output 0.0016 --cached 0.0001 \
    --unit per_1k --source "internal price sheet 2026-07"
# added pricing for 'qwen3:8b' in /tmp/nova-tour2/.novafabric/pricing.yaml

nova pricing list        # merged catalog: builtin layer + your project layer
nova pricing show qwen3:8b

QWEN=$(ls -d runs/*/ | head -1)
nova cost estimate $QWEN
```

```
model_id  basis      layer    currency  calls  amount
--------  ---------  -------  --------  -----  --------
qwen3:8b  estimated  project  USD       1      0.000216

estimated total: 0.000216 USD
estimated amounts are derived from the local pricing catalog (sha256:adc56cc2d8d6…)
— estimates, not billing records
```

Run the same command on a llama capsule and the row reads `basis=recorded` — a
recorded `nova.cost` is reported **verbatim, never recomputed**; only cost-free
calls are priced from the catalog, and models absent from every catalog layer stay
honestly unpriced (0.0).

### Usage-type accounting (ADR-0132)

Every model call's provider-reported token breakdown is normalized into an additive
`nova.usage` block (`input_tokens`, `output_tokens`, `cached_tokens`,
`reasoning_tokens`, … — copied **verbatim**, never re-tokenized; absent ≠ zero), and
capture rolls the per-call blocks up into a capsule-level aggregate:

```bash
grep -A4 usage_totals $QWEN/capsule.yaml
# usage_totals:
#   cached_tokens: 0
#   input_tokens: 220
#   output_tokens: 80
#   total_tokens: 300
```

The cached/reasoning/audio split is what makes the pricing catalog's per-usage-type
prices (`--cached`, `--reasoning`, …) meaningful — a cache-heavy workload estimates
differently from a cache-cold one.

---

## 24. Group turns into a session and replay it

**Maturity:** experimental (ADRs 0122/0123/0124 — session capsule, session replay
P1, execution-graph reconstruction)

A multi-turn conversation is N independent runs plus an ordered manifest — **not** a
new capsule format (ADR-0034). The session manifest lives beside your capsules
(`$NOVAFABRIC_HOME/sessions/`), records content-addressed references (relative path
+ sha256 of each member's `capsule.yaml`), and never modifies a member.

Continuing in `/tmp/nova-tour2` with the agent from
[§23](#23-query-cost-tokens-and-latency-offline):

```bash
SID=$(nova session new --kind conversation --user customer-4021)
echo $SID          # 01KXMZ5H2HXSSC2ZTDEJXPQZCS

# Capture each turn with the session back-reference stamped on the capsule
for i in 0 1 2; do
  MODEL=qwen3:8b TURN=$i nova capture --output-dir session-runs \
      --session-id $SID --session-sequence $i -- python3 ticket_agent.py
done

# The manifest stays the authoritative ordered index — append each member
for t in $(ls -d session-runs/*/); do nova session add $SID "$t" --role user-turn; done
# ✓ Added run 01KXMZ5HH677… to session 01KXMZ5H2HXS… as turn 0
# ✓ Added run 01KXMZ5J3VPG… to session 01KXMZ5H2HXS… as turn 1
# ✓ Added run 01KXMZ5JNZVQ… to session 01KXMZ5H2HXS… as turn 2

nova session list
nova session show $SID
```

```
Session 01KXMZ5H2HXSSC2ZTDEJXPQZCS (conversation, created 2026-07-16T08:02:25…)
user_ref: customer-4021
                                Turns (3)
 seq  run_id                      integrity  run status  duration  role
   0  01KXMZ5HH677Z09ZNARA68Y5X6  ok         success        27ms   user-turn
   1  01KXMZ5J3VPGTZ4ADSB3QDBWZW  ok         success        25ms   user-turn
   2  01KXMZ5JNZVQS88WTEKQ089H6P  ok         success        29ms   user-turn
turns=3  resolved=3  missing=0  tampered=0  duration=81ms  tokens=1050  cost=-
```

Note the per-member `integrity` column — `show` re-hashes each member's manifest
against the recorded sha256, so a tampered or missing turn is visible before you
ever replay. `--user` takes an opaque, redaction-safe reference (a stable hash or
handle, never a raw identifier), and the aggregate row reuses the ADR-0132/0133
extraction — `cost=-` because these turns carry no recorded cost.

### Replay the whole session in order (ADR-0123)

```bash
nova session replay $SID --mode mocked --output-dir session-replays
```

```
           Session replay: 01KXMZ5H2HXSSC2ZTDEJXPQZCS (mode: mocked)
 seq  source run                  mode    status      replay capsule
   0  01KXMZ5HH677Z09ZNARA68Y5X6  mocked  reproduced  01KXMZ5NHN3K772VJZGNH3SYGM
   1  01KXMZ5J3VPGTZ4ADSB3QDBWZW  mocked  reproduced  01KXMZ5NK570CBXE675PANN4W2
   2  01KXMZ5JNZVQS88WTEKQ089H6P  mocked  reproduced  01KXMZ5NM9VRMCD96TKHNP2D2R
whole_session_verdict: reproduced
Session replay result written: session-replays/session-replay-…/session_replay_result.json
```

Each turn runs through the single-capsule replay engine from
[§3](#3-replay-a-past-run) (any of the four modes via `--mode`), produces its own
replay capsule, and the session gets one `SessionReplayResult` with ordered per-turn
verdicts plus a whole-session verdict. Exit code is `0` only when the whole session
is `reproduced`; `--on-divergence continue` records drift and keeps going, and a
missing or tampered member is an honest per-turn **refusal**, never a silent skip.
State-seam verification *between* turns (ADR-0123 P2) is **future design** — not
implemented.

### Reconstruct one run's execution DAG (`nova graph agent`, ADR-0124)

Any capsule — session member or not — can be projected into its execution graph,
read-only and offline:

```bash
T0=$(ls -d session-runs/*/ | head -1)
nova graph agent $T0 --stats
# {"edge_count": 2, "max_depth": 2, "max_fan_out": 1, "node_count": 4}

nova graph agent $T0 --format mermaid
# graph TD
#   n0["novafabric.capture"]
#   n1["qwen3:8b"]
#   n2["root"]
#   n3["crm:lookup_account"]
#   n1 -.->|invokes| n3
#   n2 --> n1

nova graph agent $T0 --digest
# sha256:87b03fac9bcd…
```

The graph is a pure function of the capsule's `model-calls.jsonl`,
`tool-calls.jsonl`, and `trace.jsonl`: model calls become nodes via their
`model_call_id`, tool calls attach to the model call that invoked them via
`agent_call_id` (the dotted `invokes` edge above), and anything without a parent
link attaches to a synthetic root with an explicit reconstruction note — **never a
heuristic reparenting**. The canonical `--format json` export carries those notes
and a stable `graph_digest`, so two capsules' shapes can be compared (`--digest`)
in a verify/diff pipeline; `dot` output is also available.

---

## 25. Run a team evaluation workflow

**Maturity:** experimental (ADRs 0117/0118/0119/0120 — score-config catalog,
annotation queues, external score submission, dataset-experiment harness)

[§18](#18-run-an-evidence-grade-eval-loop--zero-tokens) produced machine scores.
This section adds the *team*: typed score definitions everyone grades against,
human annotation queues with maker-checker review, externally-computed scores
submitted through a fail-closed gate, and A/B dataset experiments with the same
ADR-0080 significance verdicts. Still in `/tmp/nova-tour2`, using the capsules from
[§23](#23-query-cost-tokens-and-latency-offline).

### Define the metrics once (`nova eval score config`, ADR-0117)

Score configs are immutable and content-addressed — a changed body bumps the
version, and every score written later is validated against them:

```bash
nova eval score config add --name answer_quality --value-type numeric \
    --min 1 --max 5 --direction higher-better \
    --description "1-5 answer quality as judged by a support lead"
# Registered answer_quality@1  numeric  [1.0, 5.0] (higher-better)
#   digest=sha256:7e9e030b1dc9…

nova eval score config add --name polite --value-type boolean \
    --description "Is the reply polite and on-brand?"
# Registered polite@1  boolean  true|false

nova eval score config list
```

### Route runs to human reviewers (`nova annotate`, ADR-0118)

Create a queue whose criteria must be registered configs, enqueue a capsule, and
walk the maker-checker loop:

```bash
CAP=$(ls -d runs/*/ | head -1)

nova annotate queue create --name ticket-review --criteria answer_quality,polite \
    --require-checker --description "Weekly human review of prod ticket replies"
# Created queue ticket-review (maker-checker)

nova annotate queue add ticket-review --capsule $CAP
# Enqueued item 01KXMZ6JSKBR… (capsule) on queue ticket-review
#   subject=sha256:a90b6e3a7688…

nova annotate next --queue ticket-review --as dana        # claim (round-robin)
# Claimed by dana: item 01KXMZ6JSKBR…  assigned

nova annotate submit 01KXMZ6JSKBR… --score answer_quality=4 --score polite=true \
    --as dana --note "clear and friendly; missed the refund ETA"
# Submitted 2 HUMAN score(s) by dana:
#   answer_quality = 4.0  (score_id=01KXMZ71DT3Y…)
#   polite = True         (score_id=01KXMZ71DT3Y…)
#   Awaiting checker — run nova annotate confirm … as a different identity.

nova annotate confirm 01KXMZ6JSKBR… --as dana             # refused (exit 1):
# checker equals the maker — a distinct reviewer must confirm (ADR-0118 D4 / ADR-0003)

nova annotate confirm 01KXMZ6JSKBR… --as erin
# Confirmed item 01KXMZ6JSKBR… by erin (maker: dana) — completed.

nova annotate queue show ticket-review    # progress: completed=1; maker + checker recorded
```

The completed scores land in the capsule's append-only `scores.jsonl` as
`source=human` records, Ed25519-signed by the maker's keyring key — the same file
[§18](#18-run-an-evidence-grade-eval-loop--zero-tokens)'s machine scores use, so
`nova eval score list --capsule $CAP` and the significance diff see human and
machine scores uniformly. Every queue criterion must be graded or explicitly
skipped with `--skip-criterion`; `nova annotate skip` is the terminal no-score exit.

### Submit externally-computed scores (`nova score submit`, ADR-0119)

Scores computed *outside* NovaFabric (a perf pipeline, a nightly judge job) enter
through one fail-closed gate. A submission must name a registered config, an eval
card ([§18](#18-run-an-evidence-grade-eval-loop--zero-tokens)), and a subject digest
that is actually anchored in the target capsule — the capsule's own manifest digest
works:

```bash
nova eval score config add --name latency_slo_met --value-type boolean \
    --description "p95 latency within the 2s SLO, computed by the perf pipeline"
nova eval card new --source code --card-id perf-pipeline --name "Perf pipeline v2" \
    --out perf-card.json
nova eval card sign perf-card.json && nova eval card register perf-card.json
# Registered eval-card:perf-pipeline@0.1.0+sha256:b1e1f2415868…

SUBJECT=sha256:$(sha256sum $CAP/capsule.yaml | cut -d' ' -f1)

nova score submit --capsule $CAP --name latency_slo_met --value true \
    --value-type boolean --source code --evaluator perf-pipeline \
    --subject $SUBJECT --subject-kind capsule \
    --eval-card sha256:b1e1f2415868…
# {"score_id":"01KXMZ7JVG87…","subject":"sha256:b6a40d84c4e3…","name":"latency_slo_met",
#  "value":true,"source":"code","evaluator_id":"perf-pipeline",…}      (exit 0)
```

Rejections are structured, written nowhere, and non-zero — try them:

```bash
nova score submit … --subject sha256:$(printf x | sha256sum | cut -d' ' -f1) …
# {"error": "subject_not_found", "message": "subject sha256:2d711642… is not a
#  span/capsule digest of capsule 01KXMZ5E7BVH…"}                      (exit 1)

nova score submit … --name answer_quality --value 9 --value-type numeric …
# {"error": "config_violation", "message": "score value 9.0 is outside the range
#  [1.0, 5.0] of config answer_quality@1 (sha256:7e9e030b1dc9…)"}      (exit 1)
```

Re-running with the same client-minted `--score-id` is idempotent. Note that in
this slice the `--eval-card` digest is recorded **verbatim** on the score record —
it is format-checked but not resolved against the card registry, so pass the
canonical digest `nova eval card register` printed.

### A/B a change over a pinned dataset (`nova experiment`, ADR-0120)

A dataset is one local JSONL file (`item_id`, free-form `input`, optional
`expected`), pinned by content hash. `experiment run` executes a command once per
item — one Run Capsule each, through the normal capture path — and items with an
`expected` value get a zero-token boolean exact-match score:

```bash
cat > capitals.jsonl <<'EOF'
{"item_id": "q1", "input": "capital:france", "expected": "Paris"}
{"item_id": "q2", "input": "capital:italy", "expected": "Rome"}
{"item_id": "q3", "input": "capital:japan", "expected": "Tokyo"}
{"item_id": "q4", "input": "capital:spain", "expected": "Madrid"}
{"item_id": "q5", "input": "capital:kenya", "expected": "Nairobi"}
{"item_id": "q6", "input": "capital:peru", "expected": "Lima"}
{"item_id": "q7", "input": "capital:norway", "expected": "Oslo"}
{"item_id": "q8", "input": "capital:egypt", "expected": "Cairo"}
{"item_id": "q9", "input": "capital:canada", "expected": "Ottawa"}
{"item_id": "q10", "input": "capital:cuba", "expected": "Havana"}
EOF

cat > qa_agent.py <<'EOF'
"""Toy lookup 'agent' for the experiment harness walkthrough (stdlib only)."""
import os, sys
TABLE = {
    "capital:france": "Paris", "capital:italy": "Rome", "capital:japan": "Tokyo",
    "capital:spain": "Madrid", "capital:kenya": "Nairobi", "capital:peru": "Lima",
    "capital:norway": "Oslo", "capital:egypt": "Cairo", "capital:canada": "Ottawa",
    "capital:cuba": "Havana",
}
query = sys.argv[1]
answer = TABLE.get(query, "unknown")
# The planted regression: v2 answers in uppercase, breaking exact-match.
if os.environ.get("AGENT_VERSION") == "2.0.0" and query >= "capital:k":
    answer = answer.upper()
print(answer)
EOF

AGENT_VERSION=1.0.0 nova experiment run --dataset capitals.jsonl \
    --target capital-bot@1.0.0 --runs-dir exp-runs -- python3 qa_agent.py "{input}"
# ✓ Experiment 01KXMZ8XCVKP… finalized
#   dataset: capitals@22d30466dedd (sha256:22d30466dedd…)
#   items: 10/10 ok
#   exact_match: pass_rate=1.000  n=10  wilson=[0.722, 1.000]

AGENT_VERSION=2.0.0 nova experiment run --dataset capitals.jsonl \
    --target capital-bot@2.0.0 --runs-dir exp-runs -- python3 qa_agent.py "{input}"
#   exact_match: pass_rate=0.600  n=10  wilson=[0.313, 0.832]

nova experiment list
nova experiment show 01KXMZ97E78D…      # per-item runs (one capsule each) + aggregate
```

Compare the two — per-item alignment by `item_id`, verdict produced verbatim by the
ADR-0080 significance gate from
[§18](#18-run-an-evidence-grade-eval-loop--zero-tokens):

```bash
nova experiment compare 01KXMZ8XCVKP… 01KXMZ97E78D… --metric exact_match
# metric: exact_match  items: 10 (changed=4, unmatched=0)
# SPRT verdict: accept_h1  llr=3.64  (exit 3)
```

Exit `3` — a statistically significant regression, CI-gateable. Or fold the gate
into the run itself: `nova experiment run … --baseline 01KXMZ8XCVKP…` runs the
candidate *and* exits with the gate's code in one step. Comparing experiments over
different pinned datasets is a hard error — the dataset hash is part of the record,
so an apples-to-oranges comparison cannot happen silently.

---

## Summary and next steps

You have now exercised every shipped NovaFabric surface, from a bare capture to
signed evidence, and previewed the experimental graph, policy, and dashboard tiers. The
through-line is the **Capture → Seal → Replay → Diff → Audit** verb chain, with the
**Run Capsule** as the source of truth and every index (registry, lineage, KG) a
rebuildable derivation of it.

Where to go next:

| I want to… | Go to |
|---|---|
| See every CLI flag and command | [CLI reference](../cli-reference.md) |
| Understand capsule structure and schema fields | [Concepts](../concepts.md) |
| Read the four replay modes in depth | [User guide: replay](../user-guide.md) |
| Write a custom capture hook plugin | [Writing a hook plugin](../integrations/writing-a-hook-plugin.md) |
| Dig into the security graph | [Security knowledge graph](../security-knowledge-graph.md) |
| See what is shipped vs planned | [ROADMAP.md](../../ROADMAP.md) |
| Understand the cluster-scale design intent | [Cluster scale — 1,000,000 agents](cluster-scale.md) |

> **Reminder on maturity:** most v0.1–v0.9 surfaces carry `experimental` maturity —
> they work today, but on-disk formats (Run Capsule, Evidence Bundle) are **not frozen
> until v1.0**. Sections marked *planned* or *future design* above are documented intent
> only and are not yet implemented. Never treat a planned item as shipped.
