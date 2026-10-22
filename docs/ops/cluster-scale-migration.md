# Cluster-Scale Migration Guide

> **Status: corrected 2026-07-30 — read the per-phase labels.** Earlier drafts
> of this guide described the KuzuDB/JanusGraph/AGE/Postgres lineage backends
> as "planned / pending-benchmark". That is stale: **all four are implemented
> and testcontainers-verified** (KuzuDB's ADR-0053 10M-edge benchmark cleared
> 2026-05-16 at p99 45.5ms; Postgres, AGE, and JanusGraph each have a parity
> suite proving identical answers to the SQLite reference). What is
> genuinely still missing is a **bulk-migration CLI path** into any of the
> three non-default backends — `nova lineage-store migrate` only ever
> targets a SQLite store today (see Phase 2/4 below for the real gap and the
> real workaround). Phase 0/0.5/1 (backup, schema migration, Postgres
> MetadataStore via `nova migrate-to-postgres`) and `nova storage validate`
> **work today**. Phase 5's NATS consumer now has a real CLI entrypoint
> (`nova lineage consume`, shipped v0.94.0/ADR-0219) that writes live into
> KuzuDB — the remaining hardware-gated piece is the Slurm-prolog/DaemonSet
> deployment plumbing around it, not the consumer itself. The OCS
> bulk-migration step (Phase 3) is still **future design**. Some CLI
> invocations shown below do not exist yet and are labeled inline.
> Hardware-specific steps (NATS-on-Lustre, dedicated Postgres cluster) are marked
> **hardware-gated** and require site-specific provisioning.

This guide walks through migrating a local-mode NovaFabric installation
(SQLite + filesystem) to a cluster-scale deployment (Postgres + Object Capsule Store
+ JanusGraph lineage + PgBouncer). Follow the phases in order — each phase is
independently testable and roll-backable.

---

## Prerequisites

- NovaFabric ≥ v0.25.0 installed
- `nova --help` and `nova server start` verified working
- Postgres 16+ accessible (local Docker or managed service)
- (Optional, Phase 3+) Object storage endpoint (S3-compatible, Azure Blob, or GCS)
- (Optional, Phase 5) JanusGraph ≥ 1.0 (use the Helm chart at `deploy/helm/janusgraph/`)

---

## Phase 0 — Backup current state

Before any migration step, create a full backup of your local data.

```bash
export NOVA_HOME="${NOVAFABRIC_HOME:-$HOME/.novafabric}"

# Snapshot the entire NOVA_HOME directory.
tar -czf nova-backup-$(date +%Y%m%d-%H%M%S).tar.gz "$NOVA_HOME"

# Verify the backup.
tar -tzf nova-backup-*.tar.gz | head -20
```

---

## Phase 0.5 — Migrate capsule schemas to v1.0.0

Before moving capsule data to any external store, ensure all local capsule
directories are on schema v1.0.0.  This is a fast, in-place operation.

```bash
# Preview what would change (no files modified):
nova migrate-schema --capsule-dir "${NOVA_HOME}/capsules" --dry-run

# Apply migration (with backups of originals):
nova migrate-schema --capsule-dir "${NOVA_HOME}/capsules" --backup
```

What this does per capsule:
- Sets `schema_version: "1.0.0"` in `manifest.json` when absent or < 1.
- Renames `event_log.jsonl` → `model-calls.jsonl` (legacy v0 file name).
- Adds `format_version: "1"` to capsule metadata when absent.

Backup files (`*.v0.bak`) can be removed once you have verified the migration:

```bash
find "${NOVA_HOME}/capsules" -name "*.v0.bak" -delete
```

**Rollback:** Copy `*.v0.bak` files back over their originals — no state
changes have been made to any external system at this point.

---

## Phase 1 — Migrate the registry and MetadataStore to Postgres

### 1.1 Start Postgres

```bash
# Docker quick start (dev/test only — use a managed service in production):
docker run -d \
  --name nova-postgres \
  -e POSTGRES_DB=novafabric \
  -e POSTGRES_USER=novafabric_app \
  -e POSTGRES_PASSWORD=changeme \
  -p 5432:5432 \
  postgres:16-alpine
```

