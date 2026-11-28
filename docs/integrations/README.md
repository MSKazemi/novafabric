# Integrations

How to run NovaFabric alongside something else. Each guide is written against the
integration's own source of truth and says plainly what it does **not** cover.

| Guide | Status | What it is for |
|---|---|---|
| [GitHub Actions](github-actions.md) | **works today** | Capture a CI step as a capsule and upload it as a build artifact. |
| [Hugging Face](huggingface.md) | **experimental** (new in v0.109.0) | Capture `smolagents` agent runs and Inference Providers calls; opt-in Hub revision receipts and evaluation receipts. Mocked replay serves recorded Inference Providers calls from their recorded bytes; `smolagents` tools always run live. |
| [Writing a hook plugin](writing-a-hook-plugin.md) | **experimental** | The wire-level capture plugin contract, for instrumenting an SDK NovaFabric does not know about. |
| [MCP evidence server (stdio)](mcp-evidence-server.md) | **experimental** (unreleased) | Let an MCP client list, summarize and verify local capsules offline, read-only, within directories you name. No prompt or response content is returned. |
| [MCP client setup](mcp-client-setup.md) | **experimental** (unreleased) | Configure Claude Code (connection verified), Claude Desktop, Cursor, VS Code (not verified) or any stdio client for the evidence server; what it exposes; an offline sealed-versus-tampered check; troubleshooting. |
| [MCP Registry submission checklist](mcp-registry-checklist.md) | **planned** (not submitted) | What must be true before the evidence server is listed in an MCP registry, and which items are not met today. |

## Not here yet

These are honest gaps rather than an oversight — nothing packaged ships for them:

- **GitLab CI, Jenkins, CircleCI, Buildkite.** The pattern generalises (wrap the
  command in `nova capture`, archive the output directory), but there is no
  packaged integration to point you at.
- **Airflow, Prefect, Dagster.** Same shape, same absence.
- **Slurm and Kubernetes** are *runners*, not integrations — see
  [`examples/hpc-slurm-job/`](https://github.com/MSKazemi/novafabric/tree/main/examples/hpc-slurm-job)
  and [`examples/docker-run/`](https://github.com/MSKazemi/novafabric/tree/main/examples/docker-run).

If you build one, a PR adding a guide here is welcome — see
[CONTRIBUTING.md](https://github.com/MSKazemi/novafabric/blob/main/CONTRIBUTING.md).
