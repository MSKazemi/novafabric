"""In-process micro-benchmark of the CaptureOrchestrator only.

The wall-clock-comparing harness in ``capture_overhead.py`` measures
``nova capture <cmd>`` end-to-end including the cost of the outer
Python interpreter starting + importing typer/rich/etc. for the
``nova`` CLI itself, plus a second Python startup for the workload's
subprocess. Those startup costs dominate (~400 ms combined) and do
not move when orchestrator code changes.

This micro-benchmark imports the orchestrator inside an already-warm
Python process and measures only its ``run()`` time. That isolates
the orchestrator-internal overhead the v0.6.8 optimizations target.

Usage::

    uv run python benchmarks/orchestrator_microbench.py
    uv run python benchmarks/orchestrator_microbench.py --n 100
"""
from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

from novafabric.capture.orchestrator import CaptureOrchestrator


def _measure_one(orch: CaptureOrchestrator) -> float:
    """One orchestrator.run() call. Workload is the cheapest Python
    subprocess we can spawn — a no-op `pass`. The remaining cost is
    interpreter startup + sitecustomize import + orchestrator overhead."""
    t0 = time.perf_counter()
    orch.run(command=[sys.executable, "-c", "pass"])
    return time.perf_counter() - t0


def _summarize(label: str, samples: list[float]) -> str:
    median = statistics.median(samples)
    mean = statistics.mean(samples)
    stdev = statistics.stdev(samples) if len(samples) > 1 else 0.0
    p95 = sorted(samples)[max(0, int(len(samples) * 0.95) - 1)]
    return (
        f"{label:32s}  "
        f"median={median*1000:7.1f}ms  "
        f"p95={p95*1000:7.1f}ms  "
        f"mean={mean*1000:7.1f}ms  "
        f"stdev={stdev*1000:6.1f}ms  "
        f"(n={len(samples)})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=3)
    args = parser.parse_args(argv)

    print(
        f"In-process orchestrator.run() micro-benchmark  "
        f"n={args.n}  warmup={args.warmup}\n"
    )

    with tempfile.TemporaryDirectory(prefix="nf_microbench_") as tmpdir:
        orch = CaptureOrchestrator(base_dir=Path(tmpdir) / "runs")

        # Warm-up to amortize first-call lazy imports.
        for _ in range(args.warmup):
            _measure_one(orch)

        samples = [_measure_one(orch) for _ in range(args.n)]

    print(_summarize("orchestrator.run() in-process", samples))
    print()
    print("Notes:")
    print("  - The cost INCLUDES one inner Python subprocess startup +")
    print("    sitecustomize hook-loader import (those are inside the")
    print("    measured subprocess.run() call).")
    print("  - It EXCLUDES the outer `nova` CLI startup that the wall-")
    print("    clock harness in capture_overhead.py captures.")
    print("  - This is the right number to compare across orchestrator")
    print("    code changes. Use capture_overhead.py for user-perceived")
    print("    wall-clock.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
