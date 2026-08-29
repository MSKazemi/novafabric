# Notebook capture

**Status: works today**, in two specific shapes. This page documents both, and is
equally explicit about the shape that does *not* exist — see
[What is not captured](#what-is-not-captured).

NovaFabric ships **no notebook-specific code**: there is no IPython magic, no
kernel hook, no `%%nova` cell decorator. Notebook capture is done entirely with
the two general mechanisms below.

```
examples/notebook-capture/
├── analysis.ipynb   a deterministic, stdlib-only notebook (no API key, no network)
├── run.sh           the documented command; skips cleanly with no Jupyter installed
└── README.md
```

## Pattern 1 — capture the whole notebook run (recommended)

`nova capture` wraps any command, and executing a notebook is just a command:

```bash
nova capture --output-dir ./capsules -- \
  jupyter nbconvert --to notebook --execute analysis.ipynb \
                    --output-dir ./capsules --output executed.ipynb
```

`./run.sh` runs exactly that. One capsule per notebook execution, reproducible in
CI. Verified: `status: success`, `exit_code: 0`.

## Pattern 2 — capture one cell's work in-process

The closest thing to capturing "a notebook cell". Put this in a cell:

```python
from novafabric.sdk.agent import agent

@agent(name="notebook-cell", version="0.1.0", capsule_dir="capsules")
def analyse():
    ...          # LLM calls made in here are recorded
    return result

analyse()
```

Re-running the cell writes a capsule per call. Verified in a real kernel.

Note the two differ: `nova capture --output-dir DIR` creates `DIR/<run_id>/`,
while `@agent(capsule_dir=DIR)` treats `DIR` itself as the capsule directory.

## Requirement: one environment

**The Jupyter kernel must run in the same environment where `novafabric` is
installed.** This is not a style preference — when the kernel is a different
virtualenv, capture degrades silently:

| | separate envs | same env |
|---|---|---|
| capture hooks | `hook install failed: No module named 'novafabric'` (stderr only) | installed |
| `model-calls.jsonl` | empty — **every LLM call is missed** | recorded |
| Python in `capsule.yaml` | `3.14.6` — the `nova` process's interpreter, **not the kernel's** | `3.12.3`, matching the kernel |
| run status | `success` | `success` |

Both columns report `status: success`. The failure is visible only in
`outputs/stderr.txt`, so a capsule from a mismatched environment looks complete
and is not. Install the example's extras beside NovaFabric:

```bash
pip install nbconvert ipykernel     # into the SAME env as novafabric
```

`nbconvert` is an extra for this example only — never a NovaFabric dependency.

## What is captured

- The notebook execution as a process: exit code, status, duration, command.
- `env.lock` — the resolved environment (correct only under one environment, above).
- `model-calls.jsonl` / `tool-calls.jsonl` — LLM and tool traffic from cells,
  via the normal wire hooks.
- `outputs/stderr.txt` — nbconvert's own progress and warnings.
- The usual `redaction-proof.json`, `trace.jsonl`, `assets.jsonl`, `replay.yaml`.

## What is not captured

Stated plainly, because each of these is easy to assume and wrong:

1. **Cell outputs.** A notebook's `print()` and display output is routed to the
   notebook document by the kernel, never to the process stdout. In a capture of
   `analysis.ipynb` the strings the notebook prints appear **nowhere in the
   capsule** — verified by grepping the whole capsule for them. Under pattern 1
   the capsule has no `outputs/stdout.txt` at all.
2. **The executed notebook.** `--output-dir` points nbconvert wherever you say;
   it is not placed inside the capsule. If the executed notebook is your
   evidence, write results to a file the capture records, or attach it
   deliberately.
3. **Per-cell boundaries.** One capsule covers the whole run. Nothing records
   which cell an event came from, cell execution order, or per-cell timing.
4. **Interactive sessions.** Capturing cells as you run them by hand in Jupyter
   Lab is not supported; the kernel is already running when you would attach.
   Pattern 2 is the supported in-notebook route.

Point 1 is why `analysis.ipynb` writes `results.json` rather than only printing:
a file survives into evidence, a `print()` does not.

## Reproduce

```bash
./run.sh                 # skips with a message if Jupyter is absent
./run.sh /tmp/capsules   # or choose the output directory
```

## See also

- [`examples/docker-run/`](../docker-run/) — capture inside a container.
- [`examples/hpc-slurm-job/`](../hpc-slurm-job/) — capture under a batch scheduler.
