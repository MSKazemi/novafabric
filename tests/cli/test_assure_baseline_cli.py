"""nova assure-baseline — CLI surface for ADR-0147 D1 / NF-160."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


@pytest.fixture()
def capsule_dir(tmp_path: Path) -> Path:
    d = tmp_path / "capsule"
    d.mkdir()
    (d / "capsule.yaml").write_text("run_id: run-8f2a\n", encoding="utf-8")
    (d / "outputs.txt").write_text("hello\n", encoding="utf-8")
    return d


def _pin(capsule: Path, out: Path) -> object:
    return runner.invoke(
        app,
        [
            "assure-baseline", "pin",
            "--capsule", str(capsule),
            "--run", "run-8f2a",
            "--id", "bl-golden",
            "--criterion", "goal",
            "--pinned-at", "2026-07-01T00:00:00Z",
            "--out", str(out),
        ],
    )


def test_pin_writes_a_pin_carrying_the_sealed_root(
    capsule_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "pin.json"
    result = _pin(capsule_dir, out)

    assert result.exit_code == 0, result.output
    doc = json.loads(out.read_text())
    assert doc["baseline_id"] == "bl-golden"
    assert doc["criterion"] == "goal"
    assert doc["immutable"] is True
    assert doc["runs"][0]["baseline_root"].startswith("sha256:")


def test_every_command_carries_the_assurance_honesty_line(
    capsule_dir: Path, tmp_path: Path
) -> None:
    """ADR-0147: every drift/assure CLI output MUST carry it."""
    result = _pin(capsule_dir, tmp_path / "pin.json")
    assert "does not" in result.output
    assert "remediate" in result.output


def test_verify_succeeds_on_an_unchanged_capsule(
    capsule_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "pin.json"
    assert _pin(capsule_dir, out).exit_code == 0

    result = runner.invoke(
        app,
        ["assure-baseline", "verify", "--pin", str(out),
         "--capsule", str(capsule_dir), "--run", "run-8f2a"],
    )
    assert result.exit_code == 0, result.output
    assert '"matches": true' in result.output.replace("'", '"').lower()


def test_verify_exits_nonzero_when_the_capsule_changed(
    capsule_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "pin.json"
    assert _pin(capsule_dir, out).exit_code == 0

    (capsule_dir / "outputs.txt").write_text("tampered\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["assure-baseline", "verify", "--pin", str(out),
         "--capsule", str(capsule_dir), "--run", "run-8f2a"],
    )
    assert result.exit_code == 1, result.output


def test_an_unknown_criterion_exits_two(capsule_dir: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["assure-baseline", "pin", "--capsule", str(capsule_dir),
         "--run", "r", "--id", "bl", "--criterion", "vibes",
         "--pinned-at", "2026-07-01T00:00:00Z"],
    )
    assert result.exit_code == 2


def test_a_missing_capsule_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["assure-baseline", "pin", "--capsule", str(tmp_path / "nope"),
         "--run", "r", "--id", "bl", "--criterion", "goal",
         "--pinned-at", "2026-07-01T00:00:00Z"],
    )
    assert result.exit_code == 2


def test_verifying_a_run_absent_from_the_pin_exits_two(
    capsule_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "pin.json"
    assert _pin(capsule_dir, out).exit_code == 0

    result = runner.invoke(
        app,
        ["assure-baseline", "verify", "--pin", str(out),
         "--capsule", str(capsule_dir), "--run", "some-other-run"],
    )
    assert result.exit_code == 2
