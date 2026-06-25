# NovaFabric feature tour — every capability, hands-on

> **New to NovaFabric?** Start with the [Getting Started guide](../getting-started.md)
> for a focused 15-minute first run (capture → validate → replay → diff → lineage →
> seal). This tour is the comprehensive walk-through: it covers those basics *and*
> the advanced features — proxies, every LLM provider, LangChain, evidence bundles,
> the capsule knowledge graph, capture-level policy, GDPR erasure, dashboard reports,
> topology views, and compliance exports.

> **Prerequisites:** NovaFabric installed (`uv pip install -e .` from the repo).
> Run `nova --help` to confirm.

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

This guide walks you through all five core capabilities with real commands and
real output. Read as much or as little as you need.

---

## 1. Capture your first run

The simplest possible capture — no LLM, no agent, just proof the system works:

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

The capsule is a plain directory. No server needed to read it:

```
capsule.yaml          # manifest: model, command, exit code, timing
model-calls.jsonl     # every LLM API call, one record each (OTel GenAI semconv)
tool-calls.jsonl      # tool invocations (MCP or plugin hooks)
assets.jsonl          # datasets this run consumed
redaction-proof.json  # proof secrets were scanned before storage
inputs/               # stdin, files passed in
outputs/              # stdout, stderr, files written out
```

This is the unit everything else works with.

---

## 2. Detect a regression with diff

Capture two runs of the same agent — one baseline, one with a behavior change —
then let NovaFabric tell you exactly what differs:

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

You updated a prompt, bumped a model version, or changed a tool — diff tells you
immediately whether agent behavior actually changed, not just whether the code changed.

---

## 3. Replay a past run

Re-drive an agent against the exact same inputs it saw in a past run — no network,
no live model needed:

```bash
nova replay capsules/01KR9Q2AD… --mode forensic
```

The agent sees the same log content, same model responses, same tool results as the
original run. If it produces the same output, the result is reproducible. If it
doesn't, something drifted — a model update, a tool change, non-determinism.

The replay itself becomes a new capsule, so you can diff original vs replay:

```bash
nova diff capsules/01KR9Q2AD… capsules/01KRB4F7…
```

---

## 4. Trace data dependencies with lineage

Declare what data an agent consumed during capture:

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

When a dataset is found corrupt or wrong, blast radius tells you exactly which runs
to re-validate. No manual tracking, no grep — the graph is built automatically from
what each run declared it consumed.

> **Dashboard shortcut:** Open the dashboard's **Lineage** tab — it renders the full blast-radius and provenance graph interactively. Select any node to inspect its edges, and use the **Export OpenLineage** panel to emit the lineage for a run as a standards-compliant OpenLineage event without touching the CLI.

---

## 5. Capture without changing your agent code

NovaFabric captures at the HTTP layer. You don't need to touch agent code at all.

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

All four approaches produce the same capsule format.

---

## 6. Capture any LLM provider

NovaFabric is provider-agnostic. Ollama, OpenAI, Azure OpenAI, Amazon Bedrock —
all captured identically because the hook lives at the HTTP layer, not inside any
provider SDK.

**Azure OpenAI:**

```bash
export AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
export AZURE_OPENAI_API_KEY=<your-key>
export AZURE_OPENAI_DEPLOYMENT=<deployment-name>

nova capture python examples/azure-openai/agent.py
```

The capsule records `gen_ai.system = "openai"` (the SDK identifier) regardless of
whether the backend is Azure, a private proxy, or an on-prem gateway.

---

## 7. Capture a LangChain / LangGraph agent

No special configuration. Just wrap with `nova capture`:

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

You see two LLM round-trips: the model deciding to call `add_two_numbers(17, 25)`,
and then incorporating the result `42` into its final answer. The whole tool-use
chain is captured without any LangChain-specific instrumentation.

---

## Build a signed evidence bundle

For compliance, audits, or any situation where you need to prove what an agent did
and that the record hasn't been altered:

```bash
nova export-evidence capsules/01KR9Q2AD…
# → ~/.novafabric/evidence/01KR9Q2AD….zip  (ed25519-signed)
```

The ZIP contains the full event trace and a cryptographic signature. An auditor can
verify the signature without needing NovaFabric installed.

To also publish the DSSE envelope to the Rekor transparency log (requires
`NOVA_REKOR_URL`):

```bash
export NOVA_REKOR_URL=https://rekor.sigstore.dev
nova export-evidence capsules/01KR9Q2AD… --sigstore
# ✓ evidence bundle written: ~/.novafabric/evidence/01KR9Q2AD….zip
# ✓ Rekor transparency log entry: 24296fb24b3c…
```

If `NOVA_REKOR_URL` is not set the step is silently skipped (exit 0).

---

## Cryptographically seal a capsule (NovaSeal, v0.10)

For regulated environments that need DSSE signatures, RFC 3161 timestamps, and
an append-only Merkle log:

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

> **Dashboard shortcut:** Open the dashboard's **Seal** tab and use the **Capsule Integrity Verify** panel — enter the run ID, click **Verify capsule**, and see per-check pass/fail for signature, timestamp, and Merkle log inclusion without leaving the browser.

NovaSeal is completely opt-in. Without `novaseal.yaml`, capture is unchanged.

---

## Build a Capsule Knowledge Graph (v0.17)

