# NovaFabric Capture — GitHub Action

Capture any CI step as a portable, signed-capable, secret-redacted evidence
capsule, and keep it as a build artifact you can **replay and diff months
later** — long after the runner is gone and the logs have rotated.

```yaml
- uses: novafabric/novafabric/.github/actions/capture@main
  with:
    run: python my_agent.py
```

That is the whole integration. No application code changes.

## Why bother

CI logs answer *"did it pass?"*. They cannot answer:

- *Which package versions were actually installed when this passed last month?*
- *What did the agent's tool calls return on that run?*
- *Can I re-run that exact execution without the model provider?*
- *Has this record been edited since?*

A capsule answers all four. It carries the span tree, every model and tool call,
an environment lock, a redaction proof, and the inputs and outputs — as a plain
folder that verifies with no server and no network.

## Examples

**Capture an evaluation and keep the evidence on every push**

```yaml
- uses: novafabric/novafabric/.github/actions/capture@main
  with:
    run: python -m evals.nightly --suite regression
    artifact-name: nightly-eval-capsule
    retention-days: 365
```

**Pin the version so CI is reproducible**

```yaml
- uses: novafabric/novafabric/.github/actions/capture@main
  with:
    run: python agent.py
    novafabric-version: "0.100.1"
```

**Use the outputs in later steps**

```yaml
- id: capture
  uses: novafabric/novafabric/.github/actions/capture@main
  with:
    run: python agent.py

- name: Report
  run: |
    echo "run ${{ steps.capture.outputs.run-id }} finished ${{ steps.capture.outputs.status }}"
    ls "${{ steps.capture.outputs.capsule-path }}"
```

## Inputs

| Input | Default | Description |
|---|---|---|
| `run` | — | **Required.** The command to capture. |
| `environment` | `test` | Recorded on the capsule. See the note below. |
| `timeout` | — | Wall-clock deadline in seconds. |
| `validate` | `true` | Run `nova validate` and fail the step if the capsule is invalid. |
| `upload-artifact` | `true` | Upload the capsule as a workflow artifact. |
| `artifact-name` | `novafabric-capsule` | Artifact name. |
| `retention-days` | `90` | Artifact retention. |
| `novafabric-version` | latest | Pin for reproducible CI. |
| `python-version` | `3.12` | NovaFabric requires 3.12+. |
| `extras` | — | e.g. `all`. Core install is ~113 MB; `all` is ~412 MB and most CI does not need it. |

> **On `environment`:** the default is `test` because that is inside NovaFabric's
> conventional set (`development`, `test`, `staging`, `production`). A custom
> value like `ci` is legitimate and recorded verbatim, but `nova validate` warns
> about anything outside the set — and a warning on every run only teaches people
> to ignore warnings.

## Outputs

| Output | Also exported as | Description |
|---|---|---|
| `run-id` | `$NOVAFABRIC_RUN_ID` | ULID of the captured run |
| `capsule-path` | `$NOVAFABRIC_CAPSULE_PATH` | Path to the capsule directory |
| `status` | `$NOVAFABRIC_STATUS` | `success` or `failure` — the command's own outcome |
| `exit-code` | — | The captured command's exit code |

> **Use the environment variables if the capture may fail.** GitHub leaves a
> composite action's declared `outputs` **empty to the caller when the action
> fails** — which is exactly the case where you want the capsule. The `$NOVAFABRIC_*`
> variables are written to `GITHUB_ENV` and survive.

## Behaviour on failure — the part that matters

**A failing command still produces a capsule**, and the action still uploads it.

The step fails with the captured command's own exit code, so your workflow behaves
exactly as it did before. But the artifact survives, because *the capsule from a
failing run is the one anyone actually wants.*

Verified rather than assumed, and the verification changed the design. The first
version of this action failed the capture step immediately — and **uploaded
nothing**, because a composite action aborts its remaining steps once one fails
(`always()` does not rescue them). So the capsule from a crashing run, the one
this action exists to preserve, was the one case that lost it.

The capture step now always exits 0; validation and upload run; and a final step
re-raises the captured command's exit code. `.github/workflows/capture-action.yml`
proves it end to end by downloading the artifact from the failed run and asserting
it contains `status: failure` and `exit_code: 3`.

## Getting the capsule back

Download the artifact, then:

```bash
nova validate <capsule-dir>
nova replay <capsule-dir> --mode forensic
nova diff <capsule-a> <capsule-b>
```

[The auditor tutorial](https://github.com/novafabric/novafabric/blob/main/docs/tutorials/prove-a-run-to-an-auditor.md)
walks the whole path, including what it does *not* prove.

## Limitations

- **Linux and macOS runners.** The composite steps use `bash`; Windows runners are
  untested.
- **The capsule is not sealed by this action.** Signing needs a key, and shipping
  a key-management opinion inside a CI action would be the wrong default. Seal
  after download, or add a `nova seal sign` step with your own key material.
- **`run` is expanded by the shell**, so it accepts a normal command line but is
  not a substitute for a full script. For anything long, put it in a file and
  capture the file.
