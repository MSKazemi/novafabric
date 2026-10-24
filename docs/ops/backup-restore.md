# Backup & Restore Runbook

How to back up and restore a NovaFabric deployment. The first half documents
procedures that **work today** with standard tools; the second half covers the
`nova backup` tooling
([ADR-0181](../decisions.md), accepted
2026-07-16 — `nova backup create`/`verify`, `nova restore` for the **local
profile and the `--profile pg` create path work today (experimental)**;
[ADR-0217](../decisions.md) automates the
pg_restore path — **`nova restore <set> --dsn …`**, profile auto-detected from
the verified manifest (experimental), §1.2;
[ADR-0211 Part B](../decisions.md)
adds the startup schema-skew guard and `nova db upgrade --track`).

What a NovaFabric deployment consists of, and who owns its durability:

| Data | Where | Durability owner |
|---|---|---|
| Registry + local capsules | `$NOVAFABRIC_HOME` (`~/.novafabric/`: SQLite `registry.db`, `capsules/`, legacy `runs/`) and `$NOVAFABRIC_CAPSULE_DIR` when set | this runbook |
| Server/metadata DB | Postgres (or SQLite in local mode) | Postgres tooling + this runbook |
| Capsule objects (WORM) | S3/Azure/GCS/local object store | bucket versioning/replication |
| Signing keys | Cloud KMS (AWS KMS, Azure Key Vault, GCP KMS) / `$NOVAFABRIC_HOME/keys/` | [NovaSeal Key Management](../novaseal-key-management.md) — **never** part of a data backup by default |
| Config | `~/.config/novafabric/server.yaml` (or the `--config` path) + env secrets | this runbook (config yes, secrets via your secret manager) |

---

## Part 1 — What works today

### 1.1 Local mode (SQLite)

**Status: works today.**

SQLite files must not be copied while a writer is active. Stop `nova serve` /
`nova server` and any running capture, then:

```bash
tar czf novafabric-local-$(date +%F).tar.gz \
    ~/.novafabric/registry.db \
    ~/.novafabric/capsules/ \
    ~/.novafabric/runs/
```

`capsules/` is where `nova capture` writes; `runs/` is the other capsule directory
`nova backup` archives (`backup/create.py::_CAPSULE_DIRS`). If `NOVAFABRIC_CAPSULE_DIR` is set, add that directory too: capture writes
there instead of `~/.novafabric/capsules/`. `nova backup create` (Part 2) covers both
automatically.

Restore = untar to the same paths, then verify (see §1.5). If you cannot stop
the writer, use SQLite's online backup (`sqlite3 ~/.novafabric/registry.db
".backup 'registry-backup.db'"`), which is safe against a live database.

### 1.2 Server mode (Postgres)

**Backup: works today** (standard Postgres operations). **Automated restore:
experimental** (ADR-0211).

```bash
# Backup — either the standard tool directly (password from PGPASSWORD or
# ~/.pgpass, never in --dbname, which `ps` shows to every local user):
pg_dump --format=custom --dbname="host=db user=nova dbname=nova" \
        --file=novafabric-$(date +%F).dump
