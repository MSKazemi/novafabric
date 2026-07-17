# Upgrade Guide

How to upgrade a NovaFabric deployment from one version to the next — local
SQLite installs and Postgres server mode — and what is honestly supported
when you need to go back. Labels follow the
[docs honesty rule](../../CLAUDE.md): **works today**, **experimental**,
**planned**, or **future design**.

> **Before every upgrade: take a backup.** The
> [Backup & Restore Runbook](backup-restore.md) is the authoritative
> procedure (`nova backup create` for local installs, `pg_dump` for server
> mode). Its cadence table says it plainly: back up *before upgrades,
> always*. A verified backup is also the only fully supported downgrade path
> (§7).

---

## 1. The compatibility contract: expand-contract, N/N+1

**Status: contract — accepted
[ADR-0180](../../design/adr/0180-ha-and-zero-downtime-upgrade-posture.md)
(D4); enforced as release gate §0 in
[`docs/release-process.md`](../release-process.md).**

Every Alembic migration shipped in a release MUST be backward-compatible for
**one minor version** (the N/N+1 window): code at version N must run
correctly against the schema after version N+1's migrations have been
applied.

- **Expand first** — additive changes only: new nullable/defaulted columns,
  new tables, new indexes.
- **Contract later** — destructive changes (drops, renames, tightened
  constraints) only after one full minor cycle in which no supported code
  version reads the old shape.
- **Violations are release blockers** — a migration that breaks the N/N+1
  window does not ship.

What this buys you as an operator:

1. The standard upgrade "**migrate the schema, then roll the writer**" is
   safe, with a bounded write-unavailability window.
2. **Rolling the code back one minor version is safe without rolling back
   the schema** — the previous minor's code runs correctly against the
   upgraded schema.
3. Skipping minors (N → N+3 in one hop) is **outside** the stated window.
   It usually works because contraction is rare, but the contract only
   covers N/N+1 — for multi-minor jumps, step through minors or lean on the
   backup.

---

## 2. Standard upgrade — local mode (SQLite)

**Status: works today.**

```bash
# 1. Back up first (see backup-restore.md)
nova backup create -o pre-upgrade.tar.gz       # experimental, ADR-0181
# (or the tar/.backup procedure in backup-restore.md §1.1)

# 2. Stop anything writing: nova serve, nova server, running captures

# 3. Upgrade the package
pip install -U novafabric          # add extras you use: 'novafabric[serve]' etc.
# uv:  uv pip install -U novafabric

# 4. Health check — reports schema version and migration state
nova doctor
nova doctor --check-storage
```

SQLite schema migrations for the registry/metadata tables run on the SQLite
Alembic track (§4). `nova doctor --check-storage` reports the backend name,
Alembic schema version, migration status, and per-table row counts — if it
reports pending migrations, run `nova db upgrade` (see §4).

---

## 3. Standard upgrade — server mode (Postgres)

**Status: experimental** (server mode itself is experimental).

The supported topology is a **single writer** (ADR-0180), so the sequence is
strictly: back up → migrate schema → roll the one writer.

```bash
# 1. Back up (pg_dump; see backup-restore.md §1.2)
pg_dump --format=custom --dbname="$NOVA_DSN" --file=pre-upgrade.dump

# 2. Upgrade the package on the server host
pip install -U 'novafabric[server]'

# 3. Apply migrations (expand-contract-safe per §1) while the OLD writer
#    is still running — N code against N+1 schema is within the contract:
alembic -c alembic-postgres.ini upgrade head
# or, using the CLI wrapper against the configured backend:
nova db upgrade

# 4. Roll the writer: stop the old process, start the new one.
#    Never run two writers at once (ADR-0180 fencing invariant).
nova server start --backend postgres

# 5. Verify
nova doctor --check-storage --backend postgres --postgres-dsn "$NOVA_DSN"
```

Kubernetes/Helm installs apply migrations via an init container on rollout
([Server Deployment Guide — Scenario 5](server-deployment.md)); keep
`replicaCount: 1`.

---

## 4. Alembic dual-track migrations

**Status: works today.**

NovaFabric maintains **two parallel Alembic tracks**, one per backend:

| Track | Config file | Version scripts | Invocation |
|---|---|---|---|
| SQLite (default) | `alembic.ini` | `alembic/sqlite/versions/` | `alembic upgrade head` |
| Postgres | `alembic-postgres.ini` | `alembic/postgres/versions/` | `alembic -c alembic-postgres.ini upgrade head` (needs `NOVAFABRIC_POSTGRES_DSN` or a `db_url` override) |

