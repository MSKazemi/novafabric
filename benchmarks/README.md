# NovaFabric capture-overhead benchmarks

This directory holds the capture-overhead harnesses measured against the
budgets in [ADR-0021 §6](../design/adr/0021-ai-factory-design-intent.md),
plus the SPK-COL spike harnesses gating
[ADR-0020](../design/adr/0020-cluster-scale-low-overhead-capture.md)
(cluster collector, gap-002).

The benchmarks are **stdlib-only** (no `pytest-benchmark` dependency)
and **not run in CI** (overhead measurement needs a stable runtime).
Run them locally when you change something that touches the hot path
(orchestrator, hooks, body adapters, semconv extractor) — and paste
the results into the PR.

## Running

```bash
# Capture vs raw subprocess for a no-op workload (baseline)
uv run python benchmarks/capture_overhead.py

# Capture vs raw subprocess for an httpx call to a fake endpoint
uv run python benchmarks/capture_overhead.py --workload httpx
```

Output is a small table printed to stdout: median, p95, mean, stdev
across N samples (default 30, configurable with `--n`).

## What the budgets are

ADR-0021 §6 sets these targets for the wire-level capture path:

- **No-op overhead (no captured calls):** ≤ 50 ms added to wall-clock
  time for `nova capture python -c "pass"` vs raw `python -c "pass"`.
  (Includes capsule allocation + env capture + secret scan + lineage.)
- **Per-captured-call overhead:** ≤ 5% added latency on a real LLM
  round-trip whose underlying network/inference time is ≥ 100 ms.
- **Compute-node hot-path overhead:** ≤ 1% (the strict budget — this
  is for HPC / cluster-scale scenarios where the proxy / hook is on
  the latency-critical path).

The benchmark prints the measured values; comparing them to the
budgets is a manual exercise (deliberately — local hardware varies).

## When to update

- After any change to `src/novafabric/capture/orchestrator.py` or
  `runners/`.
- After any new hook in `src/novafabric/capture/hooks/`.
- After any new body adapter in `src/novafabric/capture/body_adapters/`.
- Before cutting a release that touches the hot path.

The harness deliberately keeps results out of git. A run produces a
small text report you can paste into a PR description or attach to a
release note.

## Current measurement (v0.6.7 baseline)

A first run on developer hardware produced:

```
workload='noop'  n=30  warmup=2

raw subprocess            median=   12.4ms  p95=   13.9ms  mean=   12.6ms  stdev=   1.3ms
nova capture              median=  492.5ms  p95=  522.3ms  mean=  503.7ms  stdev=  17.8ms

capture overhead (median): +480.2 ms  (+3885.2%)
```

**This exceeds the ADR-0021 §6 ≤50ms no-op budget by ~10×.** The
benchmark exists precisely to surface findings like this. The bulk of
the overhead is amortized cost — capsule allocation, environment
capture, secret scanning, lineage emission, OpenLineage emission —
that's effectively constant per `nova capture` invocation regardless
of workload.

For the production-relevant case (LLM call with ≥100 ms upstream
latency) the percentage overhead drops to a much smaller fraction.
The ≤50 ms no-op budget is still the right target long-term;
optimization tickets to chase it down are queued for v0.7.x:

- Lazy-load lineage emission until at least one capsule artifact is
  present
- Defer secret scanning to capsule finalization (currently runs at
  open and at write)
- Skip OpenLineage emission entirely when no transport is configured
  (currently builds the event then short-circuits)
- Profile capsule allocation — likely the env-lock dump is the
  fattest single step

Documented here, not fixed in this commit — the v0.6.x program is
about feature completeness, v0.7.x will be about hot-path
optimization.

## Reproducibility

Numbers above are from one developer machine; your hardware will
differ. The point of running the harness in a PR is **comparison
between before and after the change**, not the absolute number.

## SPK-COL spike harnesses (ADR-0020 gate)

```bash
# SPK-COL-2 — per-event hot-path overhead, warm process, mock-LLM endpoint.
# RESOLVED on n1 2026-06-12: +0.366 ms (+0.36 %) per 100 ms call — PASS.
uv run python benchmarks/spk_col2_hotpath.py --n 200 --latency-ms 100

# SPK-COL-1 — offset-replay rebuild against NATS JetStream (needs a broker;
# on n1 the prod docker profile provides novafabric-nats on :4222).
# RESOLVED on n1 2026-06-12: PASS 3/3 (byte-equal rebuild, per-run order,
# RF1 restart no-loss).
uv run python benchmarks/spk_col1_offset_replay.py --runs 50 --events-per-run 200 \
    --restart-cmd "docker restart novafabric-nats"
```

Outcomes are recorded in
`design/research/novafabric-sota-landscape-2026/_spikes/SPK-COL.md`.

```bash
# SPK-COL-3 — OTel-Arrow vs OTLP+zstd wire A/B (needs otelcol-contrib binary
# in $WORK and docker for telemetrygen; see spk_col3/run_ab.sh header).
# RESOLVED on n1 2026-06-12: 31.5 % egress reduction, burst RSS bounded — PASS.
WORK=$HOME/spk-col3 bash benchmarks/spk_col3/run_ab.sh
```

All three SPK-COL spikes are resolved; ADR-0020 is Accepted.
