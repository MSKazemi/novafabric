# spkg-anomaly-scan

**Use this when you want to:** see the Security & Provenance Knowledge Graph
(SPKG, ADR-0111) flag a suspicious lineage edge — *"which action in this run
doesn't look like anything my fleet normally does?"* — with a MITRE ATT&CK
label instead of a bare score. See the
[CLI reference](../../docs/cli-reference.md#nova-kg-detect) for all flags.

> **Status:** experimental. The anomaly scanner (`nova kg detect`) needs **no**
> optional extra. The RDF/graph commands (`nova kg build-provenance`, `nova kg
> build`) need `pip install 'novafabric[spkg]'`.

## What it does

`make_fixture.py` writes one capsule whose `lineage.jsonl` is 40 benign edges
(runs reading the training set, writing model artifacts) plus **one planted
attack edge**: `run-evil --executes--> tool:/bin/shell`. A shell exec never
appears in the benign distribution, so it is a structural outlier.

The detector learns the fleet's own edge distribution and scores each edge by
combined surprisal — unsupervised, no labels, no training data required.

## Run it

```bash
# 1. Write the demo capsule (benign fleet + one planted shell exec)
python examples/spkg-anomaly-scan/make_fixture.py

# 2. Scan it — the planted edge should rank first, mapped to ATT&CK T1059.004
nova kg detect examples/spkg-anomaly-scan/capsule/ -k 3
```

You should see a table topped by the `executes → tool:/bin/shell` edge with a
score near `1.000` and the `T1059.004` (Command and Scripting Interpreter:
Unix Shell) technique. Every reported edge carries a technique — a raw anomaly
score alone is rejected (ADR-0111 R2).

### Baseline on a known-good corpus

In production you learn "normal" from prior clean runs, then scan new ones:

```bash
nova kg detect suspect-run/ --baseline good-week-1/ --baseline good-week-2/
```

### Machine-readable findings for a pipeline

```bash
nova kg detect examples/spkg-anomaly-scan/capsule/ --json -o findings.json
```

Each record is a schema-valid `AnomalyFinding` (see
`schemas/spkg-anomaly-finding-v1.schema.json`) with an `explanation.attack_technique_id`.

## Build the provenance graph (optional, needs the extra)

```bash
pip install 'novafabric[spkg]'

# Canonical W3C PROV-O RDF, SHACL-validated on the way out
nova kg build-provenance examples/spkg-anomaly-scan/capsule/ -o provenance.ttl

# Both layers: canonical RDF + operational KùzuDB graph
nova kg build examples/spkg-anomaly-scan/capsule/
```

The graph is fully derivable from the capsule — there is no state in the SPKG
that cannot be rebuilt from `lineage.jsonl` (ADR-0111 R4).
