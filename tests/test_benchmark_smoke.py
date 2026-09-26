"""Smoke test for the capture-overhead benchmark harness.

Confirms the harness imports cleanly and runs end-to-end with the
smallest possible sample size. Does NOT enforce timing — that's
deliberately a manual exercise per benchmarks/README.md.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from _help_assert import assert_flag_in_help

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
    assert_flag_in_help(result, "--workload")


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


def test_harness_finds_nova_next_to_interpreter_without_path() -> None:
    """Regression: the harness must run the ``nova`` installed beside
    ``sys.executable`` even when that venv's bin dir is not on ``$PATH``
    (venv invoked by absolute path, not activated). Previously it used
    ``shutil.which("nova")`` only and exited 1 with "`nova` not on PATH"."""
    env = {**os.environ, "PATH": os.defpath}
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--n", "1", "--warmup", "0"],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert result.returncode == 0, (
        f"benchmark exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "nova capture" in result.stdout
    assert "capture overhead" in result.stdout
