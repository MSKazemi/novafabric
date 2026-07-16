"""ADR-0123 CLI surface: `nova session replay` (experimental)."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

RUN_1 = "01HZ8T0A00YZ2K7N9DPBYK2W01"
RUN_2 = "01HZ8T1B00YZ2K7N9DPBYK2W02"


def make_capsule(base: Path, run_id: str, exit_code: int = 0) -> Path:
    capsule_dir = base / run_id
    capsule_dir.mkdir(parents=True)
    (capsule_dir / "capsule.yaml").write_text(
        yaml.dump(
            {
                "schema_version": "1.0.0",
                "run_id": run_id,
                "created_at": "2026-07-15T09:00:00.000000Z",
                "status": "success",
                "command": [sys.executable, "-c", f"import sys; sys.exit({exit_code})"],
            }
        )
    )
    (capsule_dir / "model-calls.jsonl").write_text("")
    return capsule_dir


def build_session(tmp_path: Path, run_ids: list[str]) -> tuple[str, list[str]]:
    session_root = tmp_path / "sessions"
    caps = tmp_path / "caps"
    result = runner.invoke(app, ["session", "new", "--session-dir", str(session_root)])
    assert result.exit_code == 0, result.output
    session_id = result.stdout.strip().splitlines()[0]
    for run_id in run_ids:
        make_capsule(caps, run_id)
        added = runner.invoke(
            app,
            ["session", "add", session_id, str(caps / run_id),
             "--session-dir", str(session_root)],
        )
        assert added.exit_code == 0, added.output
    return session_id, _replay_args(tmp_path)


def _replay_args(tmp_path: Path) -> list[str]:
    return [
        "--session-dir", str(tmp_path / "sessions"),
        "--capsule-dir", str(tmp_path / "caps"),
        "--output-dir", str(tmp_path / "replays"),
    ]


def test_session_replay_two_turns_reproduced(tmp_path: Path) -> None:
    session_id, args = build_session(tmp_path, [RUN_1, RUN_2])
    result = runner.invoke(app, ["session", "replay", session_id, *args])
    assert result.exit_code == 0, result.output
    assert "reproduced" in result.output
    assert "whole_session_verdict" in result.output
    # the session replay result record was persisted under --output-dir
    records = list((tmp_path / "replays").glob("session-replay-*/session_replay_result.json"))
    assert len(records) == 1
    payload = json.loads(records[0].read_text())
    assert payload["whole_session_verdict"] == "reproduced"
    assert [t["sequence"] for t in payload["turns"]] == [0, 1]


def test_session_replay_json_output(tmp_path: Path) -> None:
    session_id, args = build_session(tmp_path, [RUN_1, RUN_2])
    result = runner.invoke(app, ["session", "replay", session_id, "--json", *args])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["session_id"] == session_id
    assert payload["mode"] == "mocked"
    assert payload["whole_session_verdict"] == "reproduced"
    assert [t["source_capsule_id"] for t in payload["turns"]] == [RUN_1, RUN_2]


def test_session_replay_missing_member_refuses_nonzero_exit(tmp_path: Path) -> None:
    session_id, args = build_session(tmp_path, [RUN_1, RUN_2])
    shutil.rmtree(tmp_path / "caps" / RUN_1)
    result = runner.invoke(app, ["session", "replay", session_id, *args])
    assert result.exit_code == 1
    assert "refused" in result.output
    assert "not replayed" in result.output  # halted honestly, later turn absent


def test_session_replay_divergence_nonzero_exit(tmp_path: Path) -> None:
    session_id, args = build_session(tmp_path, [RUN_1])
    make_capsule(tmp_path / "caps2", RUN_2, exit_code=3)
    added = runner.invoke(
        app,
        ["session", "add", session_id, str(tmp_path / "caps2" / RUN_2),
         "--session-dir", str(tmp_path / "sessions")],
    )
    assert added.exit_code == 0, added.output
    result = runner.invoke(app, ["session", "replay", session_id, *args])
    assert result.exit_code == 1
    assert "diverged" in result.output


def test_session_replay_unknown_session_fails(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["session", "replay", "01HZ8S9K3M4YZ2K7N9DPBYK2W0", *_replay_args(tmp_path)],
    )
    assert result.exit_code == 1
    assert "unknown session" in result.output


def test_session_replay_empty_session_fails(tmp_path: Path) -> None:
    session_id, args = build_session(tmp_path, [])
    result = runner.invoke(app, ["session", "replay", session_id, *args])
    assert result.exit_code == 1
    assert "nothing to replay" in result.output


def test_session_replay_help_lists_flags() -> None:
    result = runner.invoke(app, ["session", "replay", "--help"])
    assert result.exit_code == 0
    assert "--mode" in result.output
    assert "--on-divergence" in result.output
    # rich truncates the long flag name with an ellipsis at narrow widths
    assert "--continue-past-refus" in result.output
