# NovaFabric — Claude Code plugin

A Claude Code plugin that helps you **instrument** a Python AI agent with NovaFabric and
**deploy** a NovaFabric service, by asking Claude Code in plain language. Status:
**experimental**. The skills only run the `nova` CLI and `helm`/`kubectl`/`docker`
commands they describe, through Claude Code's normal permission prompts. The plugin has no
hooks, no MCP server, no executables and no network endpoints of its own.

It bundles two skills:

| Skill | What it does | Say something like |
|---|---|---|
| `novafabric-instrument` | Capture a Python agent's runs as Run Capsules (replayable folders you own), validate them, verify the seal once sealing is configured, then replay or export a signed Evidence Bundle. | *"instrument my agent with NovaFabric"*, *"capture my agent runs"*, *"make my agent auditable"* |
| `novafabric-deploy` | Deploy the Helm chart's default multi-user REST API server (`nova server start`), or the experimental dashboard (`nova serve`), to Kubernetes or Docker. | *"deploy NovaFabric"*, *"install the NovaFabric helm chart"*, *"run NovaFabric on k8s"* |

## Install

The repository root carries the marketplace file (`.claude-plugin/marketplace.json`); the
plugin itself lives in `integrations/claude-plugin/`. In a Claude Code session:

```text
/plugin marketplace add MSKazemi/novafabric
/plugin install novafabric@novafabric
```

Or from your shell: `claude plugin marketplace add MSKazemi/novafabric`, then
`claude plugin install novafabric@novafabric`. The GitHub shorthand reads
`.claude-plugin/marketplace.json` from the default branch.

Observed output, Claude Code 2.1.332, installing from a local clone of this repository:

```console
$ claude plugin marketplace add ./novafabric
✔ Successfully added marketplace: novafabric (declared in user settings)
$ claude plugin install novafabric@novafabric
✔ Successfully installed plugin: novafabric@novafabric (scope: user)
$ claude plugin details novafabric
novafabric 0.2.0
...
Component inventory
  Skills (2)  novafabric-deploy, novafabric-instrument
  Agents (0)
  Hooks (0)
  MCP servers (0)
  LSP servers (0)
```

Claude Code invokes a skill when your request matches its description, or you can ask for
one by name (`/novafabric:novafabric-instrument`, `/novafabric:novafabric-deploy`).

To check a checkout yourself before installing:

```bash
claude plugin validate .                          # the marketplace file
claude plugin validate integrations/claude-plugin # the plugin and its skills
```

## Using it to instrument an agent

- **"Add NovaFabric to this agent."** Claude installs `novafabric` into the agent's own
  Python environment (the capture hooks load inside that interpreter), runs `nova init`,
  captures the entrypoint with `nova capture -- python <entrypoint>`, then runs
  `nova validate` on the capsule. It runs `nova verify` too, which reports the seal, or
  exits 1 and says the capsule is unsealed when sealing was never configured
  (`nova seal init`, experimental). No change to your agent's code is needed and no
  server is involved.

## Using it to deploy

- **"Deploy NovaFabric to my Kubernetes cluster."** Claude runs, roughly:
  ```bash
  helm install nova oci://ghcr.io/mskazemi/charts/novafabric --version <X.Y.Z>
  kubectl rollout status deploy/nova-novafabric
  kubectl port-forward svc/nova-novafabric 4321:4321
  TOKEN=$(kubectl exec deploy/nova-novafabric -- sh -c 'cat "$NOVAFABRIC_HOME/.server-token"')
  curl -H "Authorization: Bearer $TOKEN" http://localhost:4321/v0/version
  ```
  That is **server mode**, the chart default: an authenticated REST API, not a web
  dashboard. The token is not printed to the pod logs.

- **"I want the dashboard on Kubernetes."** Claude explains that `mode: dashboard` serves
  plain HTTP with an admin-scope shared token, and that the chart refuses to render it
  without `serve.insecure=true` plus an explicit acknowledgement value. It sets those
  only after you agree, and only behind TLS.

- **"Run NovaFabric locally with Docker to try it."** From a checkout: `make dev-up`
  (Postgres + the dashboard on `127.0.0.1:4321`), then `make docker-token` for the
  dashboard URL.

- **"Deploy it for production."** Claude uses an external Postgres with its password in a
  Secret (`postgres.enabled=false`, `externalDatabase.existingSecret=…`) and an ingress
  with TLS. It also says that the bundled Postgres password (`nova`) is for demos only.

> **Note:** anonymous `docker pull` / `helm install` works only while the GHCR image and
> chart packages are public. A 401/403 on pull means the package owner has to change
> the GHCR package visibility.

## What is NovaFabric?

Open-source (Apache-2.0), self-hosted replay and evidence infrastructure for AI agents.
It captures an agent or LLM run as a portable Run Capsule you own (sealable with your own
key), then replays it, diffs it, traces lineage, and gates promotion on policy. See the
[main repository](https://github.com/MSKazemi/novafabric) and its
[`docs/`](https://github.com/MSKazemi/novafabric/tree/main/docs).

## Honest status

- The plugin is **experimental**, and so are both services it deploys. The dashboard
  (`nova serve`) is **not read-only**: its token can delete runs, erase PII and manage
  roles, so put TLS and authentication in front of any non-loopback exposure.
- **Capture records prompt and response text by default.** The SDK and wire model-call
  hooks write it to `model-calls.jsonl`. Treat a capsule as sensitive data.
- A capsule is **unsealed** until you run `nova seal init` (or configure
  `novaseal.yaml`), and that local identity is self-asserted.
- Automatic model-call capture is for **Python** workloads. `nova api-proxy` and
  `nova mcp-proxy` cover other clients.
- `nova replay` defaults to `mocked`, which re-runs the agent: recorded model responses and
  supported tool surfaces are served, and every other tool runs live.

Both skills state these limits inline so you deploy and instrument with eyes open. This
plugin is not submitted to any public plugin directory.
