# Capacity Planning & Sizing Guide

How to size storage, memory, and CPU for a NovaFabric deployment — from a
single laptop to a shared Postgres server. Everything below is labelled per
the [docs honesty rule](../../CLAUDE.md): **works today**, **experimental**,
**planned**, or **future design**. All sizing figures that are not cited to a
benchmark file are **estimates derived from the on-disk formats** — measure
your own workload before committing hardware.

> **The one-line answer:** NovaFabric is local-first. A laptop with SQLite
> handles thousands of runs comfortably; a single Postgres-backed
> `nova server` writer is the supported team topology through v1.0
> ([ADR-0180](../../design/adr/0180-ha-and-zero-downtime-upgrade-posture.md));
> beyond that, the cluster tiers (collector, object capsule store, KuzuDB
> lineage) take over — see
> [Cluster-scale migration](cluster-scale-migration.md).

---

## 1. What a deployment stores

| Store | Location (defaults) | Grows with | Rebuildable? |
|---|---|---|---|
| **Run Capsules** (source of truth) | `~/.novafabric/capsules/<ULID>/` (CLI) or `./.novafabric/runs/` (SDK/proxies) | every captured run, dominated by LLM payloads | No — this *is* the evidence |
| **Registry** (SQLite) | `~/.novafabric/registry.db` | assets, promotions, `runs_cache` rows | `runs_cache` yes; asset/promotion history no |
| **`runs_cache` index** | table inside `registry.db` | one row per run | Yes — always rebuildable from the capsule filesystem (`src/novafabric/registry/runs_cache.py`) |
| **Lineage graph** (SQLite) | `registry.db` (`lineage_nodes`/`lineage_edges`) | edges per run (typically a handful) | Yes — derived from each capsule's `lineage.jsonl` |
| **NovaSeal Merkle log** | `~/.novafabric/novaseal-merkle.db` | one entry per sealed capsule | No — append-only evidence |
| **Server metadata DB** | Postgres (server mode) or SQLite | runs, capsules, signatures, RBAC, audit | Partially — see `nova rebuild-metadata-db` |
| **WORM object store** | S3/Azure/GCS/Ceph bucket (experimental, cluster tier) | capsule objects + manifest chain | Metadata is rebuildable *from* it, not vice versa |

The design invariant: **the capsule is the source of truth; the registry,
metadata DB, and lineage graph are derived, rebuildable indexes**
([Concepts](../concepts.md)).

---

## 2. What drives capsule size

**Status: works today** (format is `experimental` — not frozen until v1.0).

A capsule is a ULID-named directory ([Concepts](../concepts.md)):

```
capsule.yaml          ← run manifest (~24 required fields — small, low KB)
trace.jsonl           ← execution spans (OTel)
model-calls.jsonl     ← LLM API calls — usually the dominant cost
tool-calls.jsonl      ← tool invocations
assets.jsonl          ← asset references
env.lock              ← environment snapshot
redaction-proof.json  ← secret scan proof
replay.yaml           ← replay constraints
lineage.jsonl         ← lineage edges
inputs/  outputs/     ← stdout/stderr and artifacts
```

Size drivers, in order:

1. **`model-calls.jsonl`** — one JSONL line per intercepted LLM call, holding
   the recorded prompt/response payloads (OTel GenAI semconv). A chatty agent
   with large contexts easily produces hundreds of KB to MBs per run; a
   metadata-light run produces almost nothing.
2. **`outputs/`** — whatever your workload writes to stdout/stderr plus
   captured artifacts.
3. **`trace.jsonl` / `tool-calls.jsonl`** — proportional to call counts;
   usually small relative to model payloads.
4. **The manifest and the rest** — `capsule.yaml`
   (`schemas/run-capsule.schema.json` — 24 required fields, all bounded
   metadata), `env.lock`, `redaction-proof.json`: low tens of KB combined.
5. **NovaSeal artifacts** — a `.seal/` directory per sealed capsule (DSSE
   envelope, timestamp token, log entry): small (KBs), plus one Merkle log
   row per seal.

The cluster-tier `CapsuleWriter` (`src/novafabric/capsule/writer.py`) writes
an even leaner parent/child capsule — `capsule.json` + `lineage.jsonl` — so
worker capsules on HPC spools are metadata-sized; payloads flow through the
collector tier instead.

