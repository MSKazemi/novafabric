# Security & Provenance Knowledge Graph (SPKG)

**Status:** Experimental (SPKG, ADR-0111)

The SPKG turns the lineage a capsule already records into a **security knowledge
graph** you can reason over: detect anomalous agent runs, answer "which runs
touched a poisoned model," and trace attack paths and blast radius across a run.

It is **opt-in** and never blocks any other `nova` command. If you never call
`nova kg`, nothing about capture, replay, or lineage changes.

Related:
- [CLI reference § nova kg](cli-reference.md#nova-kg--capsule-knowledge-graph-v0170-adr-0067) — exhaustive flags for every `nova kg` command
- [Concepts](concepts.md) — the capsule format and lineage edges the SPKG reads

---

## What it is, and why

Every capsule already carries a `lineage.jsonl` file: typed edges
(`source → target`, each with a `kind:ref`, an `edge_type`, the owning
`capsule_run_id`, and a `created_at` timestamp). On its own that is a record of
*what happened*. The SPKG lifts those edges into a graph you can **query for
security questions**:

- **Unify provenance into one graph.** Map the capsule's lineage to a canonical
  W3C **PROV-O** RDF graph — a standard, portable representation of who/what
  derived what.
- **Detect anomalous agent runs.** Rank the most surprising lineage edges in a
  run against a "normal" baseline, without any labels.
- **Answer "which runs touched a poisoned model."** Traverse the blast radius of
  an entity to see everything it reached (impact) or everything that produced it
  (provenance).
- **Trace lateral movement.** Find the shortest attack path between two entities,
  e.g. from an attacker-controlled run to a credentials dataset.

### Two layers

The SPKG has two layers, built from the *same* capsule and kept in sync:

| Layer | What it is | Built by | Used for |
|---|---|---|---|
| **Canonical PROV-O** | W3C PROV-O RDF, SHACL-validated | `nova kg build-provenance`, `nova kg build` | Portable, standards-based provenance facts; the validation gate |
| **Operational graph** | A KùzuDB labeled-property graph (LPG) | `nova kg build` | Fast traversal for `detect`, `attack-path`, `blast-radius` |

The canonical layer is validated **first** (SHACL, ADR-0111 R11). On validation
failure, nothing is written to the operational store — invalid provenance facts
never reach the queryable graph. The operational graph holds no state that is not
derivable from a capsule, so it can always be rebuilt from source.

### Findings map to ATT&CK / D3FEND

Every anomaly finding from `nova kg detect` carries a **MITRE ATT&CK** technique —
a raw anomaly score alone is rejected by the SHACL `nf:FindingShape` constraint
(ADR-0111 R2). Examples: a shell tool call maps to
`T1059.004` (Unix Shell), a credentials-touching edge maps to `T1078` (Valid
Accounts). This makes findings actionable and mappable to D3FEND countermeasures
rather than an opaque number.

---

## Install

The anomaly detector (`nova kg detect`) is **pure standard library** and needs no
optional extra. The RDF and graph features need the `spkg` extra:

```bash
pip install 'novafabric[spkg]'   # rdflib + pyshacl + kuzu (all permissively licensed)
```

| Command | Needs `[spkg]`? |
|---|---|
| `nova kg detect` | No — stdlib only |
| `nova kg build-provenance` | Yes |
| `nova kg build` | Yes |
| `nova kg attack-path` | Yes |
| `nova kg blast-radius` | Yes |

---

## Quickstart

Assume you have a captured capsule at `.novafabric/runs/01HXAY7M/` (see
[Getting Started](getting-started.md) for how to produce one).

### 1. Build the graph

Build both SPKG layers — the SHACL-gated PROV-O RDF and the operational KùzuDB
store — from the capsule's lineage:

```bash
nova kg build .novafabric/runs/01HXAY7M
# ✓ SPKG built from 01HXAY7M (SHACL-valid): 7 PROV-O triples · 3 LPG edges → .nova/kg/spkg.kuzu
```

The store path defaults to `.nova/kg/spkg.kuzu` (override with `--path` or
`NOVA_SPKG_PATH`). To *export* the canonical RDF instead of populating the store,
use `nova kg build-provenance … -o prov.ttl`.

### 2. Detect anomalous edges

Rank the most anomalous lineage edges. With no baseline, the detector
self-baselines on the target; pass one or more `--baseline` capsules to learn
"normal" from a known-good corpus:

```bash
# Self-baseline, top 10
nova kg detect .novafabric/runs/01HXAY7M -k 10

# Baseline against known-good runs, machine-readable output
nova kg detect suspect/ --baseline normal-week-1/ --baseline normal-week-2/ \
  --json -o findings.json
```

The table lists rank, score, edge type, `source → target`, and the MITRE ATT&CK
technique for each flagged edge. `--json` emits schema-valid `AnomalyFinding`
records instead. A finding is informational, not a failure — `detect` exits `0`.

### 3. Trace an attack path

Ask whether a path exists between two entities (each written as `kind:ref`):

```bash
nova kg attack-path .novafabric/runs/01HXAY7M \
  --from run:attacker --to dataset:aws_credentials
# ⚠ Attack path found: run:attacker → … → dataset:aws_credentials in 3 hop(s)
```

If no path exists within `--max-depth` (default 6), it prints
`✓ No attack path …`. Either way the command exits `0` (informational).

### 4. Compute blast radius

See what an entity affected (downstream / impact) or where it came from
(upstream / provenance):

```bash
# What did the poisoned model touch?
nova kg blast-radius .novafabric/runs/01HXAY7M --entity model:poisoned-model

# Where did this artifact come from?
nova kg blast-radius .novafabric/runs/01HXAY7M --entity artifact:report.md --upstream
```

`--downstream` is the default. Both print a table of affected entities
(`kind`, `ref`).

---

## Status & limitations

The SPKG is **experimental** (ADR-0111). The commands below ship today; the graph
is built per-capsule and scoped to that capsule's lineage.

**Works today (experimental):**

- `nova kg build-provenance` — capsule lineage → SHACL-validated PROV-O RDF
- `nova kg build` — both layers (PROV-O + operational KùzuDB LPG)
- `nova kg detect` — unsupervised, label-free edge-level anomaly ranking with
  ATT&CK mapping (no extra required)
- `nova kg attack-path` — bounded shortest-path query between two entities
- `nova kg blast-radius` — downstream (impact) / upstream (provenance) traversal

**Planned (design intent, not yet shipped):**

- 1M-edge scale benchmarking
- Apache AGE / pgvector server tier for graphs beyond a single node
- GNN-based detectors (PyGOD / TGN) as a resource-gated upgrade over the stdlib
  baseline detector
- Hybrid vector + graph retrieval
- Dashboard anomaly overlay

Scores from `nova kg detect` are **relative structural surprisal**, not calibrated
probabilities, and findings are informational signals for a human reviewer — not
an automated verdict.