### 1.2 Run the migration

Two separate SQLite databases move to Postgres, with two commands. Both are
additive and idempotent — the SQLite files are never deleted.

```bash
# A plain libpq URL: psycopg rejects SQLAlchemy-style schemes such as
# postgresql+asyncpg://, and the command then exits 2.
export NOVAFABRIC_POSTGRES_DSN="postgresql://novafabric_app:changeme@localhost:5432/novafabric"
export NOVAFABRIC_METADATA_DSN="$NOVAFABRIC_POSTGRES_DSN"

# Create both schemas first.
nova db upgrade --track registry --backend postgres
nova db upgrade --backend postgres

# Registry tables (assets, eval_results, lineage_nodes, lineage_edges):
nova migrate-to-postgres --target "$NOVAFABRIC_POSTGRES_DSN" --log migration.jsonl

# MetadataStore tables (runs and run metadata):
nova db migrate-to-postgres --source ~/.novafabric/metadata.db \
  --target "$NOVAFABRIC_METADATA_DSN"
```

`nova migrate-to-postgres` prints a per-table row-count table (`assets`,
`eval_results`, `lineage_nodes`, `lineage_edges`) followed by
`PASS — row counts verified.`; exit `0` success, `1` row-count mismatch, `2`
connection error. `nova db migrate-to-postgres` uses the same exit codes.

### 1.3 Verify

```bash
# Start the server on Postgres (reads NOVAFABRIC_POSTGRES_DSN) and check storage.
nova doctor --check-storage --backend postgres
nova server start --backend postgres
# Note: `nova list` always reads the local registry, not the server's Postgres.
```

### 1.4 Deploy PgBouncer (production)

See `deploy/docker/README-pgbouncer.md` for full setup instructions and SCRAM
hash generation.  Config files: `deploy/docker/pgbouncer.ini` and
`deploy/docker/pgbouncer-userlist.txt` (fill in SCRAM hashes before
deploying).  Key settings: `pool_mode = transaction`,
`default_pool_size = 20`.

After deploying, update `NOVA_DSN` to point at pgBouncer (port 6432):

```bash
export NOVAFABRIC_METADATA_DSN="postgresql+asyncpg://novafabric_app:<pw>@pgbouncer:6432/novafabric"
```

---

## Phase 2 — Migrate lineage to KuzuDB *(backend shipped; bulk-migration CLI still SQLite-only)*

> **Corrected 2026-07-30.** `KuzuLineageStore` (`src/novafabric/lineage/backends/kuzu.py`)
> is implemented and **the ADR-0053 benchmark is cleared**: blast_radius p99 =
> 45.5ms at 10M edges (gate was < 500ms), accepted 2026-05-16. The real
> remaining gap is narrower than "not shipped": there is still no `--to kuzu`
> flag on the bulk-migration CLI (`nova lineage-store migrate` only ever
> constructs a `SqliteLineageStore` target — confirmed in
> `src/novafabric/cli/lineage_migrate.py`), and no `nova lineage store`
> command exists (the real command group is `nova lineage-store`, hyphenated).

The lineage migration command that exists today is `nova lineage-store migrate`, which
loads edges into a **SQLite** lineage store (the only target) from a Parquet export.
Its `--from-ocs` source is **planned**, not implemented: it refuses with exit 1 and
writes nothing (it used to report an empty migration as a success). To rebuild lineage
from capsule directories, use `nova lineage import`:

```bash
# Dry-run (default) then commit; target is a SQLite store via --db.
nova lineage-store migrate edges.parquet --db "$NOVA_HOME/novafabric.db"
nova lineage-store migrate edges.parquet --db "$NOVA_HOME/novafabric.db" --commit
# Re-index lineage from local capsules instead:
nova lineage import "$NOVA_HOME/capsules/"
```

