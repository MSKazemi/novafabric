# Server Deployment Guide

This guide covers six deployment scenarios. Each scenario
is labelled with its current implementation status per the [docs honesty
rule](../../CONTRIBUTING.md#documentation-status-labels): **works today**, **experimental**, **planned**, or
**future design**.

Sizing the host first? The [capacity planning & sizing guide](sizing-guide.md) covers
storage, memory and CPU from a single laptop to a shared Postgres server.

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

The experimental dashboard is a separate optional extra:

```bash
pip install 'novafabric[serve]'
nova serve --experimental        # opens http://127.0.0.1:4321
```

**Status: experimental.** See [ADR-0027](../decisions.md)
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

Create `~/.config/novafabric/server.yaml`, or pass `--config <path>` to
`nova server start`. There is no other search path: when neither exists, the
built-in defaults plus the `NOVAFABRIC_SERVER_*` environment overrides apply.

```yaml
# ~/.config/novafabric/server.yaml — host and port are TOP-LEVEL keys.
# (A nested `server:` block is silently ignored.)
backend: postgres
host: "0.0.0.0"
port: 7433
```

Provide the DSN as an environment variable. Secrets are **never** read from the
YAML file — `postgres_dsn`, `offline_key_path`, `scim_token` and `local_token`
are dropped from it on load:

```bash
export NOVAFABRIC_POSTGRES_DSN="postgresql://nova_user:s3cr3t@db.example.com:5432/novafabric"
```

See [ADR-0029](../decisions.md) for the config design and the
[config file reference](#config-file-reference) below for every key.

### Start the server

```bash
nova server start --backend postgres
# or rely on the config file:
nova server start
```

The startup banner prints the resolved backend and port:

```
Starting NovaFabric server on 0.0.0.0:7433 [backend=postgres, workers=1]
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
  --target "$NOVAFABRIC_POSTGRES_DSN" \
  --log migration.jsonl

# Verify the result
nova doctor --check-storage --backend postgres
```

The migration is idempotent — safe to re-run after a partial failure. It uses
upsert semantics so no rows are duplicated. The SQLite file is never modified or
deleted. See [ADR-0016](../decisions.md) for the
migration design.

Exit codes: `0` = success, `1` = row-count mismatch, `2` = connection error.

### Storage health check

```bash
nova doctor --check-storage
nova doctor --check-storage --backend postgres --postgres-dsn "$NOVAFABRIC_POSTGRES_DSN"
```

Output includes backend name, schema version (Alembic), migration status, and
per-table row counts.

---

## Scenario 3 — OIDC setup

**Status: experimental.** The OIDC integration is implemented and tested
against mock OIDC servers. It has not been validated in production against
Keycloak, Auth0, or Okta. The config keys are stable (ADR-0029) but the
behaviour under edge cases (clock skew, key rotation races) may change.

### Config

Add the `oidc` block to your `server.yaml`. OIDC has exactly two keys and is
**on when both are set** — there is no `enabled` flag:

```yaml
backend: postgres
host: "0.0.0.0"
port: 7433
oidc:
  issuer_url: "https://keycloak.example.com/realms/nova"
  audience: "nova-server"
```

- Roles are read from the `nova_roles` claim (falling back to `roles`); the claim
  name is not configurable.
- The JWKS is cached for 3600 s; the interval is not configurable (flush it on key
  rotation, below).
- OIDC is configured **in YAML only** — there are no OIDC environment variables.

### Login from the CLI

**Not usable against a production server today.** `nova login` speaks only to
the server's own *demo* device-grant endpoints (`/v0/auth/device/code`), which
are mounted only when `NOVAFABRIC_SERVER_DEMO_DEVICE_GRANT=1` (off by default).
That demo flow is local-testing scaffolding: the HS256 tokens it mints are
**never accepted** by the real verifier, and it does not contact your IdP.

For a real OIDC deployment, obtain an access token from your identity provider
and pass it as `Authorization: Bearer <token>`; for automation use an API key or
an [offline token](#scenario-4--airgapped--offline-token-mode).

```bash
nova login --server http://nova.example.com:7433   # demo device grant only
```

Against a server without the demo grant, `nova login` exits 1 with that
explanation (not a bare `404`); after a demo login it warns on stderr that the
stored token is not accepted by the server's API.

Credentials are stored in `~/.config/novafabric/credentials.json` at mode `0600`.

### Log out

```bash
nova logout --server http://nova.example.com:7433   # remove one server
nova logout                                           # remove all servers
```

### JWKS cache management

The server caches the OIDC provider's JWKS for 3600 seconds (fixed). On key rotation, flush the cache without restarting the server:

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

`nova server issue-token` generates an ed25519 keypair on first use when
`--key-path` (default `~/.novafabric/keys/offline-key.pem`) does not exist yet,
writing the private key and a `.pub` public key beside it. The private key signs
JWTs locally; the server verifies them with the public key, without contacting
any external endpoint.

**The server accepts offline tokens only when it is started with
`NOVAFABRIC_OFFLINE_KEY_PATH` pointing at the public key** (environment only —
the YAML key is ignored). Without it, every offline JWT is rejected with 401;
`issue-token` prints this reminder on stderr.

```bash
export NOVAFABRIC_OFFLINE_KEY_PATH=/etc/novafabric/keys/offline-key.pub
nova server start --backend postgres
```

### Generate an offline token

```bash
nova server issue-token \
  --subject slurm-job-launcher@hpc.example.com \
  --roles writer \
  --expires-in 90d \
  --key-path /etc/novafabric/keys/offline-key.pem
```

The command prints the raw JWT — and nothing else — on stdout; every status
line (including the keypair-generation messages on the very first call) goes to
stderr, so `$(…)` captures only the token. Store it securely:

```bash
TOKEN=$(nova server issue-token --subject ci-runner --roles writer --expires-in 30d)
```

For SLURM jobs, pass the token via a job environment variable. `sbatch` copies its own
environment into the job by default (`--export=ALL`), so submit from a minimal environment
that carries the token and nothing else from your shell:

```bash
env -i PATH="$PATH" HOME="$HOME" NOVA_TOKEN="$TOKEN" sbatch job.sh
```

`issue-token` also reads its key path from `NOVAFABRIC_OFFLINE_KEY_PATH` when
`--key-path` is not given. The variable names **one keypair by either of its
files**: given `offline-key.pub`, `issue-token` signs with the private key next to
it (`offline-key.pem`, else `offline-key.key`, else `offline-key`), so the same
export works for the server and for issuing (works today):

```bash
NOVAFABRIC_OFFLINE_KEY_PATH=/etc/novafabric/keys/offline-key.pub \
  nova server issue-token --subject user@example.com --roles reader
```

On a verify-only host that holds just the `.pub`, `issue-token` refuses with
exit 2 and names the missing private key; it never generates a new keypair over
a public key the server trusts. (Before 2026-10-23 it read the `.pub` as a private
key and failed, or — when the `.pub` did not exist yet — wrote the public key over
the private one.) Service-account creation on the server follows the same rule:
it needs the private key and answers 400 when the server was given only the `.pub`.

### Revoke a token

Each issued token has a `jti` (token ID) claim. Revoke it by ID:

```bash
nova server revoke-token <token-id>
```

Revocation is recorded in a SQLite file next to the key,
`<key-dir>/offline-key-tokens.db`, whatever the server backend. The server sees
a revocation only if it reads that same file, so revoke on the host whose key
directory the server uses. Revoked tokens return HTTP 401 on the next API call.

### Config for offline mode

There is no YAML for the offline key: `offline_key_path` in the config file is
ignored. Set `NOVAFABRIC_OFFLINE_KEY_PATH` in the server's environment (above).
Keep the private key owned by the issuing user at mode `0600` (it is generated
that way); the server does not check the key file's permissions itself.

### Native listener TLS (ADR-0241, experimental)

Both `nova server start` and `nova serve` can terminate TLS themselves — for
deployments with no reverse proxy in front (HPC login/compute nodes, plain
VMs):

```yaml
# server.yaml
tls:
  enabled: true
  cert_path: /etc/novafabric/tls/server.crt
  key_path: /etc/novafabric/tls/server.key   # must be chmod 600
```

or `NOVA_TLS_ENABLED=1` + `NOVA_TLS_CERT_PATH` + `NOVA_TLS_KEY_PATH`
(`nova serve` takes `--tls-cert` / `--tls-key`). Works with `--workers N`.
Validation is fail-closed at launch: a missing file or a group/world-readable
key refuses to start rather than silently serving plaintext. Certificate
issuance and rotation stay with your PKI (rotation = replace files, restart);
NovaFabric never operates a CA. The reverse-proxy posture remains fully
supported — pick one termination point, not both.

---

## Scenario 5 — Kubernetes (Helm)

**Status: experimental.** Deploys `nova server start` (the multi-user REST API, with
OIDC/RBAC) by default since v0.102.0, backed by Postgres.

⚠ **The default changed in v0.102.0 (ADR-0230), and an upgrade will refuse until you say
which side you want.** The chart previously shipped `mode: dashboard` with
`serve.insecure: true` — the `nova serve` dashboard bound beyond loopback over plain HTTP,
behind a single shared token that carries full, irreversible power over signed evidence. That
pair matched **none** of [ADR-0042](../decisions.md)'s four named, tested and supported
deployment tiers.

| Values | Effect |
|---|---|
| *(stock)* | `nova server start`, no `--insecure`. Terminate TLS at the ingress. |
| `mode: dashboard` | `nova serve`. A pod must bind `0.0.0.0` for its Service and probes, and `nova serve` refuses a non-loopback bind without `--insecure` (exit 2), so **`helm template` refuses to render** `mode: dashboard` unless `serve.insecure: true` and the acknowledgement below are also set — rather than rendering a pod that crash-loops. Terminate TLS at the ingress in front of it. |
| `serve.insecure: true` | **`helm template` fails** unless `serve.acknowledgeInsecureExposure` is set to `i-accept-serving-evidence-over-plain-http`. |
| upgrading from < v0.102.0 | **fails** until `upgradeAcknowledged: true`; the message carries the exact values that restore the old behaviour. |

The dashboard is **not** read-only and has not been since v0.8 — it exposes
`DELETE /api/runs/{id}`, `POST /api/compliance/pii/erase`, `POST /api/seal/{id}/bypass` and
`POST /api/admin/roles`. Since [ADR-0228](../decisions.md) every route carries a scope, so a
narrower credential can be minted, but the shared token still holds `admin`. Harden the access
model (TLS, auth) before exposing publicly.

### Install distribution channels

NovaFabric ships to three channels from the public repository, each on a `v*` tag:

| Channel | Location | Pull |
|---|---|---|
| **PyPI** | `pypi.org/project/novafabric` | `pip install novafabric` |
| **Container image (GHCR)** | `ghcr.io/mskazemi/novafabric` | `docker pull ghcr.io/mskazemi/novafabric:<X.Y.Z>` |
| **Helm chart (GHCR OCI)** | `oci://ghcr.io/mskazemi/charts/novafabric` | `helm install … oci://ghcr.io/mskazemi/charts/novafabric` |

Images are multi-arch (`linux/amd64`, `linux/arm64`). Docker Hub is an optional
mirror, populated only when a `DOCKERHUB_TOKEN` secret is configured on the repo.

### Verifying release artifacts (supply chain)

Published images and wheels are signed and carry an SBOM + SLSA build
provenance — the provenance platform attesting its own supply chain.

```bash
# Image: keyless cosign signature (Sigstore/Fulcio + Rekor transparency log)
cosign verify ghcr.io/mskazemi/novafabric:<X.Y.Z> \
  --certificate-identity-regexp '^https://github.com/MSKazemi/novafabric/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

# Image: SLSA build provenance + SBOM (attached as OCI referrers)
gh attestation verify oci://ghcr.io/mskazemi/novafabric:<X.Y.Z> \
  --repo MSKazemi/novafabric
cosign download sbom ghcr.io/mskazemi/novafabric:<X.Y.Z>

# Wheel: SLSA provenance for the PyPI distribution
gh attestation verify novafabric-<X.Y.Z>-py3-none-any.whl --repo MSKazemi/novafabric
```

### Quick start (bundled Postgres, evaluation only)

```bash
helm install nova oci://ghcr.io/mskazemi/charts/novafabric --version <X.Y.Z>
kubectl rollout status deploy/nova-novafabric
kubectl port-forward svc/nova-novafabric 4321:4321
# The default mode is `server` (REST API, no /dashboard). The local bearer token
# is NOT printed to the logs in a container (only a fingerprint); read it from
# $NOVAFABRIC_HOME/.server-token inside the pod:
TOKEN=$(kubectl exec deploy/nova-novafabric -- sh -c 'cat "$NOVAFABRIC_HOME/.server-token"')
curl -H "Authorization: Bearer $TOKEN" http://localhost:4321/v0/version
```

### Production (external managed Postgres)

```bash
helm install nova oci://ghcr.io/mskazemi/charts/novafabric --version <X.Y.Z> \
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

> **Dashboard vs. server mode.** The **chart** defaults to server mode
> (`mode: server`, since v0.102.0). The **container image** on its own still
> defaults to `nova serve` (`NOVA_MODE=dashboard`) — the experimental dashboard.
> **It is not read-only:** it exposes irreversible operations including run
> deletion, PII erasure, and seal bypass, all behind a single token. For the
> multi-user REST API with OIDC/RBAC over Postgres, use **server mode** (Scenario 6).

---

## Scenario 6 — Hardened server mode (container / Helm)

Runs the multi-user REST API (`nova server start`, OIDC/RBAC over Postgres)
instead of the dashboard. Auth is **on** by default: configure OIDC through the
`oidc:` block of a mounted `server.yaml` (there are no OIDC environment
variables), or the server generates a local bearer token
(`--insecure-no-auth` is never used). Migrations always upgrade to the packaged
schema **head** — the server refuses to start when stamped behind head, so a
pinned-below revision breaks the deploy.

### Container (Docker / Compose)

Set `NOVA_MODE=server` on the image. It listens on `NOVA_PORT` (default `7433`)
and, with `NOVA_WORKERS>1`, runs that many uvicorn workers (requires Postgres):

The entrypoint waits for Postgres with `pg_isready` against `PGHOST` (default
`postgres`) / `PGPORT` / `PGUSER`, then runs `nova db upgrade --backend postgres`,
which reads `NOVAFABRIC_METADATA_DSN` — so set all of them:

```bash
docker run -e NOVA_MODE=server \
  -e PGHOST=my-pg -e PGUSER=nova \
  -e NOVAFABRIC_POSTGRES_DSN=postgresql://nova:***@my-pg:5432/nova \
  -e NOVAFABRIC_METADATA_DSN=postgresql://nova:***@my-pg:5432/nova \
  -e NOVA_WORKERS=4 \
  -p 7433:7433 ghcr.io/mskazemi/novafabric:<X.Y.Z>
```

For OIDC, mount a `server.yaml` with an `oidc:` block at
`~/.config/novafabric/server.yaml` of the container user (see Scenario 3).

### Helm

```bash
helm install nova oci://ghcr.io/mskazemi/charts/novafabric --version <X.Y.Z> \
  --set mode=server \
  --set server.port=7433 \
  --set server.workers=4 \
  --set postgres.enabled=false \
  --set externalDatabase.host=my-pg.example.com \
  --set externalDatabase.existingSecret=nova-db
```

In server mode the chart's readiness/liveness probes hit `/readyz` and `/livez`
(both modes expose them); `/readyz` gates traffic on database reachability and
schema-skew. Provide OIDC config as a mounted `server.yaml` (the `oidc:` block) —
`extraEnv` cannot enable OIDC, because no OIDC environment variables exist.

---

## How the server authenticates a request

Every API request carries one bearer credential. The server tries the credential
types in this order and stops at the first that applies; a credential that applies
but fails verification is rejected with 401 rather than passed on:

```mermaid
flowchart TD
    R["Request with<br/>Authorization: Bearer …"] --> K{"API key<br/>(nvfk_…)?"}
    K -- valid --> OK["authenticated"]
    K -- "not an API key" --> O{"OIDC configured?<br/>oidc.issuer_url + audience"}
    O -- yes --> J["verify JWT against<br/>the IdP's JWKS"]
    J --> OK
    O -- no --> F{"JWT-shaped and server started with<br/>NOVAFABRIC_OFFLINE_KEY_PATH?"}
    F -- yes --> T["verify offline ed25519 token<br/>+ revocation record"]
    T --> OK
    F -- no --> L["compare with the local token<br/>$NOVAFABRIC_HOME/.server-token"]
    L --> OK
```

`insecure_no_auth: true` (never in production) replaces the last step with anonymous
admin access. Roles then come from the credential, as described below.

---

## Role assignment

**Status: experimental.** Roles are enforced on all server API endpoints per
[ADR-0018](../decisions.md).

The four built-in roles are:

| Role | Permissions |
|---|---|
| `reader` | Read capsules, assets, lineage, replays |
| `writer` | Reader + create/upload capsules and assets, export evidence bundles (written only inside the server's evidence directory) |
| `admin` | Writer + manage roles, tokens, server config |
| `auditor` | Read audit logs and evidence bundles; cannot read raw capsule content |

### Assign a role

```bash
nova server assign-role user@example.com writer
nova server assign-role ci-runner@cluster admin --assigned-by ops-team
```

Roles are stored in the `role_assignments` table. When using OIDC, roles can
also be carried in the `nova_roles` (or `roles`) JWT claim. **The token's own
roles are checked first;** `role_assignments` rows are only a fallback — never for
an API key or an offline token issued with `--roles`, whose roles are their whole
grant — and that fallback is consulted only once the workspace/org model is in use (at least one
scoped membership or service account exists, ADR-0178). ⚠ In a stock deployment
that has neither, a recorded row has **no effect** on OIDC or offline-token
principals. `assign-role` still writes the row (the last-admin guard counts it,
and it takes effect the moment the workspace/org model is in use), but it then
prints `Role '<role>' recorded for '<subject>'` and a `warning: this assignment is
not in force` on stderr instead of claiming success. `GET /v0/admin/roles`
reports the same thing per row as `effective_now`. To grant a role in a stock
deployment today, carry it in the token: the OIDC `nova_roles`/`roles` claim, or
`nova server issue-token --roles …`. `promoter` and `approver` also exist for
separation-of-duties and SCIM group mapping (`nova server scim-map-group`).

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
  would remove the last admin (with no OIDC configured in the server YAML) is refused
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

The config file is `~/.config/novafabric/server.yaml`, or the file passed with
`nova server start --config <path>` — there is no other search path.
(`NOVAFABRIC_SERVER_CONFIG` is set internally to hand the path to `--workers`
child processes; do not set it yourself — `nova server start` overwrites it, or
clears an inherited value when `--config` is absent, so every worker loads the
same file as the parent.) Keys are top-level; the main ones, with
their defaults:

```yaml
backend: sqlite          # "sqlite" (default) or "postgres"
db_path: null            # SQLite registry for every server table (orgs, workspaces, roles,
                         # API keys, SCIM, usage, webhooks); null = ~/.novafabric/registry.db
host: "127.0.0.1"
port: 7433

oidc:                    # on when BOTH are set
  issuer_url: ""
  audience: ""

tls:                     # first-party listener TLS (ADR-0241)
  enabled: false
  cert_path: null
  key_path: null         # must be mode 0600 — the server refuses otherwise

scim:
  enabled: false         # SCIM 2.0 provisioning (ADR-0139, experimental).
  group_role_map: {}     # Also requires $NOVAFABRIC_SCIM_TOKEN (env only).

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
  self_tracing_endpoint: null  # OTLP/HTTP traces URL; null = local serve OTLP ingest
```

Further blocks, each documented with its feature: `ingest` (below), `saml`,
`bulk`, `usage`, `api_keys`, `pagination`, `webhooks`, `step_up`; and the
acknowledgement flags `insecure_no_auth`, `i_know_this_is_public`,
`i_accept_shared_capsule_store`, `demo_device_grant`. There are no `workers`,
`base_url`, `log_level` or `otel_endpoint` keys — worker count is the
`--workers` flag. Unknown keys are ignored silently, so check spelling.

**Environment overrides** are a fixed list, not one per key. The main ones:
`NOVAFABRIC_SERVER_HOST`, `NOVAFABRIC_SERVER_PORT`, `NOVAFABRIC_SERVER_BACKEND`,
`NOVAFABRIC_SERVER_DB_PATH`, `NOVAFABRIC_SERVER_SCIM_ENABLED`,
`NOVAFABRIC_SERVER_METRICS_EXEMPT`, `NOVAFABRIC_SERVER_RATE_LIMITS_ENABLED`, the
`NOVAFABRIC_SERVER_INGEST_*`, `NOVAFABRIC_SERVER_USAGE_*` and
`NOVAFABRIC_SERVER_WEBHOOKS_*` families, `NOVAFABRIC_SERVER_STEP_UP_ENABLED`,
`NOVAFABRIC_SERVER_INSECURE_NO_AUTH`, `NOVAFABRIC_SERVER_I_KNOW_THIS_IS_PUBLIC`,
`NOVAFABRIC_SERVER_I_ACCEPT_SHARED_CAPSULE_STORE`,
`NOVAFABRIC_SERVER_DEMO_DEVICE_GRANT` (full list: `_apply_env_overrides` in
`src/novafabric/server/config.py`).

**Secrets come from the environment only:** `NOVAFABRIC_POSTGRES_DSN`,
`NOVAFABRIC_OFFLINE_KEY_PATH`, `NOVAFABRIC_SCIM_TOKEN`, `NOVAFABRIC_SERVER_TOKEN`
(pins the local bearer token). If you put `postgres_dsn` or `offline_key_path` in
the YAML file it is dropped silently — no warning is logged.

---

## Ingest limits (experimental)

> **Status: experimental** — ADR-0203 P1
> (spec). Applies to
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
  max_concurrent_unpack: 16            # concurrent unpacks per worker (1-256)
```

Env overrides: `NOVAFABRIC_SERVER_INGEST_MAX_UPLOAD_BYTES`,
`NOVAFABRIC_SERVER_INGEST_SPOOL_CHUNK_BYTES`,
`NOVAFABRIC_SERVER_INGEST_ZIP_MAX_ENTRIES`,
`NOVAFABRIC_SERVER_INGEST_ZIP_MAX_UNCOMPRESSED_BYTES`,
`NOVAFABRIC_SERVER_INGEST_ZIP_MAX_RATIO`,
`NOVAFABRIC_SERVER_INGEST_MAX_CONCURRENT_UNPACK`.

- **`max_concurrent_unpack`** (ADR-0262, **works today**) bounds how many capsules
  one worker unpacks at the same time. Unpacking is blocking CPU + disk work; until
  ADR-0262 it ran inline on the worker's event loop, so **each worker served exactly
  one upload at a time** and ingest capacity was `workers / service_time`. A 314-VM
  fleet measured the result: throughput pinned at 61.6 req/s, flat across a 9x span
  of offered load, p99 26.8 s — on a host at ~20% CPU. Nothing was lost (41,774
  uploads, 41,774 stored capsules) — it was slow, not lossy. The work now runs in the
  threadpool, and this key is the bound that keeps a higher concurrency from becoming
  unbounded concurrent extraction. **Raising it raises peak transient disk and memory
  proportionally** (see the transient-disk note below); lower it on a small host.

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
  `<capsule_dir>/.ingest-tmp/`; both are removed on every exit path. Since
  ADR-0262 a worker can have up to `max_concurrent_unpack` extractions in flight,
  so size the volume for that multiple.

**Recommended (normative, ADR-0203 D4):** for any non-loopback deployment,
also enable the [ADR-0179](../decisions.md)
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

# 3. Create the schemas (registry track + MetadataStore track)
export NOVAFABRIC_POSTGRES_DSN="postgresql://user:pass@localhost/novafabric"
export NOVAFABRIC_METADATA_DSN="$NOVAFABRIC_POSTGRES_DSN"
nova db upgrade --track registry --backend postgres
nova db upgrade --backend postgres

# 4. Migrate existing SQLite data (idempotent)
nova migrate-to-postgres \
  --source ~/.novafabric/registry.db \
  --target "postgresql://user:pass@localhost/novafabric" \
  --log ~/migration.jsonl

# 5. Verify row counts match
nova doctor --check-storage --backend postgres \
  --postgres-dsn "postgresql://user:pass@localhost/novafabric"

# 6. Switch the server to Postgres (reads NOVAFABRIC_POSTGRES_DSN from step 3)
nova server start --backend postgres

# 7. Confirm the server is healthy
nova doctor --check-storage --backend postgres
```

The SQLite file is never deleted or modified. Keep it as a backup until the
team has confirmed that Postgres is stable.

Postgres-to-SQLite downgrade migration is explicitly not supported per
[ADR-0016](../decisions.md).
