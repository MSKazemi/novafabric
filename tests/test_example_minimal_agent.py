"""End-to-end regression test for examples/minimal-agent-run.

The example must remain runnable as the project evolves. This test runs
the example as a subprocess (mirroring what a user does), and verifies it
exits 0 in the no-API-key path. The live-LLM path is not exercised in
CI — that requires a real key and would burn credits.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "minimal-agent-run" / "agent.py"


def test_example_file_exists() -> None:
    assert EXAMPLE.is_file(), f"example missing: {EXAMPLE}"


def test_example_runs_cleanly_without_api_key(tmp_path: Path) -> None:
    """Without ANTHROPIC_API_KEY, the example must exit 0 with a skip message.

    This is the behavior CI relies on. The example is not allowed to error or
    require credentials in the no-key path.
    """
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    result = subprocess.run(
        [sys.executable, str(EXAMPLE)],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "Skipping cleanly" in result.stdout, (
        f"expected skip message, got:\n{result.stdout}"
    )
