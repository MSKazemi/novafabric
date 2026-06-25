"""Regression test for examples/azure-openai.

Verifies the no-keys skip path. Live capture is not exercised in CI
(would require Azure credentials and burn budget).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

EXAMPLE = (
    Path(__file__).resolve().parent.parent
    / "examples" / "azure-openai" / "agent.py"
)


def test_example_file_exists() -> None:
    assert EXAMPLE.is_file(), f"example missing: {EXAMPLE}"


def test_skips_cleanly_without_credentials(tmp_path: Path) -> None:
    env = os.environ.copy()
    for var in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"):
        env.pop(var, None)
    result = subprocess.run(
        [sys.executable, str(EXAMPLE)],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=30,
    )
    assert result.returncode == 0, (
        f"exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "Skipping cleanly" in result.stdout
