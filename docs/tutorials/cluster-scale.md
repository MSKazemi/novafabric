# Capturing at cluster scale — 1,000,000 agents

> **Who this is for:** platform architects planning NovaFabric deployments across
> HPC clusters, AI factories, or large-scale distributed agent systems.
> 
> **Status of this document (corrected 2026-07-30):** the single-cluster design
> (v0.7) is shipped, and — contrary to an earlier draft of this page — the
> collector hierarchy, hot/cold storage split, and lineage-at-scale tiers below
> it are **also shipped, as experimental**, not still "design + build." See
> `design/architecture/cluster-scale.md` (the authoritative, actively-maintained
> version of this material) for full per-phase evidence. What genuinely remains
> design intent only is cross-cluster **federation** (Phase 6) and full
> cross-org identity — those are called out explicitly below.

---

## Why the current design doesn't reach 1M agents

v0.7 works for one team on one cluster:

- `nova capture` writes capsules to local disk
- `nova serve` reads them into Postgres on a single server
- The lineage graph lives in that Postgres instance

At 1,000,000 concurrent agents this breaks in three places:

| Problem | What breaks |
|---|---|
| **Storage** | 1M agents × ~1,000 events each = 1 billion events per run-cycle. Postgres cannot ingest this. |
| **Ingestion** | A single server receiving writes from 1M simultaneous processes becomes a bottleneck that stalls the entire fleet. |
| **Lineage graph** | Blast-radius queries over billions of edges need a graph store with proper traversal indexes, not `SELECT WHERE` over a Postgres table. |
| **Identity** | One OIDC server issuing tokens to 1M agents is a single point of failure. Each agent needs a credential verifiable without calling home. |

---

## The one rule that makes scale possible

> **Capture is always local. Collection is always async.**

A compute node running agent #847,291 must never make a network call to a central
server during the agent's execution. If it does, one slow server stalls 1M jobs.

Instead:

```
[agent #847,291 on compute node]
        │
        ▼
  nova capture  →  writes to LOCAL buffer (tmpfs or NVMe on the node)
                   agent finishes, capsule is sealed
                   collection happens AFTER, in the background
        │
        ▼  (async, after agent exits)
  [rack-level collector]
        │
        ▼
  [cluster-level aggregator]
        │
        ▼
  [central lineage store]
```

The agent never waits for any central system. This is already baked into the
current design — `nova capture` writes locally. The collector hierarchy above
it is **also built now** (experimental, not yet proven at 1M-agent scale): a
Go node-collector (`collector/`, Lustre-safe spool + NovaSeal batch signing,
295K events/sec measured) and a lighter Python FastAPI receiver
(`collector_app/`, `POST /capsule`) both exist and forward into the object
store and event bus described below.

---

## The full architecture

```
                   ┌────────────────────────────────┐
                   │        Federation Layer         │
                   │  (cross-org, cross-site policy) │
                   └───────────────┬────────────────┘
                                   │
           ┌───────────────────────┼────────────────────────┐
           ▼                       ▼                        ▼
  [Cluster A — nova serve]  [Cluster B — nova serve]   [Cluster C]
    Postgres + REST API        Postgres + REST API        ...
           │                       │
  ┌────────┴───────┐      ┌────────┴───────┐
  ▼                ▼      ▼                ▼
[Rack collector] [Rack collector]        ...
  │    │    │
  ▼    ▼    ▼
[node][node][node]   ←  each runs nova capture locally
```

### Layer 1 — Compute node (per agent)

Responsibility: capture only, no network during execution.

- `nova capture` writes events to a local buffer (tmpfs, NVMe)
- Capsule is sealed when the agent exits
- Local buffer is bounded — if the agent crashes, the partial capsule is still
  valid and readable

Must not do:
- Synchronous writes to remote databases
- Heavy redaction or graph construction
- Block the agent workload for any capture operation

### Layer 2 — Rack collector (per rack or per node group)

Responsibility: pull, validate, and forward.

- Polls compute nodes for completed capsules
- Validates capsule schema
- Deduplicates content (identical prompts/responses stored once)
- Forwards metadata to cluster server, raw events to object storage
- Runs as a daemon process on a dedicated rack-level node

### Layer 3 — Cluster server (per cluster)

The current `nova serve` (v0.7) with Postgres. Responsibilities:

