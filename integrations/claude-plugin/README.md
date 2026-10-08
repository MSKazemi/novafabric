# NovaFabric — Claude Code plugin

A Claude Code plugin that lets you **instrument** an AI agent with NovaFabric and
**deploy** the NovaFabric dashboard — by just asking Claude Code, in plain language.

It bundles two skills:

| Skill | What it does | Say something like |
|---|---|---|
| `novafabric-instrument` | Add NovaFabric capture to your Python agent: record runs as replayable Run Capsules you own, then validate them (and verify them once sealing is configured). | *"instrument my agent with NovaFabric"*, *"capture my agent runs"*, *"make my agent auditable"* |
| `novafabric-deploy` | Deploy `nova serve` (the experimental dashboard + REST API) to Docker or Kubernetes via the published image and Helm chart. | *"deploy NovaFabric"*, *"install the NovaFabric helm chart"*, *"host the dashboard on k8s"* |

## Install

In Claude Code:

```text
/plugin marketplace add MSKazemi/novafabric
/plugin install novafabric@novafabric
```

That's it — the two skills are now available. Claude Code invokes them automatically
when your request matches, or you can ask for one by name.

## Using it to deploy

Once installed, just describe what you want. The `novafabric-deploy` skill walks
Claude Code through it. Examples:

- **"Deploy the NovaFabric dashboard to my Kubernetes cluster."**
  Claude runs, roughly:
  ```bash
  helm install nova oci://ghcr.io/mskazemi/charts/novafabric --version <X.Y.Z>
  kubectl rollout status deploy/nova-novafabric
  kubectl port-forward svc/nova-novafabric 4321:4321
  # token: kubectl logs deploy/nova-novafabric | grep -i token
  ```

- **"Deploy NovaFabric for production with my own Postgres and TLS."**
  Claude adds `--set postgres.enabled=false --set externalDatabase.host=… --set ingress.enabled=true …`
  and reminds you to terminate TLS at the ingress (`nova serve` is experimental and
  serves plain HTTP behind a bearer token; the chart refuses an insecure non-loopback
  dashboard unless you acknowledge it, ADR-0230).

- **"Run NovaFabric locally with Docker to try it."**
  Claude uses the `ghcr.io/mskazemi/novafabric` image (or the repo's
  `deploy/docker/docker-compose.yml` / `make dev-up` for a Postgres + dashboard
  stack) and fetches the access token from the container logs.

> **Note:** the GHCR image and Helm chart must be **publicly visible** for anonymous
> `docker pull` / `helm install`. If a pull returns 401/403, the package owner must
> set the GHCR package visibility to Public.

## Using it to instrument an agent

- **"Add NovaFabric to this agent."** Claude installs `novafabric`, runs `nova init`,
  captures your entrypoint with `nova capture python <entrypoint>`, then
  `nova validate`s the resulting capsule (and `nova verify`s it if you configured sealing) — no changes to your agent's
  code required. NovaFabric is self-hosted; no server is needed for capture.

## What is NovaFabric?

Open-source (Apache-2.0), self-hosted replay and evidence infrastructure for AI agents:
capture an agent/LLM run as a portable Run Capsule you own (sealable with your own key),
then replay, diff, trace lineage, and gate promotion on policy. See the [main repository](https://github.com/MSKazemi/novafabric) and
[`docs/`](https://github.com/MSKazemi/novafabric/tree/main/docs).

## Honest status

`nova serve` (the deployed dashboard) is **experimental**, and it has write endpoints
(run deletion, PII erasure, role management), so put it behind TLS and its token.
Automatic model-call capture is for **Python** workloads (`nova api-proxy` covers other
clients), capture is **self-hosted** (no server needed), and a capture is **unsealed**
until you configure a signing key. Both skills state these limits inline so you
deploy and instrument with eyes open.
