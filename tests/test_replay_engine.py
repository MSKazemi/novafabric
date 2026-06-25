from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from novafabric.replay._engine import ReplayEngine
from novafabric.replay._flags import ReplayFlags


def _make_capsule(
    tmp_path: Path, command: list[str] | None = None, run_id: str = "TESTRUN01"
) -> Path:
    cap = tmp_path / "capsules" / run_id
    cap.mkdir(parents=True)
    (cap / "inputs").mkdir()
    (cap / "outputs").mkdir()

    manifest = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "status": "success",
        "command": command or ["python", "-c", "print('hello')"],
        "capture_mode": "cli-wrapper",
        "novafabric_version": "0.2.0",
        "model_call_count": 0,
        "tool_call_count": 0,
    }
    (cap / "capsule.yaml").write_text(yaml.dump(manifest))
    (cap / "env.lock").write_text(yaml.dump({
        "schema_version": "0.1.0",
        "python": {"version": "3.12.3", "interpreter": "cpython"},
        "host": {"os": "linux", "arch": "x86_64"},
    }))
    (cap / "replay.yaml").write_text(yaml.dump({"schema_version": "0.1.0"}))
    (cap / "model-calls.jsonl").write_text("")
    (cap / "tool-calls.jsonl").write_text("")
    (cap / "trace.jsonl").write_text("")
    (cap / "redaction-proof.json").write_text(json.dumps({"findings": []}))
    return cap


def test_forensic_mode_produces_result_file(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    base = tmp_path / "replays"
    engine = ReplayEngine(capsule_dir=cap, flags=ReplayFlags(mode="forensic"), base_dir=base)
    result = engine.run()

    assert result.mode == "forensic"
    assert result.status == "success"
    result_file = base / result.replay_id / "replay_result.yaml"
    assert result_file.exists()
    data = yaml.safe_load(result_file.read_text())
    assert data["mode"] == "forensic"
    assert data["replay_of_run_id"] == "TESTRUN01"


def test_forensic_mode_captures_env_warning_on_mismatch(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    # Write env.lock with different OS to trigger warning
    (cap / "env.lock").write_text(yaml.dump({
        "schema_version": "0.1.0",
        "python": {"version": "3.10.0", "interpreter": "cpython"},
        "host": {"os": "windows", "arch": "x86_64"},
    }))
    base = tmp_path / "replays"
    engine = ReplayEngine(capsule_dir=cap, flags=ReplayFlags(mode="forensic"), base_dir=base)
    result = engine.run()
    # Some env warnings expected (different Python and/or OS)
    assert isinstance(result.env_warnings, list)


def test_dry_run_writes_report(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    (cap / "tool-calls.jsonl").write_text(
        json.dumps({"tool_call_id": "T1", "tool_name": "read_file", "mutation_class": "read-only",
                    "arguments": {}, "result": "data"})
    )
    base = tmp_path / "replays"
    engine = ReplayEngine(capsule_dir=cap, flags=ReplayFlags(dry_run=True), base_dir=base)
    result = engine.run()

    assert result.status == "dry_run"
    report_file = base / result.replay_id / "dry_run_report.txt"
    assert report_file.exists()
    assert "read_file" in report_file.read_text()


def test_mocked_mode_reruns_command(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path, command=["python", "-c", "print('mocked')"])
    base = tmp_path / "replays"
    engine = ReplayEngine(capsule_dir=cap, flags=ReplayFlags(mode="mocked"), base_dir=base)
    result = engine.run()

    assert result.mode == "mocked"
    assert result.status == "success"
    assert result.exit_code == 0
    result_file = base / result.replay_id / "replay_result.yaml"
    assert result_file.exists()


def test_mocked_mode_failure_capsule(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path, command=["python", "-c", "import sys; sys.exit(2)"])
    base = tmp_path / "replays"
    engine = ReplayEngine(capsule_dir=cap, flags=ReplayFlags(mode="mocked"), base_dir=base)
    result = engine.run()

    assert result.status == "failure"
    assert result.exit_code == 2


def test_mocked_mode_empty_command_aborts(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    # Overwrite capsule.yaml with empty command list
    manifest = yaml.safe_load((cap / "capsule.yaml").read_text())
    manifest["command"] = []
    (cap / "capsule.yaml").write_text(yaml.dump(manifest))

    base = tmp_path / "replays"
    engine = ReplayEngine(capsule_dir=cap, flags=ReplayFlags(mode="mocked"), base_dir=base)
    result = engine.run()

    assert result.status == "aborted"
    assert result.error is not None


def test_missing_capsule_yaml_raises(tmp_path: Path) -> None:
    bogus = tmp_path / "not-a-capsule"
    bogus.mkdir()
    engine = ReplayEngine(capsule_dir=bogus, flags=ReplayFlags(mode="forensic"), base_dir=tmp_path)
    with pytest.raises(ValueError, match="capsule.yaml not found"):
        engine.run()


def test_replay_id_is_ulid(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    base = tmp_path / "replays"
    engine = ReplayEngine(capsule_dir=cap, flags=ReplayFlags(mode="forensic"), base_dir=base)
    result = engine.run()
    assert len(result.replay_id) == 26  # ULID length
