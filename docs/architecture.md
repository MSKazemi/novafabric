# Architecture

A contributor's map of the codebase: what the subsystems are, where they live,
and which invariants you must not break. For the *conceptual* model — what a Run
Capsule is, how replay modes differ — read [concepts.md](concepts.md) first.

> **New here?** The fastest orientation is: read
> [Concepts](concepts.md) → skim [the subsystem map](#subsystem-map) below →
> pick a [good first issue](https://github.com/novafabric/novafabric/labels/good%20first%20issue).
> You do not need to understand the whole system to fix a bug in one subsystem.

---

## Framing

> **Local-first now. Distributed-ready always. Cluster-scale later.**

A one-node run is the smallest case of a distributed run. The architecture
deliberately avoids assumptions that would block the cluster-scale direction:
that one run equals one process, that one capsule has one writer, that every
agent is Python, that all capture is full capture, or that events arrive in
order.

SQLite is the local-mode default; Postgres is the server-mode backend.
**Postgres is never required for local mode** — if you find a code path that
makes it required, that is a bug worth reporting.

---

## System context

```
        your workload                     NovaFabric                     artifacts
  ┌────────────────────────┐      ┌───────────────────────┐      ┌────────────────────┐
  │ script / notebook cell │      │  capture (hooks +     │      │  Run Capsule       │
  │ agent / LLM app        │─────▶│  proxies + runners)   │─────▶│  ~/.novafabric/    │
  │ HPC training job       │      │                       │      │    capsules/<ulid>/│
  └────────────────────────┘      └───────────┬───────────┘      └─────────┬──────────┘
                                              │                            │
                                   ┌──────────▼──────────┐      ┌──────────▼──────────┐
                                   │ seal · redact ·     │      │ replay · diff ·     │
                                   │ validate            │      │ lineage · evidence  │
                                   └─────────────────────┘      └─────────────────────┘
```

The unit of value is **a portable folder you own**, not a row in a hosted
database. Every design decision follows from that: no server is required, no
account is required, nothing phones home, and a capsule read air-gapped five
years from now must still verify.

---

## Subsystem map

Everything lives under `src/novafabric/`. Sizes are a rough guide to where the
weight is, not a quality signal.

### The core path — capture to capsule

| Module | What it does |
|---|---|
| `capture/` | The capture orchestrator, hook installation, and the event recorder. **The hot path** — changes here need benchmarks. |
| `runners/` | Where the captured workload actually executes: local process, container, SLURM, Kubernetes. |
| `proxy/` | Transparent HTTP proxies for providers that cannot be hooked in-process. |
| `adapters/` | Framework integrations (LangChain, LangGraph, OpenAI Agents SDK, A2A, MCP, and others). Adding one is a good first contribution. |
| `capsule/` | Capsule construction, validation, parent/child relationships. |
| `spec/`, `registry/` | Asset Spec models and the local asset registry. |
| `masking/`, `pii/` | Redaction pipeline and pluggable maskers. |

### Verification and trust

| Module | What it does |
|---|---|
| `trust/` | NovaSeal — signing, Merkle trees, timestamping, transparency witnesses. |
| `evidence/`, `evidence_fabric/` | Evidence Bundle construction and export. |
| `provenance/`, `supplychain/` | Attestations, AI-BOM, in-toto/SLSA envelopes. |
| `audit/`, `governance/`, `policies/`, `policy/` | Policy gates and the audit trail. |

### Reasoning over runs

| Module | What it does |
|---|---|
| `replay/` | The replay engine and its modes. **A shared surface — coordinate before large changes.** |
| `diff/` | Structural diff between two capsules. |
| `lineage/` | Lineage graph and its storage backends (SQLite, Kuzu, Postgres, Apache AGE, JanusGraph). |
| `kg/` | The security & provenance knowledge graph. |
| `diagnose/` | Causal-graph attribution and counterfactual root-cause search. |
| `query/` | The offline capsule query DSL. |
| `eval/`, `evals/`, `judge/`, `scores.py` | Evaluation harness, scoring, regression gates. |

### Serving and scale

| Module | What it does |
|---|---|
| `cli/` | Every `nova` subcommand. The largest module by file count — and the most approachable. |
| `server/` | Server mode: REST API, auth, API keys, tenancy. |
| `serve/` | The dashboard backend that `nova serve` exposes. |
| `web/` (repo root) | The dashboard frontend — Astro + React, with its own vitest suite. |
| `metadata_store/` | SQLite and Postgres metadata backends, including row-level security. |
| `object_capsule_store/` | S3-compatible object storage for capsules at scale. |
| `collector_app/`, `collector/` (repo root, Go) | The cluster-scale collector and node spool. |
| `events/`, `envelope/`, `envelopes/` | Event Envelope v1 and the event bus. |

### Compliance and reporting

`compliance/` is the second-largest module: regulatory exporters (EU AI Act,
ISO 42001, CycloneDX AI-BOM, and others), each mapping capsule facts to a
standard's required form. `report/`, `export_blob/`, `viewer/`, `viz/` handle
presentation and portability.

---

## Design invariants

Break these and the review will ask you to start over — not out of pedantry, but
because each one is load-bearing for a promise the project makes to users.

1. **A capsule is portable and self-describing.** It must verify with no server,
   no network, and no database.
2. **Capture never blocks the user's workload by default.** If a NovaFabric
   component fails, the workload continues and the failure is recorded.
3. **Secrets never leave the redaction boundary.** No prompts, tokens, or env
   vars in logs, telemetry, or unredacted capsule fields.
4. **Full prompt/response capture is opt-in**, never the default.
5. **No silent telemetry and no update checks.** Ever. The project has none and
   will not gain any.
6. **Core local-mode features require no internet.**
7. **Compute-node hot paths never write to a database or graph.** They write to
   a local spool; something else ingests it.
8. **Schema changes are additive and optional first.** A new field must not
   break validation of an existing capsule.
9. **No root containers, no privileged Kubernetes access.**
10. **Two top-level formats only** — Run Capsule and Evidence Bundle. A third is
    not on the table.

---

## Deployment modes

| Mode | What runs | Storage | When |
|---|---|---|---|
| **Local** | The CLI only | Filesystem + SQLite | A laptop, a workstation, an HPC login node. The default. |
| **Server** | `nova server` | Postgres + object store | A team sharing capsules, with auth and tenancy. |
| **Cluster-scale** | Collector + node spools | Postgres + object store + a graph backend | Many nodes producing capsules concurrently. |

Every mode reads the same capsule format. Moving up a tier is a deployment
change, not a migration.

---

## What NovaFabric is not

Stated plainly, because knowing the boundary saves you from proposing something
that will be declined:

- **Not an observability platform.** Tracing tells you what happened; this tells
  you whether a run can be replayed, compared, and proven. If you want spans in a
  dashboard, use an APM.
- **Not a hosted service.** There is no SaaS and no account system.
- **Not a model registry or a feature store.**
- **Not a scheduler.** It captures workloads that SLURM or Kubernetes schedules;
  it does not schedule them.

See [the roadmap](../ROADMAP.md) for what is planned, and
[the decisions index](decisions.md) for what has already been decided and why.

---

## Where to go next

| You want to… | Read |
|---|---|
| Understand the concepts | [concepts.md](concepts.md) |
| Make your first change | [CONTRIBUTING.md](../CONTRIBUTING.md) |
| Add an asset type, CLI command, or report format | [developer-guide.md](developer-guide.md) |
| Understand the security posture | [SECURITY.md](../SECURITY.md) |
| Propose an architectural change | [RFC process](governance/rfc-process.md) |
| See what has been decided | [decisions.md](decisions.md) |
