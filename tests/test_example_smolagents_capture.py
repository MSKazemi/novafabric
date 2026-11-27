"""Regression test for examples/smolagents-capture (experimental, since v0.109.0).

The no-library path always runs; the live path runs wherever ``smolagents`` is importable (it is not a
NovaFabric dependency), so the example cannot silently rot once it is installed in some environment.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "smolagents-capture" / "agent.py"


def _run(cwd: Path, *args: str, with_libs: bool) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("NOVAFABRIC_CAPSULE_DIR", None)
    env["NOVAFABRIC_HOME"] = str(cwd / "home")
    env["NOVAFABRIC_AUDIT_LOG_PATH"] = str(cwd / "audit.log")
    if with_libs:  # a library found only via sys.path edits (not installed) must reach the child
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return subprocess.run(
        [sys.executable, str(EXAMPLE), *args],
        capture_output=True, text=True, env=env, cwd=cwd, timeout=120,
    )


def test_example_file_exists() -> None:
    assert EXAMPLE.is_file()


def test_runs_and_records_two_steps_and_a_tool_call_when_smolagents_is_installed(
    tmp_path: Path,
) -> None:
    pytest.importorskip("smolagents")
    result = _run(tmp_path, str(tmp_path / "runs"), with_libs=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "answer : SKU-1042 is out of stock." in result.stdout
    assert "steps  : ['action', 'action']" in result.stdout
    assert "tools  : ['lookup', 'final_answer']" in result.stdout


def test_skips_cleanly_without_smolagents(tmp_path: Path) -> None:
    code = (
        "import sys; sys.modules['smolagents'] = None; import runpy; "
        f"sys.exit(runpy.run_path({str(EXAMPLE)!r}, run_name='__main__') or 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Skipping cleanly" in result.stdout
