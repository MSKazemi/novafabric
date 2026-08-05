# Server Deployment Guide

This guide covers five deployment scenarios for NovaFabric v0.7. Each scenario
is labelled with its current implementation status per the [docs honesty
rule](../../CLAUDE.md): **works today**, **experimental**, **planned**, or
**future design**.

---

## Scenario 1 — Default local install (SQLite, no server)

**Status: experimental.**

The default NovaFabric install requires no server, no database setup, and no
network connectivity. All core commands work out of the box:

```bash
pip install novafabric

nova capture python agent.py
nova validate .novafabric/runs/01HX.../
nova replay .novafabric/runs/01HX.../ --mode forensic
nova lineage provenance <run-id>
nova export-evidence .novafabric/runs/01HX.../ --key key.pem --output evidence.zip
```

The local SQLite registry is created automatically at
`~/.novafabric/registry.db` on first use.

> **Changed in v0.99.0.** The default install is 113 MB / 42 packages instead of
> 412 MB / 50 — `duckdb`, `pyarrow`, `python-louvain` and `clickhouse-connect`
> moved to extras (ADR-0222). Every command above is unaffected. Server and
> container deployments are unaffected too: `[server]`/`[serve]` and the shipped
> Dockerfile pull in what they need. If you relied on importing those packages
> after a plain install, use `pip install 'novafabric[all]'`.

### Optional local dashboard

The experimental read-only dashboard is a separate optional extra:

```bash
pip install 'novafabric[serve]'
nova serve --experimental        # opens http://127.0.0.1:4321
```

**Status: experimental.** See [ADR-0027](../../design/adr/0027-nova-serve-experimental-dashboard.md)
and [`dashboard.md`](../dashboard.md) for limitations.

---

## Scenario 2 — Postgres opt-in

**Status: experimental.**

Operators running a shared team deployment can use Postgres as the storage
backend. SQLite remains the default; Postgres is opt-in.

### Install the server extra

```bash
pip install 'novafabric[server]'
```

### Configure the server

Create `~/.config/novafabric/nova-server.yaml` (or use `$NOVA_SERVER_CONFIG`
to point to any path):

```yaml
# nova-server.yaml
backend: postgres
# Set dsn via $NOVA_DSN env var — do not embed passwords in the config file.
server:
  host: "0.0.0.0"
  port: 7433
```

Provide the DSN as an environment variable (never inline in the YAML in
production):

```bash
export NOVA_DSN="postgresql://nova_user:s3cr3t@db.example.com:5432/novafabric"
```

See [ADR-0029](../../design/adr/0029-server-config-schema.md) for the complete config
schema, env-var override rules, and secrets handling guidance.

### Start the server

```bash
nova server start --backend postgres
# or rely on the config file:
nova server start
```

The startup banner prints the resolved backend and port:

```
Starting NovaFabric server on 0.0.0.0:7433 [backend=postgres]
API docs: http://0.0.0.0:7433/docs
Press Ctrl+C to stop.
```

### Migrate existing SQLite data

To carry existing local capsules into the new Postgres backend:

```bash
# Dry run first — see what would be migrated
nova migrate-to-postgres --dry-run

# Full migration
nova migrate-to-postgres \
  --source ~/.novafabric/registry.db \
  --target "$NOVA_DSN"             \
  --log migration.jsonl

# Verify the result
nova doctor --check-storage --backend postgres
```

The migration is idempotent — safe to re-run after a partial failure. It uses
upsert semantics so no rows are duplicated. The SQLite file is never modified or
deleted. See [ADR-0016](../../design/adr/0016-storage-backend-evolution.md) for the
migration design.

Exit codes: `0` = success, `1` = row-count mismatch, `2` = connection error.

### Storage health check

```bash
nova doctor --check-storage
nova doctor --check-storage --backend postgres --postgres-dsn "$NOVA_DSN"
```

Output includes backend name, schema version (Alembic), migration status, and
per-table row counts.

---

## Scenario 3 — OIDC setup

**Status: experimental.** The OIDC integration is implemented and tested
against mock OIDC servers. It has not been validated in production against
Keycloak, Auth0, or Okta. The API and config keys are stable (ADR-0029) but
the behaviour under edge cases (clock skew, key rotation races) may change in
v0.7.1.

