import sys
from pathlib import Path

import yaml
from typer.testing import CliRunner

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.cli.main import app

runner = CliRunner()


def _make_capsule(tmp_path: Path) -> Path:
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(command=[sys.executable, "-c", "pass"])
    return result.capsule_dir


def test_validate_capsule_dir_valid(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    result = runner.invoke(app, ["validate", str(capsule_dir)])
    assert result.exit_code == 0
    assert "Valid capsule" in result.output


def test_validate_capsule_dir_shows_run_id(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    manifest = yaml.safe_load((capsule_dir / "capsule.yaml").read_text())
    result = runner.invoke(app, ["validate", str(capsule_dir)])
    assert manifest["run_id"] in result.output


def test_validate_capsule_dir_missing_file(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    (capsule_dir / "trace.jsonl").unlink()
    result = runner.invoke(app, ["validate", str(capsule_dir)])
    assert result.exit_code == 1
    assert "missing" in result.output


def test_validate_capsule_dir_bad_manifest(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    (capsule_dir / "capsule.yaml").write_text("schema_version: bad\n")
    result = runner.invoke(app, ["validate", str(capsule_dir)])
    assert result.exit_code == 1


# --- Replay result validation ---

def _make_replay_dir(tmp_path: Path, overrides: dict | None = None) -> Path:
    replay_dir = tmp_path / "replays" / "REPLAYID01"
    replay_dir.mkdir(parents=True)
    result = {
        "schema_version": "0.1.0",
        "replay_id": "01REPLAYID01234567890ABCDE",
        "replay_of_run_id": "01RUNID000000000000000ABCD",
        "mode": "forensic",
        "status": "success",
        "start_time": "2026-05-08T00:00:00.000000Z",
        "end_time": "2026-05-08T00:00:01.000000Z",
        "duration_ms": 1000,
        "policy_flags_used": ["--mode=forensic"],
        "env_warnings": [],
        "model_calls_mocked": 0,
        "tool_calls_mocked": 0,
    }
    if overrides:
        result.update(overrides)
    (replay_dir / "replay_result.yaml").write_text(yaml.dump(result))
    return replay_dir


def test_validate_replay_dir_valid(tmp_path: Path) -> None:
    replay_dir = _make_replay_dir(tmp_path)
    result = runner.invoke(app, ["validate", str(replay_dir)])
    assert result.exit_code == 0
    assert "Valid replay result" in result.output


def test_validate_replay_dir_shows_replay_id(tmp_path: Path) -> None:
    replay_dir = _make_replay_dir(tmp_path)
    result = runner.invoke(app, ["validate", str(replay_dir)])
    assert "01REPLAYID01234567890ABCDE" in result.output


def test_validate_replay_dir_bad_mode(tmp_path: Path) -> None:
    replay_dir = _make_replay_dir(tmp_path, overrides={"mode": "invalid-mode"})
    result = runner.invoke(app, ["validate", str(replay_dir)])
    assert result.exit_code == 1


def test_validate_replay_dir_missing_file(tmp_path: Path) -> None:
    replay_dir = _make_replay_dir(tmp_path)
    (replay_dir / "replay_result.yaml").unlink()
    result = runner.invoke(app, ["validate", str(replay_dir)])
    # Falls through to asset spec validator (no capsule.yaml and no replay_result.yaml)
    assert result.exit_code == 1


# --- lineage.jsonl validation (v0.4) ---


def test_validate_capsule_with_lineage_jsonl_passes(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    # lineage.jsonl is now written automatically by CaptureOrchestrator
    result = runner.invoke(app, ["validate", str(capsule_dir)])
    assert result.exit_code == 0


def test_validate_capsule_with_invalid_lineage_jsonl_fails(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    (capsule_dir / "lineage.jsonl").write_text("not-valid-json\n")
    result = runner.invoke(app, ["validate", str(capsule_dir)])
    assert result.exit_code == 1
    assert "lineage" in result.output.lower()
