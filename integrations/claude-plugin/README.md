# NovaFabric — Claude Code plugin

A Claude Code plugin that lets you **instrument** an AI agent with NovaFabric and
**deploy** the NovaFabric dashboard — by just asking Claude Code, in plain language.

It bundles two skills:

| Skill | What it does | Say something like |
|---|---|---|
| `novafabric-instrument` | Add NovaFabric capture to your Python agent: record runs as signed, replayable Run Capsules, then validate/verify them. | *"instrument my agent with NovaFabric"*, *"capture my agent runs"*, *"make my agent auditable"* |
| `novafabric-deploy` | Deploy `nova serve` (the read-only dashboard + REST API) to Docker or Kubernetes via the published image and Helm chart. | *"deploy NovaFabric"*, *"install the NovaFabric helm chart"*, *"host the dashboard on k8s"* |

## Install

In Claude Code:

```text
/plugin marketplace add novafabric/novafabric
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
  helm install nova oci://ghcr.io/novafabric/charts/novafabric --version <X.Y.Z>
  kubectl rollout status deploy/nova-novafabric
  kubectl port-forward svc/nova-novafabric 4321:4321
  # token: kubectl logs deploy/nova-novafabric | grep -i token
  ```

- **"Deploy NovaFabric for production with my own Postgres and TLS."**
  Claude adds `--set postgres.enabled=false --set externalDatabase.host=… --set ingress.enabled=true …`
  and reminds you to terminate TLS at the ingress (`nova serve` is experimental and
  serves over HTTP with a printed token by default).

- **"Run NovaFabric locally with Docker to try it."**
  Claude uses the `ghcr.io/novafabric/novafabric` image (or the repo's
  `deploy/docker/docker-compose.yml` / `make dev-up` for a Postgres + dashboard
  stack) and fetches the access token from the container logs.

> **Note:** the GHCR image and Helm chart must be **publicly visible** for anonymous
> `docker pull` / `helm install`. If a pull returns 401/403, the package owner must
> set the GHCR package visibility to Public.

## Using it to instrument an agent

- **"Add NovaFabric to this agent."** Claude installs `novafabric`, runs `nova init`,
  captures your entrypoint with `nova capture python <entrypoint>`, then
  `nova validate` + `nova verify` the resulting capsule — no changes to your agent's
  code required. NovaFabric is self-hosted; no server is needed for capture.

## What is NovaFabric?

Open-source (Apache-2.0) replayable AI infrastructure: capture every agent/LLM run as
a signed, verifiable Run Capsule, then replay, diff, prove lineage, and gate promotion
on policy. See the [main repository](https://github.com/novafabric/novafabric) and
[`docs/`](https://github.com/novafabric/novafabric/tree/main/docs).

## Honest status

`nova serve` (the deployed dashboard) is **experimental and read-only**. Capture is
**Python-only** and **self-hosted** (no server needed for capture). Both skills state these limits inline so you
deploy and instrument with eyes open.