### Config

Add the `oidc` block to your `nova-server.yaml`:

```yaml
backend: postgres
server:
  host: "0.0.0.0"
  port: 7433
oidc:
  enabled: true
  issuer_url: "https://keycloak.example.com/realms/nova"
  client_id: "nova-server"
  audience: "nova-server"
  roles_claim: "nova_roles"       # JWT claim that carries the user's role list
  jwks_refresh_interval: 3600     # seconds between JWKS re-fetches
```

All OIDC keys can be overridden via env vars (see [ADR-0029](../../design/adr/0029-server-config-schema.md) §3):

```bash
export NOVA_OIDC_ENABLED=true
export NOVA_OIDC_ISSUER_URL="https://keycloak.example.com/realms/nova"
export NOVA_OIDC_CLIENT_ID="nova-server"
```

### Login from the CLI

Users authenticate via the Device Authorization Grant (RFC 8628):

```bash
nova login --server http://nova.example.com:7433
```

The CLI prints a URL and user code:

```
  To complete login, visit:
    https://keycloak.example.com/realms/nova/device
  Enter code: ABCD-1234

Waiting for approval …
Logged in to http://nova.example.com:7433 as user@example.com.
Credentials stored in ~/.config/novafabric/credentials.json
```

Credentials (access token, refresh token, expiry) are stored in
`~/.config/novafabric/credentials.json` at mode `0600`. The CLI auto-refreshes
tokens on subsequent commands. Re-run `nova login` when the refresh token
expires.

### Log out

```bash
nova logout --server http://nova.example.com:7433   # remove one server
nova logout                                           # remove all servers
```

### JWKS cache management

The server caches the OIDC provider's JWKS for `jwks_refresh_interval` seconds
(default 3600). On key rotation, flush the cache without restarting the server:

```bash
nova server flush-jwks-cache \
  --server http://nova.example.com:7433 \
  --token "$ADMIN_TOKEN"
```

Or use the stored credential (if logged in as admin):

```bash
nova server flush-jwks-cache --server http://nova.example.com:7433
```

---

## Scenario 4 — Airgapped / offline-token mode

**Status: experimental.** Offline tokens require no OIDC provider. Use this
mode for SLURM batch jobs, air-gapped clusters, or any environment where
reaching an external identity provider is not possible.

### How it works

`nova server` generates an ed25519 keypair on first start if `offline_key_path`
points to a file that does not exist yet. The private key signs JWTs locally;
validation also happens locally without contacting any external endpoint.

### Generate an offline token

```bash
nova server issue-token \
  --subject slurm-job-launcher@hpc.example.com \
  --roles writer \
  --expires-in 90d \
  --key-path /etc/novafabric/keys/offline-key.pem
```

The command prints the raw JWT to stdout. Store it securely:

```bash
TOKEN=$(nova server issue-token --subject ci-runner --roles writer --expires-in 30d)
```

For SLURM jobs, pass the token via a job environment variable:

```bash
#SBATCH --export=ALL
export NOVA_TOKEN="$TOKEN"
```

The key path can also be provided via `NOVAFABRIC_OFFLINE_KEY_PATH`:

```bash
export NOVAFABRIC_OFFLINE_KEY_PATH=/etc/novafabric/keys/offline-key.pem
nova server issue-token --subject user@example.com --roles reader
```

### Revoke a token

Each issued token has a `jti` (token ID) claim. Revoke it by ID:

```bash
nova server revoke-token <token-id>
```

Revocation is recorded in the token audit table (SQLite or Postgres depending
on the active backend). Revoked tokens return HTTP 401 on the next API call.

### Config for offline mode

```yaml
# nova-server.yaml
backend: sqlite
offline_key_path: "/etc/novafabric/keys/offline-key.pem"
# oidc.enabled defaults to false — no OIDC provider needed
```

The key file must be owned by the server process user and have mode `0600`.
`nova server start` refuses to start if the key file is world-readable.

---

## Scenario 5 — Kubernetes (Helm)

**Status: experimental.** Deploys the read-only `nova serve` dashboard + REST API
backed by Postgres. Harden the access model (TLS, auth) before exposing publicly.

