# LineageStore Migration Guide

> **Honesty labels:** items marked **works today** are implemented and tested.
> Items marked **experimental** are implemented but the surface may change.
> Items marked **planned** are on the roadmap but not yet implemented.

---

## Overview

The migration kit (`nova lineage-store migrate`) rebuilds a lineage graph in a target
**SQLite** lineage store. Two sources are supported:

- `--from-ocs` — the **canonical** path (ADR-0022): derives edges from the
  ObjectCapsuleStore manifest chain, reading each capsule's `lineage.jsonl`.
- a positional Parquet file — a **deprecated** legacy path, kept for backward
  compatibility.

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

- Rebuild or repair a local SQLite lineage store from the durable OCS capsules (the
  lineage graph is a derived, rebuildable index).
- Consolidate lineage from a set of runs into a single queryable SQLite store.

For deployments whose graph grows beyond ~1M edges, the embedded KuzuDB tier
(**experimental**, benchmark-cleared: blast_radius p99 45.5ms @10M edges) and the
Postgres/AGE/JanusGraph tiers (**experimental**, all testcontainers-verified) are
described in the architecture doc; the `nova lineage-store migrate` **CLI** does not
yet expose a flag to migrate into them (the backends themselves are not the gap —
see above).

---

## Prerequisites

1. **A source of lineage edges** — one of:
   - an ObjectCapsuleStore reachable locally, plus the tenant and the run IDs to migrate
     (`--from-ocs`); or
   - a Parquet file of edges from the legacy export tooling (**deprecated**).
2. **A target SQLite database path** (`--db`, default `lineage.db`). The command creates
   the store if it does not exist.
3. **Disk space** for the SQLite store (roughly proportional to the edge count).

---

## Step 1: Dry run (canonical OCS source)

```bash
nova lineage-store migrate --from-ocs \
  --ocs-tenant org \
  --ocs-run-ids run-001,run-002 \
  --db lineage.db
```

The dry run (the default, i.e. without `--commit`):

- Enumerates the named capsules in the OCS and reconstructs their `lineage.jsonl` edges.
- Runs a post-load divergence check (unless `--no-validate` is passed).
- Reports `loaded`, `diverged`, and `capsules_scanned` counts.
- Does **not** write to the target SQLite store.

**works today** — `--from-ocs` requires `--ocs-tenant` and `--ocs-run-ids`; there is no
global run enumeration (by design, FR-05).

---

## Step 2: Commit the migration

```bash
nova lineage-store migrate --from-ocs \
  --ocs-tenant org \
  --ocs-run-ids run-001,run-002 \
  --db lineage.db \
  --commit
```

Adding `--commit`:

- Runs the same reconstruction and divergence check as Step 1.
- Writes the reconstructed edges into the SQLite store at `--db`.
- Exits non-zero if a `MigrationDivergenceError` is detected (skip with `--no-validate`).

**works today** — the target is the `SqliteLineageStore`; edge IDs are content-addressed,
so re-running the migration is idempotent.

### Legacy Parquet source (deprecated)

```bash
nova lineage-store migrate edges.parquet --db lineage.db --commit
```

The positional Parquet argument routes through the deprecated `migrate()` path
(`lineage/migration/kit.py`, emits a `DeprecationWarning`). Prefer `--from-ocs`.

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

**works today** — profile generation (stdout only). Both the KuzuDB and JanusGraph
backends these profiles target are **experimental, testcontainers-verified**
implementations (not stubs) — see "Corrected 2026-07-30" above — but neither is yet a
`nova lineage-store migrate` CLI destination (that's a CLI-surface gap, not a backend gap).

---

## Known limitations

| Limitation | Status |
|------------|--------|
| `migrate` CLI target is SQLite only (the CLI hardcodes `SqliteLineageStore`; no `--target-backend` flag) | **works today, as a CLI-surface gap** — the KuzuDB / Postgres / AGE / JanusGraph *backends* are implemented (see below), just not yet wired as `migrate` CLI destinations |
| KuzuDB 10M-edge benchmark (ADR-0053 v2a gate, depth-5 p99 < 500 ms) | **cleared** — 45.5ms p99 blast-radius @10M edges, measured in the external `nova-lineage-bench` |
| Apache AGE backend | **experimental, testcontainers-verified** (v0.69.0) — `lineage/backends/age.py` is a real openCypher implementation, not a stub |
| Postgres backend | **experimental, testcontainers-verified** (v0.68.0) — `lineage/backends/postgres.py` uses recursive CTEs, no AGE extension needed |
| JanusGraph backend | **experimental, testcontainers-verified** (v0.70.0) — requires Docker/Cassandra + Gremlin (GraphSON serializer, not default GraphBinary); not suitable for local-only deployments |
| `--from-ocs` reads a locally reachable ObjectCapsuleStore; remote stores must be fetched first | **works today** (local OCS only) |
| Federation (`/federation/query`) requires OQ-04 legal sign-off for regulated-industry deployments | Open — do not use in regulated multi-site deployments until resolved |
