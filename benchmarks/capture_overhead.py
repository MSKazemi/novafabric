"""Capture-overhead benchmark for NovaFabric (ADR-0021 §6).

Measures the wall-clock cost of running a small workload under
``nova capture`` vs. running it raw. Stdlib-only, no extra deps.

Usage::

    uv run python benchmarks/capture_overhead.py
    uv run python benchmarks/capture_overhead.py --workload httpx
    uv run python benchmarks/capture_overhead.py --n 50 --workload noop

The report is plain text: a small table you can paste into a PR
description. No JSON output, no CI integration — overhead measurement
is a local-hardware exercise.
"""
from __future__ import annotations

import argparse
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

# ── Workloads (each is a short Python snippet) ───────────────────────────────

_WORKLOADS = {
    "noop": "pass",
    "httpx": (
        "import httpx\n"
        "try:\n"
        "    httpx.post('https://api.openai.com/v1/chat/completions',\n"
        "               json={'model': 'gpt-4o', 'messages': []},\n"
        "               timeout=httpx.Timeout(connect=2.0, read=2.0,\n"
        "                                      write=2.0, pool=2.0))\n"
        "except Exception:\n"
        "    pass\n"
    ),
}


def _time_one(cmd: list[str]) -> float:
    """Run ``cmd`` once and return its wall-clock duration in seconds."""
    t0 = time.perf_counter()
    subprocess.run(cmd, capture_output=True, check=False)
    return time.perf_counter() - t0


def _summarize(label: str, samples: list[float]) -> str:
    median = statistics.median(samples)
    mean = statistics.mean(samples)
    stdev = statistics.stdev(samples) if len(samples) > 1 else 0.0
    p95 = sorted(samples)[max(0, int(len(samples) * 0.95) - 1)]
    return (
        f"{label:24s}  "
        f"median={median*1000:7.1f}ms  "
        f"p95={p95*1000:7.1f}ms  "
        f"mean={mean*1000:7.1f}ms  "
        f"stdev={stdev*1000:6.1f}ms  "
        f"(n={len(samples)})"
    )


def _time_one_env(cmd: list[str], env: dict[str, str]) -> float:
    t0 = time.perf_counter()
    subprocess.run(cmd, capture_output=True, check=False, env=env)
    return time.perf_counter() - t0


def _daemon_arm(workload_src: str, n: int, warmup: int) -> list[float] | None:
    """Measure ``novacap`` against a warm daemon. ADR-0092: this is the
    cold-start-eliminated path. Returns None if the scripts are unavailable."""
    novacap_bin = shutil.which("novacap")
    nova_bin = shutil.which("nova")
    if novacap_bin is None or nova_bin is None:
        return None
    with tempfile.TemporaryDirectory(prefix="nf_bench_daemon_") as home:
        env = {**os.environ, "NOVAFABRIC_HOME": home}
        env.pop("NOVAFABRIC_CAPTURE_SOCKET", None)
        sock = os.path.join(home, "run", "capture.sock")
        daemon = subprocess.Popen(
            [nova_bin, "daemon", "start"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline and not os.path.exists(sock):
                time.sleep(0.1)
            if not os.path.exists(sock):
                return None
            cmd = [novacap_bin, sys.executable, "-c", workload_src]
            for _ in range(warmup):
                _time_one_env(cmd, env)
            return [_time_one_env(cmd, env) for _ in range(n)]
        finally:
            daemon.terminate()
            try:
                daemon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                daemon.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--workload", choices=list(_WORKLOADS.keys()), default="noop",
        help="Which workload to measure (default: noop).",
    )
    parser.add_argument(
        "--n", type=int, default=30,
        help="Sample count per condition (default: 30).",
    )
    parser.add_argument(
        "--warmup", type=int, default=2,
        help="Discarded warm-up runs per condition (default: 2).",
    )
    parser.add_argument(
        "--with-daemon", action="store_true",
        help="Also measure the warm-daemon path (novacap) — ADR-0092.",
    )
    args = parser.parse_args(argv)

    workload_src = _WORKLOADS[args.workload]
    raw_cmd = [sys.executable, "-c", workload_src]

    nova_bin = shutil.which("nova")
    if nova_bin is None:
        print("ERROR: `nova` not on PATH. Install novafabric (e.g. uv pip install -e .)",
              file=sys.stderr)
        return 1

    # Use a tmpdir for capsules so we don't pollute the user's cwd.
    with tempfile.TemporaryDirectory(prefix="nf_bench_") as tmpdir:
        capture_cmd = [
            nova_bin, "capture", "--output-dir", tmpdir,
            sys.executable, "-c", workload_src,
        ]

        print(f"workload={args.workload!r}  n={args.n}  warmup={args.warmup}\n")

        # Warm-up.
        for _ in range(args.warmup):
            _time_one(raw_cmd)
            _time_one(capture_cmd)

        # Measure.
        raw_samples = [_time_one(raw_cmd) for _ in range(args.n)]
        capture_samples = [_time_one(capture_cmd) for _ in range(args.n)]

    daemon_samples: list[float] | None = None
    if args.with_daemon:
        daemon_samples = _daemon_arm(workload_src, args.n, args.warmup)

    print(_summarize("raw subprocess", raw_samples))
    print(_summarize("nova capture", capture_samples))
    if daemon_samples:
        print(_summarize("novacap (warm daemon)", daemon_samples))
    print()

    raw_median = statistics.median(raw_samples)
    cap_median = statistics.median(capture_samples)
    overhead_ms = (cap_median - raw_median) * 1000
    overhead_pct = (cap_median / raw_median - 1) * 100 if raw_median > 0 else 0.0

    print(f"capture overhead (median): {overhead_ms:+.1f} ms  ({overhead_pct:+.1f}%)")
    if daemon_samples:
        dae_median = statistics.median(daemon_samples)
        saved_ms = (cap_median - dae_median) * 1000
        saved_pct = (1 - dae_median / cap_median) * 100 if cap_median > 0 else 0.0
        print(
            f"warm-daemon vs nova capture (median): "
            f"-{saved_ms:.1f} ms  (-{saved_pct:.1f}% per-run; cold-start removed)"
        )
    print()
    print("Budget reference (ADR-0021 §6):")
    print("  noop          : ≤ 50 ms added wall-clock")
    print("  per-call (LLM ≥ 100ms): ≤ 5% added latency")
    print("  compute-node hot path  : ≤ 1% (strict)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
