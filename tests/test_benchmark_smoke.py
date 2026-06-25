"""Smoke test for the capture-overhead benchmark harness.

Confirms the harness imports cleanly and runs end-to-end with the
smallest possible sample size. Does NOT enforce timing — that's
deliberately a manual exercise per benchmarks/README.md.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parent.parent / "benchmarks" / "capture_overhead.py"


def test_harness_file_exists() -> None:
    assert HARNESS.is_file(), f"benchmark harness missing: {HARNESS}"


def test_harness_help_works() -> None:
    """--help exits 0 and mentions the workload flag."""
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0
    assert "--workload" in result.stdout


def test_harness_runs_with_minimum_samples() -> None:
    """Run with --n 1 --warmup 0 to keep test runtime bounded; verify
    the output contains the expected report sections."""
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--n", "1", "--warmup", "0"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"benchmark exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # Both rows present in the table.
    assert "raw subprocess" in result.stdout
    assert "nova capture" in result.stdout
    # Overhead line + budget reference present.
    assert "capture overhead" in result.stdout
    assert "Budget reference" in result.stdout