The CLI wrapper `nova db upgrade` runs `alembic upgrade <revision>` against
the **configured MetadataStore backend**, so you rarely need raw `alembic`.
Check state before and after with `nova doctor --check-storage`.

---

## 5. One-time migrations you may need

### 5.1 SQLite → Postgres (`nova migrate-to-postgres`)

**Status: experimental.** One-time, **idempotent** (upsert semantics — safe
to re-run after a partial failure); the source SQLite file is never modified
or deleted.

```bash
nova migrate-to-postgres --dry-run                       # preview first
nova migrate-to-postgres \
  --source ~/.novafabric/registry.db \
  --target "$NOVA_DSN" \
  --log migration.jsonl
nova doctor --check-storage --backend postgres           # verify row counts
```

Exit codes: `0` success, `1` row-count mismatch, `2` connection error. Full
walkthrough: [Server Deployment Guide](server-deployment.md). (A separate
`nova db migrate-to-postgres` subcommand covers the MetadataStore tables.)

### 5.2 Old capsules → current schema (`nova migrate-schema`)

**Status: works today.** Capsules written by very old versions (schema v0)
can be batch-upgraded in place — sets `schema_version`, renames
`event_log.jsonl`, adds `format_version`:

```bash
nova migrate-schema --dry-run  --capsule-dir ~/.novafabric/capsules/  # preview
nova migrate-schema --backup   --capsule-dir ~/.novafabric/capsules/  # keeps <file>.v0.bak
```

`--backup` preserves originals as `<file>.v0.bak`, which allows manual
rollback of this step. Note the capsule format overall is **experimental**
(not frozen until the v1.0 schema freeze) — newer minors read older capsules;
`migrate-schema` exists for the v0-era layout specifically.

---

## 6. Version-specific upgrade notes

### v0.61 — breaking change to the local `nova server` auth default

**Status: shipped (experimental) —
[ADR-0184](../../design/adr/0184-secure-by-default-local-server-auth.md);
see [`docs/releases/v0.61.0.md`](../releases/v0.61.0.md).**

With OIDC disabled, `nova server` now **requires a bearer token**:
auto-generated, printed at startup, stored at
`$NOVAFABRIC_HOME/.server-token` (mode 0600), pinnable via
`NOVAFABRIC_SERVER_TOKEN`. After upgrading:

- **Same-machine scripts** can read `~/.novafabric/.server-token`.
- **CI** should pin `NOVAFABRIC_SERVER_TOKEN`.
- The pre-v0.61 anonymous-admin behaviour needs the explicit
  `--insecure-no-auth` opt-out, and refuses non-loopback binds without
  `--i-know-this-is-public`.

Also in v0.61: a new optional dependency in the `[server]` extra
(`prometheus-client`, Apache-2.0). All other v0.61 changes are
additive/opt-in.

Always read the release note for the version you are moving to
([`docs/releases/`](../releases/)) — breaking changes are called out under
"Upgrade notes".

---

## 7. Downgrades — what is and is not supported

Honesty section. Supported:

- **Code rollback by one minor, schema left upgraded** — safe by the
  expand-contract contract (§1). Reinstall the previous minor
  (`pip install novafabric==<X.Y.Z>`) and keep running against the migrated
  schema. This is the intended "the new release broke us" escape hatch.
- **Restore from a pre-upgrade backup** — the general downgrade path for
  anything beyond one minor: restore the DB/home from backup and reinstall
  the matching version ([Backup & Restore Runbook](backup-restore.md);
  `nova restore` for local-profile sets, `pg_restore` for Postgres).

Not supported:

- **Schema downgrades.** There is no supported or tested
  `alembic downgrade` path; do not run one against a production database.
  Roll the code back (one minor) or restore a backup.
- **Postgres → SQLite migration.** Explicitly not supported per
  [ADR-0016](../../design/adr/0016-storage-backend-evolution.md). The
  original SQLite file survives `nova migrate-to-postgres` untouched, but it
  is a snapshot of the moment you migrated — not a downgrade of later data.
- **Downgrading capsules.** Sealed evidence and Merkle log entries are
  append-only by design; there is no "unmigrate" for evidence.

---

## 8. Post-upgrade verification

```bash
nova doctor --check-storage    # backend, schema revision, migration state
nova --help                    # CLI loads
nova validate <a-recent-capsule-dir>   # capsule reads fine
nova seal log verify           # if you seal: Merkle log integrity
```

If the server won't come up or `/readyz` fails after an upgrade, switch to
the [Incident Runbook](incident-runbook.md) §1.
