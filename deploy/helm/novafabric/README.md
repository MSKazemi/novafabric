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

## CI: Kind install, upgrade and rollback

**Status: experimental** (the job is new). The
[`helm-kind.yml`](../../../.github/workflows/helm-kind.yml) workflow installs this chart into a
throwaway [Kind](https://kind.sigs.k8s.io/) cluster on a GitHub-hosted `ubuntu-24.04` runner.
It runs on pull requests that touch `deploy/helm/**`, `src/novafabric/server/**`,
`deploy/docker/Dockerfile` or the workflow itself, nightly, and on demand. No external cluster,
registry login or paid account is involved, and nothing is pushed.

The steps live in [`scripts/helm_kind_smoke.sh`](../../../scripts/helm_kind_smoke.sh):

1. Build the image from the pull request's tree and copy it into the Kind node
   (`kind load docker-image`; the chart uses it with `pullPolicy: Never`).
2. Generate a random API token and database password and store them as Kubernetes Secrets. They
   are never printed or passed on a command line, and the job masks them in its log.
3. Start a throwaway Postgres that reads that password, then install the chart in server mode
   with `postgres.enabled=false`, `externalDatabase.existingSecret` and the token pinned through
   `extraEnv` (`NOVAFABRIC_SERVER_TOKEN` from the Secret). Before installing, it checks that the
   rendered manifest contains no chart-managed Secret and no bundled Postgres.

   **Known chart defect, worked around in CI only.** The chart points both
   `NOVAFABRIC_METADATA_DSN` and `NOVAFABRIC_POSTGRES_DSN` at one database, and its init
   container runs only `nova db upgrade --backend postgres`, which migrates the *metadata* track.
   The server's schema-skew guard reads that database as the *registry* track and refuses to
   start (`SchemaSkewError: Database schema revision v005 is not known to this build (head:
   p0003)`), so a stock server-mode install crash-loops. Until the chart is fixed, the job
   creates a second database, migrates it with `nova db upgrade --track registry --backend
   postgres` in a one-off pod, and points `NOVAFABRIC_POSTGRES_DSN` at it through `extraEnv`.
4. Smoke the API through `kubectl port-forward`: `/livez` and `/readyz` return 200,
   `GET /v0/version` returns 200 with the token and 401/403 without a token or with a wrong one.
5. `helm upgrade` with a changed value (`resources.requests.memory`, plus
   `upgradeAcknowledged: true`), check the Deployment picked it up, smoke again, and check that
   the data PVC and a marker file written to it survived.
6. `helm rollback` to revision 1, check the value is back, smoke again and repeat the PVC check.
7. Check that neither generated value appears in any container log, `helm get values --all` or
   `helm get manifest`.

When a step fails, the job uploads a `helm-kind-diagnostics` artifact (kept 7 days): recent pod
logs, events, pod and PVC descriptions, and Helm history. Secrets are listed by name only, and any
collected file that contains a generated value is dropped.

Not covered yet: upgrading from the previous *published* chart and image, installing with
production values, ingress/TLS, OIDC, and multi-node clusters.

To run the same steps against a local Kind cluster you created yourself:

```bash
docker build -f deploy/docker/Dockerfile -t novafabric-ci:kind .
kind create cluster --name nova-helm-ci
kind load docker-image novafabric-ci:kind --name nova-helm-ci
bash scripts/helm_kind_smoke.sh run          # needs kubectl, helm 3, curl, jq
kind delete cluster --name nova-helm-ci
```

## Security defaults

- Runs as non-root (uid/gid/fsGroup 1000), `allowPrivilegeEscalation: false`,
  all capabilities dropped.
- No host networking, no privileged access, no node-level capabilities.
- Schema migration runs in an init container (`nova db upgrade … --revision head`
  by default, via `dbRevision`), matching the Docker entrypoint. The server
  refuses to start when the schema is stamped behind head.
- Readiness/liveness probes use `/readyz` and `/livez` (HTTP GET).
