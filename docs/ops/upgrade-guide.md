# Upgrade Guide

How to upgrade a NovaFabric deployment from one version to the next — local
SQLite installs and Postgres server mode — and what is honestly supported
when you need to go back. Labels follow the
[docs honesty rule](../../CONTRIBUTING.md#documentation-status-labels): **works today**, **experimental**,
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
[ADR-0180](../decisions.md)
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
reports pending migrations, run
`nova db upgrade --track registry --backend sqlite` (see §4 for why the
`--track` matters). The server also refuses to start on a skewed schema —
see §4a.

---

## 3. Standard upgrade — server mode (Postgres)

**Status: experimental** (server mode itself is experimental).

The supported topology is a **single writer** (ADR-0180), so the sequence is
strictly: back up → migrate schema → roll the one writer.

```bash
# 1. Back up (pg_dump; see backup-restore.md §1.2)
export NOVAFABRIC_POSTGRES_DSN="$NOVA_DSN"   # what nova db upgrade / nova server read
# `nova backup create` keeps the password out of pg_dump's argv (PGPASSWORD);
# a raw `pg_dump --dbname="$NOVA_DSN"` would show it to every local user in `ps`.
nova backup create --profile pg -o pre-upgrade.tar.gz   # reads NOVA_DSN

# 2. Upgrade the package on the server host
pip install -U 'novafabric[server]'

# 3. Apply migrations (expand-contract-safe per §1) while the OLD writer
#    is still running — N code against N+1 schema is within the contract:
alembic -c alembic-postgres.ini upgrade head
# or, using the CLI wrapper (note --track registry: this migrates the
# registry/server DB the server opens — a bare `nova db upgrade` migrates
# the separate MetadataStore tier instead; ADR-0211 D5):
nova db upgrade --track registry --backend postgres    # experimental

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

**Status: works today** (`--track` selector: **experimental**, ADR-0211).

NovaFabric maintains **two separate Alembic universes** — and they migrate
**different databases** (ADR-0211 fixed a doc trap here):

| Track (`nova db upgrade --track …`) | Database | Env vars | Version scripts |
|---|---|---|---|
| `registry` | registry/server DB (`~/.novafabric/registry.db` / `NOVAFABRIC_POSTGRES_DSN`) — the DB the server lifespan opens and `nova backup create --profile pg` dumps | `NOVAFABRIC_HOME`, `NOVAFABRIC_POSTGRES_DSN` | `alembic/{sqlite,postgres}/versions/` (packaged into wheels as `novafabric/migrations/registry/`) |
| `metadata` (default) | MetadataStore tier (`metadata.db` / `NOVAFABRIC_METADATA_DSN`) | `NOVAFABRIC_METADATA_DB_PATH`, `NOVAFABRIC_METADATA_DSN` (not `NOVAFABRIC_DB_PATH`, which names the registry DB) | `novafabric/metadata_store/migrations/` |

Each registry backend also keeps its raw invocation:
`alembic upgrade head` (SQLite, `alembic.ini`) or
`alembic -c alembic-postgres.ini upgrade head` (Postgres) from a source
checkout. From an installed package, use
`nova db upgrade --track registry --backend sqlite|postgres` — the migration
trees ship inside the wheel since ADR-0211 D2.

**After a Postgres restore or a server-mode upgrade, the command you want is
`nova db upgrade --track registry --backend postgres`.** A bare
`nova db upgrade` migrates the MetadataStore tier — a different database than
the one the server serves. Check state before and after with
`nova doctor --check-storage`.

---

## 4a. Startup schema-skew guard (experimental, ADR-0211)

The `nova server` lifespan compares the database's Alembic stamp against the
installed build's migration head **before** touching the database, for both
backends:

- **behind** (new code, old schema) → the server **refuses to start** with
  `E-SKEW-BEHIND`, naming both revisions and the fix:
  `nova db upgrade --track registry --backend <backend>`.
- **ahead/foreign** (old code, newer schema) → refuses with `E-SKEW-AHEAD`:
  upgrade the `novafabric` package; do **not** downgrade the schema.
- **unstamped** (bootstrapped by `init_schema()`, no Alembic stamp — every
  pre-ADR-0211 deployment) → starts with a structured warning — except an
  unstamped **Postgres** database with no application tables at all, which
  refuses to start with `E-SKEW-EMPTY` (same `NOVAFABRIC_ALLOW_SCHEMA_SKEW`
  override); its fix is `nova db upgrade --track registry --backend postgres`
  (a bare `nova db upgrade --backend postgres` migrates the MetadataStore track,
  not the database the guard checks). `nova db upgrade --revision <id>` stops at
  a given revision when you must hold a gated migration back.
- **unknown** (head unresolvable / DB unreadable) → starts with a warning —
  never a fake `ok`, never a refusal based on ignorance.

**Escape hatch:** `NOVAFABRIC_ALLOW_SCHEMA_SKEW=1` (also `true`/`yes`)
downgrades both refusals to a single structured warning and starts anyway —
the documented break-glass for emergency read-mostly access and support.
Unsupported for normal operation; unset it after the incident.

`/readyz`'s `migrations` check uses the same comparator and now returns real
`ok`/`fail` for Postgres as well (previously hardcoded `unknown`).

⚠ **Known defect — fixed for new databases, manual recovery for stamped ones.**
The Helm chart's init container ran `nova db upgrade --backend postgres` (the
MetadataStore track) against the same Postgres database the server's guard
reads, and both Alembic tracks keep their stamp in the same `alembic_version`
table. The database was therefore stamped with a MetadataStore revision (`v005`)
that the registry track does not know, and `nova server start` refused with
`SchemaSkewError` / `E-SKEW-AHEAD` — whose "upgrade the package" advice does not
apply here. In server mode the chart now runs only
`nova db upgrade --track registry --backend postgres`. The Docker image's
entrypoint had the same defect and is fixed the same way (`deploy/docker/entrypoint.sh`). The structural
fix, a distinct version table per track, is still open (pinned by
`tests/server/test_schema_skew_track_names.py`).

---

## 4b. Recovering a database stamped `v005` by an earlier chart or entrypoint

**Status: manual procedure; not exercised by this repository's CI.** Upgrading
the chart does **not** repair such a database: its init container runs
`--track registry`, Alembic cannot locate `v005` in the registry tree, and the
pod stays in `Init:CrashLoopBackOff`. Recover explicitly:

1. **Stop the writer and back up.** `kubectl scale deploy/<release>-novafabric --replicas=0`,
   then, from a host with the PostgreSQL client tools,
   `pg_dump --format=custom --file=nova-before-restamp.dump "$DSN"`. Keep the dump.
2. **Confirm the state with `psql`** (for the chart's bundled Postgres:
   `kubectl exec -it deploy/<release>-novafabric-postgres -- psql --username=<user> --dbname=<database>`, using `postgres.auth.username` / `postgres.auth.database`):

   ```sql
   SELECT version_num FROM alembic_version;
   SELECT table_name FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name IN ('schema_version', 'assets', 'eval_results',
                         'lineage_nodes', 'lineage_edges', 'approvals');
   ```

   Continue **only if** the first query returns exactly one row, a MetadataStore
   revision (`v001`…`v005`), **and** the second query returns **no rows**. Those
   six tables are everything the registry track creates (`alembic/postgres/versions/`
   `0001`–`0003`). The old init container never created them, so they exist only if
   something else did, for example a server started with
   `NOVAFABRIC_ALLOW_SCHEMA_SKEW=1`, whose lineage backend creates `lineage_*`
   itself.
3. **Clear the foreign stamp:** `DELETE FROM alembic_version;`. The MetadataStore
   tables and their data are untouched; only the bookkeeping row goes.
4. **Restart:** `kubectl scale deploy/<release>-novafabric --replicas=1`. The init
   container runs `nova db upgrade --track registry --backend postgres` from base,
   creates the six registry tables and stamps the registry head; the server then
   starts.
5. **Afterwards, never run the bare `nova db upgrade --backend postgres` against
   this database.** With the registry stamp in place, the MetadataStore track
   fails because it cannot locate `p0003`. Clearing the stamp again to make it run
   would re-apply `v001` over the existing tables.

If step 2 shows any of the six registry tables, or a `p000N` row in
`alembic_version`, **stop**. Stamping the registry head over tables of unknown
shape, or dropping them, could lose lineage data. A safe procedure for that state
needs a product decision (the per-track version table above) and is not
documented here. Restore nothing; keep the dump and open an issue.

## 4c. MetadataStore tables in server mode

**Status: works today (behaviour of the fixed chart).** With only the registry track
migrated, the server creates the MetadataStore tables it uses on **first use**, not
at startup: `PostgresMetadataStore.bootstrap()` (`server/deps.py`) runs `CREATE TABLE
IF NOT EXISTS` for `runs`, `capsules`, `signatures` and `retention_policies`, creates
the `novafabric_app` (NOBYPASSRLS) and `novafabric_migrator` (**BYPASSRLS**) roles if
absent, enables row-level security with the `tenant_isolation` policy, and grants
the app role its privileges, including `DELETE` on `runs`, `capsules` and
`signatures`. Two consequences:

- **Privileges.** Creating those roles needs `CREATEROLE`. Under PostgreSQL's
  `CREATE ROLE` rules, creating a role with `BYPASSRLS` also needs a creator that
  is superuser or has `BYPASSRLS` itself. The old init container needed the same
  thing for migration `v001`, but there a missing privilege failed the init
  container. Now `/readyz` passes and the failure surfaces at the first
  MetadataStore call. An upload still succeeds and logs
  `capsule_metadata_index_failed`; other MetadataStore calls, such as the
  tenant-scoped index delete, can fail. On managed Postgres without that
  privilege, have an administrator pre-create both roles
  (`CREATE ROLE novafabric_app NOLOGIN NOBYPASSRLS;`
  `CREATE ROLE novafabric_migrator NOLOGIN BYPASSRLS;`). `bootstrap()` skips
  existing roles.
- **Shape.** `bootstrap()` builds a non-partitioned `runs` table. It has the
  `(run_id, tenant_id, started_at)` primary key that `v004` produces, but not
  `v002`'s quarterly partitions, and it creates no `v003` knowledge-graph tables
  (`kg_alias_table`, `kg_entity_resolution_queue`). A database the MetadataStore
  migrations built keeps those, which is the shape of a database stamped `v005`
  by an earlier chart.

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
[ADR-0184](../decisions.md);
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
  [ADR-0016](../decisions.md). The
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