**The actual production path into KuzuDB today is not this bulk-migrate command —
it is the live NATS consumer** (Phase 5): `nova lineage consume` writes edges
directly into a KuzuDB directory via bulk-COPY as they arrive, bypassing this
SQLite-only batch tool entirely. If you need a one-shot bulk load into KuzuDB
today (rather than a live stream), the `KuzuLineageStore` class is usable
directly from Python as the `target` argument to
`novafabric.lineage.migration.kit.migrate()` / `migrate_from_ocs()` — those
functions are backend-agnostic (`target: AbstractLineageStore`); only the CLI
wrapper hardcodes SQLite.

Cleared acceptance for the KuzuDB tier (ADR-0053, both now met):
- blast_radius p99 < 500ms at 10M edges — **measured 45.5ms**, cleared 2026-05-16
- provenance query at depth 5 returns consistent results vs. SQLite baseline —
  proven by the parity suite (`tests/lineage/test_backends_kuzu.py`)

### Postgres and Apache AGE — the other two shipped alternatives

Not a distinct migration phase in this guide (no separate CLI path exists for
either), but worth knowing both are implemented and testcontainers-verified as
peers of KuzuDB, in case your site standardizes on plain Postgres or the AGE
extension instead of an embedded graph engine:

- `PostgresLineageStore` (`lineage/backends/postgres.py`) — recursive-CTE
  queries against a plain Postgres table, no extension required.
- `AGELineageStore` (`lineage/backends/age.py`) — openCypher via the Apache AGE
  extension, same query surface (`provenance`/`blast_radius`/`replay_chain`).

Both answer identically to the SQLite reference per their parity suites
(`tests/lineage/test_backends_postgres.py`, `tests/lineage/test_backends_age.py`).
Like KuzuDB, reaching either from a bulk migration today means using
`migrate()`/`migrate_from_ocs()` directly in Python with that backend as the
`target` — the CLI wrapper does not expose them.

---

## Phase 3 — Migrate capsule storage to Object Capsule Store

```bash
# Install S3 WORM adapter (or azure/gcs equivalent).
pip install 'novafabric[worm-s3]'

# Set credentials (MinIO, AWS S3, Ceph — any S3-compatible endpoint).
export NOVA_S3_ENDPOINT_URL="http://minio:9000"
export AWS_ACCESS_KEY_ID="minio-access-key"
export AWS_SECRET_ACCESS_KEY="minio-secret"

# Validate WORM readiness first. This checks a single property — that the target
# bucket's default retention (Rule.DefaultRetention.Mode) is COMPLIANCE — and exits
# 1, naming the mode found, for GOVERNANCE or no default retention.
nova storage validate

# Migrate capsules from local filesystem to OCS.
# NOTE (future design): there is no `nova storage migrate` command today — the
# `storage` group exposes only `inspect` and `validate`. Bulk copy-and-chain-build
# to S3 is not yet implemented; migrate capsules via the Object Capsule Store API.
```

**Rollback:** OCS migration is additive — local capsules remain intact until
you set `NOVA_OCS_BACKEND=s3` in production.

---

## Phase 4 — Enable JanusGraph lineage (cluster-scale) *(backend shipped; live verification needs a running instance)*

