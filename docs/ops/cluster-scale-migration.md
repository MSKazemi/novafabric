# Cluster-Scale Migration Guide

> **Status:** works today — all commands are implemented in v0.25.0+.
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

## Phase 1 — Migrate to Postgres MetadataStore

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

```bash
export NOVA_DSN="postgresql+asyncpg://novafabric_app:changeme@localhost:5432/novafabric"

# Migrate capsule metadata from SQLite to Postgres.
# This is additive — the SQLite database is NOT deleted.
nova migrate-to-postgres --target "$NOVA_DSN" --log migration.jsonl
```

Expected output:
```
✓ Schema created (v0.14.0)
✓ Migrated 1234 runs
✓ Migrated 567 assets
✓ Migrated 89 role_assignments
✓ Migration complete — log: migration.jsonl
```

### 1.3 Verify

```bash
# Switch to Postgres mode and verify.
NOVA_BACKEND=postgres NOVA_DSN="$NOVA_DSN" nova list --limit 10
NOVA_BACKEND=postgres NOVA_DSN="$NOVA_DSN" nova server start --experimental
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

## Phase 2 — Migrate lineage to KuzuDB

```bash
# Install the lineage-kuzu extra.
pip install 'novafabric[lineage-kuzu]'

# Migrate from SQLite lineage store to KuzuDB.
# Source: SQLite lineage_edges table in $NOVA_HOME/novafabric.db
# Target: KuzuDB graph at $NOVA_HOME/lineage/nova_lineage.kuzu
nova lineage store migrate \
  --from sqlite \
  --to kuzu \
  --kuzu-path "$NOVA_HOME/lineage/nova_lineage.kuzu" \
  --verify
```

Expected acceptance:
- blast_radius p99 < 500ms at 10M edges (ADR-0053 gate — `nova-lineage-bench`)
- provenance query at depth 5 returns consistent results vs. SQLite baseline

---

## Phase 3 — Migrate capsule storage to Object Capsule Store

```bash
# Install S3 WORM adapter (or azure/gcs equivalent).
pip install 'novafabric[worm-s3]'

# Set credentials (MinIO, AWS S3, Ceph — any S3-compatible endpoint).
export NOVA_S3_ENDPOINT_URL="http://minio:9000"
export AWS_ACCESS_KEY_ID="minio-access-key"
export AWS_SECRET_ACCESS_KEY="minio-secret"

# Validate WORM conformance first (11/11 checks must pass).
nova storage validate

# Migrate capsules from local filesystem to OCS.
# This copies capsules to S3 and builds the manifest chain.
nova storage migrate \
  --source "$NOVA_HOME/capsules" \
  --target "s3://nova-capsules-bucket" \
  --verify-chain
```

**Rollback:** OCS migration is additive — local capsules remain intact until
you set `NOVA_OCS_BACKEND=s3` in production.

---

## Phase 4 — Enable JanusGraph lineage (cluster-scale) *(hardware-gated)*

> Requires a running JanusGraph instance. Use the Helm chart:
> ```bash
> helm install janusgraph deploy/helm/janusgraph/ --namespace nova-system
> ```

```bash
pip install 'novafabric[janusgraph]'

# Migrate lineage edges from KuzuDB to JanusGraph.
nova lineage store migrate \
  --from kuzu \
  --to janusgraph \
  --janusgraph-url "ws://janusgraph:8182/gremlin" \
  --verify

# Run the LDBC SNB BI smoke test (hardware-gated, ~4h for full suite).
# Quick smoke (5 minutes):
uv run pytest tests/lineage/test_janusgraph.py -v -m "not slow"
```

---

## Phase 5 — Enable NATS JetStream collector *(hardware-gated)*

> Requires NATS server ≥ 2.10 with JetStream enabled.
> On Lustre: requires Lustre 2.15+ with `lustre_flock` kernel module.

```bash
# Install the scale extra.
pip install 'novafabric[scale]'

# Configure the collector to use NATS.
export NOVAFABRIC_HUB_ADDRESS="nats://nats-hub:4222"
export NOVAFABRIC_CLUSTER_ID="my-cluster-01"

# Start the collector (Slurm Prolog / Kubernetes DaemonSet).
# See collector/scripts/slurm-prolog.sh and deploy/k8s/collector-daemonset.yaml.
```

---

## Phase 6 — Enable Postgres MetadataStore with RLS partitioning

```bash
# Apply the v002 DDL migration (partition tables by tenant + time).
# Requires ADR-0051 Postgres partition DDL — run after Phase 1.
nova server start --experimental --migrate-postgres-v002

# Verify RLS is active.
uv run pytest tests/metadata_store/ -v -k "rls"
```

---

## Rollback procedures

| Phase | Rollback command |
|---|---|
| Phase 1 (Postgres) | `unset NOVA_BACKEND` → SQLite fallback is automatic |
| Phase 2 (KuzuDB) | `unset NOVA_LINEAGE_BACKEND` → SQLite lineage fallback |
| Phase 3 (OCS) | `unset NOVA_OCS_BACKEND` → local filesystem |
| Phase 4 (JanusGraph) | `unset NOVA_LINEAGE_JANUSGRAPH_URL` → KuzuDB |
| Phase 5 (NATS) | `unset NOVAFABRIC_HUB_ADDRESS` → in-process queue |
| Phase 6 (RLS DDL) | Restore from Phase 0 backup |

---

## Verification checklist

After completing all phases:

```bash
# Quality gates.
uv run pytest --cov=novafabric --cov-report=term-missing
uv run ruff check src tests
uv run mypy src

# CLI smoke.
nova --help
nova list --limit 5
nova server start --experimental &
curl -s http://localhost:4321/api/stats | python -m json.tool

# WORM conformance (if OCS enabled).
nova storage validate

# Lineage integrity (if KuzuDB/JanusGraph enabled).
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

See `design/architecture/cluster-scale.md` for the full architecture.
