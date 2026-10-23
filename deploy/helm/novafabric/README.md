# NovaFabric Helm chart

Deploys NovaFabric backed by a Postgres metadata store, in one of two modes:
`nova server start` — the multi-user OIDC/RBAC REST API (`mode: server`, the
default since v0.102.0) — or `nova serve`, the experimental dashboard
(`mode: dashboard`, see below).

> Status: **experimental.** Server mode runs the authenticated REST API; harden
> the access model (TLS at the ingress, OIDC) before exposing it publicly.

## Install

From the published OCI chart (pushed to GHCR by the release workflow):

```bash
helm install nova oci://ghcr.io/mskazemi/charts/novafabric --version <X.Y.Z>
```

From a checkout of the repo:

```bash
helm install nova deploy/helm/novafabric
```

Then port-forward and call the API. In a container the local bearer token is
not printed to the logs (only a fingerprint); read it from the pod:

```bash
kubectl port-forward svc/nova-novafabric 4321:4321
TOKEN=$(kubectl exec deploy/nova-novafabric -- sh -c 'cat "$NOVAFABRIC_HOME/.server-token"')
curl -H "Authorization: Bearer $TOKEN" http://localhost:4321/v0/version
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

By default the chart runs `nova server start` — the multi-user REST API with
OIDC/RBAC (ADR-0230). `mode: dashboard` runs `nova serve` instead; that dashboard
is **not** read-only and has not been since v0.8 — it exposes irreversible
operations including `DELETE /api/runs/{id}` and `POST /api/compliance/pii/erase`.
A dashboard pod must bind `0.0.0.0` for its Service and probes to reach it, and
`nova serve` deliberately refuses a non-loopback bind without `--insecure`
(exit 2). So `mode: dashboard` needs `serve.insecure: true` plus
`serve.acknowledgeInsecureExposure: i-accept-serving-evidence-over-plain-http`
(and TLS terminated at the ingress); without them **`helm template` refuses to
render** instead of producing a pod that would crash-loop. In server mode auth is on by default:
configure OIDC with an `oidc:` block in a mounted `server.yaml` (there are no
OIDC environment variables), or a local token is generated; probes use `/readyz`
and `/livez`.

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
| `image.repository` | `ghcr.io/mskazemi/novafabric` | Container image |
| `image.tag` | `""` (chart appVersion) | Image tag |
| `mode` | `server` | `server` (`nova server start`) or `dashboard` (`nova serve`) |
| `dbRevision` | `head` | Alembic revision the init container migrates to |
| `serve.port` | `4321` | Dashboard/API port (dashboard mode) |
| `serve.insecure` | `false` | Serve over HTTP with a shared token; requires `serve.acknowledgeInsecureExposure` |
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
