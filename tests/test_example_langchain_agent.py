"""Regression test for examples/langchain-agent.

Verifies the no-keys skip path. Live capture is not exercised in CI —
would require either an Anthropic or OpenAI key plus large optional
dependencies (langchain, langgraph) installed in the test env.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

EXAMPLE = (
    Path(__file__).resolve().parent.parent
    / "examples" / "langchain-agent" / "agent.py"
)


def test_example_file_exists() -> None:
    assert EXAMPLE.is_file(), f"example missing: {EXAMPLE}"


def test_skips_cleanly_without_credentials(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    result = subprocess.run(
        [sys.executable, str(EXAMPLE)],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=30,
    )
    assert result.returncode == 0, (
        f"exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "Skipping cleanly" in result.stdout
