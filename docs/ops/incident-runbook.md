# Incident Runbook (on-call)

Symptom → diagnosis → action, for the operator on call for a NovaFabric
deployment (`nova server` API, `nova serve` dashboard, or a local install).
Labels follow the [docs honesty rule](../../CLAUDE.md): **works today**,
**experimental**, **planned**, or **future design**. Every command below is
part of the shipped CLI (check `--help` for exact flags on your version —
most of these surfaces are **experimental**).

First moves for almost any incident:

```bash
nova doctor --check-storage      # backend, schema revision, migration state
nova support-bundle              # secret-safe diagnostics tarball (§8)
```

---

## 1. Server won't start / `/readyz` failing

**Status: experimental**
([ADR-0182](../../design/adr/0182-self-observability-surface.md)).

The two probes mean different things:

- **`/livez`** — process liveness only. Failing ⇒ the process is wedged:
  restart it. It performs *no* dependency checks.
- **`/readyz`** — dependency readiness, with each check **itemized in the
  JSON body**, e.g.
  `{"checks": {"db": "ok", "migrations": "ok", "object_store": "skipped"}}` —
  a failing probe names its cause. (`/health` remains as a compatibility
  alias.)

| Symptom | Diagnosis | Action |
|---|---|---|
| Process exits at startup, key-file error | Offline key file is world-readable — the server refuses to start | `chmod 600` the `offline_key_path` file; owner must be the server user |
| Process exits at startup, config error | Bad `nova-server.yaml` / env override | Check resolution order: `--config` flag → `$NOVA_SERVER_CONFIG` → `~/.config/novafabric/nova-server.yaml` → `/etc/novafabric/nova-server.yaml` ([Server Deployment Guide](server-deployment.md)) |
| Bind error | Port 7433 (server) / 4321 (serve) already in use | `--port`, or find the stale process. Never start a second writer against the same DB ([ADR-0180](../../design/adr/0180-ha-and-zero-downtime-upgrade-posture.md) fencing invariant) |
| `/readyz` → `"db": "fail"` | Postgres unreachable / DSN wrong / pool exhausted | Verify `$NOVA_DSN`, DB up, network path; `nova doctor --check-storage --backend postgres --postgres-dsn "$NOVA_DSN"` |
| `/readyz` → `"migrations": "fail"` | Schema behind the code (upgrade rolled out without migrating) | `nova db upgrade` (or `alembic -c alembic-postgres.ini upgrade head`), then re-probe. See [Upgrade Guide](upgrade-guide.md) §3 |
| `/readyz` → `"object_store": "fail"` | Configured WORM store unreachable | Check endpoint/credentials; see §4. (`"skipped"` = not configured — that is normal, not a failure) |
| `/livez` OK but `/readyz` red for minutes | Dependency down, process healthy | Fix the named dependency; do **not** restart-loop the pod — liveness is fine |
| Kubernetes restart-loops the pod | Probes pointed at the wrong endpoint | Liveness must target `/livez`, readiness `/readyz` — a wedged-DB pod is *not-ready*, not *dead* |

---

## 2. Auth lockout

**Status: experimental**
([ADR-0184](../../design/adr/0184-secure-by-default-local-server-auth.md),
[ADR-0060](../../design/adr/0060-role-management-http-surface.md)).

| Symptom | Diagnosis | Action |
|---|---|---|
| Everything returns 401 after upgrading to ≥ v0.61 (local mode) | Breaking default: with OIDC off, the server now requires the local bearer token | Read the token from `~/.novafabric/.server-token` (`$NOVAFABRIC_HOME/.server-token`, mode 0600) — also printed at startup. Pin a stable one via `NOVAFABRIC_SERVER_TOKEN` for CI/Docker |
| Lost the local token | Token file is the credential | It survives restarts in `.server-token`; with shell access to the host you can read it. To force a known value, set `NOVAFABRIC_SERVER_TOKEN` and restart (env var wins over the file) |
| Role revoke returns **409 Conflict** | The **last-admin guard**: `rbac_store.revoke_role` refuses any revoke that would leave zero `admin` rows in `role_assignments` while no OIDC issuer is configured (`LastAdminError`, ADR-0060) | This is working as designed. Assign a second admin first (`nova server assign-role`), then revoke |
| SCIM deprovision returns SCIM 409 | Same guard: a deprovision that would remove the last admin is refused and nothing is mutated | Ensure another admin exists before deactivating that user in the IdP |
| No admin exists at all (e.g. someone edited the DB directly) | The guard only protects the HTTP surface; direct `sqlite3`/SQL edits can bypass it | With shell/DB access, re-insert an admin row into `role_assignments`, or use `nova server assign-role <subject> admin` on the host. Restrict filesystem access to the DB to prevent this class |
| OIDC logins fail after IdP key rotation | Stale JWKS cache | `nova server flush-jwks-cache --server <url>` ([Server Deployment Guide — Scenario 3](server-deployment.md)) |
| Air-gapped/CI identity broken | Offline token expired or revoked | Issue a new one: `nova server issue-token --subject <who> --roles <roles> --expires-in <days>d`; revocations live in the `token_audit` table (`nova server revoke-token <jti>`) |

