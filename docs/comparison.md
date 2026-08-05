# How NovaFabric compares

An honest comparison, **including where NovaFabric loses**. If a tool below is a
better fit for what you are doing, use it — a wrong recommendation costs you days
and costs this project the only currency it has.

The short version: most tools in this space answer *"how is my system performing
right now?"* NovaFabric answers *"can I prove, replay, and compare a run that
happened three months ago?"* Those are different questions, and it is completely
reasonable to need both.

---

## At a glance

| | **NovaFabric** | Self-hosted observability (Langfuse, Arize Phoenix) | Hosted SaaS (LangSmith, W&B Weave, Helicone) | Experiment tracking (MLflow, DVC) |
|---|---|---|---|---|
| Deployment | Self-hosted CLI, server optional, **no account** | Self-hosted server + database (required) | Managed cloud | Self-hosted or managed |
| Primary artifact | Portable evidence **capsule** — a folder | Trace row in a database | Trace row in a vendor cloud | Run record + artifacts |
| Where your data lives | **Your machine** | Your server | Vendor cloud | Your store or vendor |
| Replay a past run | **✓ four modes** | ✗ | ✗ | partial — re-run a script |
| Run-to-run structural diff | **✓** | partial (eval) | partial (eval) | metric comparison only |
| Cryptographic signing / provenance | **✓** in-toto DSSE, Sigstore, RFC 3161 | ✗ | ✗ | ✗ |
| Capture with no code changes | **✓** hooks, wire-level, proxy | SDK instrumentation | SDK or proxy | explicit logging calls |
| Works fully offline / air-gapped | **✓** | self-host only | ✗ | partial |
| Real-time dashboards & alerting | ✗ **weak** | ✓ strong | ✓ strong | partial |
| Hosted multi-user convenience | ✗ **none** | partial | ✓ strong | ✓ |
| Prompt playground / A-B testing | ✗ **none** | ✓ | ✓ | ✗ |
| Maturity | **beta, pre-1.0, small** | mature | mature | very mature |

---

## Where NovaFabric is genuinely the wrong choice

Use something else if:

- **You want live monitoring and alerting.** NovaFabric is not an APM and does not
  try to be. Langfuse, Phoenix, LangSmith, and Helicone all do this properly.
- **You want a hosted service with a team UI and zero operations.** There is no
  SaaS, no account system, and no plan to build one.
- **You want a prompt playground or A/B testing workflow.** Not built, not planned.
- **You need a mature, battle-tested tool right now.** NovaFabric is pre-1.0. The
  capsule format is not frozen and will change before v1.0.
- **Your workload is a classic ML training sweep and you want metric comparison.**
  MLflow and W&B are better at exactly that, and have been for years.

## Where NovaFabric is genuinely better

- **Proving what a past run did, months later, to someone who does not trust you.**
  A capsule is signed, timestamped, and verifiable with no server and no network.
- **Replaying a run offline** with model and tool calls served from the capsule —
  no API keys, no tokens spent, no vendor availability required.
- **Structural diff between two runs** — not "metric A went up", but *what changed
  in the execution*.
- **Air-gapped and regulated environments.** No telemetry, no update checks, no
  internet requirement for core features.
- **HPC and SLURM workloads.** Capture works on a login node with no daemon and no
  privileged access — a genuinely under-served case.
- **Regulatory evidence.** EU AI Act, ISO 42001, CycloneDX AI-BOM exporters run
  off the same captured facts.

---

## Specific comparisons

### NovaFabric vs Langfuse

**Use Langfuse if** you want self-hosted LLM observability with strong dashboards,
cost and token analytics, prompt management, and a team UI. It is mature, widely
deployed, and better than NovaFabric at all of it.

**Use NovaFabric if** you need to replay a run, diff two runs structurally, or hand
an auditor a signed artifact that verifies without your infrastructure being up.

**Use both:** NovaFabric emits OpenTelemetry GenAI spans and OpenLineage events, so
it can feed a Langfuse deployment while keeping portable capsules for replay and
audit. They are complementary; this is not a migration story.

### NovaFabric vs LangSmith

LangSmith is a hosted service tightly integrated with LangChain, with a mature
evaluation and tracing product. Its data lives in LangSmith's cloud.

The distinction is ownership: with NovaFabric the artifact is a folder on your
filesystem that you can `tar` and archive for a decade. If your constraint is *"the
data cannot leave our infrastructure"* — regulated industry, national lab,
air-gapped cluster — that constraint alone decides it.

### NovaFabric vs MLflow / Weights & Biases

Different eras of the same instinct. MLflow and W&B track *experiments*: parameters,
metrics, artifacts, model versions, and they are excellent at it.

NovaFabric captures *executions*: the full call graph of an agent or job, the
environment lock, the redaction proof, and a signature over all of it. If you want
to compare learning curves, use MLflow. If you want to prove what an agent did and
re-run it offline, use NovaFabric. Many teams will reasonably run both.

### NovaFabric vs OpenTelemetry alone

OpenTelemetry is a standard, not a product, and NovaFabric emits it rather than
competing with it. Raw OTel gives you spans in a backend; it does not give you a
portable folder, a replay engine, an environment lock, a redaction proof, or a
signature. If a span in Jaeger answers your question, you do not need NovaFabric.

### NovaFabric vs "just save the logs"

The honest baseline, and it works longer than people expect. It stops working when
you need to prove the logs were not edited, re-run the workload without the
provider, know exactly which package versions were installed, or show that no
secret leaked into the record. Those are the four things a capsule adds.

---

## Frequently asked

**Is NovaFabric production-ready?** It is **beta**. Local capture, replay, diff,
lineage, and the trust layer are stable and used daily. Server mode, the
cluster-scale collector, and the dashboard are `experimental`. The capsule format
is **not frozen** — expect schema changes before v1.0. Read
[the roadmap](../ROADMAP.md) before betting a production pipeline on it.

**Does it send any data anywhere?** No. No telemetry, no update checks, no
analytics. Core local-mode features need no internet at all.

**Does it work without a server or database?** Yes — that is the default mode. A
server is optional and exists for teams sharing capsules.

**Does it capture my prompts and responses?** Not by default. Full prompt and
response capture is explicitly opt-in, and everything captured passes through
secret scanning and redaction first.

**Which frameworks does it support?** Capture is framework-agnostic — it wraps a
command. Adapters add richer detail for LangChain, LangGraph, the OpenAI Agents
SDK, A2A, MCP, and others. See [the architecture map](architecture.md).

**Can I use it with my existing observability stack?** Yes, and that is the
intended shape. Emit OTel GenAI to your backend, keep capsules for replay and
evidence.

---

## See also

- [Concepts](concepts.md) — the five primitives and the replay modes
- [Architecture](architecture.md) — the subsystem map and design invariants
- [Roadmap](../ROADMAP.md) — what is shipped, experimental, and planned
- [Getting started](getting-started.md)

*Found something unfair or out of date here? That is a bug —
[open an issue](https://github.com/novafabric/novafabric/issues/new?template=documentation.yml).
Comparisons rot, and a comparison that flatters us is worse than useless.*
