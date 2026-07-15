# NovaFabric feature tour — every capability, hands-on

> **New to NovaFabric?** Start with the [Getting Started guide](../getting-started.md)
> for a focused 15-minute first run (capture → validate → replay → diff → lineage →
> seal). This tour is the comprehensive walk-through: it covers those basics *and*
> the advanced surfaces — proxies, every LLM provider, LangChain/LangGraph, evidence
> bundles, the capsule knowledge graph, capture-level policy, GDPR erasure, dashboard
> reports, topology views, and compliance exports.

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
nova export-evidence capsules/01KR9Q2AD…
# → ~/.novafabric/evidence/01KR9Q2AD….zip  (ed25519-signed)
```

The ZIP is self-contained: it embeds the capsule, a lineage subgraph, in-toto DSSE
attestations, ed25519 signatures, and vendored JSON schemas. An auditor can verify it
with only `sha256sum` plus an ed25519 verifier — **no NovaFabric runtime required.**
That offline-verifiability is what makes the Evidence Bundle the compliance primitive.

To also publish the DSSE envelope to the Rekor transparency log (requires
`NOVA_REKOR_URL`):

```bash
export NOVA_REKOR_URL=https://rekor.sigstore.dev
nova export-evidence capsules/01KR9Q2AD… --sigstore
# ✓ evidence bundle written: ~/.novafabric/evidence/01KR9Q2AD….zip
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

**Maturity note — read this first.** A dedicated **NovaSeal signing service** (DSSE +
RFC 3161 timestamps + append-only Merkle log as a standalone service) is **planned
design intent** (ADR-0041), and the RFC-3161 timestamping *implementation* is planned.
The `nova capture` / `nova verify` seal flow below is present on `main` behind an
opt-in `novaseal.yaml`; treat it as **experimental** and do not rely on it as the
production sealing service described in the roadmap. What is shipped and stable toward
this space today is the ed25519-signed Evidence Bundle in [§8](#8-build-a-signed-evidence-bundle),
in-toto DSSE attestations, verifiable redaction proofs, OPA/Rego policy gates with
maker-checker promotion, and WORM storage adapters.

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
> leaving the browser.

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

# Ingest a captured run
nova kg ingest --capsule capsules/01KR9Q2AD…/capsule.yaml

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

> The dashboard is **local-only and read-only** (Layer A): it binds `127.0.0.1`, uses a
> one-shot token, and every page shows its CLI equivalent so anything you see can be
> reproduced in a pipeline. It is not a hosted UI.

---

## 15. Open the dashboard topology view

**Maturity:** experimental

NovaFabric ships two complementary topology views:

```bash
# 2D Sigma.js view
nova serve --topology

# 3D Three.js view (TV-5)
nova serve --tv5
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

| Panel | CLI equivalent | Endpoint | Regulation |
|---|---|---|---|
| GDPR Art.30 RoPA Export | `nova export-ropa` | `POST /api/compliance/export/ropa` | GDPR Art.30 |
| AI-SBOM Export | `nova export-aibom` | `POST /api/compliance/export/aibom` | EU CRA (2026-09-11) |
| NIST AI RMF Report | `nova export-nist-rmf` | `POST /api/compliance/export/nist-rmf` | NIST AI 100-1 |
| AI-SBOM Coverage Status | `nova aibom status` | `GET /api/aibom/status` | EU CRA |

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

In the dashboard, open the **Compliance** tab and scroll to the relevant panel. Each
panel shows the equivalent CLI command at the bottom so teams can reproduce the export
in CI pipelines.

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
