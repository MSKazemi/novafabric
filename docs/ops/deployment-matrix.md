# Deployment matrix: compatibility, operations and CI evidence

This page compares every deployment path in the repository side by side:
how mature it is, what it needs, how it is backed up and upgraded, and what
CI actually runs for it. It does not replace the install guide.
[Installation options and software stack](../installation.md) is still the
place to start; this page links back to it and to the runbooks it names.

**How the cells were filled.** Each cell comes from code, a chart or Compose
file, or a GitHub Actions workflow in this repository, read at `main`
`8875b8b7` on 2026-11-27. A cell says **not verified** when nothing in the
repository shows the claim one way or the other. Maturity uses the
documentation labels: **works today**, **experimental**, **planned**,
**future design**. A path that exists only in an open pull request is marked
**in review, PR #N**. It is not available on `main`.

**"CI exercises"** names the workflow file and job. A test that CI collects
but skips (because it needs a live cluster, Docker Compose or a real SLURM
scheduler) does not count. Those skips are listed as gaps.

## Platform and access

| Path | Maturity | OS / arch / Python | Required external services | Storage backend | Auth options | TLS |
|---|---|---|---|---|---|---|
| **Local CLI** — `pip install novafabric` | **works today** (package classifier `Development Status :: 4 - Beta`) | Python `>=3.12`; pure-Python wheel (`py3-none-any`). The only OS classifier is `POSIX :: Linux`. CI runs Python 3.12 on `ubuntu-24.04` x86_64 only. macOS, Windows, arm64 and Python 3.13+: **not verified** | None | Capsule directories on the local filesystem, plus a SQLite registry (`$NOVAFABRIC_HOME/registry.db`) | None (no listener); OS file permissions | Not applicable (no listener) |
| **Local dashboard** — `novafabric[serve]`, `nova serve --experimental` | **experimental** (the `--experimental` flag is required) | Same as the local CLI. The frontend ships inside the wheel, so Node.js is not needed to run it | None | The same capsule directory and SQLite registry; DuckDB runs in-process for the topology view | A session token with `admin` scope, written to a mode-0600 file; narrower tokens are minted with `POST /api/admin/tokens`. No OIDC | Off by default. `--tls-cert`/`--tls-key` or `NOVA_TLS_CERT_PATH`/`NOVA_TLS_KEY_PATH`. Binds `127.0.0.1:4321` and refuses a non-loopback bind unless `--insecure` is given |
| **Single-host pip server** — `novafabric[server]`, `nova server start` | **experimental**, single-tenant | Same as the local CLI | None for SQLite. Postgres is opt-in; CI tests against Postgres 16 | Capsule directory on the server filesystem; SQLite (default) or Postgres via `--backend postgres` | Local bearer token (default); OIDC/JWKS; API keys (`nova server api-key create`); offline ed25519 tokens (`nova server issue-token`); SAML assertion verification (experimental, partial); SCIM provisioning (off by default); `--insecure-no-auth` as an explicit opt-out | Native listener TLS through the `tls:` block or `NOVA_TLS_ENABLED`/`NOVA_TLS_CERT_PATH`/`NOVA_TLS_KEY_PATH`. It fails closed on a missing file or a group- or world-readable key. A reverse proxy also works |
| **Docker Compose, default profile** — Postgres + `nova serve` | **experimental** | Image `python:3.12-slim` (pinned by digest). Built for `linux/amd64` and `linux/arm64` on release tags; only amd64 is built on pull requests; neither image is started in CI | Postgres 16 (`postgres:16-alpine`). The entrypoint waits for Postgres in **both** modes | Host bind mount `${NOVA_DATA_DIR:-~/novafabric-data}` (capsules + registry), named volumes `pg-data` and `kuzu-data`, metadata in Postgres | Dashboard mode (default): shared `admin` token in the container logs. `NOVA_MODE=server`: server auth as above; OIDC needs a mounted `server.yaml` | None configured. The dashboard runs with `--insecure` inside the container and is published on host `127.0.0.1:4321` only |
| **Docker Compose, `prod` profile** — adds ClickHouse, NATS, Kafka, PgBouncer, JanusGraph | **experimental** (an integration/example stack) | As the default profile | ClickHouse 24.12, NATS 2.10, Kafka 3.9.0, PgBouncer v1.25.2-p0, JanusGraph 1.1.0 (images pinned in `docker-compose.yml`) | As the default profile, plus ClickHouse (cost data) and JanusGraph (lineage) volumes | As the default profile | None configured. Service ports are published on host loopback only |
| **Docker Compose, `age` profile** — standalone Apache AGE | **experimental** | `apache/age:release_PG16_1.6.0` | Starts only the AGE service | Named volume `age-data` | Example Postgres credentials in the Compose file | None configured. Published on host `127.0.0.1:5433` |
| **Helm chart** — `deploy/helm/novafabric` | **experimental** (chart README) | Image as above. The chart declares no `kubeVersion`; supported Kubernetes versions: **not verified** | A Kubernetes cluster and a storage class. Postgres is either the bundled single-replica `postgres:16-alpine` (evaluation only) or an external one (`externalDatabase.*`). An ingress controller if `ingress.enabled` | PVC at `/data` (`ReadWriteOnce`, 5Gi default) for capsules, SQLite and KuzuDB; Postgres for metadata (bundled PVC 8Gi) | `mode: server` (default): local bearer token at `$NOVAFABRIC_HOME/.server-token`, API keys, OIDC through a mounted `server.yaml`. `mode: dashboard`: shared `admin` token; refuses to render without `serve.insecure` and the acknowledgement value | At the Ingress (`ingress.tls`). Native listener TLS would need certificate files in the pod; the chart has no values key for extra volumes (**not verified** as a working setup) |
| **HPC / SLURM capture** — `nova capture --runner slurm` | **experimental** (ROADMAP v0.6 row). [Deployment topologies](../architecture/deployment-topologies.md) lists the runner as works today; the two pages disagree, and CI tests only mocked submission | Linux submit and compute nodes with `sbatch`, `scontrol` and `sacct`; Python `>=3.12` reachable from compute nodes. ROADMAP records a manual check on SLURM 23.11; other SLURM versions: **not verified** | A SLURM cluster; a filesystem shared by the submit and compute nodes (the runner copies no artifacts); `slurmdbd` for the `sacct` fallback | Capsule directory on the shared filesystem; SQLite registry on the submit side | The scheduler's own; NovaFabric adds none | Not applicable |
| **HPC collector tier** — `deploy/hpc/` (prolog/epilog, NATS templates, Ansible playbook, test cluster) | **experimental** (ROADMAP.md, Phase 2 collector tier; `deploy/hpc/README.md` still says planned and gives no target). The `novafabric-hpc-hub` binary is a stub that exits "not implemented" (**future design**) | SLURM `>=22.05`, NATS Server `>=2.10` (prerequisites stated in the README); **not verified** on any cluster | SLURM, NATS JetStream, the Go spool forwarder | Node spool (`$NOVAFABRIC_HOME/spool`) → NATS JetStream; the server is not fed from it | **not verified** | **not verified** |
| **Offline install** — wheel mirror + signed bundle format | Wheel mirroring: **works today** (standard `pip download` / `pip install --no-index`). Signed bundle: **experimental**, format and verifier only | As the local CLI. Wheels must match the target Python and platform | None at install time | As the profile you install | As the profile you install | As the profile you install |
| **Ansible single-host role** | **in review, PR #354** | Not on `main` — see the PR | — | — | — | — |
| **OpenTofu single-host module** (AWS EC2 and GCP Compute examples) | **in review, PR #356** | Not on `main` — see the PR | — | — | — | — |
| **Air-gap closure bundle** (complete wheelhouse + offline verifier) | **in review, PR #353** | Not on `main` — see the PR | — | — | — | — |