### Install distribution channels

NovaFabric ships to three channels from the public repository, each on a `v*` tag:

| Channel | Location | Pull |
|---|---|---|
| **PyPI** | `pypi.org/project/novafabric` | `pip install novafabric` |
| **Container image (GHCR)** | `ghcr.io/novafabric/novafabric` | `docker pull ghcr.io/novafabric/novafabric:<X.Y.Z>` |
| **Helm chart (GHCR OCI)** | `oci://ghcr.io/novafabric/charts/novafabric` | `helm install … oci://ghcr.io/novafabric/charts/novafabric` |

Images are multi-arch (`linux/amd64`, `linux/arm64`). Docker Hub is an optional
mirror, populated only when a `DOCKERHUB_TOKEN` secret is configured on the repo.

### Verifying release artifacts (supply chain)

Published images and wheels are signed and carry an SBOM + SLSA build
provenance — the provenance platform attesting its own supply chain.

```bash
# Image: keyless cosign signature (Sigstore/Fulcio + Rekor transparency log)
cosign verify ghcr.io/novafabric/novafabric:<X.Y.Z> \
  --certificate-identity-regexp '^https://github.com/novafabric/novafabric/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

# Image: SLSA build provenance + SBOM (attached as OCI referrers)
gh attestation verify oci://ghcr.io/novafabric/novafabric:<X.Y.Z> \
  --repo novafabric/novafabric
cosign download sbom ghcr.io/novafabric/novafabric:<X.Y.Z>

# Wheel: SLSA provenance for the PyPI distribution
gh attestation verify novafabric-<X.Y.Z>-py3-none-any.whl --repo novafabric/novafabric
```

### Quick start (bundled Postgres, evaluation only)

```bash
helm install nova oci://ghcr.io/novafabric/charts/novafabric --version <X.Y.Z>
kubectl rollout status deploy/nova-novafabric
kubectl port-forward svc/nova-novafabric 4321:4321
# open http://localhost:4321/dashboard  (access token printed in `kubectl logs`)
```

### Production (external managed Postgres)

```bash
helm install nova oci://ghcr.io/novafabric/charts/novafabric --version <X.Y.Z> \
  --set postgres.enabled=false \
  --set externalDatabase.host=my-pg.example.com \
  --set externalDatabase.existingSecret=nova-db \
  --set ingress.enabled=true \
  --set ingress.hosts[0].host=novafabric.example.com
```

The chart runs non-root by default (uid/gid/fsGroup 1000, all capabilities
dropped) and applies schema migrations via an init container. See
[`deploy/helm/novafabric/README.md`](../../deploy/helm/novafabric/README.md) for
the full values reference.

> **Dashboard vs. server mode.** By default both the container and the chart run
> `nova serve` — the experimental read-only dashboard, which serves over HTTP
> with a printed token (`--insecure`). For the multi-user REST API with
> OIDC/RBAC over Postgres, use **server mode** (Scenario 6). The default is
> unchanged for backward compatibility.

---

## Scenario 6 — Hardened server mode (container / Helm)

Runs the multi-user REST API (`nova server start`, OIDC/RBAC over Postgres)
instead of the dashboard. Auth is **on** by default: configure OIDC via
`NOVAFABRIC_SERVER_*` env, or the server generates a local bearer token
(`--insecure-no-auth` is never used). Migrations always upgrade to the packaged
schema **head** — the server refuses to start when stamped behind head, so a
pinned-below revision breaks the deploy.

### Container (Docker / Compose)

Set `NOVA_MODE=server` on the image. It listens on `NOVA_PORT` (default `7433`)
and, with `NOVA_WORKERS>1`, runs that many uvicorn workers (requires Postgres):

```bash
docker run -e NOVA_MODE=server \
  -e NOVAFABRIC_POSTGRES_DSN=postgresql://nova:***@my-pg:5432/nova \
  -e NOVA_WORKERS=4 \
  -p 7433:7433 ghcr.io/novafabric/novafabric:<X.Y.Z>
```

Extra env for OIDC: `NOVAFABRIC_SERVER_OIDC_ISSUER_URL`,
`NOVAFABRIC_SERVER_OIDC_AUDIENCE` (see Scenario 3).