Once you've captured a few runs, you can build a **cross-capsule knowledge graph**
that aggregates which agents called which models and used which tools. This is
the foundation for the v1.2 "AI call graph" dashboard view (ADR-0067).

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

The graph is **separate** from the lineage graph — it's a derived artifact
purely for aggregation, safe to rebuild from capsule replay at any time.

**MCP server auto-detection (v0.29):** tool names containing `:` (e.g.
`filesystem:read_file`, `github:search_code`) are automatically split — the left
part becomes an `MCPServer` node, the right part the `Tool` node, and a
`SERVED_BY` edge links them.  No configuration required.

The KG schema has **5 node types** (`Agent`, `Model`, `Tool`, `MCPServer`,
`InferenceEndpoint`) and **4 relationship types** (`CALLS`, `USES_TOOL`,
`SERVED_BY`, `ROUTES_TO`).

`nova serve` ingests both `model-calls.jsonl` and `tool-calls.jsonl` automatically
every 60 seconds, so MCP tool-call events are included without manual ingestion.

Dashboard equivalent: open `nova serve --experimental` and visit the **KG** tab.
The **Multi-Layer Topology** panel shows per-layer node counts and edge breakdown.

Additional KG dashboard panels (v0.31.0):

| Panel | CLI equivalent |
|---|---|
| **KG Query** | `nova kg query <entity>` — search for agents, models, or tools by name |
| **KG Audit** | `nova kg audit` — orphaned edges and zero-call-count anomalies |
| **Entity Queue** | `nova kg entity-queue list/approve/reject` — Tier-3 human review queue for ambiguous entities |
| **KG Aliases** | `nova kg alias list/register` — alias → canonical entity mapping management |

For the human review queue:

```bash
nova kg entity-queue list                         # pending review items
nova kg entity-queue approve ITEM_ID              # resolve to canonical entity
nova kg entity-queue reject ITEM_ID               # discard ambiguous candidate
nova kg alias register gpt4 openai/gpt-4 model   # register an alias
```

---

## Manage capture-level policy (v0.17, cap-004)

`nova capture` records different field sets depending on the capture-level
policy. Four levels are available:

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

Dashboard equivalent: the **Policy** tab now has a Capture Level panel (v0.18)
with the same get/set semantics.

---

## Split-storage and GDPR erasure (v0.17, cap-003) — experimental

The dual-object split keeps the PII payload (prompts, responses) in a
separately addressable S3 object so it can be erased without breaking the
audit trail. **This is gated on a feature flag** — `NOVA_CAP003_ENABLED=true`
— until the legal review (OQ-01) closes.

```bash
# Validate the S3 backend supports Object Lock COMPLIANCE
nova storage validate --endpoint https://s3.amazonaws.com --bucket nova-capsules

# Inspect the object split for a run (informational; full behaviour gated on flag)
nova storage inspect --run-id run_xyz

# Request erasure of a subject's PII payload
nova erasure request --run-id run_xyz
nova erasure status --request-id <id>
```

Dashboard equivalents (v0.18): the **Compliance** tab has a GDPR Erasure
panel; the **Infra** tab has a Storage Operations card.

---

## Generate reports from the dashboard (v0.30.3)

The **Reports** tab in `nova serve --experimental` provides a Catalog+Builder layout
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

---

## Open the dashboard topology view (v0.16 + v0.17)

NovaFabric ships two complementary topology views:

```bash
# 2D Sigma.js view (v0.16.1)
nova serve --topology

# 3D Three.js view (v0.17.0, TV-5)
nova serve --tv5
```

Both views render the agent / model / tool call graph in real time from the
ADS encoder + DeltaBuffer pipeline. The TV-5 3D view adds depth via per-tier
Z-stacking using `networkx.spring_layout` (resolves OQ-030).

---

## Export compliance reports from the dashboard (v0.37.0)

Four compliance-export CLI commands now have full dashboard equivalents in the
**Compliance tab** of `nova serve --experimental`. No command line needed for
compliance officers or auditors.

| Panel | CLI equivalent | Endpoint | Regulation |
|---|---|---|---|
| GDPR Art.30 RoPA Export | `nova export-ropa` | `POST /api/compliance/export/ropa` | GDPR Art.30 |
| AI-SBOM Export | `nova export-aibom` | `POST /api/compliance/export/aibom` | EU CRA (2026-09-11) |
| NIST AI RMF Report | `nova export-nist-rmf` | `POST /api/compliance/export/nist-rmf` | NIST AI 100-1 |
| AI-SBOM Coverage Status | `nova aibom status` | `GET /api/aibom/status` | EU CRA |

**Quick start — dashboard compliance export:**

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

In the dashboard, open the **Compliance** tab and scroll to the relevant panel.
Each panel shows the equivalent CLI command at the bottom so teams can reproduce
the export in CI pipelines.

---

## What's not covered here

| Topic | Where to look |
|---|---|
| Every CLI flag | [CLI reference](../cli-reference.md) |
| Capsule structure and schema fields | [Concepts](../concepts.md) |
| Cluster-scale design | [Cluster scale — 1,000,000 agents](cluster-scale.md) |
| Writing a custom hook plugin | [Writing a hook plugin](../integrations/writing-a-hook-plugin.md) |
| Replay modes explained | [User guide: replay](../user-guide.md) |
| NovaSeal full reference | [CLI reference: `nova verify`](../cli-reference.md) |
| What's coming next | [`ROADMAP.md`](../../ROADMAP.md) |
