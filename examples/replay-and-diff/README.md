# replay-and-diff

**Use this when you want to:** see why replay and diff matter — capture
two runs of the same script with a controlled behavior change, then have
NovaFabric tell you exactly what differs.

This is the regression-detection workflow at the core of NovaFabric's
value: you can prove an agent's behavior changed, structurally, without
re-reading logs.

## What it does

`agent.py` prints a small JSON result. Its behavior is controlled by the
`AGENT_MODE` env var:

- `AGENT_MODE=baseline` → `score: 0.85`
- `AGENT_MODE=regressed` → `score: 0.62`

The example captures both, then diffs them.

## Run it

```bash
# Capture the baseline
AGENT_MODE=baseline nova capture \
  --output-dir examples/replay-and-diff/runs \
  python examples/replay-and-diff/agent.py

# Capture the regression
AGENT_MODE=regressed nova capture \
  --output-dir examples/replay-and-diff/runs \
  python examples/replay-and-diff/agent.py
```

Two capsule directories now exist under `examples/replay-and-diff/runs/`.

## Inspect either capsule (replay, forensic mode)

```bash
# List the captured runs
ls examples/replay-and-diff/runs/

# Forensic replay = read-only inspection, no re-execution, no network.
nova replay examples/replay-and-diff/runs/<run-id-of-baseline>/ --mode forensic
```

## Diff the two captures

```bash
nova diff \
  examples/replay-and-diff/runs/<run-id-of-baseline>/ \
  examples/replay-and-diff/runs/<run-id-of-regressed>/
```

You should see structural differences between the two runs.

## Use as a CI gate

```bash
nova diff cap-a/ cap-b/ --assert-no-regressions
# exits 1 if any changes detected — wire into CI
```
