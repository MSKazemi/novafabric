# NovaFabric Helm chart

Deploys NovaFabric backed by a Postgres metadata store, in one of two modes:
`nova serve` — the read-only dashboard (default) — or `nova server start`, the
multi-user OIDC/RBAC REST API (`mode: server`, see below).

> Status: **experimental.** The default `nova serve` dashboard is the
> experimental Layer-A view; harden the access model (TLS, auth) before exposing
> it publicly. Server mode (`mode: server`) runs the authenticated REST API.

## Install

From the published OCI chart (pushed to GHCR by the release workflow):

```bash
helm install nova oci://ghcr.io/novafabric/charts/novafabric --version <X.Y.Z>
```

From a checkout of the repo:

```bash
helm install nova deploy/helm/novafabric
```

Then port-forward and open the dashboard:

```bash
kubectl port-forward svc/nova-novafabric 4321:4321
# open http://localhost:4321/dashboard  (token printed in `kubectl logs`)
```

## Production: external Postgres

The bundled Postgres is for evaluation only. For production, disable it and point
at a managed instance:

```bash
helm install nova deploy/helm/novafabric \
  --set postgres.enabled=false \
  --set externalDatabase.host=my-pg.example.com \
  --set externalDatabase.username=nova \
  --set externalDatabase.database=nova \
  --set externalDatabase.existingSecret=nova-db \
  --set externalDatabase.existingSecretPasswordKey=password
```

## Server mode (multi-user REST API)

By default the chart runs `nova serve` — the experimental read-only dashboard.
Set `mode: server` to run `nova server start` instead: the multi-user REST API
with OIDC/RBAC over Postgres. Auth is on by default (configure OIDC via env, or
a local token is generated); probes use `/readyz` and `/livez`.

```bash
helm install nova deploy/helm/novafabric \
  --set mode=server \
  --set server.port=7433 \
  --set server.workers=4 \
  --set postgres.enabled=false \
  --set externalDatabase.host=my-pg.example.com \
  --set externalDatabase.existingSecret=nova-db
```

`server.workers > 1` requires Postgres (the CLI refuses SQLite across worker
processes). Provide OIDC settings through `extraEnv`.

## Key values

| Key | Default | Description |
|---|---|---|
| `image.repository` | `ghcr.io/novafabric/novafabric` | Container image |
| `image.tag` | `""` (chart appVersion) | Image tag |
| `mode` | `dashboard` | `dashboard` (`nova serve`) or `server` (`nova server start`) |
| `dbRevision` | `head` | Alembic revision the init container migrates to |
| `serve.port` | `4321` | Dashboard/API port (dashboard mode) |
| `serve.insecure` | `true` | Serve over HTTP with a printed token; front with TLS |
| `serve.topology` / `serve.tv5` | `true` | Enable topology view / TV5 |
| `server.port` | `7433` | REST API port (server mode) |
| `server.workers` | `1` | uvicorn workers (server mode; `>1` needs Postgres) |
| `persistence.enabled` | `true` | PVC for `/data` (capsules + registry + KuzuDB) |
| `persistence.size` | `5Gi` | Data volume size |
| `postgres.enabled` | `true` | Bundle a single-replica Postgres (eval only) |
| `externalDatabase.*` | — | External Postgres (used when `postgres.enabled=false`) |
| `ingress.enabled` | `false` | Create an Ingress |
| `resources` | requests 100m/256Mi | Pod resources |

See [`values.yaml`](values.yaml) for the full list.

## Security defaults

- Runs as non-root (uid/gid/fsGroup 1000), `allowPrivilegeEscalation: false`,
  all capabilities dropped.
- No host networking, no privileged access, no node-level capabilities.
- Schema migration runs in an init container (`nova db upgrade … --revision head`
  by default, via `dbRevision`), matching the Docker entrypoint. The server
  refuses to start when the schema is stamped behind head.
- Readiness/liveness probes use `/readyz` and `/livez` (HTTP GET).
