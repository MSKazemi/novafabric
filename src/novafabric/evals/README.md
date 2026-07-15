# `novafabric.evals`

The **standard eval-suite adapter layer**: `EvalSuiteAdapter`, `EvalResult`,
`Metric`/`MetricComparison`, and `RegressionDetector` — the common interface
that named suites (GAIA, SWE-bench, AgentBench, MMLU, Smoke) implement.

**Not to be confused with [`novafabric.eval`](../eval/) — the offline
evaluation engine** (scoring, significance, regression diff). `evals` = suite
adapters; `eval` = the machinery they feed.
