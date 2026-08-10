"""nova capture distinguishes missing commands from workloads that exit 127 (#69)."""

from __future__ import annotations

import sys
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def test_missing_command_prints_command_not_found(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    result = runner.invoke(
        app,
        [
            "capture",
            "--output-dir",
            str(runs_dir),
            "definitely-not-a-real-command-xyz-novafabric",
        ],
    )
    assert result.exit_code == 127
    assert "Command not found: definitely-not-a-real-command-xyz-novafabric" in result.stdout
    assert "Capsule written" not in result.stdout or "failed setup is evidence" in result.stdout
    assert any(runs_dir.iterdir())


def test_workload_exit_127_keeps_generic_failure_line(tmp_path: Path) -> None:
    """A program that intentionally exits 127 must not be reported as missing."""
    runs_dir = tmp_path / "runs"
    result = runner.invoke(
        app,
        [
            "capture",
            "--output-dir",
            str(runs_dir),
            sys.executable,
            "-c",
            "raise SystemExit(127)",
        ],
    )
    assert result.exit_code == 127
    assert "Command not found" not in result.stdout
    assert "Capsule written" in result.stdout