---

## 3. Clients getting 429s (rate limits / quotas)

**Status: experimental**
([ADR-0179](../../design/adr/0179-api-rate-limiting-quotas.md)). Both
mechanisms are **off by default** — if you see 429s, someone enabled
`server.rate_limits` in the config. Two distinct 429s:

| Signal | Meaning | Action |
|---|---|---|
| `429`, `error.code = "rate_limited"`, with `Retry-After` + `X-RateLimit-Limit/-Remaining/-Reset` headers | Token-bucket rate limit hit (per principal → tenant → client-IP; separate `ingest`/`read`/`admin` classes) | Client should honor `Retry-After`. Operator: identify the hot key (sustained limiting emits an audit event per window), raise the class budget in `server.rate_limits`, or fix the runaway client |
| `429`, `error.code = "quota_exceeded"`, **no** `Retry-After` | **Hard storage quota** (capsule count or total bytes) — does not decay on a clock | Free space (retention/[ADR-0134](../../design/adr/0134-data-retention-policy-scheduler.md)), raise the quota, or accept the block. Usage is derived from the capsule store with a short TTL cache — recounts lag by seconds |
| Writes succeed but carry `X-NovaFabric-Quota-Warning: <kind> <usage>/<limit>` | **Soft** quota crossed — warn-then-reject phase | Act now, before the hard limit: this is the early warning, and it also emits one audit event per window |
| Probes failing under load | Should be impossible | `/health`, `/livez`, `/readyz`, `/metrics` are **never** rate limited by contract — if a probe 429s, that is a bug: file it |

Buckets are in process memory: they reset on restart, and a restart is a
(crude) way to clear a wedged limiter.

---

## 4. Full disk / WORM store issues

**Status: local paths work today; WORM object-store tier experimental.**

| Symptom | Diagnosis | Action |
|---|---|---|
| Captures failing, SQLite errors, server 5xx | Disk full under `~/.novafabric` (capsules, `registry.db`, Merkle log) | Free space first. Apply retention policies rather than hand-deleting capsule directories — capsules are the evidence; deletion should itself be recorded ([ADR-0134](../../design/adr/0134-data-retention-policy-scheduler.md)). Then run the §5 drift checks |
| WORM writes rejected | Bucket unreachable, credentials expired, or Object Lock/immutability policy blocking a non-compliant write | Check `/readyz`'s `object_store` check, endpoint config and credentials. Remember WORM is *supposed* to reject overwrites/deletes of locked objects — verify you are not trying to mutate immutable evidence |
| "Cleanup" of the WORM bucket fails | Object Lock / legal hold doing its job | Working as designed. Retention on WORM data is policy-driven, not `rm`-driven |
| Metadata DB lost or corrupted, WORM bucket intact | Derived state lost; evidence safe | The metadata DB is rebuildable from the object store's manifest chain: `nova rebuild-metadata-db --backend <s3\|minio\|ceph_rgw\|azure_blob\|local> --target-db <path>`; then `nova doctor --check-storage`. Losing the app node loses no evidence (ADR-0180 D2) |

---

## 5. Capsule index drift (dashboard shows wrong/missing runs)

**Status: works today.** The dashboard's `runs_cache` table (inside
`registry.db`) is **a cache, never the source of truth** — it is always
rebuildable from the capsule filesystem
(`src/novafabric/registry/runs_cache.py`).

