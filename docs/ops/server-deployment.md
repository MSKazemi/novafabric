# Server Deployment Guide

This guide covers four deployment scenarios for NovaFabric v0.7. Each scenario
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
