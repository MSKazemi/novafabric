"""ADR-0159 D6 / NF-278 — the ``nova export-adverse-action`` CLI.

Read-only. Exit 0 on render (a ``missing`` principal-reasons row is valid, honest output); exit 2
only when the capsule cannot be found, its sealed evidence is corrupt, or ``--out`` would write
inside the capsule. The CFPB honesty line and the finance banner are always printed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from _adverse_action_capsule import SECRET, make_capsule, valid_facet
from _adverse_action_capsule import call as _call
from _help_assert import assert_flag_in_help
from rich.console import Console
from typer.testing import CliRunner

from novafabric.cli import export_adverse_action as mod
from novafabric.cli.main import app
from novafabric.compliance.export.finance import adverse_action_collect as collect
from novafabric.compliance.export.finance.adverse_action import CFPB_HONESTY_LINE

runner = CliRunner()


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "console", Console(width=400))


def _squash(text: str) -> str:
    return " ".join(text.split())


def test_json_output_renders_complete_pack(tmp_path: Path) -> None:
    make_capsule(tmp_path, calls=[_call("mc-001")], inputs={"a": b"1"}, facet=valid_facet())
    res = runner.invoke(
        app,
        [
            "export-adverse-action",
            "--run-id",
            "01RUNCREDIT",
            "--capsule-dir",
            str(tmp_path),
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["summary"] == {"complete": 4, "partial": 0, "missing": 0}
    assert [r["rank"] for r in data["principal_reasons"]] == [2, 1, 3]
    assert data["cfpb_honesty"] == CFPB_HONESTY_LINE


def test_rich_output_missing_attribution_exits_zero_with_banner(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path, calls=[_call("mc-001")])
    res = runner.invoke(app, ["export-adverse-action", "--run-id", str(cap)])
    assert res.exit_code == 0, res.output
    out = _squash(res.output)
    assert "principal_reasons missing" in out
    assert "no attribution facet" in out
    assert "NOT an adverse-action notice" in out
    assert "does not guarantee compliance" in out
    assert "model=credit-underwriter-v4" in out


def test_rich_output_lists_reasons_in_recorded_order(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path, calls=[_call("mc-001")], facet=valid_facet())
    res = runner.invoke(app, ["export-adverse-action", "--run-id", str(cap)])
    assert res.exit_code == 0, res.output
    out = _squash(res.output)
    first = out.index("#1 rank=2 Number of recent delinquencies")
    second = out.index("#2 rank=1 Debt-to-income ratio too high")
    assert first < second


def test_out_writes_json_file(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path / "store", calls=[_call("mc-001")], facet=valid_facet())
    target = tmp_path / "aa-pack.json"
    res = runner.invoke(app, ["export-adverse-action", "--run-id", str(cap), "--out", str(target)])
    assert res.exit_code == 0, res.output
    assert json.loads(target.read_text())["run_id"] == "01RUNCREDIT"
    assert "Wrote" in res.output


def test_out_inside_capsule_is_refused(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path, calls=[_call("mc-001")])
    res = runner.invoke(
        app, ["export-adverse-action", "--run-id", str(cap), "--out", str(cap / "x.json")]
    )
    assert res.exit_code == 2
    assert not (cap / "x.json").exists()


def test_out_unwritable_exits_two(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path / "store", calls=[_call("mc-001")])
    target = tmp_path / "no-such-dir" / "x.json"
    res = runner.invoke(app, ["export-adverse-action", "--run-id", str(cap), "--out", str(target)])
    assert res.exit_code == 2


def test_unknown_run_id_exits_two(tmp_path: Path) -> None:
    res = runner.invoke(
        app, ["export-adverse-action", "--run-id", "NOPE", "--capsule-dir", str(tmp_path)]
    )
    assert res.exit_code == 2


def test_corrupt_capsule_exits_two(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path, calls=[_call("mc-001")])
    (cap / "model-calls.jsonl").write_text("tampered\n")
    res = runner.invoke(app, ["export-adverse-action", "--run-id", str(cap), "--json"])
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
def test_sealed_model_calls_that_vanished_exits_two(tmp_path: Path, how: str) -> None:
    cap = make_capsule(tmp_path, calls=[_call("mc-001")], facet=valid_facet())
    _vanish(cap / "model-calls.jsonl", how)
    res = runner.invoke(app, ["export-adverse-action", "--run-id", str(cap), "--json"])
    assert res.exit_code == 2, res.output
    assert "Corrupt" in res.output


def test_oversize_model_calls_exits_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collect, "MODEL_CALLS_MAX_BYTES", 10)
    cap = make_capsule(tmp_path, calls=[_call("mc-001")], facet=valid_facet())
    res = runner.invoke(app, ["export-adverse-action", "--run-id", str(cap), "--json"])
    assert res.exit_code == 2, res.output
    assert "Corrupt" in res.output


def test_secret_in_rank_is_never_emitted(tmp_path: Path) -> None:
    facet = valid_facet()
    facet["reasons"][0]["rank"] = SECRET
    cap = make_capsule(tmp_path, calls=[_call("mc-001")], facet=facet)
    res = runner.invoke(app, ["export-adverse-action", "--run-id", str(cap), "--json"])
    assert res.exit_code == 2
    assert SECRET not in res.output


@pytest.mark.parametrize("json_flag", [["--json"], []])
def test_secret_in_producer_is_suppressed_in_every_output(
    tmp_path: Path, json_flag: list[str]
) -> None:
    facet = valid_facet() | {"producer": SECRET, "method": f"shap {SECRET}"}
    cap = make_capsule(tmp_path, calls=[_call("mc-001")], facet=facet)
    out_file = tmp_path / "pack.json"
    res = runner.invoke(
        app,
        ["export-adverse-action", "--run-id", str(cap), "--out", str(out_file), *json_flag],
    )
    assert res.exit_code == 0, res.output
    assert SECRET not in res.output
    assert SECRET not in out_file.read_text()
    assert json.loads(out_file.read_text())["attribution_suppressed_fields"] == [
        "method",
        "producer",
    ]


def test_help_lists_command() -> None:
    res = runner.invoke(app, ["export-adverse-action", "--help"])
    assert res.exit_code == 0
    assert_flag_in_help(res, "--run-id")
    assert_flag_in_help(res, "--out")
