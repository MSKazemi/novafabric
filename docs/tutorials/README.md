# NovaFabric Tutorials

Plain-English guides for engineers at every level. Read in any order.

---

## Start here

| Tutorial | What you learn |
|---|---|
| [Why NovaFabric?](why-novafabric.md) | The problem it solves, with five concrete scenarios using real testbench workloads |
| [Getting Started](../getting-started.md) | The 15-minute first run: install, capture, validate, replay, diff, lineage, seal |
| [Feature tour](feature-tour.md) | Hands-on tour of every capability — proxies, all providers, LangChain, evidence bundles, knowledge graph, capture-level policy, GDPR erasure, dashboard reports, topology, compliance exports |
| [5-minute black-box recorder demo](../../examples/blackbox_demo/README.md) | End-to-end demo: capture → validate → scan-secrets → replay → diff → lineage → verify; no live API key required |

---

## Go deeper

| Tutorial | What you learn |
|---|---|
| [How capture works](how-capture-works.md) | What HTTP is, what `requests` and `aiohttp` are, how monkey-patching intercepts LLM calls without code changes, the URL registry, all four capture modes |
| [Multi-agent capture](multi-agent-capture.md) | Same-process vs separate-process topologies, how to capture each, MCP sub-agents, the parent/child linking design (ADR-0032) |
| [Cluster scale — 1,000,000 agents](cluster-scale.md) | Where capsules live (always filesystem), why the current single-cluster design doesn't reach 1M agents, the local-first rule, the four-layer architecture, the three hard problems (storage, lineage, identity), and the phased build plan |

---

## Compare and evaluate

| Tutorial | What you learn |
|---|---|
| [NovaFabric vs Langfuse](novafabric-vs-langfuse.md) | Where they overlap, where they differ, honest assessment of what each does better |

---

## Related docs

- [Concepts](../concepts.md) — capsule structure, capture hook mechanism, replay modes, lineage edge types
- [CLI reference](../cli-reference.md) — every `nova` command with flags and examples
- [User guide](../user-guide.md) — every shipped command, organized by workflow
- [Developer guide](../developer-guide.md) — local dev setup, extension points, contributing
- [ROADMAP.md](../../ROADMAP.md) — release sequencing v0.1 → v1.0
