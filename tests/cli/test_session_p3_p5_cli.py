"""CLI surface for ADR-0122 P3/P4 and ADR-0123 P5 (experimental).

`nova session list --rebuild-index`, `nova session reindex`,
`nova session export | verify-bundle | import`, and
`nova session replay --from/--to/--turn-mode/--dry-run`.
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

import pytest
import yaml
from _help_assert import assert_flag_in_help
from rich.console import Console
from typer.testing import CliRunner

import novafabric.cli.session as session_cli
from novafabric.cli.main import app

runner = CliRunner()


def squash(text: str) -> str:
    """Whitespace-free text, so Rich's terminal-width wrapping is not the subject."""
    return re.sub(r"\s+", "", text)


@pytest.fixture(autouse=True)
def _wide_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    # Rich reads COLUMNS once, when a Console is built; the module-level
    # console in cli/session.py may have been built earlier in this worker
    # at a narrower width (test-order dependent under xdist), so replace it.
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setattr(session_cli, "console", Console(width=200))


RUN_1 = "01HZ8T0A00YZ2K7N9DPBYK2W01"
RUN_2 = "01HZ8T1B00YZ2K7N9DPBYK2W02"
RUN_3 = "01HZ8T2C00YZ2K7N9DPBYK2W03"


def make_capsule(base: Path, run_id: str) -> Path:
    capsule_dir = base / run_id
    capsule_dir.mkdir(parents=True)
    (capsule_dir / "capsule.yaml").write_text(
        yaml.dump(
            {
                "schema_version": "1.0.0",
                "run_id": run_id,
                "created_at": "2026-07-15T09:00:00.000000Z",
                "status": "success",
                "command": [sys.executable, "-c", "pass"],
            }
        )
    )
    (capsule_dir / "model-calls.jsonl").write_text("")
    return capsule_dir


@pytest.fixture()
def session(tmp_path: Path) -> tuple[str, Path, Path]:
    root, caps = tmp_path / "sessions", tmp_path / "caps"
    result = runner.invoke(app, ["session", "new", "--session-dir", str(root)])
    assert result.exit_code == 0, result.output
    sid = result.stdout.strip().splitlines()[0]
    for run_id in (RUN_1, RUN_2, RUN_3):
        make_capsule(caps, run_id)
        added = runner.invoke(
            app, ["session", "add", sid, str(caps / run_id), "--session-dir", str(root)]
        )
        assert added.exit_code == 0, added.output
    return sid, root, caps


class TestIndexCli:
    def test_reindex_then_list(self, session: tuple[str, Path, Path]) -> None:
        sid, root, _ = session
        result = runner.invoke(app, ["session", "reindex", "--session-dir", str(root)])
        assert result.exit_code == 0, result.output
        assert "Indexed 1 session(s)" in result.output
        assert (root / ".session-index.sqlite").is_file()

        listed = runner.invoke(app, ["session", "list", "--session-dir", str(root), "--json"])
        assert listed.exit_code == 0, listed.output
        assert [m["session_id"] for m in json.loads(listed.stdout)] == [sid]

    def test_reindex_json_and_unreadable(self, session: tuple[str, Path, Path]) -> None:
        _sid, root, _ = session
        bad = root / "01HZZZZZZZZZZZZZZZZZZZZZZZ"
        bad.mkdir()
        (bad / "session.json").write_text("{nope")
        result = runner.invoke(app, ["session", "reindex", "--session-dir", str(root)])
        assert "1 unreadable (skipped)" in result.output
        result = runner.invoke(app, ["session", "reindex", "--session-dir", str(root), "--json"])
        assert json.loads(result.stdout)["unreadable"] == 1

    def test_reindex_error_exits_1(self, tmp_path: Path) -> None:
        blocker = tmp_path / "file"
        blocker.write_text("not a dir")
        result = runner.invoke(app, ["session", "reindex", "--session-dir", str(blocker / "x")])
        assert result.exit_code != 0

    def test_list_stale_index_hints_and_still_lists(self, session: tuple[str, Path, Path]) -> None:
        sid, root, _ = session
        runner.invoke(app, ["session", "reindex", "--session-dir", str(root)])
        manifest = root / sid / "session.json"
        manifest.write_text(manifest.read_text() + "\n")  # external edit
        result = runner.invoke(app, ["session", "list", "--session-dir", str(root)])
        assert result.exit_code == 0
        assert sid in result.stdout
        assert squash("nova session reindex") in squash(result.stderr)
        # --rebuild-index repairs it; no hint afterwards
        result = runner.invoke(
            app, ["session", "list", "--session-dir", str(root), "--rebuild-index"]
        )
        assert result.exit_code == 0
        assert "reindex" not in result.stderr

    def test_list_rebuild_failure_is_non_fatal(self, tmp_path: Path) -> None:
        blocker = tmp_path / "file"
        blocker.write_text("x")
        result = runner.invoke(
            app, ["session", "list", "--session-dir", str(blocker / "x"), "--rebuild-index"]
        )
        assert result.exit_code == 0
        assert squash("not rebuilt") in squash(result.stderr)

    def test_help_documents_flags(self) -> None:
        result = runner.invoke(app, ["session", "list", "--help"])
        assert_flag_in_help(result, "--rebuild-index")


