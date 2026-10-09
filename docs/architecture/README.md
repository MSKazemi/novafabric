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
| 8 | [OTLP ingest](otlp-ingest.md) | `POST /api/otlp/v1/traces` into a new capsule versus `POST /api/otlp/v1/logs` into the append-only sidecar store, and why a sealed capsule is never touched |
| 9 | [The `nova serve` request path](serve-request-path.md) | Host guard, scope table (deny unclassified), the tenancy start-up gate, and the audit record on a 403 |
| 10 | [Encryption at rest](encryption-at-rest.md) | Envelope v2 with object-key binding, fail-closed plaintext reads, the digest-pinned legacy inventory and v1 strict mode |
| 11 | [Server data plane](server-data-plane.md) | API-key workspace binding, per-org budgets and keyset pagination in `nova server` |

## Interactive explainer

[`explainer.html`](explainer.html) is a single self-contained page: no network,
no CDN, no build step. It has three sections:

1. **How NovaFabric works** — an animated map of the whole system, end to end:
   the workload and its framework adapters, capture (SDK interception, the
   transport wire record, secret scanning, the residual pass and
   `capture-health.json`), Run Capsule assembly, sealing, registry and lineage,
   replay (including recorded model errors and `intervention`), `nova diff` and
   its CI exit codes, `nova verify` and the Evidence Bundle, and server mode
   (`nova serve`, OTLP ingest, `nova server`). Data tokens travel the arrows, the
   active stage is highlighted, and every step has a caption, the modules it was
   checked against, its maturity label, and an **on main, unreleased** marker when
   the behaviour is newer than the last release. A last stage, *Not built yet*,
   shows planned and future-design work as exactly that. Controls: play, pause,
   step, scrub, speed (0.5× to 2×), and the keyboard (`Space`, `←` `→`, `Home`
   `End`, `1`–`9` and `0` for stages, `-` `+` for speed). Deep links:
   `explainer.html#story-11`, `explainer.html#stage-diff`.
2. **One run, end to end** — **capture → seal → replay → verify** on one example
   run, with the capsule's files appearing as they are written
   (`explainer.html#step-7`).
3. **Detailed flows** — two pipeline flows (mocked replay and the diff gate) and
   the four server-side flows of pages 8 to 11, stepped the same way
   (`explainer.html#flow-diff-gate-6`, `explainer.html#flow-otlp-ingest-3`).

Under `prefers-reduced-motion` nothing moves: tokens rest, labelled, at the end
of their arrows, and every step is also listed as text. The page follows the
system light or dark theme (or a toggle) and fits a 360 px phone screen; the
diagrams scroll sideways inside their own frame.
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
| [`otlp-ingest.svg`](../assets/architecture/otlp-ingest.svg) | otlp-ingest |
| [`serve-request-path.svg`](../assets/architecture/serve-request-path.svg) | serve-request-path |
| [`encryption-at-rest.svg`](../assets/architecture/encryption-at-rest.svg) | encryption-at-rest |
| [`server-data-plane.svg`](../assets/architecture/server-data-plane.svg) | server-data-plane |
| [`how-it-works.svg`](../assets/architecture/how-it-works.svg) | the explainer's system map ([README](README.md) › Interactive explainer) |
| [`mocked-replay.svg`](../assets/architecture/mocked-replay.svg) | the explainer; zooms into [replay-modes](replay-modes.md) |
| [`diff-gate.svg`](../assets/architecture/diff-gate.svg) | the explainer; zooms into [pipeline](pipeline.md) § Diff |

The last seven are generated, together with the explainer's flow and story data,
by `python scripts/gen_architecture_flows.py`. Edit that script, not the SVGs;
`tests/docs/test_architecture_diagrams.py` fails when they drift. Each step lights
its boxes and arrows in turn and every step is also listed as text under the
diagram, so nothing depends on watching the animation. A dagger (†) on a step
marks behaviour that is on main but not in a release yet.
