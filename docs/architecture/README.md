# Architecture, as built

This folder is the illustrated, as-built companion to the
[architecture overview](../architecture.md). Each page describes what the code
in `src/novafabric/` does **today**, names the modules it was checked against,
and labels every capability with one of the four maturity terms used across the
docs:

| Label | Meaning |
|---|---|
| **works today** | Shipped, tested, and the default or a supported path |
| **experimental** | Implemented and tested, but the interface or on-disk format may still change |
| **planned** | On the roadmap with a target; not implemented |
| **future design** | Documented intent; no implementation and no target |

Where an older page describes broader behaviour than the code implements, these
pages describe the code. The diagrams are animated SVGs. They honour
`prefers-reduced-motion` and render on both light and dark backgrounds. Every page
also has a plain Mermaid diagram that diffs cleanly in review.

![The five primitives](../assets/architecture/primitives.svg)

## Pages

| # | Page | Covers |
|---|---|---|
| 1 | [System overview and the five primitives](overview.md) | What the five primitives are, how they relate, and the invariant that the capsule is the source of truth |
| 2 | [The pipeline: capture → seal → replay → diff → audit](pipeline.md) | The verb chain end to end, with the CLI command and module behind each step |
| 3 | [Run Capsule anatomy](run-capsule.md) | Every file in a capsule directory, who writes it, and in what order |
| 4 | [Sealing and verification](sealing-and-verification.md) | NovaSeal DSSE signatures, RFC 3161 timestamps, the Merkle log, the redaction proof, and `nova verify` |
| 5 | [Replay modes](replay-modes.md) | What each of the five `nova replay` modes does, and what it does not do |
| 6 | [The lineage graph](lineage-graph.md) | Node and edge types, the four traversals, and the storage backends |
| 7 | [Deployment topologies](deployment-topologies.md) | Local, server and cluster-scale deployments, with a maturity label on each component |

## Interactive explainer

[`explainer.html`](explainer.html) is a single self-contained page: no network,
no CDN, no build step. It steps through **capture → seal → replay → verify** on
one example run, with play, pause and step controls and full keyboard support.
GitHub shows HTML source rather than rendering it, so to use the explainer,
download the file or clone the repository and open it in any browser.

## Diagram index

| Diagram | Used on |
|---|---|
| [`primitives.svg`](../assets/architecture/primitives.svg) | overview |
| [`pipeline-flow.svg`](../assets/architecture/pipeline-flow.svg) | pipeline |
| [`capsule-assembly.svg`](../assets/architecture/capsule-assembly.svg) | run-capsule |
| [`seal-verify-chain.svg`](../assets/architecture/seal-verify-chain.svg) | sealing-and-verification |
| [`replay-modes.svg`](../assets/architecture/replay-modes.svg) | replay-modes |
| [`lineage-graph.svg`](../assets/architecture/lineage-graph.svg) | lineage-graph |
| [`topology.svg`](../assets/architecture/topology.svg) | deployment-topologies |