- Stores run metadata (not raw events — those live in object storage)
- Maintains the lineage graph for this cluster
- Serves the REST API and dashboard
- Issues short-lived credentials to agents

### Layer 4 — Federation (cross-cluster, cross-org)

Not designed yet. Responsibilities will include:

- Cross-cluster lineage queries ("show me every run across all clusters that
  consumed this dataset")
- Cross-org identity (an agent at facility A needs a credential verifiable at
  facility B)
- Policy propagation (a data governance rule defined at org level applies to all
  clusters)

---

## The three hard problems at 1M scale

### 1. Storage: separate hot and cold paths

Storing 1 billion raw events in Postgres is not feasible. The design separates:

**Hot path** — Postgres stores only metadata:

```
run_id | workload | status | model | created_at | asset_refs | ...
```

Small rows, fast queries, manageable volume.

**Cold path** — object storage (any S3-compatible system) stores raw capsules:

```
s3://novafabric-capsules/
    01KR9Q2AD…/
        events.jsonl      ← raw event stream, immutable
        capsule.yaml
        redaction-proof.json
```

Raw events are write-once, content-addressed, and cheap to store at scale.

**Content deduplication** — a capsule stores a content hash, not the content.
If 1,000 runs all use the same system prompt, it is stored once. The 1,000
capsules each store only a hash pointer.

### 2. Lineage graph at billions of edges

A `SELECT WHERE target_id = X` query over a billion-row Postgres table is
unusable. At scale, the lineage graph needs:

- **Proper graph traversal indexes** — blast-radius and provenance queries
  follow edges, not scan rows
- **Pre-computed materialized subgraphs** for common patterns (frequently-queried
  assets, recent runs)
- **Sharding by namespace** — HPC facility A's lineage doesn't need to be in the
  same shard as facility B's

**Corrected 2026-07-30 — this is no longer undecided.** All four tiered
backends are implemented and testcontainers-verified: SQLite (<1M edges,
local default), Postgres recursive-CTE (v0.68.0), embedded KuzuDB (v0.17.0,
benchmark-cleared at 45.5ms p99 blast-radius @10M edges), Apache AGE openCypher
(v0.69.0), and JanusGraph Gremlin for billion-edge scale (v0.70.0, needs the
GraphSON serializer and `.emit()` on traversals — see `lineage/backends/`).
Sharding by namespace and pre-computed materialized subgraphs remain future
work; picking *which* backend to run at a given scale is now a config choice,
not an open research question.

### 3. Agent identity at scale

Currently: one `nova serve` instance issues tokens. One server = one failure point.

At 1M agents, each agent needs a **short-lived, scoped credential**:

- Valid only for this job's duration
- Scoped to this agent's namespace (can't write to another agent's capsule)
- Verifiable by any collector without calling the issuing server (offline
  verification)

This maps to a standard JWT / OIDC design with short TTLs and an offline
verification key distributed to collectors. The issuing server can be
replicated. The verifying collectors need only the public key, not network
access to the issuer.

---

## What exists today vs what's needed

| Capability | Status |
|---|---|
| Local capture with zero network overhead | ✓ shipped (v0.1) |
| Portable capsule format (self-contained directory) | ✓ shipped (v0.1) |
| Single-cluster server with Postgres | ✓ shipped (v0.7) |
| `runs_cache` index — O(1) `/api/runs` at 100K capsules (Scale-S1) | ✓ shipped (v0.32.0) |
| Background capsule watcher + `nova ingest-capsule` (Scale-S3) | ✓ shipped (v0.36.0) |
| Postgres partition DDL for 10K-tenant MetadataStore (Scale-S2) | ✓ shipped (v0.32.0) |
| Content-addressed capsule storage (identical content stored once, `cas.py`) | ✓ shipped experimental (Phase 4, v0.14.5) — SHA-256 CAS key `capsules/<tenant>/<sha[0:4]>/<sha>/data.zst`; event-level dedup by `event_id` at the collector forwarder |
| Hot/cold storage split (Postgres metadata + object storage events) | ✓ shipped experimental (Phase 4, `object_capsule_store/`, v0.14.5) |
| Rack/node-level collector daemon | ✓ shipped experimental (Phase 2, v0.14.3 — Go `collector/`, 295K events/sec p99 4.7ms; plus a lighter Python `collector_app/` FastAPI receiver, v0.29.0) |
| Graph traversal indexes for lineage at scale | ✓ shipped experimental — all four at-scale backends (KuzuDB v0.17.0/Phase 6, Postgres recursive-CTE v0.68.0, Apache AGE v0.69.0, JanusGraph v0.70.0), all testcontainers-verified; zero remaining stub backends |
| Short-lived agent credentials (offline-verifiable, scoped per-agent-namespace) | — needs design + build. (A related but narrower piece exists: `nova server` issues Ed25519 offline service-account tokens for CI, not per-agent scoped compute-node identity at 1M scale.) |
| Multi-cluster federation | — not yet designed (Phase 6; depends on 2–5 being stable in production, per `design/architecture/cluster-scale.md`) |
| Cross-org lineage and identity | — not yet designed |

---

## Using `nova ingest-capsule` (Scale-S3, shipped v0.36.0)

`nova ingest-capsule` populates the `runs_cache` SQLite index from capsule files
on disk. It does **not** require `nova serve` to be running — useful for batch
jobs, cron tasks, or CI pipelines that capture capsules and want to index them
without starting the full server.

### Quick start

```bash
# Index a single capsule by run ID
nova ingest-capsule abc123

# Re-index all capsules in the default capsule directory
nova ingest-capsule --all

# Foreground watcher loop — prints each new capsule as it appears
nova ingest-capsule --watch

# Stop with Ctrl+C
```

### Faster indexing with inotify / FSEvents

The default backend (`PollingBackend`) scans the capsule directory every 2 s.
For environments where `watchdog` is available, the `WatchdogBackend` reacts to
OS-level filesystem events instead of polling:

```bash
# Install the optional extras
pip install 'novafabric[watch]'

# Use the event-driven backend
nova ingest-capsule --watch --backend watchdog

# Or set it globally
export NOVA_WATCHER_BACKEND=watchdog
nova ingest-capsule --watch
```

### Override capsule directory and DB path

```bash
nova ingest-capsule --all \
  --capsule-dir /shared/nfs/capsules \
  --db-path /var/lib/novafabric/registry.db
```

### In `nova serve`

When you run `nova serve`, it automatically creates a `CapsuleWatcher` internally
and delegates all startup indexing and incremental polls to it. The backend is
selected by `NOVA_WATCHER_BACKEND` (default: `auto`, which uses `WatchdogBackend`
if `novafabric[watch]` is installed, else `PollingBackend`).

You do not need to run `nova ingest-capsule --watch` alongside `nova serve` —
the server already does this in its background stats loop every 2 s.

### Environment variables

| Variable | Default | Effect |
|---|---|---|
| `NOVA_WATCHER_BACKEND` | `auto` | `auto` \| `polling` \| `watchdog` |
| `NOVA_WATCHER_INTERVAL` | `2.0` | Poll interval in seconds |

---

## Why we built the single-cluster case first

The collector hierarchy, storage split, and lineage-at-scale tiers described
above are now built (experimental) — but they were built in this order
deliberately, and the reasoning still matters for what's genuinely left
(federation): you need to know what the data actually looks like under load
before designing a cross-cluster protocol on top of it.

- How large is a typical capsule?
- How many events does a typical agent run produce?
- What does the lineage graph topology look like in practice — deep chains or
  wide fans?
- What are the hot query patterns — blast-radius, provenance, or replay-chain?

The `nova-testbench` continuous loop exists to answer these questions with real
data, not assumptions. 1,000 capsules from the testbench tells you more about
storage requirements and graph shape than any design document can.

**The sequence:**
1. Make one cluster solid and measure it — **done**
2. Build the collector, storage-split, and lineage-at-scale layers on measured
   data — **done** (all shipped experimental; see the status table above)
3. Design federation based on measured multi-cluster patterns — **still open**
   (Phase 6; genuinely not yet designed)

Building a federation protocol before you know the single-cluster behavior was
speculation. That's why it was measured first and built second — federation is
the one piece still waiting on real multi-cluster patterns.

---

## References

- `design/architecture/cluster-scale.md` — formal design principles
- `design/adr/0020` — low-overhead capture on compute nodes
- `design/adr/0021` — AI-factory design intent
- `design/adr/0022` — polyglot persistence and object storage
- `design/adr/0023` — cache architecture
- `design/adr/0032` — parent/child capsule hierarchy (prerequisite for multi-node
  agent linking)
- `ROADMAP.md` — release sequencing
