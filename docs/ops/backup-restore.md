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
| Registry + local capsules | `~/.novafabric/` (SQLite `registry.db`, `runs/`) | this runbook |
| Server/metadata DB | Postgres (or SQLite in local mode) | Postgres tooling + this runbook |
| Capsule objects (WORM) | S3/Azure/GCS/local object store | bucket versioning/replication |
| Signing keys | HSM / KMS / `~/.novafabric/keys/` | [NovaSeal Key Management](../novaseal-key-management.md) — **never** part of a data backup |
| Config | `nova-server.yaml` + env secrets | this runbook (config yes, secrets via your secret manager) |

---

## Part 1 — What works today

### 1.1 Local mode (SQLite)

**Status: works today.**

SQLite files must not be copied while a writer is active. Stop `nova serve` /
`nova server` and any running capture, then:

```bash
tar czf novafabric-local-$(date +%F).tar.gz \
    ~/.novafabric/registry.db \
    ~/.novafabric/runs/
```

Restore = untar to the same paths, then verify (see §1.5). If you cannot stop
the writer, use SQLite's online backup (`sqlite3 ~/.novafabric/registry.db
".backup 'registry-backup.db'"`), which is safe against a live database.

### 1.2 Server mode (Postgres)

**Backup: works today** (standard Postgres operations). **Automated restore:
experimental** (ADR-0211).

```bash
# Backup — either the standard tool directly:
pg_dump --format=custom --dbname="$NOVA_DSN" \
        --file=novafabric-$(date +%F).dump
# …or the verified backup set (adds manifest + hashes + optional DSSE,
# plus the pg_dump version and per-table row counts used by restore
# verification — ADR-0211 D6):
nova backup create --profile pg --dsn "$NOVA_DSN" -o pg-nightly.tar.gz
```

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
comparator `ok`, storage check). Exit codes: `0` success · `1` a step failed
· `2` usage error · `3` set invalid/wrong profile · `4` tooling
missing/incompatible · `5` target-state refusal. The DSN is treated as a
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
[NovaSeal Key Management Guide](../novaseal-key-management.md) (HSM-backed or
software keystore). Two rules this runbook enforces:

1. **No private key ever enters a data backup.** A restored deployment
   without keys can still *verify* everything; it just cannot *sign* until
   keys are re-provisioned.
2. **Crypto-shredded keys stay deleted.** If retention ran `CRYPTO_SHRED`
   ([ADR-0134](../decisions.md)),
   restoring an older key escrow must not resurrect shredded data — check the
   retention decision log before re-importing any archived key material.

### 1.5 Verify after restore — restore is not done until this passes

**Status: works today.** The evidence is hash-chained and signed, so a restore
is *checkable*:

```bash
nova doctor --check-storage        # backend, schema revision, migration state
nova seal log verify               # Merkle log integrity
nova ledger verify                 # accountability ledger chains (ADR-0094)
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

The import is fail-closed (a tampered or incomplete batch refuses whole),
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
and a secret-free backend fingerprint. Create **refuses while the local WAL
has pending un-chained uploads** (`--allow-pending-wal` overrides and records
the gap). Restore verifies every pinned head as an *ancestor* of the live
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
