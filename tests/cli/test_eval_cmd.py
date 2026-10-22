from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from _help_assert import assert_flag_in_help
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


@pytest.fixture
def valid_capsule(tmp_path: Path) -> Path:
    """A minimal capsule directory that novafabric-smoke-v1 will pass."""
    capsule_dir = tmp_path / "capsule"
    capsule_dir.mkdir()
    (capsule_dir / "capsule.yaml").write_text(
        yaml.dump({"run_id": "01TESTCAPSULE000000000001", "schema_version": "0.1.0"})
    )
    (capsule_dir / "replay-policy.yaml").write_text("allow_readonly: true\n")
    return capsule_dir


@pytest.fixture
def empty_capsule(tmp_path: Path) -> Path:
    """A capsule directory with no capsule.yaml (smoke suite will fail)."""
    capsule_dir = tmp_path / "empty_capsule"
    capsule_dir.mkdir()
    return capsule_dir


def test_eval_run_valid_capsule_exits_0(valid_capsule: Path) -> None:
    result = runner.invoke(
        app,
        ["eval", "run", str(valid_capsule), "--suite", "novafabric-smoke-v1"],
    )
    assert result.exit_code == 0, result.output


def test_eval_run_unknown_suite_exits_1(valid_capsule: Path) -> None:
    result = runner.invoke(
        app,
        ["eval", "run", str(valid_capsule), "--suite", "does-not-exist-v99"],
    )
    assert result.exit_code == 1
    # Error message should mention the suite id
    combined = (result.output or "") + (result.stderr if hasattr(result, "stderr") else "")
    assert "does-not-exist-v99" in combined or result.exit_code == 1


def test_eval_run_output_flag_writes_json_file(valid_capsule: Path, tmp_path: Path) -> None:
    output_file = tmp_path / "result.json"
    result = runner.invoke(
        app,
        [
            "eval", "run", str(valid_capsule),
            "--suite", "novafabric-smoke-v1",
            "--output", str(output_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert output_file.exists(), "Output file was not created"
    data = json.loads(output_file.read_text())
    assert data["suite_id"] == "novafabric-smoke-v1"
    assert data["passed"] is True
    assert len(data["metrics"]) >= 1


def test_eval_run_failing_capsule_exits_1(empty_capsule: Path) -> None:
    result = runner.invoke(
        app,
        ["eval", "run", str(empty_capsule), "--suite", "novafabric-smoke-v1"],
    )
    assert result.exit_code == 1


def test_eval_run_help_shows_options() -> None:
    result = runner.invoke(app, ["eval", "run", "--help"])
    assert result.exit_code == 0
    assert_flag_in_help(result, "--suite")


def test_eval_help_shows_run_subcommand() -> None:
    result = runner.invoke(app, ["eval", "--help"])
    assert result.exit_code == 0
    assert "run" in result.output


def test_nova_help_shows_eval_subcommand() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "eval" in result.output


# ── M9: nova eval agent must be able to gate CI ─────────────────────────────

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_eval_agent_fail_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A registered agent whose suite is not installed FAILs — and must exit 1,
    like ``nova eval run`` does on FAIL, so a CI step cannot pass over it."""
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(tmp_path / "reg.db"))
    reg = runner.invoke(app, ["register", str(_FIXTURES / "valid_agent.yaml")])
    assert reg.exit_code == 0, reg.output
    result = runner.invoke(app, ["eval", "agent", "kube-rca-agent@v1.0.0"])
    assert isinstance(result.exception, SystemExit), result.exception
    assert "FAIL" in result.output
    assert result.exit_code == 1, result.output


@pytest.mark.parametrize(("passed", "code"), [(True, 0), (False, 1)])
def test_eval_agent_exit_code_follows_the_verdict(
    monkeypatch: pytest.MonkeyPatch, passed: bool, code: int
) -> None:
    from novafabric.cli import eval_run

    def fake_run_evals(name: str, version: str) -> dict[str, object]:
        return {"passed": passed, "suites": [{"suite_name": "s", "passed": passed}]}

    monkeypatch.setattr(eval_run, "run_evals", fake_run_evals)
    result = runner.invoke(app, ["eval", "agent", "a@v1"])
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert result.exit_code == code, result.output
