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

## Production values

**Status: experimental** (the chart as a whole). [`values-production.yaml`](values-production.yaml)
is a layered starting point for a production install. Use it on top of the stock values; it
needs two values you supply:

```bash
kubectl create secret generic nova-db --from-literal=password='<db password>'
helm install nova deploy/helm/novafabric \
  -f deploy/helm/novafabric/values-production.yaml \
  --set externalDatabase.host=my-pg.example.com \
  --set externalDatabase.existingSecret=nova-db
```

From the published OCI chart, `helm pull oci://ghcr.io/mskazemi/charts/novafabric --version <X.Y.Z> --untar`
gives you the same file at `novafabric/values-production.yaml`.

What it sets (each item **works today** at render time; CI renders the file with real Helm in
the `release-toolchain` workflow, but no cluster install is exercised by this repository's CI):

| Concern | Setting |
|---|---|
| Component | `mode: server` (`nova server start`, auth on); `serve.insecure: false` |
| Database | `postgres.enabled: false` — no bundled evaluation Postgres |
| Secrets | `externalDatabase.requireExistingSecret: true` — `helm template` **refuses** to render when `existingSecret` is empty, when `postgres.enabled=true`, or when `externalDatabase.password` is set. No chart-managed Secret is rendered. |
| Container | non-root uid/gid 1000, `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, all capabilities dropped, `privileged: false`, seccomp `RuntimeDefault` (pod and container) |
| Writable paths | `/data` (PVC) and an `emptyDir` at `/tmp` (`tmpVolume.sizeLimit: 1Gi`) in both the app and the migration container. The audit log (`NOVAFABRIC_AUDIT_LOG_PATH=/data/audit/audit.jsonl`) and signing keyring (`NOVAFABRIC_KEYRING_DIR=/data/keyring`) are moved off the read-only home directory. |
| Resources | requests `250m` CPU / `512Mi`, limit `2Gi` memory, no CPU limit — estimates, not benchmarks ([sizing guide](../../../docs/ops/sizing-guide.md)) |
| Persistence | PVC on, `20Gi` starting size |
| Probes | unchanged from the chart: `/readyz` (readiness) and `/livez` (liveness) |
| Exposure | `ingress.enabled: false`, Service `ClusterIP` |

What it does **not** provide, and you must:

- **TLS and ingress.** No ingress is created. Add your own `ingress:` block with a `tls:` entry,
  or put the Service behind your gateway.
- **OIDC.** Configured only through the `oidc:` block of a `server.yaml`. Mount one from a
  Secret with `extraVolumes` / `extraVolumeMounts` at
  `/home/nova/.config/novafabric/server.yaml` (the image user's default config path; the
  commented example in the file has not been exercised in a cluster by this repository).
  Without it the server runs in local-token mode, auth still on.
- **NetworkPolicy.** The chart has no NetworkPolicy template; apply one from your platform.
- **Postgres operations** — backups, HA, and rotating the password (update the Secret, then
  `kubectl rollout restart deploy/<release>-novafabric`; the pod reads it at start).
- **Upgrades.** `helm upgrade` refuses until `upgradeAcknowledged: true` is set
  (ADR-0230 D5; the refusal message lists the values); that is unchanged by this file.

## Production: external Postgres (with `--set`)

The bundled Postgres is for evaluation only. For production, disable it and point
at a managed instance (or use `values-production.yaml` above, which also hardens the pod):

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
processes). OIDC cannot be set through `extraEnv` — no OIDC environment variables
exist; mount a `server.yaml` with `extraVolumes` / `extraVolumeMounts` (see
[Production values](#production-values)).

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
| `externalDatabase.requireExistingSecret` | `false` | Refuse to render unless the DB password comes from `existingSecret` |
| `securityContext.readOnlyRootFilesystem` | `false` | When `true`, an `emptyDir` is mounted at `/tmp` |
| `tmpVolume.sizeLimit` | `""` | Size limit of that `/tmp` volume |
| `extraVolumes` / `extraVolumeMounts` | `[]` | Extra pod volumes / nova-container mounts (e.g. `server.yaml`) |
| `ingress.enabled` | `false` | Create an Ingress |
| `resources` | requests 100m/256Mi | Pod resources |

See [`values.yaml`](values.yaml) for the full list.

## Security defaults

- Runs as non-root (uid/gid/fsGroup 1000), `allowPrivilegeEscalation: false`,
  all capabilities dropped.
- No host networking, no privileged access, no node-level capabilities.
- Schema migration runs in an init container. In server mode it runs
  `nova db upgrade --track registry --backend postgres --revision head` (`dbRevision`
  overrides the revision): the registry track is the one `nova server start` checks,
  and the server refuses to start when it is stamped behind head. The MetadataStore
  track is **not** run in server mode — both tracks share one `alembic_version`
  table in the same database, and its stamp (`v005`) made the server refuse to start
  with `SchemaSkewError` (fixed after v0.109.0). The server creates the
  MetadataStore tables it uses itself on first use. Dashboard mode still runs
  `nova db upgrade --backend postgres`, as the Docker entrypoint does.
- A database already stamped `v005` by an earlier chart is **not** repaired by
  upgrading the chart: `--track registry` cannot locate `v005`. See
  [upgrade guide §4a](../../../docs/ops/upgrade-guide.md#4a-startup-schema-skew-guard-experimental-adr-0211).
- Readiness/liveness probes use `/readyz` and `/livez` (HTTP GET).
- The stock `values.yaml` keeps a writable root filesystem and a bundled Postgres
  whose password is the literal `nova`: an **evaluation** default. Use
  [`values-production.yaml`](#production-values) for anything else.

## Versioning

`Chart.yaml` `version` and `appVersion` both equal the package version in
`pyproject.toml`, so `helm install` from a checkout deploys the image of that
release. `tests/docs/test_release_metadata_is_consistent.py` fails when they
drift, and `publish-chart.yml` refuses to publish a tag that disagrees with them.