### Index cost per run

- **`runs_cache`**: one row (12 columns: status, timestamps, call counts,
  command JSON, paths) plus three indexes — order of a few hundred bytes per
  run (*estimate from the DDL in
  `src/novafabric/registry/runs_cache.py`*).
- **Lineage**: a handful of nodes/edges per run at a few hundred bytes each
  (*estimate*). The SQLite lineage backend is documented as well suited
  **below roughly one million edges** ([Concepts](../concepts.md)); beyond
  that, the KuzuDB v2 tier applies (§5).

---

## 3. Rough sizing table — 1K / 100K / 1M runs

**Status: estimates.** These are *illustrative* numbers derived from the
formats above, **not measured benchmarks**. The per-capsule scenarios:
**light** ≈ 50 KB (few/no LLM payloads), **typical** ≈ 500 KB (agent run with
moderate prompts), **heavy** ≈ 5 MB (long contexts, large outputs). Measure a
week of your own capsules (`du -sh ~/.novafabric/capsules`) and scale from
that instead wherever possible.

| Runs | Capsule store (light / typical / heavy) | `runs_cache` (~0.5 KB/run) | Lineage SQLite (~1 KB/run) | Notes |
|---|---|---|---|---|
| 1 000 | 50 MB / 500 MB / 5 GB | < 1 MB | ~1 MB | Laptop-class. SQLite everywhere. |
| 100 000 | 5 GB / 50 GB / 500 GB | ~50 MB | ~100 MB | Still fine for SQLite indexes; capsule *payload* disk is the real budget. Consider Postgres server mode for multi-user access. |
| 1 000 000 | 50 GB / 500 GB / 5 TB | ~500 MB | ~1 GB (≈ millions of edges — **past** the documented SQLite lineage comfort zone) | One directory per run × ~12 files ⇒ ~12 M inodes: plan filesystem limits, or move capsule objects to the WORM object store tier. KuzuDB lineage tier recommended. |

Two non-obvious limits at the top end:

- **Inodes, not just bytes.** Each capsule is a directory of ~10–12 files.
  At 1M runs that is on the order of 10⁷ filesystem entries under one tree.
- **Lineage edges.** SQLite lineage is documented for < ~1M edges; the
  KuzuDB migration path exists for beyond (§5).

---

## 4. SQLite defaults vs Postgres server mode

**Status: SQLite local mode works today; Postgres server mode experimental.**

| Concern | Local (SQLite) | Server mode (Postgres) |
|---|---|---|
| Default | Yes — zero setup, `~/.novafabric/registry.db` | Opt-in (`pip install 'novafabric[server]'`, [Server Deployment Guide](server-deployment.md)) |
| Concurrency | Single machine, single writer per DB file | Multi-user over `/v0` REST; RLS tenant isolation |
| Writers | One | **Still one** — see §7 (ADR-0180) |
| Migration | — | `nova migrate-to-postgres` (idempotent; the SQLite file is never modified) |
| Query latency target | — | `query_runs` p99 ≤ 200 ms was the Phase-5 acceptance criterion (`docs/releases/v0.24.0.md`) — a target the suite gates on, not a promise for your hardware |

Postgres capacity itself (connections, WAL, vacuum) is standard Postgres
operations — NovaFabric adds no special requirements
([Backup & Restore Runbook](backup-restore.md) §1.2).

---

## 5. Measured performance numbers that exist

These are the benchmark results actually recorded in this repo — cite these,
not folklore. They were measured on the hardware named in their source files;
treat them as evidence the design meets its gates, not as an SLA:

| Component | Measured result | Source |
|---|---|---|
| Collector NovaSeal batch signer | **295 K events/sec, p99 4.7 ms** (gate: ≥100 K events/sec, p99 < 200 ms) | `docs/releases/v0.14.3.md` |
| `NovaSeal.seal()` per-capsule latency | CI gate **p99 < 200 ms** over 100 rounds; ≈ 16 ms on a modern laptop | `docs/releases/v0.12.16.md`, `docs/release-process.md` §1a |
| `nova seal log verify` (sampled) | **p99 < 200 ms at 1 M log entries** | `docs/releases/v0.38.0.md`, `docs/cli-reference.md` |
| KuzuDB lineage tier (BQ-015 gate) | **blast_radius p99 = 45.5 ms @ 10 M edges** (gate < 500 ms, cleared 2026-05-16) | `docs/lineage/migration-guide.md` |

