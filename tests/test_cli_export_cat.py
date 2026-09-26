"""ADR-0159 D6 / NF-280 — the ``nova export-cat`` CLI.

Read-only and offline. Exit 0 on render (a ``missing`` / ``partial`` stage is valid, honest
output); exit 2 only when the capsule cannot be found, its sealed evidence is corrupt, or
``--out`` would write inside the capsule. The CAT honesty line and the finance banner are always
printed.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest
from _cat_capsule import SECRET, make_capsule, valid_fixture
from _help_assert import assert_flag_in_help
from rich.console import Console
from typer.testing import CliRunner

from novafabric.cli import export_cat as mod
from novafabric.cli.main import app
from novafabric.compliance.export.finance import cat_collect
from novafabric.compliance.export.finance.cat_trail import CAT_HONESTY_LINE, CAT_REGIME

runner = CliRunner()


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "console", Console(width=400))


def _squash(text: str) -> str:
    return " ".join(text.split())


def test_json_output_renders_complete_trail(tmp_path: Path) -> None:
    make_capsule(tmp_path)
    res = runner.invoke(
        app, ["export-cat", "--run-id", "01RUNCAT", "--capsule-dir", str(tmp_path), "--json"]
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["trail_status"] == "complete"
    assert [e["stage"] for e in data["events"]] == [
        "origination",
        "decision",
        "authorization",
        "authorization",
        "action",
        "disposition",
    ]
    assert data["cat_honesty"] == CAT_HONESTY_LINE
    assert data["regime"] == CAT_REGIME


def test_rich_output_missing_stage_exits_zero_with_banner(tmp_path: Path) -> None:
    fx = valid_fixture()
    cap = make_capsule(tmp_path, streams={"model-calls.jsonl": fx["model-calls.jsonl"]})
    res = runner.invoke(app, ["export-cat", "--run-id", str(cap)])
    assert res.exit_code == 0, res.output
    out = _squash(res.output)
    assert "trail=partial" in out
    assert "action missing events=0" in out
    assert "authorization missing events=0 (optional)" in out
    assert _squash(CAT_HONESTY_LINE) in out
    assert "NOT a CAT submission" in out
    assert "actor=order-router-v2-2026-08" in out


def test_out_writes_json_file(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path / "store")
    target = tmp_path / "cat-events.json"
    res = runner.invoke(app, ["export-cat", "--run-id", str(cap), "--out", str(target)])
    assert res.exit_code == 0, res.output
    assert json.loads(target.read_text())["run_id"] == "01RUNCAT"
    assert "Wrote" in res.output


def test_out_inside_capsule_is_refused(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path)
    res = runner.invoke(app, ["export-cat", "--run-id", str(cap), "--out", str(cap / "x.json")])
    assert res.exit_code == 2
    assert not (cap / "x.json").exists()


def test_out_unwritable_exits_two(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path / "store")
    target = tmp_path / "no-such-dir" / "x.json"
    res = runner.invoke(app, ["export-cat", "--run-id", str(cap), "--out", str(target)])
    assert res.exit_code == 2


def test_unknown_run_exits_two(tmp_path: Path) -> None:
    res = runner.invoke(app, ["export-cat", "--run-id", "NOPE", "--capsule-dir", str(tmp_path)])
    assert res.exit_code == 2


def test_corrupt_evidence_exits_two(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path)
    with (cap / "model-calls.jsonl").open("a") as fh:
        fh.write("{}\n")
    res = runner.invoke(app, ["export-cat", "--run-id", str(cap)])
    assert res.exit_code == 2
    assert "Corrupt" in res.output


def _vanish(path: Path, how: str) -> None:
    if how == "symlink":
        outside = path.parent.parent / f"outside-{path.name}"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        return
    path.unlink()
    if how == "fifo":
        os.mkfifo(path)


@pytest.mark.parametrize("how", ["deleted", "symlink", "fifo"])
def test_sealed_stream_that_vanished_exits_two(tmp_path: Path, how: str) -> None:
    """Reviewer PoC: a deleted sealed human_approvals.jsonl must not render ``complete``."""
    cap = make_capsule(tmp_path)
    _vanish(cap / "human_approvals.jsonl", how)
    res = runner.invoke(app, ["export-cat", "--run-id", str(cap), "--json"])
    assert res.exit_code == 2, res.output
    assert "Corrupt" in res.output


def test_oversize_stream_exits_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cat_collect, "STREAM_MAX_BYTES", 10)
    cap = make_capsule(tmp_path)
    res = runner.invoke(app, ["export-cat", "--run-id", str(cap), "--json"])
    assert res.exit_code == 2, res.output
    assert "Corrupt" in res.output


def test_secret_never_reaches_any_output(tmp_path: Path) -> None:
    fx = valid_fixture()
    tc = dict(fx["tool-calls.jsonl"][0], tool_name=SECRET)
    cap = make_capsule(tmp_path / "store", streams={"tool-calls.jsonl": [tc]})
    target = tmp_path / "o.json"
    for extra in ([], ["--json"]):
        res = runner.invoke(app, ["export-cat", "--run-id", str(cap), "--out", str(target), *extra])
        assert res.exit_code == 0, res.output
        assert SECRET not in res.output
        assert SECRET not in target.read_text()


def test_cli_opens_no_network_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_network(*_a: object, **_k: object) -> None:
        raise AssertionError("nova export-cat must never connect to the CAT repository")

    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    cap = make_capsule(tmp_path)
    res = runner.invoke(app, ["export-cat", "--run-id", str(cap), "--json"])
    assert res.exit_code == 0, res.output


def test_help_lists_flags() -> None:
    res = runner.invoke(app, ["export-cat", "--help"])
    assert res.exit_code == 0
    for flag in ("--run-id", "--capsule-dir", "--json", "--out"):
        assert_flag_in_help(res, flag)
    assert "NOT a CAT submission" in _squash(res.output)
