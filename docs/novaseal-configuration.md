# NovaSeal Configuration Reference

**Status:** Works today (NovaSeal v0.1+, ADR-0041)

This document is the single source of truth for configuring NovaSeal: the
`novaseal.yaml` config file, all environment variables, path resolution order,
and deployment-specific patterns.

Related documents:
- [NovaSeal Key Management Guide](novaseal-key-management.md) — key generation, rotation, HSM setup
- [NovaSeal API Stability Guarantee](novaseal-stability.md) — what changes are breaking
- [Operator Guide](operator-guide.md) — cluster and Docker deployment context
- [CLI Reference § nova seal / nova verify](cli-reference.md#novaseal-linked-envelope-chain-maker-checker)

---

## Table of contents

1. [Configuration file — `novaseal.yaml`](#1-configuration-file--novasealyaml)
2. [Profiles](#2-profiles)
   - 2.1 [local — ECDSA P-256 file key](#21-local--ecdsa-p-256-file-key)
   - 2.2 [aws_kms — AWS KMS asymmetric key](#22-aws_kms--aws-kms-asymmetric-key)
   - 2.3 [azure_kv — Azure Key Vault](#23-azure_kv--azure-key-vault)
   - 2.4 [gcp_kms — GCP Cloud KMS](#24-gcp_kms--gcp-cloud-kms)
3. [Merkle log database (`merkle_db`)](#3-merkle-log-database-merkle_db)
   - 3.1 [Path resolution order](#31-path-resolution-order)
   - 3.2 [Docker and container deployments](#32-docker-and-container-deployments)
   - 3.3 [SLURM and shared-filesystem deployments](#33-slurm-and-shared-filesystem-deployments)
   - 3.4 [Postgres backend (experimental, Scale-S4)](#34-postgres-backend-experimental-scale-s4)
4. [Environment variables — complete reference](#4-environment-variables--complete-reference)
5. [Discovery order for `novaseal.yaml`](#5-discovery-order-for-novasealyaml)
6. [Troubleshooting](#6-troubleshooting)

---

## 1. Configuration file — `novaseal.yaml`

NovaSeal is **opt-in**: if no `novaseal.yaml` is found, NovaSeal is silently
disabled and `nova capture` proceeds without signing. This is the correct
behavior for local development.

The file is discovered automatically (see §5). A minimal example for local
use with a file-based ECDSA P-256 key:

```yaml
# ~/.novafabric/novaseal.yaml
profile: local
key_path: ~/.novafabric/seal.key     # ECDSA P-256 private key (PEM, PKCS#8)
cert_path: ~/.novafabric/seal.crt    # X.509 certificate for the key
tsa_url: https://freetsa.org/tsr     # RFC 3161 timestamp authority (optional)
merkle_db: ~/.novafabric/novaseal-merkle.db  # SQLite Merkle log (optional)
```

All path values are expanded with `~` resolution. All fields except
`profile`, `key_path`, and `cert_path` are optional.

---

## 2. Profiles

### 2.1 `local` — ECDSA P-256 file key

The default for on-premises and laptop deployments. The private key lives on
disk, optionally encrypted by the OS keychain or a secrets manager.

```yaml
profile: local
key_path: /path/to/seal.pem      # required — PKCS#8 PEM, ECDSA P-256
cert_path: /path/to/seal.crt     # required — PEM certificate
tsa_url: https://freetsa.org/tsr # optional; omit to skip RFC 3161 timestamps
merkle_db: /path/to/merkle.db    # optional; see §3
```

Generate a key and self-signed certificate:

```bash
openssl ecparam -name prime256v1 -genkey -noout | \
  openssl pkcs8 -topk8 -nocrypt -out ~/.novafabric/seal.key

openssl req -new -x509 -key ~/.novafabric/seal.key \
  -out ~/.novafabric/seal.crt \
  -days 3650 -subj "/CN=NovaSeal-Local"
```

### 2.2 `aws_kms` — AWS KMS asymmetric key

Uses an AWS KMS ECDSA_SHA_256 key. Requires the `[seal-aws]` optional extra:
`pip install novafabric[seal-aws]`.

```yaml
profile: aws_kms
kms_key_id: arn:aws:kms:us-east-1:123456789012:key/mrk-abc123  # required
aws_region: us-east-1             # optional; default us-east-1
cert_path: /path/to/cert.pem      # required — certificate for the KMS public key
tsa_url: https://freetsa.org/tsr  # optional
merkle_db: /path/to/merkle.db     # optional; see §3
```

### 2.3 `azure_kv` — Azure Key Vault

Uses an Azure Key Vault EC P-256 key. Requires `[seal-azure]`.

```yaml
profile: azure_kv
vault_url: https://myvault.vault.azure.net/  # required
key_name: my-ec-key                          # required
cert_path: /path/to/cert.pem                 # required
tsa_url: https://freetsa.org/tsr             # optional
merkle_db: /path/to/merkle.db               # optional; see §3
```

### 2.4 `gcp_kms` — GCP Cloud KMS

Uses a GCP Cloud KMS EC P-256 asymmetric signing key. Requires `[seal-gcp]`.

```yaml
profile: gcp_kms
key_version_name: projects/P/locations/L/keyRings/R/cryptoKeys/K/cryptoKeyVersions/1
cert_path: /path/to/cert.pem  # required
tsa_url: https://freetsa.org/tsr  # optional
merkle_db: /path/to/merkle.db    # optional; see §3
```

---

## 3. Merkle log database (`merkle_db`)

NovaSeal maintains a tamper-evident SQLite Merkle log of all seal operations.
The `merkle_db` field in `novaseal.yaml` controls where this file lives.

### 3.1 Path resolution order

All three NovaSeal call sites in NovaFabric (`serve/app.py` diagnostic check,
`serve/app.py` SealTab endpoints, `server/routes/seal.py` policy store) resolve
the Merkle DB path through a single canonical function:
`novafabric.trust.novaseal.config.resolve_merkle_db_path()`.

**Precedence (highest to lowest):**

| Priority | Source | When to use |
|---|---|---|
| 1 | `NOVAFABRIC_SEAL_DB_PATH` env var | Explicit override; CI/test environments; Postgres URL (`postgres://...`, experimental — see §3.4) |
| 2 | `merkle_db:` field in `novaseal.yaml` | **Production config** — set this for non-default paths |
| 3 | `~/.novafabric/novaseal-merkle.db` | Default for local laptop use |

If `novaseal.yaml` is not found or cannot be parsed, priority 2 is skipped and
priority 3 applies.

### 3.2 Docker and container deployments

In Docker the default home directory and `NOVAFABRIC_HOME` are typically
different from the host. Set `merkle_db` explicitly in `novaseal.yaml` so the
path always resolves to the mounted volume:

```yaml
# /data/nova/novaseal.yaml  (mounted into the container)
profile: local
key_path: /data/nova/keys/seal.key
cert_path: /data/nova/keys/seal.crt
merkle_db: /data/nova/novaseal-merkle.db
```

```yaml
# docker-compose.yml (excerpt)
services:
  nova-serve:
    environment:
      NOVAFABRIC_SEAL_CONFIG: /data/nova/novaseal.yaml
    volumes:
      - nova-data:/data/nova
```

Setting `NOVAFABRIC_SEAL_CONFIG` ensures the config is found regardless of
where the container's home directory is, which fixes the `nova doctor`
`novaseal_db FAIL` that would otherwise appear when `NOVAFABRIC_HOME` and
`~/.novafabric` diverge.

### 3.3 SLURM and shared-filesystem deployments

For multi-node SLURM jobs, `merkle_db` must point to a path on the shared
filesystem (Lustre, GPFS, NFS) so all compute nodes write to the same log:

```yaml
# /home/$USER/.novafabric/novaseal.yaml
profile: local
key_path: /lustre/project/novafabric/keys/seal.key
cert_path: /lustre/project/novafabric/keys/seal.crt
merkle_db: /lustre/project/novafabric/novaseal-merkle.db
```

SQLite's WAL mode handles concurrent appends from multiple workers within a
single job. For large-scale parallelism (>100 concurrent writers), the
Postgres backend (§3.4) is the appropriate path.

### 3.4 Postgres backend (experimental, Scale-S4)

**Experimental** — implemented (ROADMAP Scale-S4). SQLite remains the default;
the Postgres backend is opt-in via a Postgres DSN. The merkle-log factory
(`open_merkle_log`) routes `postgresql://` / `postgres://` URIs to the
`PostgresMerkleLog` backend.

The `merkle_db` field accepts a Postgres connection URL:

```yaml
merkle_db: postgres://user:pass@pghost/novafabric_seal
```

The `NOVAFABRIC_SEAL_DB_PATH` env var also accepts this format for
programmatic override without editing `novaseal.yaml`.

---

## 4. Environment variables — complete reference

| Variable | Default | Description |
|---|---|---|
| `NOVAFABRIC_SEAL_CONFIG` | `~/.novafabric/novaseal.yaml` | Path to `novaseal.yaml`. Takes precedence over the default location. Set this in Docker/K8s to point to the config on the mounted volume. |
| `NOVAFABRIC_SEAL_DB_PATH` | — | Explicit Merkle DB path (or a `postgresql://` DSN for the experimental Postgres backend, §3.4). Overrides `merkle_db` in `novaseal.yaml`. Intended for CI/test environments. |
| `NOVA_BYPASS_NOTIFY_URL` | — | Webhook URL for bypass-event notifications (see ADR-0059). |
| `NOVASEAL_KMS_ENDPOINT` | — | NovaSeal KMS endpoint for collector batch signing (cluster-scale, experimental). |

**Interaction between `NOVAFABRIC_SEAL_CONFIG` and `NOVAFABRIC_SEAL_DB_PATH`:**

If both are set, `NOVAFABRIC_SEAL_DB_PATH` wins for the Merkle DB path (priority 1),
even if `novaseal.yaml` (pointed to by `NOVAFABRIC_SEAL_CONFIG`) also has a
`merkle_db` field. The YAML `merkle_db` only applies when the env var is absent.

---

## 5. Discovery order for `novaseal.yaml`

`load_signing_profile()` searches in this order:

1. `NOVAFABRIC_SEAL_CONFIG` env var — if set and the file exists, use it.
   If set but the file is missing, raise `SealConfigError` (fail fast; do not
   silently fall through to the default).
2. `~/.novafabric/novaseal.yaml` — if it exists, use it.
3. No config found — NovaSeal is disabled; `load_signing_profile()` returns `None`.

This means NovaSeal is **completely opt-in** at the individual user level.
A system administrator can pre-create `~/.novafabric/novaseal.yaml` in the
user's home directory (e.g., via SLURM prologue or dotfiles management) to
enable NovaSeal transparently.

---

## 6. Troubleshooting

### `nova doctor` shows `novaseal_db FAIL`

The diagnostic check calls `resolve_merkle_db_path()` and tests whether the
returned path exists. Common causes:

| Symptom | Cause | Fix |
|---|---|---|
| Path shown is `/data/nova/novaseal-merkle.db` but the file isn't there | `NOVAFABRIC_HOME=/data/nova` in Docker, but `novaseal.yaml` not found or has no `merkle_db` — so the old default was used incorrectly | Set `merkle_db: /data/nova/novaseal-merkle.db` in `novaseal.yaml` and set `NOVAFABRIC_SEAL_CONFIG` to its path |
| Path shown is `~/.novafabric/novaseal-merkle.db` but the file isn't there | No capsule has been sealed yet — the DB is created on first `nova seal propose` | Run a seal operation, or pre-create the file: `touch ~/.novafabric/novaseal-merkle.db` |
| `novaseal_db FAIL` after setting `NOVAFABRIC_SEAL_DB_PATH` | The env var path doesn't exist yet | Create the parent directory and touch the file, or run a seal operation |

### `SealConfigError: NOVAFABRIC_SEAL_CONFIG points to missing file`

The env var is set but the file at that path doesn't exist. Either create the
file or unset the env var to fall back to `~/.novafabric/novaseal.yaml`.

### NovaSeal silently disabled — no seal directory in capsule

`load_signing_profile()` returned `None` (no config found). Create
`~/.novafabric/novaseal.yaml` or set `NOVAFABRIC_SEAL_CONFIG`.

### Merkle DB path differs between `nova seal` and `nova doctor`

All code paths now use `resolve_merkle_db_path()` from
`novafabric.trust.novaseal.config`. If you observe a discrepancy, verify that
no stale `NOVAFABRIC_SEAL_DB_PATH` env var is set in one shell but not another.

### TSA timeout or `tsa_url` errors

The default `https://freetsa.org/tsr` is a free public TSA with rate limits.
For production, use an organizational or commercial TSA. Set `tsa_url:` in
`novaseal.yaml`. To disable RFC 3161 timestamps entirely, omit the field —
NovaSeal still signs with ECDSA but without a timestamp token.