### Helm

```bash
helm install nova oci://ghcr.io/novafabric/charts/novafabric --version <X.Y.Z> \
  --set mode=server \
  --set server.port=7433 \
  --set server.workers=4 \
  --set postgres.enabled=false \
  --set externalDatabase.host=my-pg.example.com \
  --set externalDatabase.existingSecret=nova-db
```

In server mode the chart's readiness/liveness probes hit `/readyz` and `/livez`
(both modes expose them); `/readyz` gates traffic on database reachability and
schema-skew. Provide OIDC config through `extraEnv` or a mounted secret.

---

## Role assignment

**Status: experimental.** Roles are enforced on all server API endpoints per
[ADR-0018](../../design/adr/0018-auth-model.md).

The four built-in roles are:

| Role | Permissions |
|---|---|
| `reader` | Read capsules, assets, lineage, replays |
| `writer` | Reader + create/upload capsules and assets |
| `admin` | Writer + manage roles, tokens, server config |
| `auditor` | Read audit logs and evidence bundles; cannot read raw capsule content |

### Assign a role

```bash
nova server assign-role user@example.com writer
nova server assign-role ci-runner@cluster admin --assigned-by ops-team
```

Roles are stored in the `role_assignments` table of the active backend (SQLite
or Postgres). When using OIDC, roles can also be carried in the
`nova_roles` JWT claim (configurable via `oidc.roles_claim`). The local
assignment table takes precedence over JWT claims for the same subject.

---

## SCIM provisioning (experimental)

**Status: experimental** (ADR-0139, first slice: discovery + Users). Lets an
enterprise IdP (Okta, Entra ID, OneLogin, Keycloak) push the user lifecycle —
create, deactivate, delete — into the server over standard SCIM 2.0
(RFC 7643/7644). Server-mode only; local mode is structurally unaffected.

### Enable it

SCIM is doubly opt-in and **disabled by default**. Both of these must be set
before the `/scim/v2/*` endpoints exist (otherwise they return plain 404):

```yaml
# server config (ADR-0029)
scim:
  enabled: true
```

```bash
# The dedicated provisioning bearer token — env var only, never YAML.
export NOVAFABRIC_SCIM_TOKEN="<long random secret>"
# (flag can also be set via env: NOVAFABRIC_SERVER_SCIM_ENABLED=true)
```

The IdP authenticates with `Authorization: Bearer $NOVAFABRIC_SCIM_TOKEN`.
This token is provisioning-scoped: it is not a JWT and grants **no** access
to the `/v0/` API, capsules, assets, lineage, or evidence.

### Endpoints (first slice)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/scim/v2/Users` | Provision a user (409 `uniqueness` on duplicate `userName`). |
| `GET` | `/scim/v2/Users/{id}` | Read one user. |
| `GET` | `/scim/v2/Users?filter=…` | List; `eq` filter on `userName`/`externalId`/`active`; `startIndex`/`count` pagination. |
| `PATCH` | `/scim/v2/Users/{id}` | Partial update — `active: false` **de-provisions** (Okta and Entra ID PATCH dialects accepted). |
| `DELETE` | `/scim/v2/Users/{id}` | Hard delete (discouraged; prefer deactivate — it preserves the trail). |
| `POST` | `/scim/v2/Groups` | Provision a group (group→role mapping applied via `nova server scim-map-group`). |
| `GET` | `/scim/v2/Groups/{id}` | Read one group. |
| `GET` | `/scim/v2/Groups?filter=…` | List groups. |
| `PATCH` | `/scim/v2/Groups/{id}` | Partial update (membership add/remove). |
| `PUT` | `/scim/v2/Groups/{id}` | Full-replace update. |
| `DELETE` | `/scim/v2/Groups/{id}` | Delete a group. |
| `GET` | `/scim/v2/ServiceProviderConfig`, `/ResourceTypes`, `/Schemas` | SCIM capability discovery. |

Responses use `application/scim+json` and the RFC 7644 error envelope, not
the NovaFabric `/v0/` error model.

### De-provisioning semantics

