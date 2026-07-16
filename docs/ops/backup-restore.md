# Backup & Restore Runbook

How to back up and restore a NovaFabric deployment. The first half documents
procedures that **work today** with standard tools; the second half covers the
`nova backup` tooling
([ADR-0181](../../design/adr/0181-backup-restore-dr-tooling.md), accepted
2026-07-16 — `nova backup create`/`verify`, `nova restore` for the **local
profile and the `--profile pg` create path work today (experimental)**;
**restore of a pg-dump set remains the `pg_restore` runbook, §1.2**).

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

**Status: works today** (standard Postgres operations — NovaFabric adds nothing
and requires nothing special).

```bash
pg_dump --format=custom --dbname="$NOVA_DSN" \
        --file=novafabric-$(date +%F).dump
```

- Use `pg_restore --clean --if-exists` into a freshly created database.
- **PITR** is delegated to Postgres: enable WAL archiving /
  `restore_command`, or use your managed provider's point-in-time recovery.
  NovaFabric deliberately does not reimplement this
  ([ADR-0181](../../design/adr/0181-backup-restore-dr-tooling.md)).
- After restore, run migrations to the current head if the binary is newer
  than the dump: `nova db upgrade` (check state first with
  `nova doctor --check-storage`).

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
   ([ADR-0134](../../design/adr/0134-data-retention-policy-scheduler.md)),
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

---

## Part 2 — `nova backup` tooling

Governed by [ADR-0181](../../design/adr/0181-backup-restore-dr-tooling.md)
(accepted 2026-07-16) and specified in
[`design/spec/backup-restore-v0.md`](../../design/spec/backup-restore-v0.md).

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
- `nova restore <set.tar.gz> [--home PATH] [--force]` — **local profile only**:
  runs the spec's normative order — verify the set (no flag to skip it) →
  prepare the home (a non-empty home is refused without `--force`; with it,
  existing data is moved aside into a timestamped `.pre-restore-…/` directory,
  never deleted) → path-traversal-safe extraction → migrations to head →
  **crypto-shred replay** (every applied `CRYPTO_SHRED` in the retention
  decision log is re-applied against the restored DEK store, so shredded data
  cannot be resurrected from an older backup) → the §1.5 verification chain
  (doctor storage check + `seal log verify` when a Merkle log exists). The
  restore reports ok **only when verification passes**; exit 1 otherwise.

**Honest limits (this slice):**

- **Restore of a `pg-dump` set is not automated** — `nova restore` refuses it
  and points here: restore a Postgres deployment with
  `pg_restore --clean --if-exists` per §1.2, then run `nova db upgrade` and
  the §1.5 verification chain.
- The pg create path is verified with a contract test (fake `pg_dump`);
  **verification against a live Postgres remains infra-gated** — run a restore
  drill against a real DSN before relying on it.
- `--profile manifest-only|full` for **WORM** deployments (object-store hash
  listing + manifest-driven rebuild) remains **future design**.

Keys remain excluded by design in every profile, shipped or planned. For
Postgres deployments, Part 1 remains the supported restore procedure.
