"""Capture-overhead benchmark — p95 CI gate (ROADMAP W1; ADR-0021 §6, ADR-0092).

Benchmark: 30 full captured runs of a trivial ``python -c pass`` workload
through ``CaptureOrchestrator(fast_emit=True)`` (the ADR-0092 slice-B path).
CI gate:   p95 wall-clock per captured run must stay below 2000 ms.

Why p95 (the NovaSeal gate uses p99): at 30 samples the nearest-rank p99
equals the single worst round, which on shared CI runners is dominated by
scheduler noise (one observed 2.08 s outlier against a 342 ms median).
p95 excludes exactly that one round while still bounding tail latency.

Budget rationale: v0.54 measured ~464 ms for a compute-only workload with
fast-emit on a warm laptop (docs/releases/v0.54.0.md). The 2000 ms ceiling is
deliberately generous (~4x) to absorb cold-cache and shared-CI-runner noise
while still catching order-of-magnitude regressions (e.g. re-introducing
eager SDK imports on the capture startup path).

Normal unit-test run (--benchmark-disable):
    The p95 assertion is skipped because there is only one timing sample;
    the benchmark still performs one captured run to verify correctness.

Dedicated CI step (no --benchmark-disable):
    uv run pytest tests/bench/test_capture_overhead_gate.py -v \\
        --benchmark-json=bench-results/capture_overhead.json
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

from novafabric.capture.orchestrator import CaptureOrchestrator

_P95_LIMIT_S = 2.000  # 2000 ms — CI-safe ceiling over the ~464 ms warm baseline
_ROUNDS = 30
_WARMUP_ROUNDS = 2


def test_capture_overhead_p95_gate(
    benchmark: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Full captured run of ``python -c pass`` p95 must be below 2000 ms.

    Each round performs a complete capture (subprocess spawn, hook install via
    fast-emit, capsule write) into a fresh run directory under a shared
    base_dir, so on-disk capsule accumulation matches real usage.

    The gate is skipped when --benchmark-disable is active (< 10 samples),
    which is the default for the regular unit-test step. It is enforced in
    the dedicated capture-overhead-gate CI step.
    """
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs", fast_emit=True)
    command = [sys.executable, "-c", "pass"]
    last_exit: dict[str, int | None] = {"code": None}

    def _captured_run() -> None:
        result = orch.run(command=command)
        last_exit["code"] = result.exit_code

    benchmark.pedantic(  # type: ignore[attr-defined]
        _captured_run, rounds=_ROUNDS, iterations=1, warmup_rounds=_WARMUP_ROUNDS
    )

    # Correctness holds in both modes: the fixture calls the function at
    # least once even under --benchmark-disable.
    assert last_exit["code"] == 0, "captured `python -c pass` must exit 0"

    # benchmark.stats is None when --benchmark-disable is active; the fixture
    # still calls the function once for correctness but collects no timing
    # data. In pytest-benchmark 5.x, Metadata.stats is the inner Stats object
    # with the per-round timing list in its .data attribute.
    meta = benchmark.stats  # type: ignore[attr-defined]
    raw: list[float] = meta.stats.data if meta is not None else []
    if len(raw) < 10:
        pytest.skip("Too few samples — run without --benchmark-disable to enforce the p95 gate")

    p95 = _p95(raw)
    assert p95 < _P95_LIMIT_S, (
        f"captured run p95={p95 * 1000:.1f}ms exceeds "
        f"{_P95_LIMIT_S * 1000:.0f}ms CI gate "
        f"(n={len(raw)}, "
        f"min={min(raw) * 1000:.1f}ms, "
        f"median={sorted(raw)[len(raw) // 2] * 1000:.1f}ms, "
        f"max={max(raw) * 1000:.1f}ms)"
    )


def _p95(data: list[float]) -> float:
    """Nearest-rank 95th percentile without external dependencies."""
    s = sorted(data)
    idx = min(math.ceil(0.95 * len(s)) - 1, len(s) - 1)
    return s[idx]