- `active: false` (or `DELETE`) revokes **all** of the subject's
  `role_assignments` rows immediately — the same table `nova server
  assign-role` writes; the `userName` is the auth subject.
- The ADR-0060 **last-admin lockout guard** applies: a SCIM deprovision that
  would remove the last admin (with no OIDC issuer configured) is refused
  with a SCIM `409` and *nothing* is mutated — the user stays active and
  keeps its roles.
- Every mutation appends an event (`user.create` / `user.update` /
  `user.deactivate` / `user.delete`, with `roles_before`/`roles_after`) to
  the append-only `scim_audit_events` table.
- PII minimization: only `userName`, `active`, `externalId`, `displayName`
  and `emails` are stored. Other wire attributes (enterprise extension,
  addresses, phone numbers, …) are accepted but dropped, never persisted.

### Group → role mapping (shipped, experimental)

Groups → role mapping (`/scim/v2/Groups`, ADR-0139 P3 / ADR-0190) is
implemented: the `Groups` resource supports POST/GET/list/PATCH/PUT
(full-replace)/DELETE, and group membership maps to RBAC roles via
`nova server scim-map-group <group> <role>` (`--list` / `--remove` supported).
Provisioning events are inspectable with `nova server list-scim-events`. Roles
may still also be assigned via `nova server assign-role` or OIDC claims; SCIM
manages the user lifecycle and *revokes* roles on deprovision.

**Not implemented yet (planned):** the `nova server issue-scim-token` CLI
command — provision the SCIM bearer token via `$NOVAFABRIC_SCIM_TOKEN` for now.

---

## Config file reference

The canonical config file is `nova-server.yaml`. Full key reference:

```yaml
backend: sqlite          # "sqlite" (default) or "postgres"
dsn: ""                  # Postgres DSN — prefer $NOVA_DSN env var
sqlite_path: ""          # SQLite path (default: ~/.novafabric/nova.db)

server:
  host: "127.0.0.1"
  port: 7433
  workers: 1
  base_url: ""           # External URL for OIDC redirect URIs

oidc:
  enabled: false
  issuer_url: ""
  client_id: ""
  audience: ""
  roles_claim: "nova_roles"
  jwks_refresh_interval: 3600

scim:
  enabled: false         # SCIM 2.0 provisioning (ADR-0139, experimental).
                         # Also requires $NOVAFABRIC_SCIM_TOKEN (env only).

rate_limits:             # API rate limiting + storage quotas (ADR-0179, experimental)
  enabled: false         # disabled by default — limiter is fully inert when false
  ingest: { rate: 100, burst: 200 }   # tokens/sec + burst capacity per class
  read:   { rate: 50,  burst: 100 }
  admin:  { rate: 10,  burst: 20 }
  audit_threshold_rejections: 100     # rejections/window that raise an audit event
  audit_window_seconds: 60
  quota:                 # storage quotas (warn-then-reject); absent block ⇒ no checks
    max_capsules_soft: 0 # 0 = unlimited
    max_capsules_hard: 0
    max_bytes_soft: 0
    max_bytes_hard: 0

observability:           # self-observability surface (ADR-0182, experimental)
  metrics_exempt: false  # true serves /metrics without auth (loopback/isolated only)
  self_tracing: false    # opt-in; one OTel span per request, never leaves the deployment
  self_tracing_endpoint: ""  # OTLP/HTTP traces URL; empty = local serve OTLP ingest

offline_key_path: ""     # Path to ed25519 private key PEM

tls:
  enabled: false
  cert_path: ""
  key_path: ""

log_level: "info"        # debug, info, warning, error
otel_endpoint: ""        # OTLP gRPC endpoint; empty = disabled
```

Resolution order (highest priority first):
1. `--config <path>` CLI flag
2. `$NOVA_SERVER_CONFIG` env var (absolute path)
3. `~/.config/novafabric/nova-server.yaml`
4. `/etc/novafabric/nova-server.yaml`

Every key has a corresponding `NOVA_<UPPERCASE_KEY>` env var that overrides
the file value. See [ADR-0029](../../design/adr/0029-server-config-schema.md) §3 for the
complete env-var table.