# …or the verified backup set (adds manifest + hashes + optional DSSE,
# plus the pg_dump version and per-table row counts used by restore
# verification — ADR-0211 D6). NOVA_DSN is read from the environment:
NOVA_DSN="postgresql://nova:…@db/nova" nova backup create --profile pg -o pg-nightly.tar.gz
```

`nova backup create` and `nova restore` hand `pg_dump` / `pg_restore` a DSN with
the password removed and pass the password in `PGPASSWORD`, so it never appears
in the client's argv (`/proc/<pid>/cmdline`). Prefer `NOVA_DSN` over `--dsn` for
the same reason: `--dsn` puts the password in `nova`'s own argv. Releases up to
v0.107.0 passed the full DSN as `--dbname`.

**Restore — the automated command (experimental, ADR-0217):**

```bash
nova restore pg-nightly.tar.gz --dsn "$TARGET_DSN"
```

One command runs the whole drill with per-step receipts — the profile is
auto-detected from the verified manifest: verify the set (no skip flag) →
pre-flight (target reachable; **non-empty target refused** without `--force`,
which first takes a `db.pre-restore.pgdump` safety dump) →
`pg_restore --clean --if-exists --single-transaction` (failure rolls back —
target unchanged) → `alembic upgrade head` → verification (row counts vs the
manifest, RLS re-applied and proven, schema-skew
comparator `ok`, storage check). Exit codes: `0` success · `1` any failure (an
invalid set, missing tooling, a refused non-empty target, or a failed step — the
message and per-step receipts say which) · `2` a manifest-only set whose bucket is
unreachable, or a usage error (`cli/backup.py`). The DSN is treated as a
secret throughout — never logged, scrubbed from any surfaced stderr.

Home members in the set (registry snapshot, capsules) are restored only when
you pass `--home PATH` — a pg restore never writes the local
`~/.novafabric` implicitly.

**Manual fallback (works today, standard Postgres):**

- `pg_restore --clean --if-exists` into a freshly created database.
- **PITR** is delegated to Postgres: enable WAL archiving /
  `restore_command`, or use your managed provider's point-in-time recovery.
  NovaFabric deliberately does not reimplement this
  ([ADR-0181](../decisions.md)).
- After a manual restore, migrate the **registry track** to head:
  `nova db upgrade --track registry --backend postgres` (ADR-0211 D5 — a bare
  `nova db upgrade` migrates the separate MetadataStore tier, **not** the
  database you just restored). Check state before and after with
  `nova doctor --check-storage`.

### 1.3 WORM object stores

**Status: works today** (delegated to the bucket).

Capsule objects live in a content-addressed WORM store. Durability comes from
the bucket, not from copies: enable **versioning + Object Lock** (S3),
immutability policies (Azure/GCS), and cross-region replication per your DR
tier. Two properties matter for restore:

- The store is **rebuildable**: the manifest chain lets NovaFabric re-index
  objects after a metadata-DB restore (`nova rebuild-metadata-db` /
  `nova storage …`; check `nova doctor --check-storage` afterwards).
- Object Lock + legal holds survive account-level mishaps only if the bucket
  policy denies deletion — test this, don't assume it.

### 1.4 Keys — backed up separately, never with data

**Status: works today.** Follow the
[NovaSeal Key Management Guide](../novaseal-key-management.md) (cloud KMS or
software keystore). Two rules this runbook enforces:

1. **No private key ever enters a data backup.** A restored deployment
   without keys can still *verify* everything; it just cannot *sign* until
   keys are re-provisioned.
2. **Crypto-shredded keys stay deleted.** If retention ran `CRYPTO_SHRED`
   ([ADR-0134](../decisions.md)),
   restoring an older key escrow must not resurrect shredded data — check the
   retention decision log before re-importing any archived key material.
   (Retention `crypto-shred` is **experimental**. It destroys a key only for capsules
   captured with `nova capture --pii-subject`, when the policy sets
   `include_capture_capsules: true` and `NOVA_PII_PEPPER` is set; otherwise it records
   `no_dek` or `skipped` — see [Retention scheduler](../cli-reference.md#retention-scheduler-adr-0134).
   Keys destroyed that way or by `nova pii erase` are what this rule protects.)

### 1.5 Verify after restore — restore is not done until this passes

**Status: works today.** The evidence is hash-chained and signed, so a restore
is *checkable*:

```bash
nova doctor --check-storage        # backend, schema revision, migration state
nova seal log verify               # Merkle log integrity
nova ledger verify <run-dir>       # accountability ledger chains, per capsule (ADR-0094)
nova validate <restored-run-dir>   # spot-check capsules
```

Treat any verification failure as a failed restore, not a warning.

### 1.6 Suggested cadence

| Deployment | Backup | Verify |
|---|---|---|
| Laptop / local | tar or `.backup` weekly (before upgrades always) | after every restore |
| Team server (Postgres) | `pg_dump` nightly + WAL archiving (PITR) | restore-drill quarterly, §1.5 after every drill |
| Regulated | provider PITR + replicated WORM bucket + off-site `pg_dump` | scheduled restore drills with evidence retained |

### 1.7 Instance transfer — verified capsule-level export/import

**Status: experimental** (ADR-0141 export + ADR-0207 import, P1).

For moving *capsules* between instances (DR restore of the capsule store,
air-gapped transfer, laptop → team-server migration) NovaFabric has a signed,
verified interchange path that is stronger than tarring directories:

```bash
# On the source instance: signed batch export (+ public key for the receiver)
nova export-blob --dest ./exports/dr-2026-07 --since 2026-07-01 \
    --public-key-out export.pub.pem