When your `nova-bench run` shows p99 depth-5 lineage traversal > 500 ms on
SQLite, that is the documented trigger to migrate to the KuzuDB tier
([Lineage migration guide](../lineage/migration-guide.md)).

---

## 6. Memory and CPU expectations

**Status: estimates — no published memory/CPU benchmarks exist for either
server process.** What *is* contractual:

- **`nova serve` (local dashboard, experimental).** A single FastAPI/uvicorn
  process. The file-serving endpoint has a **bounded-memory contract**: it
  never reads more than `NOVA_SERVE_MAX_FILE_BYTES` per request (**default
  5 MB** — 5 000 000 bytes; `src/novafabric/serve/app.py`). Startup cost
  scales with the capsule directory: the runs index is (re)built from
  capsule files on startup and kept current by a background stats-refresh
  thread. Expect a few hundred MB RSS and one busy core during index build
  on large capsule dirs (*estimate — measure yours*).
- **`nova server` (multi-user API, experimental).** A single-writer
  FastAPI/uvicorn process in front of Postgres; the DB does the heavy
  lifting. Rate limiting and quotas
  ([ADR-0179](../../design/adr/0179-api-rate-limiting-quotas.md),
  default **off**) are in-process token buckets with a bounded key map, so
  enabling them adds negligible memory. Quota enforcement derives usage from
  capsule-store counts with a TTL cache, adding a bounded query cost to
  ingest.
- **Observability of your actual usage** (experimental,
  [ADR-0182](../../design/adr/0182-self-observability-surface.md)): scrape
  `/metrics` (Prometheus) on either app for HTTP request rates/durations, DB
  pool gauges, and ingest counters — size from observed load, not from this
  page.

---

## 7. Where the single-writer topology limits scale

**Status: contract — accepted
[ADR-0180](../../design/adr/0180-ha-and-zero-downtime-upgrade-posture.md).**

- **One active `nova server` writer per deployment is the supported topology
  through v1.0.** Multi-writer, clustering, leader election, automatic
  failover, and cross-region active-active are explicitly **not promised**.
- Practical consequence for sizing: **ingest throughput scales vertically
  only** (bigger box, faster Postgres). If one tenant can saturate the
  writer, enable rate limiting/quotas (ADR-0179) rather than adding writers.
- A passive standby pointed at the same Postgres + object store may be
  pre-provisioned; promotion is operator-driven with a strict
  never-two-writers fencing invariant (ADR-0180 D2). RPO is bounded by
  Postgres replication lag plus WORM object-store durability.
- The Helm chart pins `replicaCount: 1` deliberately — do not scale it out.

For growth beyond one writer + one Postgres, the design intent (collector
tier, object capsule store, federation) is documented in
`design/architecture/cluster-scale.md` and the honest shipped-vs-designed
split in [Cluster scale — 1,000,000 agents](../tutorials/cluster-scale.md).
The WORM object store and collector tiers exist in-tree as **experimental**;
federation remains **future design**.

---

## 8. Sizing checklist

1. Capture a representative week; measure `du -sh` of the capsule dir and
   divide by run count — that is *your* per-capsule figure.
2. Budget capsule-store disk = per-capsule × expected runs × retention
   window; add ~1 KB/run (*estimate*) for indexes and lineage.
3. Below ~1 M lineage edges and single-machine access: stay on SQLite.
4. Multi-user or > ~100 K runs: Postgres server mode; size Postgres with
   standard tooling; keep exactly one writer (ADR-0180).
5. Millions of runs / inode pressure: move capsule objects to a WORM object
   store (experimental) and lineage to KuzuDB
   ([migration guide](../lineage/migration-guide.md)).
6. Set retention ([ADR-0134](../../design/adr/0134-data-retention-policy-scheduler.md))
   and quotas (ADR-0179) so growth is a policy, not an accident.
