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

**Langfuse** requires SDK instrumentation — you add decorators or SDK calls to
your code at every capture point:

```python
from langfuse.decorators import observe

@observe()   # you must add this to every function you want traced
def run_agent():
    ...
```

Miss one call site and it isn't captured. Third-party agents you don't control
can't be instrumented.

**NovaFabric** captures at the HTTP layer — zero code changes required:

```bash
nova capture python agent.py
```

Works for any Python code, any framework, any LLM provider. Even agents you
can't modify, because the hook lives below all frameworks at the `requests` /
`aiohttp` level.

---

### 2. Where the data lives

**Langfuse** stores traces in its own database. To read them, you open Langfuse.
The data lives in their system, not yours.

**NovaFabric** stores each run as a portable capsule directory on your filesystem:

```
capsules/01KR9Q2AD…/
    capsule.yaml          # run metadata (model, command, exit code, timing)
    events.jsonl          # full trace of every LLM call and tool call
    assets.jsonl          # which datasets this run consumed
    lineage.jsonl         # provenance graph edges
    redaction-proof.json  # secret scanner results
```

You can `tar` it, archive it, move it to cold storage, share it with a colleague,
or read it on an air-gapped machine. No running server required.

---

### 3. Forensic replay

**Langfuse** shows you what happened. It has no replay capability.

**NovaFabric** lets you re-drive the agent against the exact same inputs from
any past run:

```bash
nova replay --mode forensic capsules/01KR9Q2AD…
```

The agent sees the same log file content, same model config, same tool responses
as the original run. If it produces the same output, the result is reproducible.
If not, something drifted — model update, tool change, non-determinism.

This is the difference between a flight recorder and a flight simulator.
Langfuse is the recorder. NovaFabric also lets you re-fly the same route.

---

### 4. Structural diff between runs

**Langfuse** has no diff capability.

**NovaFabric** compares two runs structurally:

```bash
nova diff capsules/01KR9Q2A…  capsules/01KRB4F7…
```

Output:

```
model:          same  (qwen3:35b)
tools_called:   same  (read_log, write_diagnosis)
tool_args:      CHANGED — read_log now reads 40 lines, was 100
output_length:  CHANGED — 312 tokens → 180 tokens
classification: same  (OOM)
```

After a prompt update or model upgrade, you can see immediately whether agent
behavior changed — even if the final label looks the same.

---

### 5. Data lineage

**Langfuse** has no concept of which dataset fed which run.

**NovaFabric** tracks this explicitly. Each agent declares what it consumed:

```python
record_consumed("slurm-logs/job-42819-oom@v1")
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

Langfuse cannot answer this question.

---

### 6. Evidence bundles for compliance

**Langfuse** produces dashboards and exports.

**NovaFabric** produces cryptographically signed evidence bundles:

```bash
nova export-evidence capsules/01KR9Q2AD…
# → signed ZIP with ed25519 signature + full event trace
```

An auditor can verify the signature and confirm the output hasn't been modified
since capture. This is a compliance primitive — useful in regulated industries
(healthcare, finance, HPC facilities) where you need to prove what an agent did
and that the record hasn't been altered.

---

## Where Langfuse is genuinely better

| Area | Why Langfuse wins |
|---|---|
| Real-time cost tracking | Token cost per run, by model, by user, over time |
| Production alerting | P95 latency monitors, error rate alerts |
| Prompt A/B testing | Side-by-side prompt experiments at scale |
| Team collaboration | Multi-user SaaS, comments, annotations, roles |
| Ecosystem maturity | More framework integrations, larger community |
| Hosted option | No infrastructure to run yourself |

---

## Summary table

| Capability | Langfuse | NovaFabric |
|---|---|---|
| Capture LLM traces | ✓ (SDK) | ✓ (wire-level, no code change) |
| Browse runs in a UI | ✓ | ✓ |
| Cost / token analytics | ✓ | — |
| Production alerting | ✓ | — |
| Prompt management | ✓ | — |
| Portable capsule (no server to read) | — | ✓ |
| Forensic replay | — | ✓ |
| Structural diff between runs | — | ✓ |
| Data lineage / blast radius | — | ✓ |
| Signed evidence bundles | — | ✓ |
| Asset registry + lifecycle | — | ✓ |
| Wire-level capture (no SDK) | — | ✓ |

---

## Can you use both?

Yes. They complement each other:

- **Langfuse** for live production monitoring — dashboards, cost, latency, alerts.
- **NovaFabric** for reproducibility, diff, lineage, and compliance — the questions
  you need to answer weeks or months after a run happened.

They capture at different layers and answer different questions. In a mature AI
platform you might want both.
