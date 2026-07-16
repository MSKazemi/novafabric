"""ADR-0126 CLI surface: `nova capture --environment` and `nova validate` warning."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.capture.deployment_env import ENVIRONMENT_ENV_VAR
from novafabric.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_ambient_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENVIRONMENT_ENV_VAR, raising=False)


def _single_capsule_dir(runs_dir: Path) -> Path:
    run_dirs = list(runs_dir.iterdir())
    assert len(run_dirs) == 1
    return run_dirs[0]


def test_capture_help_shows_environment_flag() -> None:
    result = runner.invoke(app, ["capture", "--help"])
    assert result.exit_code == 0
    assert "--environment" in result.output


def test_capture_environment_flag_writes_manifest_fields(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(runs_dir), "--environment", "staging",
         sys.executable, "-c", "pass"],
    )
    assert result.exit_code == 0
    manifest = yaml.safe_load(
        (_single_capsule_dir(runs_dir) / "capsule.yaml").read_text()
    )
    assert manifest["deployment_environment"] == "staging"
    assert manifest["environment_source"] == "cli-flag"


def test_capture_without_flag_leaves_fields_absent(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(runs_dir), sys.executable, "-c", "pass"],
    )
    assert result.exit_code == 0
    manifest = yaml.safe_load(
        (_single_capsule_dir(runs_dir) / "capsule.yaml").read_text()
    )
    assert "deployment_environment" not in manifest
    assert "environment_source" not in manifest


def test_capture_empty_environment_normalizes_to_absent(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(runs_dir), "--environment", "",
         sys.executable, "-c", "pass"],
    )
    assert result.exit_code == 0
    manifest = yaml.safe_load(
        (_single_capsule_dir(runs_dir) / "capsule.yaml").read_text()
    )
    assert "deployment_environment" not in manifest


def test_capture_env_var_source_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENVIRONMENT_ENV_VAR, "prod-eu")
    runs_dir = tmp_path / "runs"
    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(runs_dir), sys.executable, "-c", "pass"],
    )
    assert result.exit_code == 0
    manifest = yaml.safe_load(
        (_single_capsule_dir(runs_dir) / "capsule.yaml").read_text()
    )
    assert manifest["deployment_environment"] == "prod-eu"
    assert manifest["environment_source"] == "env-var"


def test_capture_invalid_environment_fails_before_capture(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(runs_dir), "--environment", "not valid",
         sys.executable, "-c", "pass"],
    )
    assert result.exit_code != 0
    assert not runs_dir.exists() or list(runs_dir.iterdir()) == []


def test_validate_passes_capsule_with_environment(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runner.invoke(
        app,
        ["capture", "--output-dir", str(runs_dir), "--environment", "production",
         sys.executable, "-c", "pass"],
    )
    result = runner.invoke(app, ["validate", str(_single_capsule_dir(runs_dir))])
    assert result.exit_code == 0
    assert "Valid capsule" in result.output
    # Conventional value: no advisory warning.
    assert "outside the conventional set" not in result.output


def test_validate_warns_on_unconventional_environment_but_passes(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runner.invoke(
        app,
        ["capture", "--output-dir", str(runs_dir), "--environment", "prodction",
         sys.executable, "-c", "pass"],
    )
    result = runner.invoke(app, ["validate", str(_single_capsule_dir(runs_dir))])
    assert result.exit_code == 0  # warning, never an error
    assert "prodction" in result.output
    assert "Valid capsule" in result.output


def test_validate_passes_old_capsule_without_environment(tmp_path: Path) -> None:
    """Backward compatibility: absence changes nothing for `nova validate`."""
    runs_dir = tmp_path / "runs"
    runner.invoke(
        app,
        ["capture", "--output-dir", str(runs_dir), sys.executable, "-c", "pass"],
    )
    result = runner.invoke(app, ["validate", str(_single_capsule_dir(runs_dir))])
    assert result.exit_code == 0
    assert "Valid capsule" in result.output
