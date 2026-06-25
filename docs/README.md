# NovaFabric Documentation

NovaFabric turns any command — a training script, an AI agent, a notebook cell —
into a **run capsule**: a structured, secret-redacted, self-contained record of
everything observable about that execution. Capsules can be validated, replayed,
diffed, sealed, and traced through a lineage graph, without modifying the code
you run.

This is the documentation map. Each document states clearly whether a feature is
**implemented**, **experimental**, or **planned** — see the [Concepts](concepts.md)
and [ROADMAP](../ROADMAP.md) for the authoritative picture.

> **New here?** Read the [Getting Started guide](getting-started.md) first
> (about 15 minutes), then pick a path below.

---

## Start here

| Document | What it is |
|---|---|
| [Getting Started](getting-started.md) | Install → capture → validate → replay → diff → lineage → seal, in 15 minutes |
| [Why NovaFabric?](tutorials/why-novafabric.md) | Plain-English value guide with five concrete scenarios |
| [Feature tour](tutorials/feature-tour.md) | Hands-on tour of every capability, with real commands and output |

## Learn (tutorials)

Learning-oriented, read in any order. Full index: [tutorials/README.md](tutorials/README.md).

| Document | What you learn |
|---|---|
| [How capture works](tutorials/how-capture-works.md) | How NovaFabric intercepts LLM calls at the HTTP layer, the URL registry, and all four capture modes |
| [Multi-agent capture](tutorials/multi-agent-capture.md) | Same-process vs separate-process topologies, MCP sub-agents, parent/child linking |
| [Cluster scale — 1,000,000 agents](tutorials/cluster-scale.md) | Where the single-cluster design ends and the cluster-scale design intent begins |
| [NovaFabric vs Langfuse](tutorials/novafabric-vs-langfuse.md) | Honest comparison: monitoring vs reproducibility |

## Use (guides)

Task- and workflow-oriented references for day-to-day use.

| Document | Audience |
|---|---|
| [User guide](user-guide.md) | Every shipped `nova` command, organized by workflow |
| [Operator guide](operator-guide.md) | Deploying on shared infrastructure — local, Docker, Kubernetes, SLURM |
| [Dashboard](dashboard.md) | The local web dashboard (`nova serve --experimental`) — capabilities and limits |
| [Developer guide](developer-guide.md) | Local dev setup, extension points, adding asset types and commands |

## Reference

| Document | What it covers |
|---|---|
| [Concepts](concepts.md) | Capsule format, capture hooks, replay modes, lineage edges — the mental model |
| [CLI reference](cli-reference.md) | Exhaustive flag/option/example reference for every `nova` command |
| [Python API](python-api.md) | Programmatic API: `@agent` decorator, `CaptureOrchestrator`, Replay, Lineage, Registry |

## NovaSeal (cryptographic sealing — regulated environments)

| Document | What it covers |
|---|---|
| [Configuration](novaseal-configuration.md) | Config file, profiles (local / AWS KMS / Azure KV / GCP KMS), Merkle log, discovery order |
| [Key management](novaseal-key-management.md) | Key lifecycle: generation, rotation, compromise recovery, HSM options |
| [API stability](novaseal-stability.md) | Stability guarantee, versioning policy, breaking-change definition |

## Operations

| Document | What it covers |
|---|---|
| [Server deployment](ops/server-deployment.md) | Deployment scenarios from local SQLite to Postgres and Kubernetes |
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

## Conventions

- **Filenames** use lowercase `kebab-case` (e.g. `getting-started.md`).
- **Honesty labels.** Every doc marks future-facing features as *experimental*,
  *planned*, or *future design* — never as shipped. See the project's docs-honesty
  rule and the [ROADMAP](../ROADMAP.md).
- **Default storage paths.** The CLI `nova capture` writes capsules under
  `~/.novafabric/capsules/`. The Python `CaptureOrchestrator`, the `nova api-proxy`
  / `nova mcp-proxy` commands, and `nova serve --capsule-dir` default to a
  project-local `./.novafabric/runs/`. Both are correct in their respective contexts.

This `docs/` tree is the **user-facing** documentation. Internal design notes,
ADRs, strategy, and research live in the private `design/` tree and are not part
of the public documentation set.
