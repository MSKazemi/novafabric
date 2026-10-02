"""ADR-0126 replay-gate wiring — the recorded ``deployment_environment`` at replay.

Acceptance criteria:

- the mutating-replay policy gate (``action: replay_mutating``) receives the
  capsule's recorded ``deployment_environment`` as
  ``input.resource.deployment_environment`` — verbatim, ``null`` when absent,
  never inferred (same record-only reader as the evidence-export gate);
- ``nova replay --environment ENV`` is **opt-in**: without it nothing changes;
  with it the replay is refused **before anything runs** (no policy call, no
  replay directory) unless the capsule recorded ``ENV``, exiting 2 — the
  same "comparison cannot be made" code ``nova diff --environment`` uses;
- an unrecorded environment fails closed (refused, not admitted);
- an invalid ``--environment`` value is rejected as a usage error;
- the engine-level gate (``ReplayFlags.required_environment``) enforces the
  same rule for SDK callers, raising a named error.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from _help_assert import assert_flag_in_help
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.policy import PolicyDecision
from novafabric.replay import ReplayEngine, ReplayFlags
from novafabric.replay.environment_gate import (
    EXIT_ENVIRONMENT_MISMATCH,
    ReplayEnvironmentMismatchError,
    check_replay_environment,
)

runner = CliRunner()


def _capsule(tmp_path: Path, env: object = None, *, name: str = "capsule") -> Path:
    capsule = tmp_path / name
    capsule.mkdir()
    manifest: dict[str, object] = {"run_id": "run-env-1", "command": ["true"]}
    if env is not None:
        manifest["deployment_environment"] = env
        manifest["environment_source"] = "cli-flag"
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return capsule


@pytest.fixture
def policy_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    engine = MagicMock()
    engine.evaluate.return_value = PolicyDecision(allow=True, decision_id="d-1")
    monkeypatch.setattr("novafabric.replay._engine.get_policy_engine", lambda: engine)
    monkeypatch.setattr("novafabric.replay._engine.AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    return engine


# ── mutating-replay policy input ────────────────────────────────────────────


@pytest.mark.parametrize("env", ["production", "prod-eu:1", None])
def test_mutating_gate_receives_recorded_environment(
    tmp_path: Path, policy_engine: MagicMock, env: str | None
) -> None:
    capsule = _capsule(tmp_path, env)
    flags = ReplayFlags(dry_run=True, allow_mutating=True)
    ReplayEngine(capsule, flags, base_dir=tmp_path / "out").run()
    (inp,), _ = policy_engine.evaluate.call_args
    assert inp.action == "replay_mutating"
    assert inp.resource.deployment_environment == env


def test_mutating_gate_drops_out_of_rule_value(tmp_path: Path, policy_engine: MagicMock) -> None:
    capsule = _capsule(tmp_path, "prod env")  # space violates the ADR-0126 value rule
    ReplayEngine(
        capsule, ReplayFlags(dry_run=True, allow_mutating=True), base_dir=tmp_path / "out"
    ).run()
    (inp,), _ = policy_engine.evaluate.call_args
    assert inp.resource.deployment_environment is None


def test_non_mutating_replay_does_not_call_the_gate(
    tmp_path: Path, policy_engine: MagicMock
) -> None:
    capsule = _capsule(tmp_path, "production")
    ReplayEngine(capsule, ReplayFlags(dry_run=True), base_dir=tmp_path / "out").run()
    policy_engine.evaluate.assert_not_called()


# ── pure check ──────────────────────────────────────────────────────────────


def test_check_admits_matching_environment(tmp_path: Path) -> None:
    assert check_replay_environment(_capsule(tmp_path, "staging"), "staging") == "staging"


def test_check_is_case_sensitive(tmp_path: Path) -> None:
    with pytest.raises(ReplayEnvironmentMismatchError) as exc:
        check_replay_environment(_capsule(tmp_path, "Production"), "production")
    assert exc.value.recorded == "Production"
    assert exc.value.required == "production"


def test_check_refuses_unrecorded_environment(tmp_path: Path) -> None:
    with pytest.raises(ReplayEnvironmentMismatchError, match="no deployment_environment"):
        check_replay_environment(_capsule(tmp_path), "production")


def test_check_refuses_metadata_only_environment(tmp_path: Path) -> None:
    capsule = tmp_path / "c"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text(
        yaml.safe_dump({"run_id": "r", "metadata": {"deployment_environment": "production"}}),
        encoding="utf-8",
    )
    with pytest.raises(ReplayEnvironmentMismatchError):
        check_replay_environment(capsule, "production")


def test_check_rejects_invalid_required_value(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="value rule"):
        check_replay_environment(_capsule(tmp_path, "production"), "prod env")


# ── engine-level gate (SDK callers) ─────────────────────────────────────────


def test_engine_refuses_before_policy_and_output(tmp_path: Path, policy_engine: MagicMock) -> None:
    capsule = _capsule(tmp_path, "staging")
    out = tmp_path / "out"
    flags = ReplayFlags(dry_run=True, allow_mutating=True, required_environment="production")
    with pytest.raises(ReplayEnvironmentMismatchError):
        ReplayEngine(capsule, flags, base_dir=out).run()
    policy_engine.evaluate.assert_not_called()
    assert not out.exists()


def test_engine_admits_matching_environment(tmp_path: Path, policy_engine: MagicMock) -> None:
    capsule = _capsule(tmp_path, "production")
    flags = ReplayFlags(dry_run=True, required_environment="production")
    result = ReplayEngine(capsule, flags, base_dir=tmp_path / "out").run()
    assert result.status == "dry_run"


def test_flag_default_is_off() -> None:
    assert ReplayFlags().required_environment is None


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_mismatch_exits_2_and_writes_nothing(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, "staging")
    out = tmp_path / "out"
    result = runner.invoke(
        app,
        ["replay", str(capsule), "--dry-run", "--environment", "production", "-o", str(out)],
    )
    assert result.exit_code == EXIT_ENVIRONMENT_MISMATCH == 2
    assert "'staging'" in result.output
    assert "production" in result.output
    assert not out.exists()


def test_cli_unrecorded_environment_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "replay", str(_capsule(tmp_path)), "--dry-run",
            "--environment", "production", "-o", str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 2
    assert "no deployment_environment" in result.output


def test_cli_match_proceeds(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "replay", str(_capsule(tmp_path, "production")), "--dry-run",
            "--environment", "production", "-o", str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()


def test_cli_without_flag_is_unchanged(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = runner.invoke(
        app, ["replay", str(_capsule(tmp_path, "staging")), "--dry-run", "-o", str(out)]
    )
    assert result.exit_code == 0, result.output


def test_cli_invalid_environment_value_is_usage_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "replay", str(_capsule(tmp_path, "production")), "--dry-run",
            "--environment", "prod env", "-o", str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 2
    assert "value rule" in result.output
    assert not (tmp_path / "out").exists()


def test_cli_help_lists_flag() -> None:
    result = runner.invoke(app, ["replay", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert_flag_in_help(result, "--environment")
