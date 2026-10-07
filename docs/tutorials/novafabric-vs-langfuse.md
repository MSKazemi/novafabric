# NovaFabric vs Langfuse

> **Who this is for:** engineers evaluating LLM observability tools, or anyone who
> has used Langfuse and wants to understand what NovaFabric adds.

Both tools capture LLM traces. This document explains where they overlap, where
they differ, and which questions each one is designed to answer.

---

## The fundamental difference in philosophy

**Langfuse** is **monitoring infrastructure** — built around the question
*"how is my system performing right now?"*

**NovaFabric** is **reproducibility infrastructure** — built around the question
*"can I prove, replay, and compare what happened in any past run?"*

If all you want is "show me what my agent said to the LLM and what came back,"
both work. The differences matter when you need more than that.

---

## What they share

- Capture LLM traces: model name, prompt, response, token counts, latency
- A UI to browse and search runs
- Some form of evals and scoring

---

## Concrete differences

### 1. Instrumentation

**Langfuse** is typically wired in through its SDK or integrations — for example
decorators on the functions you want traced:

```python
from langfuse.decorators import observe

@observe()   # you must add this to every function you want traced
def run_agent():
    ...
```

**NovaFabric** wraps the process instead — no code changes:

```bash
nova capture -- python agent.py
```

For a Python workload it patches the OpenAI and Anthropic SDKs and the MCP client,
and records HTTP calls to known model-provider endpoints at the `httpx` /
`requests` / `aiohttp` / `urllib3` level, so it works for agents you can't modify.
Non-Python clients go through `nova api-proxy` / `nova mcp-proxy`. A framework
adapter adds richer detail but needs one line of code.

---

### 2. Where the data lives

**Langfuse** stores traces in its database — Langfuse Cloud or a server you
self-host. To read them, you query that service.

**NovaFabric** stores each run as a portable capsule directory on your filesystem:

```
capsules/01KR9Q2AD…/
    capsule.yaml          # run manifest (command, exit code, timing, evidence digests)
    trace.jsonl           # the execution span tree
    model-calls.jsonl     # each captured model call (request and response)
    tool-calls.jsonl      # tool calls seen by the MCP hook or an adapter
    env.lock              # the environment
    assets.jsonl          # which registered assets this run consumed
    lineage.jsonl         # provenance graph edges
    redaction-proof.json  # secret scanner results
```

You can `tar` it, archive it, move it to cold storage, share it with a colleague,
or read it on an air-gapped machine. No running server required.

---

### 3. Replay

**NovaFabric** can re-run a past capture against its recorded model responses:

```bash
nova replay --mode mocked capsules/01KR9Q2AD…
```

The command runs again, and its OpenAI / Anthropic chat calls are answered from the
capsule instead of the live API, so no model call is made. **Tool calls are not
substituted** — they run live — and only synchronous, non-streaming chat calls are
served from the capsule today. `--mode forensic` inspects the capsule without
re-running anything. If the replayed run diverges, something drifted: code, a
tool's behaviour, or the environment.

---

### 4. Structural diff between runs

**NovaFabric** compares two captured runs structurally:

```bash
nova diff capsules/01KR9Q2A…  capsules/01KRB4F7…
```

Output:

```
Diff: 01KR9Q2A… → 01KRB4F7…
  changed=3  added=0  removed=0

Model calls:
  ~ call at span 1a9c989341748be5
Outputs:
  ~ outputs/decision.json
  ~ outputs/stdout.txt
```

Model calls are paired in order (identical requests anchor the alignment), tool
calls by name and arguments; `--output-format json` gives the per-call
request/response/argument flags. `--assert-no-regressions` exits 1 on any change,
so it can gate CI.

After a prompt update or model upgrade, you can see immediately whether agent
behavior changed — even if the final label looks the same.

---

### 5. Data lineage

**NovaFabric** records which registered asset fed which run. Inside a captured
run, the agent declares what it consumed:

```python
import os
from pathlib import Path

from novafabric.registry.service import record_asset_consumption

record_asset_consumption(
    "slurm-logs/job-42819-oom@v1", "production",
    Path(os.environ["NOVAFABRIC_CAPSULE_DIR"]),
)
```

