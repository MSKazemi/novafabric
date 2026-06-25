from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _make_capsule(tmp_path: Path, command: list[str] | None = None) -> Path:
    cap = tmp_path / "test-capsule"
    cap.mkdir(parents=True)
    (cap / "inputs").mkdir()
    (cap / "outputs").mkdir()
    manifest = {
        "schema_version": "0.1.0",
        "run_id": "TESTREPLAY1",
        "status": "success",
        "command": command or ["python", "-c", "print('hi')"],
        "capture_mode": "cli-wrapper",
        "novafabric_version": "0.2.0",
    }
    (cap / "capsule.yaml").write_text(yaml.dump(manifest))
    (cap / "env.lock").write_text(yaml.dump({
        "python": {"version": "3.12.3", "interpreter": "cpython"},
        "host": {"os": "linux", "arch": "x86_64"},
    }))
    (cap / "replay.yaml").write_text(yaml.dump({"schema_version": "0.1.0"}))
    (cap / "model-calls.jsonl").write_text("")
    (cap / "tool-calls.jsonl").write_text("")
    (cap / "trace.jsonl").write_text("")
    (cap / "redaction-proof.json").write_text(json.dumps({"findings": []}))
    return cap


def test_forensic_exits_zero(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    result = runner.invoke(app, ["replay", str(cap), "--mode", "forensic",
                                 "--output-dir", str(tmp_path / "replays")])
    assert result.exit_code == 0


def test_forensic_shows_replay_id(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    result = runner.invoke(app, ["replay", str(cap), "--mode", "forensic",
                                 "--output-dir", str(tmp_path / "replays")])
    assert "replay_id=" in result.output or "Replay written" in result.output


def test_dry_run_shows_tool_report(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    (cap / "tool-calls.jsonl").write_text(
        json.dumps({
            "tool_call_id": "T1",
            "tool_name": "read_file",
            "mutation_class": "read-only",
            "arguments": {},
            "result": "data",
        })
    )
    result = runner.invoke(app, ["replay", str(cap), "--dry-run",
                                 "--output-dir", str(tmp_path / "replays")])
    assert result.exit_code == 0
    assert "read_file" in result.output or "dry-run" in result.output


def test_invalid_capsule_path_exits_1(tmp_path: Path) -> None:
    result = runner.invoke(app, ["replay", str(tmp_path / "no-such-dir"),
                                 "--mode", "forensic"])
    assert result.exit_code == 1


def test_invalid_mode_exits_1(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path)
    result = runner.invoke(app, ["replay", str(cap), "--mode", "invalid-mode"])
    # Typer/Click returns exit code 2 for invalid enum option values
    assert result.exit_code == 2


def test_mocked_mode_exit_zero(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path, command=["python", "-c", "print('ok')"])
    result = runner.invoke(app, ["replay", str(cap), "--mode", "mocked",
                                 "--output-dir", str(tmp_path / "replays")])
    assert result.exit_code == 0