class TestBundleCli:
    def test_export_verify_import_round_trip(
        self, session: tuple[str, Path, Path], tmp_path: Path
    ) -> None:
        sid, root, caps = session
        out = tmp_path / "s.zip"
        result = runner.invoke(
            app,
            [
                "session",
                "export",
                sid,
                "-o",
                str(out),
                "--session-dir",
                str(root),
                "--capsule-dir",
                str(caps),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "3 member(s)" in result.output

        verified = runner.invoke(app, ["session", "verify-bundle", str(out)])
        assert verified.exit_code == 0, verified.output
        assert "PASSED" in verified.output
        verified_json = runner.invoke(app, ["session", "verify-bundle", str(out), "--json"])
        assert json.loads(verified_json.stdout)["ok"] is True

        dest = tmp_path / "dest"
        imported = runner.invoke(app, ["session", "import", str(out), "--session-dir", str(dest)])
        assert imported.exit_code == 0, imported.output
        shown = runner.invoke(
            app,
            [
                "session",
                "show",
                sid,
                "--session-dir",
                str(dest),
                "--json",
                "--capsule-dir",
                str(tmp_path / "nowhere"),
            ],
        )
        members = json.loads(shown.stdout)["members"]
        assert [m["status"] for m in members] == ["ok", "ok", "ok"]

        again = runner.invoke(app, ["session", "import", str(out), "--session-dir", str(dest)])
        assert again.exit_code == 1
        assert "already exists" in again.output

    def test_export_json_and_default_capsule_dir(
        self, session: tuple[str, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sid, root, caps = session
        monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(caps))
        out = tmp_path / "s.zip"
        result = runner.invoke(
            app, ["session", "export", sid, "-o", str(out), "--session-dir", str(root), "--json"]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["members"] == 3

    def test_export_refusal_exits_1(self, session: tuple[str, Path, Path], tmp_path: Path) -> None:
        sid, root, caps = session
        (caps / RUN_2 / "capsule.yaml").write_text("run_id: tampered\n")
        result = runner.invoke(
            app,
            [
                "session",
                "export",
                sid,
                "-o",
                str(tmp_path / "s.zip"),
                "--session-dir",
                str(root),
                "--capsule-dir",
                str(caps),
            ],
        )
        assert result.exit_code == 1
        assert "tampered" in result.output

    def test_verify_failure_exits_1(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("../escape.txt", b"x")
        result = runner.invoke(app, ["session", "verify-bundle", str(bad)])
        assert result.exit_code == 1
        assert "FAILED" in result.output
        assert "unsafe archive member" in result.output
        imported = runner.invoke(
            app, ["session", "import", str(bad), "--session-dir", str(tmp_path / "d")]
        )
        assert imported.exit_code == 1


class TestReplayP5Cli:
    def _args(self, root: Path, caps: Path, tmp_path: Path) -> list[str]:
        return [
            "--session-dir",
            str(root),
            "--capsule-dir",
            str(caps),
            "--output-dir",
            str(tmp_path / "replays"),
        ]

    def test_sub_range_with_pin(self, session: tuple[str, Path, Path], tmp_path: Path) -> None:
        sid, root, caps = session
        result = runner.invoke(
            app,
            [
                "session",
                "replay",
                sid,
                "--from",
                "1",
                "--to",
                "2",
                "--turn-mode",
                "2=forensic",
                "--json",
                *self._args(root, caps, tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        record = json.loads(result.stdout)
        assert record["range"] == {"from": 1, "to": 2}
        assert record["turn_mode_policy"] == {"2": "forensic"}
        assert [t["effective_mode"] for t in record["turns"]] == ["mocked", "forensic"]

    def test_sub_range_table_output(self, session: tuple[str, Path, Path], tmp_path: Path) -> None:
        sid, root, caps = session
        result = runner.invoke(
            app, ["session", "replay", sid, "--from", "2", *self._args(root, caps, tmp_path)]
        )
        assert result.exit_code == 0, result.output
        assert squash("Sub-range: turns 2..2") in squash(result.output)

    def test_sub_range_halt_reports_unreplayed(
        self, session: tuple[str, Path, Path], tmp_path: Path
    ) -> None:
        import shutil

        sid, root, caps = session
        shutil.rmtree(caps / RUN_2)
        result = runner.invoke(
            app, ["session", "replay", sid, "--from", "1", *self._args(root, caps, tmp_path)]
        )
        assert result.exit_code == 1
        assert squash("1 later turn(s) not replayed") in squash(result.output)

    def test_dry_run_writes_nothing(self, session: tuple[str, Path, Path], tmp_path: Path) -> None:
        sid, root, caps = session
        result = runner.invoke(
            app,
            [
                "session",
                "replay",
                sid,
                "--dry-run",
                "--turn-mode",
                "0=exact",
                *self._args(root, caps, tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert squash("dry run") in squash(result.output.lower())
        assert squash("exact (pinned)") in squash(result.output)
        assert not (tmp_path / "replays").exists()

        as_json = runner.invoke(
            app,
            [
                "session",
                "replay",
                sid,
                "--dry-run",
                "--json",
                "--to",
                "1",
                *self._args(root, caps, tmp_path),
            ],
        )
        plan = json.loads(as_json.stdout)
        assert plan["range"] == {"from": 0, "to": 1}
        assert [t["sequence"] for t in plan["turns"]] == [0, 1]
        assert not (tmp_path / "replays").exists()

    def test_dry_run_table_with_missing_member(
        self, session: tuple[str, Path, Path], tmp_path: Path
    ) -> None:
        import shutil

        sid, root, caps = session
        shutil.rmtree(caps / RUN_1)
        result = runner.invoke(
            app,
            [
                "session",
                "replay",
                sid,
                "--dry-run",
                "--from",
                "0",
                "--to",
                "0",
                *self._args(root, caps, tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert squash("missing → refuse") in squash(result.output)
        assert squash("turns 0..0 of 3") in squash(result.output)

    @pytest.mark.parametrize(
        ("pins", "match"),
        [
            (["x=mocked"], "SEQ=MODE"),
            (["1=turbo"], "SEQ=MODE"),
            (["1"], "SEQ=MODE"),
            (["1=mocked", "1=exact"], "more than once"),
        ],
    )
    def test_bad_turn_mode_is_usage_error(
        self, session: tuple[str, Path, Path], tmp_path: Path, pins: list[str], match: str
    ) -> None:
        sid, root, caps = session
        args = [a for p in pins for a in ("--turn-mode", p)]
        result = runner.invoke(
            app, ["session", "replay", sid, *args, *self._args(root, caps, tmp_path)]
        )
        assert result.exit_code == 2
        assert match in result.output

    def test_bad_range_exits_1(self, session: tuple[str, Path, Path], tmp_path: Path) -> None:
        sid, root, caps = session
        result = runner.invoke(
            app,
            [
                "session",
                "replay",
                sid,
                "--from",
                "2",
                "--to",
                "1",
                *self._args(root, caps, tmp_path),
            ],
        )
        assert result.exit_code == 1
        assert "after" in result.output

    def test_help_documents_flags(self) -> None:
        result = runner.invoke(app, ["session", "replay", "--help"])
        for flag in ("--from", "--to", "--turn-mode", "--dry-run"):
            assert_flag_in_help(result, flag)
