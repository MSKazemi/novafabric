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

## CI: Kind install, upgrade and rollback

**Status: experimental** (the job is new). The
[`helm-kind.yml`](../../../.github/workflows/helm-kind.yml) workflow installs this chart into a
throwaway [Kind](https://kind.sigs.k8s.io/) cluster on a GitHub-hosted `ubuntu-24.04` runner.
It runs on pull requests that touch `deploy/helm/**`, `src/novafabric/server/**`, the
migrations (`alembic/**`, `src/novafabric/migrations/**`), `src/novafabric/metadata_store/**`,
`pyproject.toml`, `uv.lock`, `deploy/docker/Dockerfile` or the workflow itself, nightly, and on
demand. No external cluster, registry login or paid account is involved, and nothing is pushed.

The steps live in [`scripts/helm_kind_smoke.sh`](../../../scripts/helm_kind_smoke.sh):

1. Build the image from the pull request's tree and copy it into the Kind node
   (`kind load docker-image`; the chart uses it with `pullPolicy: Never`).
2. Generate a random API token and database password and store them as Kubernetes Secrets. They
   are never printed or passed on a command line, and the job masks them in its log.
3. Start a throwaway Postgres that reads that password, then install the chart in server mode
   with `postgres.enabled=false`, `externalDatabase.existingSecret` and the token pinned through
   `extraEnv` (`NOVAFABRIC_SERVER_TOKEN` from the Secret). Before installing, it checks that the
   rendered manifest contains no chart-managed Secret and no bundled Postgres.
4. Smoke the API through `kubectl port-forward`: `/livez` and `/readyz` return 200,
   `GET /v0/version` returns 200 with the token and 401/403 without a token or with a wrong one.
   `GET /metrics` returns 200; it is the call that opens the MetadataStore for a tenant-less local
   token. The script then checks in Postgres that the one database carries a registry-track
   Alembic stamp (`p…`) and the four MetadataStore tables, created by the server's lazy
   bootstrap with row-level security forced. The job uses the chart's own migration step and
   database layout; it adds no CI-only migration or DSN override.
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
- Schema migration runs in an init container. In server mode it runs
  `nova db upgrade --track registry --backend postgres --revision head` (`dbRevision`
  overrides the revision): the registry track is the one `nova server start` checks,
  and the server refuses to start when it is stamped behind head. The MetadataStore
  track is **not** run in server mode — both tracks share one `alembic_version`
  table in the same database, and its stamp (`v005`) made the server refuse to start
  with `SchemaSkewError` (fixed after v0.109.0). Dashboard mode still runs
  `nova db upgrade --backend postgres`.
- **`dbRevision` changed meaning in server mode.** It is now a registry-track
  revision (`p0001`…). A pinned MetadataStore id such as `v001` makes the init
  container fail.
- In server mode the server creates the MetadataStore tables it uses on **first
  use** (`PostgresMetadataStore.bootstrap()`). This needs a database role that can
  `CREATE ROLE … BYPASSRLS`, or the two roles pre-created by an administrator. The
  resulting `runs` table is not partitioned and there are no knowledge-graph tables,
  unlike a migrated database. See
  [upgrade guide §4c](../../../docs/ops/upgrade-guide.md#4c-metadatastore-tables-in-server-mode).
- A database already stamped `v005` by an earlier chart is **not** repaired by
  upgrading the chart: `--track registry` cannot locate `v005`, and the pod stays in
  `Init:CrashLoopBackOff`. Follow the manual recovery in
  [upgrade guide §4b](../../../docs/ops/upgrade-guide.md#4b-recovering-a-database-stamped-v005-by-an-earlier-chart-or-entrypoint).
- Readiness/liveness probes use `/readyz` and `/livez` (HTTP GET).
- The stock `values.yaml` keeps a writable root filesystem and a bundled Postgres
  whose password is the literal `nova`: an **evaluation** default. Use
  [`values-production.yaml`](#production-values) for anything else.

## Versioning

`Chart.yaml` `version` and `appVersion` both equal the package version in
`pyproject.toml`, so `helm install` from a checkout deploys the image of that
release. `tests/docs/test_release_metadata_is_consistent.py` fails when they
drift, and `publish-chart.yml` refuses to publish a tag that disagrees with them.
