# NovaFabric Documentation

NovaFabric turns any command — a training script, an AI agent, a notebook cell —
into a **Run Capsule**: a structured, secret-redacted, self-contained record of
everything observable about that execution. Capsules can be validated, replayed,
diffed, sealed, and traced through a lineage graph, **without modifying the code
you run**.

The product thesis is *replayable AI infrastructure*: tracing tells you what
happened; NovaFabric tells you whether a past run can be safely **replayed**,
**compared**, and **proven**. Everything runs inside your own infrastructure —
laptop to cluster, online or air-gapped — with no accounts and no telemetry.

This page is the documentation map. Each linked document states clearly whether a
feature is **works today**, **experimental**, or **planned / future design** —
the [Concepts](concepts.md) page and the [ROADMAP](../ROADMAP.md) are the
authoritative picture of what is and is not shipped.

> **New here?** Read the [Getting Started guide](getting-started.md) first
> (about 15 minutes), then pick a path from the reading paths below.

---

## What you will find here

The docs are organized by intent, following the four canonical documentation
modes:

| If you want to… | Go to |
|---|---|
| **Get running fast** | [Start here](#start-here) — install, first capture, first replay |
| **Understand the ideas** | [Learn (tutorials)](#learn-tutorials) — how capture, replay, and lineage actually work |
| **Do a specific task** | [Use (guides)](#use-guides) — day-to-day workflows and operations |
| **Look up an exact detail** | [Reference](#reference) — every command, flag, endpoint, and format |
| **Build on top of it** | [Extend](#extend) — plugins and extension points |

The five primitives that everything below is built from — **Asset Registry**,
**Run Capsule**, **Replay**, **Lineage**, and **Evidence Bundle** — are defined
in [Concepts](concepts.md). The strategic verb chain across them is
**Capture → Seal → Replay → Diff → Audit**.

---

## Start here

| Document | What it is |
|---|---|
| [Getting Started](getting-started.md) | Install → capture → validate → replay → diff → lineage → seal, in about 15 minutes |
| [Why NovaFabric?](tutorials/why-novafabric.md) | Plain-English value guide with five concrete scenarios |
| [Feature tour](tutorials/feature-tour.md) | Hands-on tour of every capability, with real commands and output |
| [Black-box recorder demo](../examples/blackbox_demo/README.md) | End-to-end demo (capture → validate → scan-secrets → replay → diff → lineage → verify) — no live API key required |

## Learn (tutorials)

Learning-oriented, read in any order. Full index: [tutorials/README.md](tutorials/README.md).

| Document | What you learn |
|---|---|
| [How capture works](tutorials/how-capture-works.md) | How NovaFabric intercepts LLM calls at the HTTP layer, the URL registry, and all capture modes |
| [Multi-agent capture](tutorials/multi-agent-capture.md) | Same-process vs separate-process topologies, MCP sub-agents, parent/child linking |
| [Cluster scale — 1,000,000 agents](tutorials/cluster-scale.md) | Where the shipped single-cluster design ends and the cluster-scale *design intent* begins |
| [NovaFabric vs Langfuse](tutorials/novafabric-vs-langfuse.md) | Honest comparison: monitoring vs reproducibility, and why they are complementary |

## Use (guides)

Task- and workflow-oriented references for day-to-day use.

| Document | Audience |
|---|---|
| [User guide](user-guide.md) | Every shipped `nova` command, organized by workflow — including a summary of what shipped `experimental` in v0.59 |
| [Operator guide](operator-guide.md) | Deploying on shared infrastructure — local, Docker, Kubernetes, SLURM |
| [Dashboard](dashboard.md) | The local web dashboard (`nova serve --experimental`) — capabilities and limits |
| [Warm capture daemon](warm-capture-daemon.md) | Removing per-run cold-start cost for fleets, HPC job arrays, and CI farms (experimental, Linux only) |
| [Security & Provenance Knowledge Graph](security-knowledge-graph.md) | `nova kg` (experimental) — anomaly detection, attack-path, and blast-radius over a capsule's lineage |
| [Developer guide](developer-guide.md) | Local dev setup, extension points, adding asset types and commands |

## Reference

Look up an exact detail — commands, flags, endpoints, and on-disk formats.

| Document | What it covers |
|---|---|
| [Concepts](concepts.md) | Capsule format, capture hooks, replay modes, lineage edges — the mental model and the five primitives |
| [CLI reference](cli-reference.md) | Exhaustive flag/option/example reference for every `nova` command, including the v0.59 experimental groups (`prompt`, `label`, `session`, `query`/`view`/`trend`, `annotate`, `score`, `retention`, `events`, `experiment`, `graph`, `pricing`, …) |
| [Python API](python-api.md) | Programmatic API: `@agent` decorator, `CaptureOrchestrator`, Replay, Lineage, Registry, `novafabric.scores.submit` |
| [REST API reference](api-reference.md) | The `nova serve` FastAPI route table — endpoints, methods, and auth |

## NovaSeal (cryptographic sealing — regulated environments)

Sealing binds a capsule to a signature, and — where available — a trusted
timestamp and an append-only log, so an auditor can verify offline that a capsule
is unmodified since signing. Check each page's status header for what is
implemented versus design intent.

| Document | What it covers |
|---|---|
| [Configuration](novaseal-configuration.md) | Config file, profiles (local / AWS KMS / Azure KV / GCP KMS), Merkle log, discovery order |
| [Key management](novaseal-key-management.md) | Key lifecycle: generation, rotation, compromise recovery, HSM options |
| [API stability](novaseal-stability.md) | Stability guarantee, versioning policy, breaking-change definition |

## Operations

| Document | What it covers |
|---|---|
| [Server deployment](ops/server-deployment.md) | Deployment scenarios from local SQLite to Postgres and Kubernetes |
| [Server admin guide](ops/server-admin-guide.md) | Operating `nova server` in production — auth, tokens, RBAC, quotas, observability endpoints |
| [Backup & restore runbook](ops/backup-restore.md) | `nova backup` / `nova restore`, verification, DR procedure (works today + planned sections) |
| [Cluster-scale migration](ops/cluster-scale-migration.md) | Migrating from SQLite + filesystem toward Postgres + object storage + graph DB |
| [Lineage store migration](lineage/migration-guide.md) | Migrating the lineage store from SQLite to KuzuDB |

## Extend

| Document | What it covers |
|---|---|
| [Writing a hook plugin](integrations/writing-a-hook-plugin.md) | Authoring a third-party capture-hook plugin (entry-point discovery, install contract) |

## Releases

- [Release process](release-process.md) — how a release is cut (quality gates, version bump, tag)
- [Release notes](releases/) — per-version notes from v0.1.0 onward

---

## Reading paths

Not sure where to start? Follow the path that matches your goal.

- **"I just want to see it work."**
  [Getting Started](getting-started.md) → [Black-box recorder demo](../examples/blackbox_demo/README.md) → [Feature tour](tutorials/feature-tour.md).

- **"I want to understand how it captures without touching my code."**
  [How capture works](tutorials/how-capture-works.md) → [Concepts](concepts.md) → [Multi-agent capture](tutorials/multi-agent-capture.md).

- **"I need a CI regression gate."**
  [Getting Started](getting-started.md) → [User guide](user-guide.md) (`nova diff --assert-no-regressions`) → [CLI reference](cli-reference.md).

- **"I'm deploying this for a team / on a cluster."**
  [Operator guide](operator-guide.md) → [Server deployment](ops/server-deployment.md) → [Warm capture daemon](warm-capture-daemon.md).

- **"I care about audit and compliance evidence."**
  [Concepts](concepts.md) (Evidence Bundle) → [NovaSeal configuration](novaseal-configuration.md) → [Security & Provenance Knowledge Graph](security-knowledge-graph.md).

- **"I want to extend or integrate it."**
  [Developer guide](developer-guide.md) → [Python API](python-api.md) → [Writing a hook plugin](integrations/writing-a-hook-plugin.md).

- **"I'm coming from an LLM-observability platform (prompts, scores, sessions, analytics)."**
  [NovaFabric vs Langfuse](tutorials/novafabric-vs-langfuse.md) → [User guide](user-guide.md#what-shipped-experimental-in-v059) (the v0.59 experimental cohort: prompt versioning, sessions, offline analytics, annotation queues) → [CLI reference](cli-reference.md). All of these surfaces are **experimental**.

---

## Conventions

- **Filenames** use lowercase `kebab-case` (e.g. `getting-started.md`).
- **Honesty labels.** Every doc marks future-facing features as *experimental*,
  *planned*, or *future design* — never as shipped. Read each page's status
  header, and treat the [ROADMAP](../ROADMAP.md) as the source of truth for what
  is implemented. NovaFabric produces evidence that *supports* compliance
  workflows; it does not certify or guarantee compliance with any regulation.
- **Two top-level formats only.** Everything reduces to the **Run Capsule** and
  the **Evidence Bundle**. Event envelopes, lineage edges, env locks, redaction
  proofs, and attestations all live *inside* those two formats.
- **Default storage paths.** The CLI `nova capture` writes capsules under
  `~/.novafabric/capsules/`. The Python `CaptureOrchestrator`, the `nova api-proxy`
  / `nova mcp-proxy` commands, and `nova serve --capsule-dir` default to a
  project-local `./.novafabric/runs/`. Both are correct in their respective contexts.

---

## Next steps

- Brand new? Start with [Getting Started](getting-started.md).
- Want the mental model before the commands? Read [Concepts](concepts.md).
- Evaluating against another tool? Read [NovaFabric vs Langfuse](tutorials/novafabric-vs-langfuse.md).
- Ready to build on it? Head to the [Developer guide](developer-guide.md).

This `docs/` tree is the **user-facing** documentation. Internal design notes,
ADRs, strategy, and research live in the private `design/` tree and are not part
of the public documentation set.