| Symptom | Diagnosis | Action |
|---|---|---|
| Runs on disk but missing in the dashboard (or vice versa) | `runs_cache` drifted from the capsule dir (e.g. capsules copied in/removed out-of-band) | Restart `nova serve` — it rebuilds the runs index from capsule files on startup and keeps it current via the background stats-refresh thread. Confirm `--capsule-dir`/`--db-path` point where you think they do |
| Lineage queries missing edges for capsules you restored/copied in | Lineage graph is also a derived cache | Re-import: `nova lineage import <capsule-dir>...` (imports lineage edges from capsule directories) |
| Doubt about DB health itself | — | `nova doctor --check-storage` (schema version, migration state, per-table row counts); server-mode rebuild path: §4 last row |

---

## 6. Seal verification failures (`nova verify`)

**Status: works today.** `nova verify <capsule-dir>` checks three layers in
the capsule's `.seal/` directory — DSSE ECDSA P-256 signature, RFC 3161
timestamp (structural hash), Merkle log inclusion — printing each check and
**exiting 0 only if all pass, 1 otherwise** (there are no distinct per-cause
exit codes; read the itemized ✓/✗ output).

| Output | Diagnosis | Action |
|---|---|---|
| `No .seal/ directory found` (exit 1) | Capsule was never sealed — NovaSeal is opt-in | Not tampering. Configure `~/.novafabric/novaseal.yaml` (or `NOVAFABRIC_SEAL_CONFIG`) so future captures are sealed ([NovaSeal configuration](../novaseal-configuration.md)) |
| `NovaSeal is not configured` (exit 1) | Verification host has no seal config, so it cannot locate the Merkle DB | Point `--seal-config`/`NOVAFABRIC_SEAL_CONFIG` at the same profile (esp. `merkle_db:`) used at signing time |
| `Signature … FAIL` | Capsule content changed since signing, or wrong key/cert profile | Treat as a potential integrity incident: preserve the capsule, compare against backups, check which profile signed it |
| `Timestamp … FAIL` | Timestamp token missing/damaged — e.g. capsule sealed with no `tsa_url` (air-gapped installs legitimately skip RFC 3161) | Check whether your seal profile sets `tsa_url`; if it never did, an absent timestamp is expected, not tampering |
| `Merkle log inclusion … FAIL` | Verifier's Merkle DB is not the one that recorded the seal (common on shared FS/multi-host setups), or the log is damaged | Verify `merkle_db:` paths match across hosts; audit the log itself with `nova seal log verify` (sampled; `--full` for O(N) re-hash) |
| Sigstore backend errors | `--backend sigstore` needs the `novafabric[sigstore]` extra and a stored bundle; Rekor/Fulcio need network | Use the local backend in air-gapped deployments ([Air-gapped guide](air-gapped-install.md)) |

Escalation: repeated signature failures across capsules ⇒ treat as key
compromise until proven otherwise —
[NovaSeal key management](../novaseal-key-management.md) has the compromise
procedure.

---

## 7. Restoring from backup

Follow the [Backup & Restore Runbook](backup-restore.md) — do not improvise:

- Local-profile sets: `nova restore <set.tar.gz>` (verifies first, extracts
  safely, migrates to head, replays crypto-shreds, then runs the
  verification chain — it reports ok **only** when verification passes).
- Postgres: `pg_restore --clean --if-exists` per runbook §1.2, then
  `nova db upgrade` and the §1.5 verification chain.
- **A restore is not done until verification passes** (`nova doctor
  --check-storage`, `nova seal log verify`, `nova ledger verify`,
  spot-check `nova validate`).

---

## 8. Collecting a support bundle

**Status: experimental**
([ADR-0187](../../design/adr/0187-support-bundle-diagnostics.md)). Works in
local mode and offline.

```bash
nova support-bundle                          # ./nova-support-bundle-<ts>.tar.gz
nova support-bundle -o /tmp/diag.tar.gz --log-window-hours 24
```

Allowlist-only and deny-by-default: doctor output, versions, env-var
**names** only, health snapshot, secret-redacted config, bounded recent logs
(via `NOVAFABRIC_LOG_DIR` / `$NOVAFABRIC_HOME/logs`), plus a manifest with
the SHA-256 of every member and the redaction-ruleset version. It never
contains tokens, keys, credentials, capsule payloads, prompts, responses, or
env-var values — safe to attach to a ticket as-is.