This builds a graph:

```
slurm-logs/job-42819-oom@v1  ──consumed──►  run:01KR9Q2AD…
slurm-logs/job-42819-oom@v1  ──consumed──►  run:01KRB4F7…
slurm-logs/job-42819-oom@v1  ──consumed──►  run:01KRC3X9…
```

When a dataset is found to be corrupt or wrong, a blast-radius query tells you
exactly which runs to re-validate:

```bash
nova lineage blast-radius slurm-logs/job-42819-oom@v1
# → run:01KR9Q2AD…  run:01KRB4F7…  run:01KRC3X9…
```

---

### 6. Evidence bundles for compliance

**Langfuse** produces dashboards and exports.

**NovaFabric** produces cryptographically signed evidence bundles:

```bash
nova export-evidence capsules/01KR9Q2AD… --output bundle.zip --key ed25519.pem
# → signed ZIP with ed25519 signature + full event trace
```

(`--output` and `--key` are both required flags — omitting either exits non-zero.)

An auditor can verify the signature and confirm the record hasn't been modified
since it was signed. It is an evidence primitive — useful in regulated industries
(healthcare, finance, HPC facilities) where you must show what an agent run
recorded and that the record hasn't been altered. It does not prove the record is
complete, and it does not certify compliance.

---

## Where Langfuse is genuinely better

**Corrected 2026-07-30:** NovaFabric shipped a Langfuse-parity cohort (all
**experimental**, offline-only, no hosted backend) that closes some of this gap
— prompt versioning/labels (`nova prompt`, `nova label`, ADR-0112/0113),
offline cost/token/latency analytics (`nova query`/`nova view`/`nova trend`/
`nova pricing`, ADR-0129–0133), sessions and execution-graph replay
(`nova graph agent`, ADR-0123/0124), and a team evaluation workflow
(`nova eval score config`, `nova annotate`, `nova score submit`,
`nova experiment`, ADR-0117–0120) — see `docs/tutorials/feature-tour.md`
§§22–25. Real-time production alerting and a hosted multi-user SaaS remain
genuinely Langfuse-only.

| Area | Why Langfuse wins |
|---|---|
| Real-time production alerting | Live P95 latency monitors, error rate alerts against a running service — NovaFabric's analytics are offline/batch over captured capsules, not a live monitor |
| Prompt A/B testing at scale | Hosted, multi-user prompt experimentation UI; NovaFabric's `nova experiment` (§25) is a local, dataset-pinned A/B comparison, not a hosted testing platform |
| Team collaboration | Multi-user SaaS, comments, roles, real-time dashboards; NovaFabric's `nova annotate` (§25) queues reviews locally with no hosted multi-user surface |
| Ecosystem maturity | More framework integrations, larger community |
| Hosted option | No infrastructure to run yourself |

---

## Summary table

| Capability | Langfuse | NovaFabric |
|---|---|---|
| Capture LLM traces | ✓ (SDK) | ✓ (wire-level, no code change) |
| Browse runs in a UI | ✓ | ✓ |
| Cost / token analytics | ✓ (live) | ✓ (offline/batch, experimental — `nova query`/`view`/`trend`/`pricing`) |
| Production alerting | ✓ | — |
| Prompt management | ✓ (hosted) | ✓ (local, experimental — `nova prompt`/`nova label`) |
| Portable capsule (no server to read) | — | ✓ |
| Re-run against recorded model responses | — | ✓ (tools run live) |
| Structural diff between runs | — | ✓ |
| Data lineage / blast radius | — | ✓ |
| Signed evidence bundles | — | ✓ |
| Asset registry + lifecycle | — | ✓ |
| Wire-level capture (no SDK) | — | ✓ |

The Langfuse column reflects our reading of its features; check Langfuse's current
documentation before relying on any "—".

---

## Can you use both?

Yes. They complement each other:

- **Langfuse** for live production monitoring — dashboards, cost, latency, alerts.
- **NovaFabric** for reproducibility, diff, lineage, and compliance — the questions
  you need to answer weeks or months after a run happened.

They capture at different layers and answer different questions. In a mature AI
platform you might want both.