> **Corrected 2026-07-30.** `JanusGraphLineageStore`
> (`src/novafabric/lineage/backends/janusgraph.py`) is a real Gremlin
> implementation, not a type-stub — the earlier "type-stubs only" framing here
> was wrong (it correctly notes the module isn't *imported* at runtime unless
> `gremlinpython` is installed, but the implementation behind that guard is
> real, not a stub). It required two fixes the first time it was run against a
> live JanusGraph instance: the **GraphSON v3** serializer (JanusGraph's
> default GraphBinary serializer crashes on JanusGraph's custom vertex IDs),
> and **`.emit()`** on the traversal (without it, `repeat(out()).times(n)`
> returns only depth-exact nodes, not the whole reachable set) — both are in
> the shipped code today and testcontainers-verified
> (`tests/lineage/test_backends_janusgraph.py`). What is still genuinely
> missing: there is no `--to janusgraph` flag on the bulk-migration CLI and no
> `nova lineage store` command (see Phase 2's note on the same CLI gap). Using
> it today still requires a running JanusGraph instance (Helm chart at
> `deploy/helm/janusgraph/`) and constructing `JanusGraphLineageStore` directly
> from Python as the migration `target`.

The command shown here is illustrative of the *planned CLI* interface, not a shipped command — the backend behind it is real:

```bash
# PLANNED — does not exist today.
# Migrate lineage edges from KuzuDB to JanusGraph.
# nova lineage-store migrate --from kuzu --to janusgraph \
#   --janusgraph-url "ws://janusgraph:8182/gremlin" --verify

# Run the LDBC SNB BI smoke test (hardware-gated, ~4h for full suite).
# Quick smoke (5 minutes):
uv run pytest tests/lineage/test_janusgraph.py -v -m "not slow"
```

---

## Apache AGE lineage backend — docker-compose profile *(experimental)*

> **Status correction, scoped to this section only:** unlike the JanusGraph section
> above, the `AGELineageStore` Python backend
> (`src/novafabric/lineage/backends/age.py`) is already implemented and
> behavioural-parity-tested against the SQLite reference via testcontainers
> (`tests/lineage/test_age_backend.py`). What's new here is a docker-compose
> **profile** that lets you run Apache AGE locally without testcontainers — this
> profile is **experimental**: opt-in only, not part of the default dev stack or
> `prod`.

```bash
# Start ONLY a standalone AGE-enabled Postgres instance on :5433.
make age-up
make age-down
```

Both targets name the `age` service explicitly on every underlying command
(`up -d age`, `stop age`, `rm -f age`) and never call bare `up`/`down` — the compose
file's `postgres`/`nova` services have no `profiles:` key, so they are always
"active" regardless of which profile is selected, and a bare `up`/`down` under
`--profile age` would start or tear down that entire union, not just `age`. This was
verified live: `make age-up`/`make age-down` never create, restart, or remove an
already-running dev/prod stack's `novafabric-postgres` or `novafabric-serve`.

Connect from Python (requires `pip install novafabric[server]` for `psycopg[binary]`):

```python
from novafabric.lineage.backends.age import AGELineageStore

store = AGELineageStore(dsn="postgresql://nova:nova@localhost:5433/nova_lineage")
```

`AGELineageStore.__init__` self-initializes the AGE extension and graph
(`CREATE EXTENSION IF NOT EXISTS age`, `create_graph`) on first connect — no init-SQL
mount or manual setup step is needed. This is a standalone Postgres instance, separate
from the MetadataStore's `postgres` service (port 5432); the AGE lineage graph is a
derived, rebuildable artifact and deliberately does not share the metadata database.

`make age-down` never passes `-v`, so the named `age-data` volume is preserved across
restarts (matching `dev-down`/`prod-down` preserving `pg-data`/`kuzu-data`). To
intentionally discard it, remove it explicitly:
`docker volume rm docker_age-data` (check the exact name with
`docker volume ls | grep age-data` first — the prefix depends on your compose
project name).

---

## Phase 5 — Enable NATS JetStream collector *(consumer shipped v0.94.0; deployment plumbing hardware-gated)*

> Requires NATS server ≥ 2.10 with JetStream enabled.
> On Lustre: requires Lustre 2.15+ with `lustre_flock` kernel module.

**Corrected 2026-07-30 — the `LineageConsumer` now has a real CLI entrypoint.**
`nova lineage consume` (`src/novafabric/cli/lineage_consume.py`, shipped
v0.94.0, ADR-0061/ADR-0066/ADR-0219) runs `LineageConsumer.run_from_nats()` as
a foreground daemon: it pulls from a NATS JetStream subject, extracts lineage
edges, deduplicates across batches, and bulk-COPYs them into a KuzuDB
directory. Before this command existed, that consumer logic was fully
implemented and tested but reachable only from Python or tests, with no
deployable entrypoint anywhere in the CLI.

```bash
# Install the scale extra (nats-py) and the KuzuDB extra (kuzu).
pip install 'novafabric[scale,lineage-kuzu]'

# The Go `novafabric-hpc-hub` binary (collector side) reads
# NOVAFABRIC_CLUSTER_ID (required — it exits 2 without it) and
# NOVAFABRIC_HUB_ADDRESS. The Python consumer below reads NOVA_NATS_URL.
export NOVAFABRIC_CLUSTER_ID="my-cluster-01"

# Run the live lineage consumer (works today, given a reachable NATS server —
# this part is not itself hardware-gated, only the HPC-specific deployment
# wrapper around it is):
export NOVA_NATS_URL="nats://nats-hub:4222"
export NOVA_KUZU_PATH="/lustre/scratch/myproject/lineage.kuzu"
nova lineage consume --subject "novafabric.lineage.>" \
  --flush-batch-size 2000 --flush-interval-s 15

# Start the collector's HPC-side spool/emit half (Slurm Prolog / Kubernetes
# DaemonSet) — this half is the still hardware-gated piece.
# See deploy/hpc/prolog.sh, deploy/hpc/epilog.sh and the manifests in deploy/k8s/.
```

---

## Phase 6 — Enable Postgres MetadataStore with RLS partitioning

```bash
# Apply the MetadataStore Postgres migrations (v002 partition DDL through v005).
# Starting the server applies no migrations — `nova db upgrade` does.
NOVAFABRIC_METADATA_DSN="$NOVAFABRIC_POSTGRES_DSN" nova db upgrade --backend postgres
nova server start --backend postgres

# Verify RLS is active.
uv run pytest tests/metadata_store/ -v -k "rls"
```

---

## Rollback procedures

| Phase | Rollback command |
|---|---|
| Phase 1 (Postgres) | Start the server with `--backend sqlite` (or unset `NOVAFABRIC_SERVER_BACKEND`); the SQLite files were never modified |
| Phase 2 (KuzuDB) | Stop `nova lineage consume`. There is no backend switch to undo: the `nova lineage` CLI always reads the local SQLite lineage store |
| Phase 3 (OCS) | `unset NOVA_OCS_BACKEND` → local filesystem |
| Phase 4 (JanusGraph) | Stop writing to JanusGraph; no switch exists (the CLI never reads it) |
| Phase 5 (NATS) | Stop `nova lineage consume` and the hub binary; an automatic in-process-queue fallback is **planned**, not implemented |
| Phase 6 (RLS DDL) | Restore from Phase 0 backup |

---

## Verification checklist

After completing all phases:

```bash
# Quality gates.
uv run pytest --cov=novafabric --cov-report=term-missing
uv run ruff check src tests scripts
uv run mypy src

# CLI smoke.
nova --help
nova list | head -n 10
nova serve --experimental --no-browser &
curl -s -H "Authorization: Bearer $(cat "${NOVAFABRIC_HOME:-$HOME/.novafabric}/.serve-token")" \
  http://localhost:4321/api/stats | python -m json.tool

# WORM conformance (if OCS enabled).
nova storage validate

# Lineage integrity — checks the local SQLite lineage store only; query
# KuzuDB/JanusGraph through their Python backends.
nova lineage blast-radius <any-run-id> --depth 3
```

---

## Cluster topology reference

```
[Compute Nodes]          [Collector DaemonSet]
  nova capture    ──────▶  spool (NovaPySpool)
                           │
                           ▼ (NATS JetStream, Phase 5)
                     [NovaSeal Batch Signer]
                           │
                           ▼
                     [HPC Hub / NATS Server]
                           │
                ┌──────────┴──────────┐
                ▼                     ▼
         [Object Capsule Store]  [MetadataStore]
           (S3/Azure/GCS)         (Postgres + PgBouncer)
                │                     │
                ▼                     ▼
         [Lineage Store]        [Evidence Fabric]
         (KuzuDB → JanusGraph)  (DuckDB accumulator)
```

See `design/architecture/cluster-scale.md` (private) for the full architecture.
