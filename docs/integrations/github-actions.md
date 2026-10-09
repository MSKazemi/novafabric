# Capturing a CI step with GitHub Actions

**Status: works today.**

NovaFabric ships a composite GitHub Action that captures a CI step as a Run
Capsule and uploads it as a build artifact. CI is where "prove what this run did"
matters most — it is the run nobody was watching.

Everything below is taken from
[`.github/actions/capture/action.yml`](https://github.com/MSKazemi/novafabric/blob/main/.github/actions/capture/action.yml),
which is authoritative. If this page and that file ever disagree, the file is
right and this page is the bug.

## The minimal usage

```yaml
- uses: MSKazemi/novafabric/.github/actions/capture@main
  with:
    run: python my_agent.py
```

That installs NovaFabric from PyPI, runs the command under `nova capture`,
validates the resulting capsule, uploads it as an artifact named
`novafabric-capsule`, and re-raises the command's own exit code.

## A complete workflow you can paste

```yaml
name: capture

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

jobs:
  capture:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v7

      - name: Capture the agent run
        id: capture
        uses: MSKazemi/novafabric/.github/actions/capture@main
        with:
          run: python my_agent.py --input data/prompt.txt
          environment: test
          # Pin for reproducible CI. Without this you get the latest release,
          # which means a capsule produced by a version you did not choose.
          novafabric-version: "0.105.0"
          artifact-name: capsule-${{ github.run_id }}
          retention-days: "90"

      - name: Report what was captured
        if: always()
        run: |
          echo "run id : ${NOVAFABRIC_RUN_ID:-<unset>}"
          echo "status : ${NOVAFABRIC_STATUS:-<unset>}"
          echo "capsule: ${NOVAFABRIC_CAPSULE_PATH:-<unset>}"
```

## Inputs

Every row matches `action.yml` exactly.

| Input | Required | Default | What it does |
|---|---|---|---|
| `run` | **yes** | — | The command to capture. Runs under `nova capture`. |
| `environment` | no | `test` | Deployment environment recorded on the capsule (ADR-0126). See the note below — the default is deliberate. |
| `timeout` | no | `""` | Wall-clock deadline in seconds. Empty means no NovaFabric-imposed limit. |
| `validate` | no | `true` | Run `nova validate` and fail the step if the capsule is invalid. |
| `upload-artifact` | no | `true` | Upload the capsule as a workflow artifact. |
| `artifact-name` | no | `novafabric-capsule` | Name for the uploaded artifact. |
| `retention-days` | no | `90` | How long to retain the artifact. |
| `novafabric-version` | no | `""` (latest) | Version specifier passed to pip, e.g. `0.105.0`. **Pin it** for reproducible CI. |
| `python-version` | no | `3.12` | NovaFabric requires 3.12+. |
| `extras` | no | `""` (core) | Extras to install, e.g. `all`. Core is ~113 MB; `all` is ~412 MB. |

### How `run` is executed — one command, spliced into a shell script

`run` is not a separate shell. GitHub pastes its text into the action's own bash
step, directly after `nova capture … --`. Quoting therefore works as you would
type it (`run: python -c 'print("a b")'` is fine), but **shell operators apply to
the whole line, not to the captured command**:

| `run:` value | What lands in the capsule |
|---|---|
| `pytest -q` | `pytest -q` — the normal case |
| `pytest -q && python report.py` | **only `pytest -q`**; `report.py` runs after the capture, unrecorded |
| `bash -c 'pytest -q && python report.py'` | both commands, as one captured process |

Measured by splicing each value into the action's capture line exactly as GitHub
does. For anything compound — `&&`, `|`, `;`, redirections — wrap it in
`bash -c '…'`, or better, put it in a script and capture the script.

Because the text is spliced into a script, **never put untrusted input in `run`**
(an issue title, a PR branch name, `${{ github.event.* }}` text): that is shell
injection into your CI job. This is GitHub's general
[script-injection rule](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions#understanding-the-risk-of-script-injections),
and it applies here because the action interpolates `run` the same way.

### Why `environment` defaults to `test` and not `ci`

`nova validate` prints a warning for any environment outside
`{development, test, staging, production}`. `ci` is a perfectly legitimate value
and is recorded verbatim — but it would produce a warning on **every** CI run,
and a warning that fires every time is a warning people learn to ignore. The
default is `test` so the common path is quiet, and a real warning still means
something. Override it when you genuinely have staging or production CI.

## Outputs

| Output | What it is |
|---|---|
| `run-id` | ULID of the captured run. |
| `capsule-path` | Filesystem path to the capsule directory. |
| `status` | `success` or `failure` — the captured command's own outcome. |
| `exit-code` | The captured command's exit code. |

> **⚠ Outputs are empty when the action fails.** GitHub does not propagate a
> composite action's declared outputs when the action itself fails, so a step
> reading `steps.capture.outputs.run-id` after a failed capture gets nothing.
> The action therefore *also* exports `NOVAFABRIC_RUN_ID`,
> `NOVAFABRIC_CAPSULE_PATH` and `NOVAFABRIC_STATUS` through `GITHUB_ENV`, which
> **do** survive. Use the environment variables in any `if: always()` step.

## What happens when the step fails

This is the case the action was built around, and it is worth understanding
because the obvious implementation gets it wrong.

**A failed run is evidence, not lost state.** NovaFabric writes a complete
capsule with `status: failure` for a command that crashed. So the action:

1. runs your command and records its exit code without aborting;
2. locates the capsule **regardless** of that exit code;
3. validates it, uploads it, writes the job summary;
4. and only then, as its **last** step, re-raises your command's exit code.

The ordering is load-bearing. A composite action abandons its remaining steps
once one of them fails — `always()` does not rescue them — so failing earlier
would skip the upload and lose the capsule from exactly the run that needed it.
The first two versions of this action failed that way and uploaded nothing for a
crashing command.

Your workflow still sees the real exit code, so a failing test still fails the
build. You just also get the capsule.

## Retrieving and replaying the capsule later

The artifact holds the capsule's **contents** at its root — `capsule.yaml`,
`env.lock`, `outputs/` and so on — not a directory named after the run.

**From your machine,** download it into a directory of its own. `gh run download`
extracts a single named artifact straight into the current directory unless you
pass `--dir`:

```bash
gh run download <workflow-run-id> --name capsule-<id> --dir capsule-<id>
nova validate ./capsule-<id>
nova replay --mode forensic ./capsule-<id>   # read-only: inspect, run nothing
nova replay ./capsule-<id>
```

**From a later job in the same workflow,** use `actions/download-artifact` — the
same step this repository's own
[`capture-action.yml`](https://github.com/MSKazemi/novafabric/blob/main/.github/workflows/capture-action.yml)
uses to prove a crashed run's capsule is recoverable:

```yaml
  inspect:
    needs: capture
    if: always()          # a failed capture still uploaded its capsule
    runs-on: ubuntu-latest
    steps:
      - uses: actions/download-artifact@v8
        with:
          name: capsule-${{ github.run_id }}
          path: capsule/
      - run: grep -E '^(status|exit_code):' capsule/capsule.yaml
```

To compare two CI runs — the classic "it passed yesterday" question — download
both and:

```bash
nova diff ./capsule-monday ./capsule-tuesday
```

## What this guide does not cover

- **Self-hosted runners and air-gapped CI.** The action installs from PyPI; an
  offline runner needs its own mirror.
- **Sealing capsules in CI** (`nova seal`, NovaSeal). That needs key material in
  the workflow and is a separate decision — see the NovaSeal guides.
- **Uploading to a collector.** The action writes an artifact; it does not push
  to a NovaFabric server.
- **GitLab CI, Jenkins, CircleCI, Buildkite.** The pattern generalises — wrap the
  command in `nova capture` and archive the output directory — but no packaged
  integration ships for them yet.
- **Matrix builds.** Give each matrix leg a distinct `artifact-name`, or the
  uploads collide.
- **Compound commands in `run`.** Supported only by wrapping them, as above —
  the action captures exactly one command.

## See also

- [`docs/cli-reference.md`](../cli-reference.md) — every flag `nova capture` takes.
- [`.github/actions/capture/action.yml`](https://github.com/MSKazemi/novafabric/blob/main/.github/actions/capture/action.yml)
  — the authoritative inputs, defaults and outputs.
- [`docs/integrations/README.md`](README.md) — the other integration guides.