## Operations and evidence

| Path | Backup / restore | Upgrade path | What CI actually exercises | Known gaps |
|---|---|---|---|---|
| **Local CLI** | `nova backup create` / `nova backup verify` / `nova restore`, local profile (**experimental**): registry snapshot + capsules + redacted config, DSSE-signed when a local NovaSeal profile exists. Key material never goes into a set | `pip install -U novafabric`, then `nova doctor --check-storage`; run `nova db upgrade --track registry --backend sqlite` if it reports pending migrations. No supported schema downgrade; restore a pre-upgrade backup instead | `ci.yml`: `lint` (CLI `--version`/`--help` smoke), `unit` (6 shards, including `tests/backup/`), `golden-capsule` (wheel built and installed into a clean venv), `capture-overhead-gate`, `seal-latency-gate`. `publish-pypi.yml` on `v*` tags | No CI on macOS, Windows, arm64 or Python other than 3.12. Capsules can hold model prompts and responses by default ([installation](../installation.md#security-and-production-readiness-limits)) |
| **Local dashboard** | As the local CLI (same files) | `pip install -U 'novafabric[serve]'`; otherwise as the local CLI | `ci.yml` `unit` (`tests/test_serve_*.py`, `tests/serve/`, `tests/cli/test_serve_printed_urls.py`); `dashboard.yml` (type-check, vitest, build; runs only when `packages/nova-dashboard/**` changes); `nightly-scale-gates.yml` `dashboard-scale` (100K-row SQLite p95 gate). Browser (Playwright) suites are not run in CI (`ci.yml` comment) | Not read-only: the `admin` token can delete runs and erase PII. Single-tenant: `NOVAFABRIC_SERVE_TENANCY=multi` is refused while any store is unsafe |
| **Single-host pip server** | Local profile as above; `nova backup create --profile pg` adds a `pg_dump` member, and `nova restore` restores it with `pg_restore`. Restore needs the Postgres client tools on `PATH` | `pip install -U 'novafabric[server]'` → `nova db upgrade --track registry --backend postgres` → restart the single writer. The server refuses to start on a schema behind or ahead of the package (override: `NOVAFABRIC_ALLOW_SCHEMA_SKEW=1`). Steps: [upgrade guide §3](upgrade-guide.md#3-standard-upgrade--server-mode-postgres) | `ci.yml` `unit` (server, auth, RBAC, TLS — including a real HTTPS round trip in `tests/server/test_tls.py` — and the pg restore cycle against a testcontainers Postgres); `ci.yml` `integration` (Postgres 16 service, `alembic -c alembic-postgres.ini upgrade head`, `tests/integration/`); `metadata_store_security_gate.yml` (RLS checks on Postgres, on metadata-store path changes); `nightly-scale-gates.yml` `postgres-scale` | No service unit or process supervisor ships on `main` (the Ansible PR adds one). `--workers N` is not high availability; one writer only. Capsule bytes are served from the filesystem only; the object store is not wired into server routes. More than one organization is refused unless the operator accepts a shared capsule store |
| **Docker Compose, default profile** | No backup service in the Compose file. `nova backup create` (including `--profile pg`) is in the image, but no workflow runs it against this stack: **not verified** | `docker compose ... up --build -d`; the entrypoint runs `nova db upgrade --backend postgres --revision head` on every start | `release-toolchain.yml` `image-toolchain`: builds the Dockerfile for amd64 without pushing, when `deploy/docker/**`, `pyproject.toml` or `uv.lock` change. `ci.yml` `unit`: static checks only (`tests/deploy/test_image_pins.py`, `tests/docs/test_force_include_reaches_the_docker_build.py`, `tests/packaging_metadata/test_dockerfile_nonroot.py`). **Nothing runs `docker compose up`** | Example credentials (`nova`/`nova`) are written into the Compose file (PR #345, in review, changes this). The entrypoint migrates only the default `metadata` track, while the server's schema guard checks the `registry` track; effect on an upgrade: **not verified** |
| **Docker Compose, `prod` profile** | None for ClickHouse, NATS, Kafka or JanusGraph data | As the default profile | Image pins only (`tests/deploy/test_image_pins.py`); the profile is never started in CI | Example credentials (ClickHouse `novapass`). No Kafka client in `src/` (Kafka is only a service here). The `nova` service connects to Postgres directly, not through PgBouncer |
| **Docker Compose, `age` profile** | None | Not covered | `ci.yml` `unit`: `tests/deploy/test_compose_profiles.py` (YAML structure), `tests/lineage/test_age_backend.py` (AGE in testcontainers). The live Compose test is opt-in and skipped (`NOVAFABRIC_RUN_DOCKER_COMPOSE_TESTS=1`) | Experimental lineage backend; not part of `prod` |
| **Helm chart** | None in the chart (no CronJob, no Postgres backup). **not verified** | `helm upgrade`; the `migrate` init container runs `nova db upgrade --backend postgres --revision head`. Crossing v0.102.0 refuses until `upgradeAcknowledged: true`. Keep `replicaCount: 1` | `ci.yml` `unit`: `helm template` tests (`tests/deploy/test_secure_by_default_chart.py`, `tests/deploy/test_dashboard_mode_starts.py`; 27 passed on the snapshot run). `release-toolchain.yml` `chart-toolchain`: `helm lint` + `helm package` (Helm v3.16.4) on `deploy/helm/**` changes. `publish-chart.yml` on `v*` tags: lint, package, push to GHCR OCI. **No workflow installs the chart into a cluster** | `Chart.yaml` `appVersion` is `0.58.0`; only the release workflow overrides it, so a chart installed from a checkout without `image.tag` pulls an old image (PR #343, in review, addresses chart versioning). The bundled Postgres uses `nova`/`nova`. The README says to mount `server.yaml` for OIDC, but the chart template mounts only `/data`. Same `metadata`-track-only migration as Compose |
| **HPC / SLURM capture** | As the local CLI, on the submit host. **not verified** on a cluster | As the local CLI; the environment must be visible from the compute nodes | `ci.yml` `unit`: `tests/test_runners_slurm.py` with mocked `sbatch`/`sacct` (34 passed, 2 skipped); `tests/test_example_hpc_slurm_job.py` (no-scheduler path). The live SLURM test is opt-in (`NOVAFABRIC_TEST_LIVE_SLURM=1`) and skipped; `nightly-scale-gates.yml` states live SLURM is still skipped there too | No live-scheduler run in CI. The capsule does not yet record the SLURM node list (ROADMAP). LSF and PBS runners are experimental and are not covered by this row |
| **HPC collector tier** | **not verified** | **not verified** | `collector-ci.yml` (Go lint, unit, spec, build, govulncheck) when `collector/**` or `deploy/**` change; it builds the `novafabric-hpc-hub` stub and runs `--help` only. No SLURM or NATS cluster test | Hub signing is not implemented. The collector image is not published. The cluster event stream does not feed the server |
| **Offline install** | As the profile you install | Mirror the new wheels, then upgrade as the profile you install | `ci.yml` `unit`: `tests/packaging_metadata/test_airgap_bundle.py` (bundle format: round trip, tamper detection, wrong key). **No workflow on `main` installs from a wheelhouse with the network off** | The bundle on `main` holds only the files you pass to it. Assembling the full closure is PR #353 |
| **Ansible / OpenTofu / air-gap closure** | In review: PRs #354, #356, #353 | — | Each PR adds its own workflow (`ansible.yml`, `iac-validate.yml`, `airgap-bundle.yml`); none is on `main` | Not available until merged |

## Cloud providers

| Provider | In this repository |
|---|---|
| On-premises or any Linux host | The rows above |
| AWS | No example on `main`. An EC2 example is **in review, PR #356** |
| Google Cloud | No example on `main`. A Compute Engine example is **in review, PR #356** |
| Azure | No example; not planned in this repo |

No path above has been deployed to a managed Kubernetes service or a cloud
VM by any workflow in this repository. No workflow creates cloud resources.

## Cost and resource sizing

**Cloud cost is not measured.** This repository publishes no cost figures,
and this page invents none. The only resource sizing the repository
documents is:

| Source | What it sets |
|---|---|
| [`deploy/helm/novafabric/values.yaml`](../../deploy/helm/novafabric/values.yaml) | `nova` container: requests `cpu: 100m`, `memory: 256Mi`; limit `memory: 1Gi` (no CPU limit). Bundled Postgres: requests `cpu: 100m`, `memory: 256Mi` (no limits). PVCs: data `5Gi`, Postgres `8Gi` |
| [`deploy/helm/janusgraph/values.yaml`](../../deploy/helm/janusgraph/values.yaml) | JanusGraph: requests `cpu: 500m`, `memory: 1Gi`; limits `cpu: 2`, `memory: 4Gi` |
| [`deploy/docker/docker-compose.yml`](../../deploy/docker/docker-compose.yml) (header comment) | Default profile: "~512 MB RAM". No `mem_limit` or `deploy.resources` is set on any service |
| [Sizing guide](sizing-guide.md) | Capsule and index storage per 1K / 100K / 1M runs, labelled **estimates** derived from the on-disk formats, not benchmarks. No published memory or CPU benchmark exists for either server process |

When you estimate cost for your own environment, these are the cost drivers
that the deployment shapes above consume. They are listed without numbers on
purpose:

- **Compute.** One NovaFabric writer process (dashboard or server) and, when
  used, one Postgres instance. Extra uvicorn workers run in the same pod or
  host. Each `prod` profile service adds its own container.
- **Storage.** Capsule bytes grow with every run and dominate the budget
  ([sizing guide §3](sizing-guide.md#3-rough-sizing-table--1k--100k--1m-runs)).
  Persistent volumes, database storage and backup sets come on top.
- **Managed database.** Only when you point `externalDatabase.*` or
  `NOVAFABRIC_POSTGRES_DSN` at a managed Postgres.
- **Network.** Core local mode makes no network calls. The opt-in RFC 3161
  timestamp authority is the one network dependency named in
  [deployment topologies](../architecture/deployment-topologies.md#local-mode-works-today).
  The workload you capture may call model APIs; that traffic is the
  workload's, not NovaFabric's.
- **Logs.** Container stdout and the server's structured logs
  (`--log-format json`), retained by whatever log system you run.

## Data residency and identity ownership

Every path above is self-hosted, so capsules, registries and backups stay on
storage you provision. Jurisdiction-aware capsule placement is a separate,
**experimental** feature: see [Data residency](data-residency.md). Identity
stays with you: a local token generated on the host, offline ed25519 tokens
signed by your own key, or your OIDC identity provider. NovaFabric runs no
identity service of its own.

## Evidence snapshot

These are the runs this page was checked against. They show what passed on
those commits, not that every path above works.

| Workflow | Run | Commit | Result |
|---|---|---|---|
| `ci.yml` (all 14 jobs, including `integration` and `golden-capsule`) | [45923424858](https://github.com/MSKazemi/novafabric/actions/runs/45923424858) | `8875b8b7` (main) | success |
| `nightly-scale-gates.yml` (`postgres-scale`, `object-store-scale`, `dashboard-scale`) | [45812430319](https://github.com/MSKazemi/novafabric/actions/runs/45812430319) | `06675788` (main) | success |
| `release-toolchain.yml` (image and chart toolchain) | [45647630640](https://github.com/MSKazemi/novafabric/actions/runs/45647630640) | `06675788` (main) | success |
| `publish-image.yml` / `publish-chart.yml` / `publish-pypi.yml` | [45046197296](https://github.com/MSKazemi/novafabric/actions/runs/45046197296) / [45046197111](https://github.com/MSKazemi/novafabric/actions/runs/45046197111) / [45046196973](https://github.com/MSKazemi/novafabric/actions/runs/45046196973) | tag `v0.109.0` | success |

Open pull requests that will change cells here when they merge: #341
(single-host backup/restore drill in CI), #343 (Helm production values and
chart versioning), #345 (real secrets in Compose), #353, #354, #356.

## Read next

- [Installation options and software stack](../installation.md) — the install guide
- [Deployment topologies](../architecture/deployment-topologies.md) — local, server and cluster tiers
- [Server deployment](server-deployment.md) — step-by-step scenarios, including Helm
- [Backup & restore runbook](backup-restore.md) · [Upgrade guide](upgrade-guide.md) · [Sizing guide](sizing-guide.md)
- [Air-gapped install](air-gapped-install.md) · [HPC deployment reference](../../deploy/hpc/README.md)