# On the target instance: verify + classify first, with zero writes
nova import ./exports/dr-2026-07 --public-key export.pub.pem --dry-run

# Then the real, receipted import (idempotent — safe to re-run)
nova import ./exports/dr-2026-07 --public-key export.pub.pem
```

The import is fail-closed (a tampered or incomplete batch refuses whole, and so
does a member `capsule_id` that is not one plain directory name, so no member can
land outside the store),
idempotent by content address (interrupted imports resume on re-run), never
overwrites a diverged local capsule (collisions are reported, exit 5), and
reindexes lineage + the dashboard runs cache automatically. Every run — dry
runs and refusals included — leaves a receipt under
`$NOVAFABRIC_HOME/import-receipts/` plus an audit entry, so a scheduled
`--dry-run` doubles as a restore drill with retained evidence. See the
[CLI reference](../cli-reference.md#nova-import-source) for exit codes and
flags. This transfers **capsules only** — the asset registry, holds, and
policies still travel via the database backup paths above.

---

### Unreadable WORM or retention-policy files

**Status: works today.** Deletion fails closed on local WORM state (see the
[CLI reference](../cli-reference.md#nova-capsule-delete-capsule-id)). When a `worm.db` exists
in any registry location — `$NOVAFABRIC_HOME/registries/<r>/`, the legacy
`./.novafabric/registries/<r>/`, or a capsule store's `../registries/<r>/` — but cannot be
read, **every** deletion refuses with `worm_state_unavailable`: `nova capsule delete`,
`DELETE /v0/capsules/{id}` and bulk delete (HTTP 409), the dashboard's
`DELETE /api/runs/{id}`, metadata-index delete, and `nova retention plan|apply|status|explain`.
An unreadable `retention-policy.yaml` refuses `nova capsule delete` and `nova retention` with
`retention_policy_unavailable`. Nothing is deleted, and there is no override flag.

PII erasure (`nova pii erase`, the erasure queue, REST erasure) and the restore-time
crypto-shred replay do not read `worm.db` at all: crypto-shred destroys a key, not the
WORM-locked object (ADR-0134 D4, ADR-0062), so they are not blocked by it. The retention
sweep's own `crypto-shred` action does honour WORM locks and is refused like `purge`.

Recovery:

1. **Find the file.** The refusal names it and the reason (also logged at ERROR by
   `novafabric.retention.worm_state`).
2. **Stop and keep evidence.** Copy the file (and any `worm.db-journal` beside it) aside
   before changing anything: `cp -p worm.db worm.db.damaged-$(date +%F)`.
3. **Fix the cause.**
   - *Permission denied* — restore the owner/mode the NovaFabric process expects
     (`chmod 600 worm.db`, correct owner).
   - *Locked* — another process holds a write lock; let it finish or stop it, then retry.
   - *Hot journal left by a crashed writer* — open it read-write once so SQLite rolls the
     journal back: `python -c "import sqlite3,sys; sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchall()" worm.db`.
   - *Corrupt, empty, wrong file, dangling symbolic link* — restore `worm.db` from the latest
     verified backup set (`nova restore`, or copy the member out of the set and check its
     hash against the manifest). Then re-apply any WORM lock recorded after that backup.
4. **Verify.** `nova retention plan --registry <r>` exits 0 and shows the expected
   `worm_hold` rows; then retry the delete.

Removing the file is **not** a fix: with no `worm.db` anywhere, NovaFabric treats local WORM
as not configured, and every lock that file held stops protecting its capsule. Do it only if
the locks are recorded elsewhere and you have decided they no longer apply, and record that
decision. The local WORM adapter is dev/test-only (ADR-0031); production immutability comes
from the object store's own lock (S3 Object Lock, Azure immutable blob, GCS Bucket Lock).

## Part 2 — `nova backup` tooling

Governed by [ADR-0181](../decisions.md)
(accepted 2026-07-16); the underlying spec is held in the maintainers' private
`design/` tree and is not published.

**Works today (experimental):**

- `nova backup create [-o PATH] [--home PATH] [--profile local|pg]` —
  one-command backup set (`.tar.gz`). The default `local` profile
  (`local-full`): registry snapshot via the SQLite **online-backup API** (safe
  against a live writer), the capsule directories, and a secret-redacted
  config. `--profile pg` (`pg-dump`) additionally runs
  `pg_dump --format=custom` against `--dsn` / `NOVA_DSN` /
  `NOVAFABRIC_POSTGRES_DSN` and adds the dump as a member — the DSN is treated
  as a secret: never logged, never stored; the manifest records only the
  redacted `host/dbname` (`db_target`). Either way the manifest is DSSE-signed
  when a local NovaSeal profile is configured (honest
  `signing_status: "unsigned"` otherwise), and key material is excluded by a
  normative deny-filter — never by convention.
- `nova backup verify <set.tar.gz>` — offline integrity check: every member's
  SHA-256 against the manifest plus the DSSE signature when present. Exit 1 on
  any mismatch. No live deployment, network, or private keys required.
- `nova restore <set.tar.gz> [--home PATH] [--force]` — **local profile**:
  runs the spec's normative order — verify the set (no flag to skip it) →
  prepare the home (a non-empty home is refused without `--force`; with it,
  existing data is moved aside into a timestamped `.pre-restore-…/` directory,
  never deleted) → path-traversal-safe extraction → migrations to head →
  **crypto-shred replay** (every applied `CRYPTO_SHRED` in the retention
  decision log is re-applied against the restored DEK store, so shredded data
  cannot be resurrected from an older backup) → the §1.5 verification chain
  (doctor storage check + `seal log verify` when a Merkle log exists). The
  restore reports ok **only when verification passes**; exit 1 otherwise.

  The replay destroys a key only on evidence it can check (works today). A
  decision log whose audit hash chain does not verify contributes no record, and
  the step fails. The chain is unkeyed: it catches an edited or appended line,
  not a writer who rewrites the whole chain. A record's erasure receipt must be a
  regular file named `<item_id>.receipt.json` for that record; a symbolic link
  or another item's receipt makes the record unresolvable, and the step fails.
  **Legal holds win.** If any hold is active, in the deployment's hold store or
  in the restored home's `registries/`, no resurrected key is destroyed and the
  step fails, naming the holds. Destruction cannot be undone, and only a person
  can decide what the hold requires. Release the hold, or establish that it does
  not cover those subjects, then restore again.
- `nova restore <set.tar.gz> --dsn <target> [--force] [--home PATH]` —
  **pg-dump profile (experimental, ADR-0217)**: the automated §1.2 restore,
  auto-detected from the manifest — verified set, pre-flight + safety dump,
  single-transaction `pg_restore`, alembic-to-head, row-count + RLS + storage
  verification, per-step receipts. Home members in the set restore alongside
  (with crypto-shred replay, same as the local path).

**Coverage (ADR-0216 — works today):** the `local` profile now backs up
**every persistent local store**: `registry.db`, `capsules/`, `runs/`,
`incidents.db`, `metadata.db`, `dek.db` (sensitive; restored 0600),
`tsa_nonces.db`, `novaseal-merkle.db` (when local — a Postgres-backed seal
log is skipped with a signed coverage row and covered by the server backup),
`seal/ratchet/` (sensitive; epoch regression is burned on restore),
`dashboard.duckdb` (DuckDB-native snapshot; skipped honestly when a live
`nova serve` holds the writer lock), `dashboard-audit.jsonl`, `spool/`, the
hash-chained audit log, and a secret-redacted config. The signed manifest
carries a **coverage table** — absences and skips are recorded evidence,
never silence.

**Capsule store outside the home (works today):** `nova backup create` finds
capsules the way `nova capture` writes them — `$NOVAFABRIC_CAPSULE_DIR` when
set, else `<home>/capsules`. A store outside the home (the Docker layout keeps
capsules in `/data/capsules`) is archived under `external/capsule-dir/`
(member origin `capsule-dir`, manifest schema 0.2.1), and the `capsules`
coverage row names the directory. `nova restore` puts those capsules back by
the same rule on the target: into `$NOVAFABRIC_CAPSULE_DIR` when set there,
else `<target home>/capsules`. An existing capsule file is never overwritten
without `--force`, which moves it aside under `.pre-restore-…/external/capsule-dir/`.
Before this fix a Docker deployment's set recorded `capsules: absent`.

> **Custody note:** because `dek.db` (raw per-subject DEKs) travels in the
> default set, a backup set is a **sensitive artifact** — store it encrypted
> at rest with restricted access. Crypto-shred replay on restore guarantees
> shredded subjects stay shredded regardless (the replay also reads a
> moved-aside live audit log, so shreds applied *after* the backup survive a
> `--force` restore of an older set).

**Key policy (ADR-0216 D4):** signing keys (`keyring`, `novaseal.yaml` +
PEMs) stay excluded by default. A full-DR set needs the **dual opt-in**:
`nova backup create --include-keys` AND `nova restore --restore-keys`. Both
commands print custody warnings; see `docs/novaseal-key-management.md`.

**Automated pg restore (ADR-0217 — works today):** `nova restore` now
restores `pg-dump` sets: a non-empty target DB is refused without `--force`
(which first writes `db.pre-restore.pgdump` into the `.pre-restore-…/`
directory), then `pg_restore --clean --if-exists --single-transaction
--no-owner --no-privileges` (failure rolls back — target unchanged), `alembic
upgrade head`, manifest-anchored row-count verification, and RLS
re-application + proof. The manual §1.2 procedure remains the fallback for
very large dumps (`--jobs` parallel restore is incompatible with
`--single-transaction`).

**Manifest-only profile (ADR-0216 D6 — works today):** `nova backup create
--profile manifest --backend s3` records, per (tenant, run), the chain head
version + head-commit hash (hash linkage pins the whole chain — the
countermeasure for chain logs not being WORM-locked), the newest checkpoint,
and a secret-free backend fingerprint. Given the client's WAL (`--wal-db PATH`),
create **refuses while it has pending or dead-lettered un-chained uploads**
(`--allow-pending-wal` overrides and records the gap); without `--wal-db` the WAL
is not checked and the command prints so. Restore verifies every pinned head as an *ancestor* of the live
chain, rebuilds the metadata DB from the chain (ADR-0175 path), and exits
**2** when the bucket is unreachable. Note there is no global point-in-time
cut across runs — each chain is individually consistent (append-only makes
this safe).

**Honest limits:**

- Live-Postgres verification of the automated pg restore is exercised by a
  testcontainers integration test; on hosts without Docker + the Postgres
  client tools it skips — **run a restore drill against a real DSN before
  relying on it**.
- `--single-transaction` holds the whole dump in one transaction — for very
  large DBs use the manual §1.2 procedure with `--jobs`.
- Ratchet epoch-regression detection depends on the restored epoch registry;
  if the registry itself was lost, regression is undetectable — operators
  SHOULD rotate once after any restore.
