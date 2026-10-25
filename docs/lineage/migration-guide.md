# LineageStore Migration Guide

> **Honesty labels:** items marked **works today** are implemented and tested.
> Items marked **experimental** are implemented but the surface may change.
> Items marked **planned** are on the roadmap but not yet implemented.

---

## Overview

The migration kit (`nova lineage-store migrate`) rebuilds a lineage graph in a target
**SQLite** lineage store. One source works from the CLI today:

- a positional Parquet file — a **deprecated** legacy path, kept for backward
  compatibility (**works today**).
- `--from-ocs` — the ADR-0022 path that would derive edges from the ObjectCapsuleStore
  manifest chain — is **planned**, not implemented in the CLI: it refuses with exit 1 and
  writes nothing (see [`--from-ocs` is not implemented](#--from-ocs-is-not-implemented)).

To rebuild lineage from local capsule directories, use `nova lineage import
<capsule-dir>` (**works today**): it indexes each capsule's `lineage.jsonl`.

**Corrected 2026-07-30.** The `nova lineage-store migrate` **CLI command's target flag
is SQLite only** today — that part of this guide was always accurate. What was *not*
accurate: this guide previously described KuzuDB, Postgres, and Apache AGE as
**planned, unimplemented** graph backends. As of v0.70.0 all four at-scale lineage
backends (KuzuDB, Postgres recursive-CTE, Apache AGE openCypher, and JanusGraph
Gremlin) are **implemented and testcontainers-verified** — there are zero
`NotImplementedError` stubs left in `lineage/backends/`. The underlying migration
function, `migrate_from_ocs()` (`lineage/migration/kit.py`), already takes any
`AbstractLineageStore` as its `dest_store`, so it works against those backends when
called from Python; the **CLI wrapper** (`cli/lineage_migrate.py`) simply hardcodes
`SqliteLineageStore` as the target and has no `--target-backend` flag yet — that CLI
gap, not backend implementation, is what's genuinely missing. See the tiered-backend
table in [the architecture overview](../architecture.md#lineage-at-scale--tiered-backends)
for full per-backend status.

**When to migrate:**

- Load a legacy Parquet edge export into a queryable SQLite store.
- To rebuild or repair lineage from capsules (the lineage graph is a derived,
  rebuildable index), use `nova lineage import` on the capsule directories instead.

For deployments whose graph grows beyond ~1M edges, the embedded KuzuDB tier
(**experimental**, benchmark-cleared: blast_radius p99 45.5ms @10M edges) and the
Postgres/AGE/JanusGraph tiers (**experimental**, all testcontainers-verified) are
described in the architecture doc; the `nova lineage-store migrate` **CLI** does not
yet expose a flag to migrate into them (the backends themselves are not the gap —
see above).

---

## Prerequisites

1. **A source of lineage edges** — a Parquet file of edges from the legacy export
   tooling (**deprecated**). (`--from-ocs` is **planned**; see below.)
2. **A target SQLite database path** (`--db`, default `lineage.db`). The command creates
   the store if it does not exist.
3. **Disk space** for the SQLite store (roughly proportional to the edge count).

---

## Step 1: Dry run

```bash
nova lineage-store migrate edges.parquet --db lineage.db
```

The dry run (the default, i.e. without `--commit`) reads the Parquet edges, reports the
`loaded` count and does **not** write to the target SQLite store.

## Step 2: Commit the migration

```bash
nova lineage-store migrate edges.parquet --db lineage.db --commit
```

Adding `--commit`:

- Writes the edges into the SQLite store at `--db`.
- Runs a post-load divergence check (unless `--no-validate` is passed) and exits 2 if a
  `MigrationDivergenceError` is detected.

**works today** (deprecated path) — the target is the `SqliteLineageStore`; the Parquet
argument routes through the deprecated `migrate()` (`lineage/migration/kit.py`, emits a
`DeprecationWarning`). A missing or absent Parquet file exits 1 before the target store is
created.

### `--from-ocs` is not implemented

**planned.** Before this fix, `nova lineage-store migrate --from-ocs` built an empty
in-memory ObjectCapsuleStore, never read `--ocs-data-dir`, and printed
`ok loaded=0 … committed=yes` with exit 0 — a migration that migrated nothing reported as
a success. NovaFabric has no on-disk ObjectCapsuleStore backend this command could read
(the router's `local` backend is in-memory and test-only), so `--from-ocs` now **refuses
with exit 1**, says that `--ocs-data-dir` was not read, and creates no target store. The
Python function `novafabric.lineage.migration.kit.migrate_from_ocs()` still works against an
`ObjectCapsuleStore` you construct yourself over a real WORM adapter. `--ocs-tenant`,
`--ocs-run-ids` and `--ocs-data-dir` are kept so existing scripts fail loudly rather than
with "no such option".

---

## Rollback

Without `--commit`, no changes are persisted. If you have already committed and need to
roll back, delete or restore the `--db` SQLite file — the migration only writes to that
target. The source `lineage.jsonl` inside each capsule (and the OCS capsules themselves)
are **never modified** by the migration kit; they remain the authoritative source.

---

## Deployment profiles for planned backends

`nova lineage-store profile` prints a ready-to-use docker-compose deployment profile for a
graph backend. It emits configuration only — it does **not** migrate data.

```bash
# KuzuDB vertical single-node (default)
nova lineage-store profile --target kuzudb-vertical --node-size 16g-ram-500g-nvme

# JanusGraph + Cassandra minimal cluster
nova lineage-store profile --target janusgraph-minimal --rf 3
```

**works today** — profile generation (stdout only). **The generated stack is an experimental
template, not a working deployment:** its `nova-lineage` service is configured with
`NOVA_LINEAGE_BACKEND` / `NOVA_KUZU_DB_PATH` / `NOVA_JANUSGRAPH_URL`, which this release does
not read (it does not select the backend from them; `nova lineage consume` reads
`NOVA_KUZU_PATH` and needs NATS), and the JanusGraph profile is one Cassandra node with the
replication factor set, not a 3-node cluster. Both the KuzuDB and JanusGraph
backends these profiles target are **experimental, testcontainers-verified**
implementations (not stubs) — see "Corrected 2026-07-30" above — but neither is yet a
`nova lineage-store migrate` CLI destination (that's a CLI-surface gap, not a backend gap).

---

## Upgrading an existing KuzuDB lineage store

**works today.** Releases before 0.102 stored KuzuDB lineage in a single `Run` node
table. That format had no place for a non-run node, so an *asset* endpoint was written
as a `Run` keyed on the empty string and read back with an empty ref. From 0.102 the
backend uses one generic `Node` table keyed by `node_id_for(kind, ref)`, matching the
SQLite, Postgres and AGE backends (ADR-0266).

**No action is required.** Opening an existing database migrates it in place: run nodes
and run-to-run edges are copied into the new tables and the old ones are dropped. The
migration is idempotent and runs once.

⚠ **One class of edge cannot be migrated.** An edge whose non-run endpoint was stored as
an empty-key `Run` is not recoverable, because the old schema never persisted that
endpoint's ref. Those edges are counted and reported through a warning on the
`novafabric.lineage.backends.kuzu` logger:

```
migrated legacy kuzu lineage schema (ADR 0266): N run nodes and M edges copied;
K edges could not be migrated because the legacy schema never persisted their
non-run endpoint (re-ingest those capsules to restore them)
```

If `K > 0`, re-ingest the affected capsules to restore those edges.

⚠ **`node_id` changed.** It was the raw `run_id` and is now the 26-character
`node_id_for(kind, ref)` hash used by every other backend. Anything that persisted a
KuzuDB `node_id` outside the store must be recomputed. `ref` is unchanged.

---

## Known limitations

| Limitation | Status |
|------------|--------|
| `migrate` CLI target is SQLite only (the CLI hardcodes `SqliteLineageStore`; no `--target-backend` flag) | **works today, as a CLI-surface gap** — the KuzuDB / Postgres / AGE / JanusGraph *backends* are implemented (see below), just not yet wired as `migrate` CLI destinations |
| KuzuDB 10M-edge benchmark (ADR-0053 v2a gate, depth-5 p99 < 500 ms) | **cleared** — 45.5ms p99 blast-radius @10M edges, measured in the external `nova-lineage-bench` |
| KuzuDB replay chains are bounded at 30 hops; the SQLite reference bounds at 100 | **works today, as a documented backend limit** (ADR-0266) — an engine ceiling, not a tuning choice: Kuzu's binder rejects a variable-length upper bound above 30 outright. A replay chain deeper than 30 hops is truncated on KuzuDB and not on SQLite |
| Apache AGE backend | **experimental, testcontainers-verified** (v0.69.0) — `lineage/backends/age.py` is a real openCypher implementation, not a stub |
| Postgres backend | **experimental, testcontainers-verified** (v0.68.0) — `lineage/backends/postgres.py` uses recursive CTEs, no AGE extension needed |
| JanusGraph backend | **experimental, testcontainers-verified** (v0.70.0) — requires Docker/Cassandra + Gremlin (GraphSON serializer, not default GraphBinary); not suitable for local-only deployments |
| `--from-ocs` is not implemented in the CLI: it refuses with exit 1 and writes nothing; `migrate_from_ocs()` works from Python over a store you construct | **planned** (CLI) |
| Federation (`/federation/query`) requires OQ-04 legal sign-off for regulated-industry deployments | Open — do not use in regulated multi-site deployments until resolved |
