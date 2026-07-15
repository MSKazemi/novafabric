---
name: novafabric-deploy
description: Use when a user wants to deploy the NovaFabric server — the read-only dashboard + REST API (`nova serve`) — to Docker or Kubernetes. Triggers — "deploy NovaFabric", "run nova serve on k8s/docker", "install the NovaFabric helm chart", "host the NovaFabric dashboard", "stand up a NovaFabric server". Server mode is experimental; front it with TLS.
---

# Deploy NovaFabric (dashboard + REST API)

Deploys `nova serve` — NovaFabric's **experimental, read-only** dashboard and REST
API — backed by Postgres. This skill does not capture agent runs; for that use
`novafabric-instrument`.

Published artifacts (multi-arch `amd64`+`arm64`, cut from each `vX.Y.Z` tag):
- **Container image:** `ghcr.io/novafabric/novafabric`
- **Helm chart (OCI):** `oci://ghcr.io/novafabric/charts/novafabric`

## Choose a target
- **Single host / laptop / evaluation** → Docker (bundled or external Postgres).
- **Kubernetes cluster** → the Helm chart.

Ask the user which fits; default to Docker for a quick look, Helm for a cluster.

## Procedure — Docker (single host)
```bash
# Quick look (needs a reachable Postgres; see deploy/docker/docker-compose.yml
# in the repo for a one-command Postgres + serve stack via `make dev-up`):
docker run --rm -p 4321:4321 ghcr.io/novafabric/novafabric:<X.Y.Z> nova --help
```
For a full stack (Postgres + dashboard) the repo's `deploy/docker/docker-compose.yml`
(`make dev-up`) is the simplest path. The dashboard prints an access token on
startup — retrieve it from the container logs.

## Procedure — Kubernetes (Helm)
```bash
# Evaluation (bundled single-replica Postgres):
helm install nova oci://ghcr.io/novafabric/charts/novafabric --version <X.Y.Z>
kubectl rollout status deploy/nova-novafabric
kubectl port-forward svc/nova-novafabric 4321:4321
# open http://localhost:4321/dashboard  (token printed in `kubectl logs`)
kubectl logs deploy/nova-novafabric | grep -i token
```

## Production hardening (recommend, don't skip)
- **External managed Postgres** instead of the bundled one:
  ```bash
  helm install nova oci://ghcr.io/novafabric/charts/novafabric --version <X.Y.Z> \
    --set postgres.enabled=false \
    --set externalDatabase.host=<host> \
    --set externalDatabase.existingSecret=<secret-name>
  ```
- **Persistence** (`persistence.enabled=true`, default) so capsules + registry survive
  pod restarts.
- **Ingress + TLS** (`ingress.enabled=true`, `ingress.tls=[...]`). `nova serve` runs
  with `--insecure` and a printed token by default — **terminate TLS at the ingress**
  before exposing it.
- Pod runs **non-root** by default (uid/gid/fsGroup 1000, all capabilities dropped) —
  no privileged or host access required.

## Honest limitations (state these)
- `nova serve` is **experimental and read-only** (ADR-0027) — a dashboard over local
  capsules, not a control plane. No mutation by default.
- The image's entrypoint applies schema migrations on start (stops at `v001`).
- The GHCR image and chart must be **publicly visible** for anonymous pulls; if a pull
  returns 401/403, the package owner must set the GHCR package visibility to public.

## Verify it worked
- Pod/container is Ready; `kubectl logs` (or `docker logs`) shows
  "starting dashboard on 0.0.0.0:4321".
- The dashboard loads at `/dashboard?token=<token>` and `/api/docs` responds.
- See [`deploy/helm/novafabric/README.md`](https://github.com/novafabric/novafabric/blob/main/deploy/helm/novafabric/README.md)
  for the full values reference.
