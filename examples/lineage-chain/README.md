# lineage-chain

**Use this when you want to:** see what NovaFabric's lineage graph
actually answers — "if this dataset changes, which runs do I have to
re-evaluate?" and "what did this run depend on?"

## What it does

Three captures of `step.py`. Each declares it consumed the shared asset
`local:datasets/training-set@1.0.0` by appending one line to its capsule's
`assets.jsonl`. The capture orchestrator's lineage writer turns that into
a `consumed` edge automatically and indexes it into the local SQLite
lineage graph.

After three captures, you have three runs that all depend on the same
asset — the simplest realistic lineage shape.

## Run it

```bash
RUNS=examples/lineage-chain/runs

nova capture --output-dir $RUNS python examples/lineage-chain/step.py train-v1
nova capture --output-dir $RUNS python examples/lineage-chain/step.py eval-v1
nova capture --output-dir $RUNS python examples/lineage-chain/step.py promote-v1
```

Three capsule directories now exist under `examples/lineage-chain/runs/`,
and the local lineage DB has been updated as each capture finished.

## Query the graph

### Blast-radius: which runs depend on this dataset?

```bash
nova lineage blast-radius local:datasets/training-set@1.0.0
```

You should see all three runs (`train-v1`, `eval-v1`, `promote-v1`) listed
as dependents of the shared dataset. This is the "if I change this asset,
what do I need to re-evaluate?" question.

### Provenance: what did this run depend on?

```bash
# Pick any of the three run IDs printed by `ls $RUNS`:
nova lineage provenance <run-id>
```

You should see the run's edge to `local:datasets/training-set@1.0.0`.

### Re-import (if you ever lose the local DB)

```bash
nova lineage import $RUNS
```

The graph is fully derivable from capsule contents — losing the DB only
means re-running `import`.
