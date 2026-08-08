# Benchmarks

Every number here is reproducible with a command on this page. **If you cannot
re-run it, treat it as marketing, not measurement** — including these.

All figures below were measured on one machine on **2026-08-05** with NovaFabric
**0.100.1**. They are *not* a promise about your hardware; they are a starting
point and a method.

> **Hardware, stated because it changes every number:** 13th Gen Intel Core
> i7-13700 (24 threads), 62 GB RAM, Linux 6.8.0, Python 3.12. A CI runner has
> roughly a sixth of these cores and produces materially slower numbers — see
> [what we do *not* claim](#what-we-do-not-claim).

---

## 1. Capture overhead

**The question that matters:** how much does wrapping a workload in
`nova capture` cost?

```bash
uv run pytest tests/bench/test_capture_overhead_gate.py \
  --benchmark-json=capture.json
```

Measured over 30 full captured runs of a trivial command through
`CaptureOrchestrator(fast_emit=True)`:

| | ms |
|---|---|
| Min | 123.6 |
| **Median** | **168.8** |
| Mean | 165.3 |
| Max | 215.9 |
| Std dev | 26.9 |

**CI gate: p95 < 2000 ms**, enforced on every pull request by the
`capture-overhead-gate` job. The measured median sits about 12× under the
ceiling — the gate is deliberately loose because it must also pass on a shared
CI runner, and a gate that flakes gets ignored.

**Read this honestly.** This is the *fixed* cost of a capture: process setup,
hook installation, environment lock, redaction scan, and capsule write. It is
essentially constant, so it disappears against a workload of any real length —
a 40-second agent run pays under half a percent. For a workload that finishes in
10 ms, it dominates. **NovaFabric is not built for capturing 10 ms workloads.**

## 2. NovaSeal signing latency

**The question:** does sealing a capsule make the pipeline wait?

```bash
make benchmark
# or: uv run pytest tests/seal/test_benchmark.py --benchmark-json=seal.json
```

Over 100 rounds of `NovaSeal.seal()`:

| | ms |
|---|---|
| Min | 5.64 |
| **Median** | **6.86** |
| Mean | 7.50 |
| Max | 13.30 |
| Throughput | ~133 seals/sec |

**CI gate: p99 < 200 ms** (ADR-0041), enforced on every pull request.

This is Ed25519 over a Merkle root, so it is dominated by hashing the capsule
rather than by the signature itself — expect it to scale with capsule size, not
with the number of files.

## 3. Query engine — the measurement that changed a default

Worth including because it is the case where benchmarking **contradicted** the
design.

`nova query` shipped with DuckDB as the implicit default. Measuring it against
SQLite across 10 → 20,000 capsules found DuckDB **~20–25× slower**, at a flat
ratio — per-row index-build cost, not a crossover. Anyone installing the
`[query]`, `[scale]` or `[all]` extras was silently getting a 20× slower
`nova query` with no way to opt out.

The default is now SQLite. `NOVAFABRIC_QUERY_ENGINE=duckdb` opts back in.

A later pass moved the DuckDB index build to a columnar Arrow path — ~968 µs/row
to ~1.1 µs/row, and a build at n=5,000 from 5.14 s to 0.0125 s (411×). **The
default still did not change**, because the directory scan is 86–89% of total
time and the index build was never the bottleneck. Making the wrong thing 411×
faster changed nothing that a user can feel.

That is the whole reason this page exists.

---

## What we do *not* claim

- **No comparison benchmarks against other tools.** A benchmark you design
  against a competitor is a benchmark you win. If you want a comparison, run one
  on *your* workload; [the comparison page](comparison.md) says plainly where
  NovaFabric loses on capability.
- **No throughput claims at cluster scale.** The collector and node-spool tier is
  `experimental` and has not been measured at target scale. Saying otherwise
  would be exactly the overclaiming this project is supposed to make impossible.
- **These numbers are not a CI baseline.** A GitHub runner has far fewer cores;
  the same suite takes ~23 minutes there and under 4 locally. Do not use this
  page to diagnose a slow CI run.

## Reproducing all of it

```bash
git clone git@github.com:MSKazemi/novafabric.git
cd novafabric
uv sync --all-extras

make benchmark                  # seal latency, 100 rounds
uv run pytest tests/bench/test_capture_overhead_gate.py   # capture overhead, 30 runs
```

Both write a `--benchmark-json` artifact you can diff against these figures. If
your numbers differ substantially and your hardware is comparable,
[that is a bug report we want](https://github.com/MSKazemi/novafabric/issues/new?template=bug_report.yml).
