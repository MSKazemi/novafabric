# NovaFabric Helm chart

Deploys `nova serve` — the read-only NovaFabric dashboard + REST API — backed by
a Postgres metadata store.

> Status: **experimental.** `nova serve` is the experimental Layer-A dashboard.
> This chart targets evaluation and internal deployments; harden the access model
> (TLS, auth) before exposing it publicly.

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

## Key values

| Key | Default | Description |
|---|---|---|
| `image.repository` | `ghcr.io/novafabric/novafabric` | Container image |
| `image.tag` | `""` (chart appVersion) | Image tag |
| `serve.port` | `4321` | Dashboard/API port |
| `serve.insecure` | `true` | Serve over HTTP with a printed token; front with TLS |
| `serve.topology` / `serve.tv5` | `true` | Enable topology view / TV5 |
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
- Schema migration runs in an init container (`nova db upgrade … --revision v001`),
  matching the Docker entrypoint.