**Security:** never write secrets (Postgres passwords, private key contents)
directly into the YAML file. Use env vars or key files with restricted
permissions. The server logs a warning if `dsn` is read from the file when
`backend=postgres`.

---

## Ingest limits (experimental)

> **Status: experimental** — ADR-0203 P1
> ([spec](../../design/spec/ingest-hardening-v0.md)). Applies to
> `POST /v0/capsules` only; local-first commands are untouched.

The capsule-upload route is bounded: the request body is size-capped, spooled
to disk in 1 MiB chunks (peak memory per upload is O(chunk), not O(archive)),
extracted atomically via a temp directory + rename, and guarded against zip
bombs and `..`-traversal member names.

Config block (`server.ingest.*`, ADR-0029 conventions; all keys additive —
an absent block means the defaults below):

```yaml
ingest:
  max_upload_bytes: 268435456          # 256 MiB body cap → 413 payload_too_large
  spool_chunk_bytes: 1048576           # 1 MiB read/decompress chunk (min 65536)
  zip_max_entries: 10000               # archive member cap → 422 zip_guard_violation
  zip_max_uncompressed_bytes: 2147483648  # 2 GiB decompressed-total cap
  zip_max_ratio: 100.0                 # compression-ratio cap (per member + total)
```

Env overrides: `NOVAFABRIC_SERVER_INGEST_MAX_UPLOAD_BYTES`,
`NOVAFABRIC_SERVER_INGEST_SPOOL_CHUNK_BYTES`,
`NOVAFABRIC_SERVER_INGEST_ZIP_MAX_ENTRIES`,
`NOVAFABRIC_SERVER_INGEST_ZIP_MAX_UNCOMPRESSED_BYTES`,
`NOVAFABRIC_SERVER_INGEST_ZIP_MAX_RATIO`.

- Rejections use the standard error envelope: HTTP **413**
  `payload_too_large` (body over the cap; `details` carries
  `limit_bytes`/`received_bytes`) and HTTP **422** `zip_guard_violation`
  (`details.reason` is one of `entry_count`, `total_uncompressed`,
  `compression_ratio`, `unsafe_member_name`).
- Uploads larger than 256 MiB now require explicit operator config — raise
  `max_upload_bytes`, or set any key to `0` to disable that limit
  (escape hatch, discouraged).
- Transient disk: up to `max_upload_bytes` (spool) plus the decompressed size
  (temp extract dir) per in-flight upload, under
  `<capsule_dir>/.ingest-tmp/`; both are removed on every exit path.

**Recommended (normative, ADR-0203 D4):** for any non-loopback deployment,
also enable the [ADR-0179](../../design/adr/0179-api-rate-limiting-quotas.md)
request-rate limiter — it stays opt-in and off by default:

```yaml
rate_limits:
  enabled: true    # shipped ingest budget: 100 req/s, burst 200
```

A reverse-proxy body cap (e.g. nginx `client_max_body_size`) is a sensible
*additional* layer, but NovaFabric is safe as shipped without one.

---

## Migration from local to shared Postgres

Full walkthrough:

```bash
# 1. Install the server extra
pip install 'novafabric[server]'

# 2. Provision Postgres and create the novafabric database
createdb novafabric

# 3. Run migrations (Alembic)
NOVA_DSN="postgresql://user:pass@localhost/novafabric" \
  alembic -c alembic-postgres.ini upgrade head

# 4. Migrate existing SQLite data (idempotent)
nova migrate-to-postgres \
  --source ~/.novafabric/registry.db \
  --target "postgresql://user:pass@localhost/novafabric" \
  --log ~/migration.jsonl

# 5. Verify row counts match
nova doctor --check-storage --backend postgres \
  --postgres-dsn "postgresql://user:pass@localhost/novafabric"

# 6. Switch the server to Postgres
export NOVA_DSN="postgresql://user:pass@localhost/novafabric"
nova server start --backend postgres

# 7. Confirm the server is healthy
nova doctor --check-storage --backend postgres
```

The SQLite file is never deleted or modified. Keep it as a backup until the
team has confirmed that Postgres is stable.

Postgres-to-SQLite downgrade migration is explicitly not supported per
[ADR-0016](../../design/adr/0016-storage-backend-evolution.md).
