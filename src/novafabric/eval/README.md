# `novafabric.eval`

The **offline evaluation engine**: eval cards, scoring, statistical
significance, regression diffing, dataset provenance, and the run harness
(`card.py`, `offline.py`, `runner.py`, `scores.py`, `significance.py`,
`regression_diff.py`, `dataset_provenance.py`).

**Not to be confused with [`novafabric.evals`](../evals/) — the standard
eval-*suite* adapter layer** (GAIA, SWE-bench, AgentBench, …). `eval` = the
evaluation/scoring machinery; `evals` = adapters that plug named suites into it.
