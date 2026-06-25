# LineageStore Migration Guide

> **Honesty labels:** items marked **experimental** are implemented and tested.
> Items marked **planned** are on the roadmap but not yet implemented.

---

## Overview

The migration kit (`nova lineage-store migrate`) reads `lineage.jsonl` files from capsule
directories and bulk-loads them into a target backend. It is designed for operators who
need to move from the default SQLite store to KuzuDB or Postgres as their lineage graph
grows beyond ~1M edges.

**When to migrate:**

- Your `nova-bench run` output shows p99 depth-5 traversal latency > 500 ms on your
  current backend at 10M edges.
- You are deploying NovaFabric in server mode (`nova serve`) and want a graph backend
  that co-locates with your Postgres instance (Apache AGE — **planned**).
- You need in-process embedded graph performance without a Postgres server (KuzuDB —
  **works today** — v2a gate cleared 2026-05-16; p99 blast_radius=45.5ms @ 10M edges).

---

## Prerequisites

1. **Target backend installed:**
   - KuzuDB: `uv pip install kuzu` — **works today**
   - Apache AGE: Postgres extension; see AGE install docs — **planned**
   - JanusGraph: Docker image `janusgraph/janusgraph:latest` — **future design**
2. **Parquet export of current lineage edges** — produced by `nova lineage-store export`.
   The Parquet file is the migration contract; the `lineage.jsonl` files in each capsule
   are the authoritative source, but Parquet is faster to bulk-load.
3. **Disk space:** KuzuDB requires approximately 2× the size of your `lineage.jsonl`
   total for its on-disk column store during bulk load.

---

## Step 1: Export edges to Parquet

```bash
nova lineage-store export --output edges.parquet
```

This reads all `lineage.jsonl` files reachable from `~/.novafabric/capsules/` and writes
a single Parquet file with columns: `edge_id`, `edge_type`, `source_id`, `target_id`,
`created_at`, `payload`.

**experimental** — `nova lineage-store export` is implemented.

---

## Step 2: Dry run (validate only)

```bash
nova lineage-store migrate \
  --source-parquet edges.parquet \
  --target kuzudb \
  --validate
```

The dry run:
- Validates every row against the `lineage-edge.schema.json` schema.
- Checks that all `source_id` / `target_id` references resolve to known nodes.
- Prints a validation report with row counts and any schema violations.
- Does **not** write to the target backend.

Exit code 0 = all rows valid. Exit code 1 = validation errors (details in stderr).

**experimental** — `--validate` without `--commit` is a no-op on the target backend.

---

## Step 3: Commit the migration

```bash
nova lineage-store migrate \
  --source-parquet edges.parquet \
  --target kuzudb \
  --validate \
  --commit
```

Adding `--commit`:
- Runs the same validation as Step 2.
- If validation passes, bulk-loads all edges into the KuzuDB store at
  `~/.novafabric/lineage.kuzu/`.
- Prints a summary: rows loaded, time elapsed, p99 insert latency.
- Updates `~/.novafabric/config.yaml` to set `lineage_backend: kuzudb`.

**works today** — KuzuDB bulk load is implemented via `KuzuLineageStore.bulk_load()`.

---

## Rollback

Without `--commit`, no changes are persisted. If you have already committed and need to
roll back:

1. Delete or rename `~/.novafabric/lineage.kuzu/`.
2. Restore `lineage_backend: sqlite` in `~/.novafabric/config.yaml`.
3. The SQLite store (`~/.novafabric/registry.db`) is never modified by the migration.

The `lineage.jsonl` files inside capsule directories are **never modified** by the
migration kit — they remain the authoritative source.

---

## Checking gate conditions

Before promoting KuzuDB to production, run the benchmark harness at 10M edges:

```bash
nova-bench run --backend kuzudb --edge-count 10_000_000 --depth 5
```

The output includes:

```
backend: kuzudb
edge_count: 10000000
query: blast_radius depth=5
p50_ms: ...
p95_ms: ...
p99_ms: ...   ← must be < 500 ms to clear the ADR-0053 gate
```

If `p99_ms >= 500`, do not promote. File an issue with the benchmark output attached.

**works today** — `nova-bench run` is implemented (Phase 6 Track B). BQ-015 gate cleared 2026-05-16: KuzuDB p99=45.5ms @ 10M edges.

---

## Known limitations

| Limitation | Status |
|------------|--------|
| KuzuDB 10M-edge benchmark (BQ-015) | **Gate cleared 2026-05-16** — blast_radius p99=45.5ms @ 10M edges (10.98× margin); see ADR-0053 |
| Apache AGE backend not yet implemented | **planned** (ADR-0053 v2b) |
| JanusGraph backend requires Docker; not suitable for local-only deployments | **future design** |
| `nova lineage-store export` reads capsule directories on the local host only; remote capsules must be fetched first | **experimental** (local only) |
| Federation (`/federation/query`) requires OQ-04 legal sign-off for regulated-industry deployments | Open — do not use in regulated multi-site deployments until resolved |
