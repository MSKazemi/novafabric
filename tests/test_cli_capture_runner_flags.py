"""Tests for the new --runner and --runner-option CLI flags
(ADR-0025 Runners.2).
"""
from __future__ import annotations

import sys
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def test_default_runner_is_local(tmp_path: Path) -> None:
    """No --runner flag → use local runner (preserves v0.5.x behavior)."""
    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(tmp_path / "runs"),
         sys.executable, "-c", "print('ok')"],
    )
    assert result.exit_code == 0, result.output


def test_explicit_runner_local_works(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(tmp_path / "runs"),
         "--runner", "local",
         sys.executable, "-c", "print('ok')"],
    )
    assert result.exit_code == 0, result.output


def test_unknown_runner_name_errors_with_helpful_message(
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(tmp_path / "runs"),
         "--runner", "not-a-runner",
         sys.executable, "-c", "print('ok')"],
    )
    assert result.exit_code != 0
    # Error mentions which name was rejected and what's available.
    assert "not-a-runner" in result.output
    assert "local" in result.output
    assert "docker" in result.output


def test_runner_option_without_equals_errors(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(tmp_path / "runs"),
         "--runner", "local",
         "--runner-option", "invalid_no_equals",
         sys.executable, "-c", "print('ok')"],
    )
    assert result.exit_code != 0
    assert "key=value" in result.output


def test_runner_option_with_empty_key_errors(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(tmp_path / "runs"),
         "--runner", "local",
         "--runner-option", "=value",
         sys.executable, "-c", "print('ok')"],
    )
    assert result.exit_code != 0
    assert "key" in result.output.lower()


def test_runner_option_local_runner_passthrough(tmp_path: Path) -> None:
    """LocalRunner ignores runner_options but the CLI must still accept
    them (the dispatch is generic). This is the smoke test for the
    parsing path."""
    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(tmp_path / "runs"),
         "--runner", "local",
         "--runner-option", "ignored=value",
         sys.executable, "-c", "print('ok')"],
    )
    assert result.exit_code == 0, result.output


def test_docker_runner_without_image_returns_failed_setup(
    tmp_path: Path,
) -> None:
    """--runner docker without --runner-option image=... must fail
    cleanly with a helpful error, not crash. We don't need docker
    installed for this — DockerRunner returns the failure before any
    docker invocation."""
    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(tmp_path / "runs"),
         "--runner", "docker",
         sys.executable, "-c", "print('ok')"],
    )
    # Exit code 127 from DockerRunner.run when image missing.
    assert result.exit_code == 127, result.output
