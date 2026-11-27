---
name: novafabric-deploy
description: Use when a user wants to deploy NovaFabric as a service — the Helm chart's default multi-user REST API server (`nova server start`), or the experimental dashboard (`nova serve`) — on Kubernetes or Docker. Triggers — "deploy NovaFabric", "install the NovaFabric helm chart", "run NovaFabric on k8s/docker", "stand up a NovaFabric server", "host the NovaFabric dashboard". Both are experimental; put TLS and real credentials in front before exposing either.
---

# Deploy NovaFabric (REST API server or dashboard)

This skill deploys a NovaFabric service backed by Postgres. It does not capture agent
runs; that is the `novafabric-instrument` skill. The two things it can run are different
programs. Say which one the user is getting:

| | **Server mode** (Helm default) | **Dashboard mode** |
|---|---|---|
| Command | `nova server start` | `nova serve --experimental` |
| What it is | Multi-user REST API under `/v0/` (OpenAPI UI at `/docs`), no web dashboard | The web dashboard at `/dashboard` plus its `/api/` routes |
| Container port | `7433` | `4321` |
| Auth | OIDC, or a generated local bearer token | A shared token with `admin` scope |
| Status | experimental | experimental, **not read-only** (it can delete runs and erase PII) |

Published artifacts, cut from each `vX.Y.Z` release tag:
- **Container image:** `ghcr.io/mskazemi/novafabric:<X.Y.Z>`
- **Helm chart (OCI):** `oci://ghcr.io/mskazemi/charts/novafabric --version <X.Y.Z>`

Ask the user which release to use. Do not guess a version number.

## Kubernetes (Helm), server mode — the default

```bash
helm install nova oci://ghcr.io/mskazemi/charts/novafabric --version <X.Y.Z>
kubectl rollout status deploy/nova-novafabric
kubectl port-forward svc/nova-novafabric 4321:4321      # Service 4321 -> container 7433
```

The local bearer token is **not** printed to the pod logs. Under Kubernetes stderr is not
a terminal, so the server logs only a fingerprint and the file path. Read it from the pod:

```bash
TOKEN=$(kubectl exec deploy/nova-novafabric -- sh -c 'cat "$NOVAFABRIC_HOME/.server-token"')
curl -H "Authorization: Bearer $TOKEN" http://localhost:4321/v0/version
```

Never paste the token into a chat, an issue or a commit. To pin a token you manage, store
it in a Kubernetes Secret and pass it as `NOVAFABRIC_SERVER_TOKEN` through `extraEnv`
in a values file:

```yaml
extraEnv:
  - name: NOVAFABRIC_SERVER_TOKEN
    valueFrom:
      secretKeyRef: { name: nova-api-token, key: token }
```

OIDC is configured with an `oidc:` block in a `server.yaml` mounted at
`~/.config/novafabric/server.yaml` for the container user. Without it the server runs in
local-token mode, and auth stays on.

## Kubernetes (Helm), dashboard mode

`mode: dashboard` runs `nova serve`. A pod must bind `0.0.0.0`, and `nova serve` refuses
a non-loopback bind without `--insecure`. The chart therefore **refuses to render**
unless both of these are set:

```bash
helm install nova oci://ghcr.io/mskazemi/charts/novafabric --version <X.Y.Z> \
  --set mode=dashboard \
  --set serve.insecure=true \
  --set serve.acknowledgeInsecureExposure=i-accept-serving-evidence-over-plain-http
```

That serves the dashboard over plain HTTP. Its shared token holds `admin` scope, and with
`serve.insecure` the token is printed in `kubectl logs`. Only do this behind TLS and an
authenticating proxy, and explain the trade-off to the user before setting the flag.

## Docker (single host, evaluation)

From a checkout of the repository:

```bash
make dev-up          # builds the image from source; Postgres + the dashboard
make docker-token    # prints http://localhost:4321/dashboard?token=...
make dev-down
```

`deploy/docker/docker-compose.yml` runs the **dashboard** (the image entrypoint's default,
`NOVA_MODE=dashboard`) and publishes it on `127.0.0.1:4321` only. Setting `NOVA_MODE=server`
in the container's environment runs the REST API on `7433` instead.

The published image's entrypoint waits for a Postgres at `$PGHOST` (default `postgres`),
runs `nova db upgrade` to the packaged schema head, then starts the service. It ignores
command arguments, so `docker run <image> nova --help` waits for Postgres. To run the CLI
alone, override the entrypoint (not exercised by this plugin's checks):
`docker run --rm --entrypoint nova ghcr.io/mskazemi/novafabric:<X.Y.Z> --help`.

## Credentials are demo-only by default
- The chart's bundled Postgres (`postgres.enabled=true`) uses `postgres.auth.password: nova`,
  and the compose file uses `nova`/`nova`. Both are for evaluation only. Say so.
- For anything real, use an external, managed Postgres with its password in a Secret:
  ```bash
  helm install nova oci://ghcr.io/mskazemi/charts/novafabric --version <X.Y.Z> \
    --set postgres.enabled=false \
    --set externalDatabase.host=<host> \
    --set externalDatabase.existingSecret=<secret-name> \
    --set externalDatabase.existingSecretPasswordKey=password
  ```

## Production hardening (recommend, don't skip)
- **TLS:** enable the ingress (`ingress.enabled=true`, `ingress.tls=[…]`) and terminate TLS
  there. `nova server start` has no TLS flags of its own. `nova serve` can serve HTTPS with
  `--tls-cert`/`--tls-key`.
- **Persistence:** `persistence.enabled=true` is the default (5 Gi PVC at `/data`), so
  capsules and the server state survive pod restarts.
- **Pod security:** the pod runs as non-root by default (uid/gid/fsGroup 1000,
  `allowPrivilegeEscalation: false`, all capabilities dropped). No privileged or host
  access is needed.
- **Workers:** `server.workers > 1` needs Postgres. The CLI refuses SQLite across workers.

## Honest limitations (state these)
- Both modes are **experimental**. Server mode is an authenticated REST API, not a web
  dashboard. The dashboard is a single-tenant view over local capsules with write routes
  (run deletion, PII erasure, role management) gated by token scope.
- The image and chart are pulled anonymously only if their GHCR packages are public. A
  401/403 on pull means the package owner has to change the GHCR package visibility.
- Read the [Helm chart README](https://github.com/MSKazemi/novafabric/blob/main/deploy/helm/novafabric/README.md)
  and [server deployment guide](https://github.com/MSKazemi/novafabric/blob/main/docs/ops/server-deployment.md)
  for the full values reference before a production install.

## Verify it worked
- `kubectl rollout status` completes. The readiness probe is `/readyz` (dependencies and
  schema) and the liveness probe is `/livez`, in both modes.
- Server mode: `curl -H "Authorization: Bearer $TOKEN" http://localhost:4321/v0/version`
  returns JSON. A `401` means a missing or wrong token.
- Dashboard mode / Docker: `make docker-token` prints a URL, and
  `http://localhost:4321/dashboard?token=<token>` loads.
