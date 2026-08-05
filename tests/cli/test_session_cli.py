"""ADR-0122 CLI surface: `nova session new|add|list|show` and capture flags."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml
from _help_assert import assert_flag_in_help
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
RUN_1 = "01HZ8T0A00YZ2K7N9DPBYK2W01"
RUN_2 = "01HZ8T1B00YZ2K7N9DPBYK2W02"


def make_capsule(base: Path, run_id: str, duration_ms: int = 1200) -> Path:
    capsule_dir = base / run_id
    capsule_dir.mkdir(parents=True)
    (capsule_dir / "capsule.yaml").write_text(
        yaml.dump(
            {
                "schema_version": "1.0.0",
                "run_id": run_id,
                "created_at": "2026-07-15T09:00:00.000000Z",
                "finished_at": "2026-07-15T09:00:01.200000Z",
                "duration_ms": duration_ms,
                "status": "success",
            }
        )
    )
    return capsule_dir


@pytest.fixture()
def session_root(tmp_path: Path) -> Path:
    return tmp_path / "sessions"


def _new_session(session_root: Path, *extra: str) -> str:
    result = runner.invoke(
        app, ["session", "new", "--session-dir", str(session_root), *extra]
    )
    assert result.exit_code == 0, result.output
    session_id = result.stdout.strip().splitlines()[0]
    assert ULID_RE.match(session_id), result.output
    return session_id


def test_session_new_prints_ulid_and_writes_manifest(session_root: Path) -> None:
    session_id = _new_session(session_root, "--kind", "workflow", "--user", "user:h1")
    manifest = json.loads((session_root / session_id / "session.json").read_text())
    assert manifest["session_kind"] == "workflow"
    assert manifest["user_ref"] == "user:h1"
    assert manifest["member_runs"] == []


def test_session_add_and_show_ordered_turns(session_root: Path, tmp_path: Path) -> None:
    session_id = _new_session(session_root)
    caps = tmp_path / "caps"
    for run_id in (RUN_1, RUN_2):
        make_capsule(caps, run_id)
        result = runner.invoke(
            app,
            ["session", "add", session_id, str(caps / run_id),
             "--session-dir", str(session_root)],
        )
        assert result.exit_code == 0, result.output
    result = runner.invoke(
        app,
        ["session", "show", session_id, "--session-dir", str(session_root),
         "--capsule-dir", str(caps), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    members = payload["members"]
    assert [m["member"]["run_id"] for m in members] == [RUN_1, RUN_2]
    assert [m["member"]["sequence"] for m in members] == [0, 1]
    assert all(m["status"] == "ok" for m in members)
    assert payload["stats"]["turns"] == 2
    assert payload["stats"]["total_duration_ms"] == 2400


def test_session_add_unknown_session_fails(session_root: Path, tmp_path: Path) -> None:
    capsule = make_capsule(tmp_path / "caps", RUN_1)
    result = runner.invoke(
        app,
        ["session", "add", "01HZ8S9K3M4YZ2K7N9DPBYK2W0", str(capsule),
         "--session-dir", str(session_root)],
    )
    assert result.exit_code == 1
    assert "unknown session" in result.output


def test_session_add_missing_capsule_fails(session_root: Path, tmp_path: Path) -> None:
    session_id = _new_session(session_root)
    result = runner.invoke(
        app,
        ["session", "add", session_id, str(tmp_path / "nope"),
         "--session-dir", str(session_root)],
    )
    assert result.exit_code == 1
    assert "Capsule not found" in result.output


def test_session_show_unknown_session_fails(session_root: Path) -> None:
    result = runner.invoke(
        app,
        ["session", "show", "01HZ8S9K3M4YZ2K7N9DPBYK2W0",
         "--session-dir", str(session_root)],
    )
    assert result.exit_code == 1
    assert "unknown session" in result.output


def test_session_show_reports_missing_member_without_failing(
    session_root: Path, tmp_path: Path
) -> None:
    session_id = _new_session(session_root)
    caps = tmp_path / "caps"
    capsule = make_capsule(caps, RUN_1)
    runner.invoke(
        app,
        ["session", "add", session_id, str(capsule), "--session-dir", str(session_root)],
    )
    shutil.rmtree(capsule)
    result = runner.invoke(
        app,
        ["session", "show", session_id, "--session-dir", str(session_root),
         "--capsule-dir", str(caps)],
    )
    assert result.exit_code == 0, result.output
    assert "missing" in result.output


def test_session_list_shows_sessions(session_root: Path) -> None:
    first = _new_session(session_root)
    result = runner.invoke(app, ["session", "list", "--session-dir", str(session_root)])
    assert result.exit_code == 0
    assert first in result.output

    empty = runner.invoke(
        app, ["session", "list", "--session-dir", str(session_root / "nope")]
    )
    assert empty.exit_code == 0
    assert "No sessions found" in empty.output


def test_capture_help_shows_session_flags() -> None:
    result = runner.invoke(app, ["capture", "--help"])
    assert result.exit_code == 0
    assert_flag_in_help(result, "--session-id")
    assert_flag_in_help(result, "--session-sequence")


def test_capture_invalid_session_id_is_a_usage_error(tmp_path: Path) -> None:
    import sys

    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(tmp_path / "runs"),
         "--session-id", "not-a-ulid", sys.executable, "-c", "pass"],
    )
    assert result.exit_code == 2
    assert "session" in result.output.lower()


def test_capture_session_flags_write_manifest_fields(tmp_path: Path) -> None:
    import sys

    runs_dir = tmp_path / "runs"
    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(runs_dir),
         "--session-id", "01HZ8S9K3M4YZ2K7N9DPBYK2W0", "--session-sequence", "1",
         sys.executable, "-c", "pass"],
    )
    assert result.exit_code == 0, result.output
    run_dirs = list(runs_dir.iterdir())
    assert len(run_dirs) == 1
    manifest = yaml.safe_load((run_dirs[0] / "capsule.yaml").read_text())
    assert manifest["session_id"] == "01HZ8S9K3M4YZ2K7N9DPBYK2W0"
    assert manifest["sequence"] == 1
