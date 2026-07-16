"""ADR-0169 D1 / NF-372 — the ``nova export-transparency-register`` CLI.

Read-only. Renders a DRAFT algorithm-register record for the ``--standard``-selected register
(ATRS / Amsterdam / Helsinki). Exit 0 on render; 2 on missing/malformed input or an unknown standard.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_DOC = {
    "capsule_root": "r" * 64,
    "capsule_evidence": {"tool_name": "capsule://root/name#d1"},
    "operator_declared": {"responsible_organisation": "Acme Public Body"},
}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "reg.json"
    p.write_text(json.dumps(doc))
    return p


def test_renders_atrs_by_default_or_flag(tmp_path):
    result = runner.invoke(
        app, ["export-transparency-register", str(_write(tmp_path, _DOC)), "--standard", "atrs"]
    )
    assert result.exit_code == 0, result.output
    assert "atrs" in result.output.lower()
    assert "DRAFT" in result.output


def test_json_carries_standard_version_and_sources(tmp_path):
    result = runner.invoke(
        app,
        ["export-transparency-register", str(_write(tmp_path, _DOC)),
         "--standard", "atrs", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["standard"] == "atrs"
    assert payload["standard_version"]
    assert payload["status"] == "DRAFT"
    name = next(f for f in payload["fields"] if f["name"] == "tool_name")
    assert name["source"] == "capsule_evidence"
    assert name["evidence_ref"] == "capsule://root/name#d1"
    assert name["value"] is None
    assert "responsible_organisation" not in payload["manual_completion_required"]


def test_amsterdam_standard_selects_its_own_shape(tmp_path):
    result = runner.invoke(
        app,
        ["export-transparency-register", str(_write(tmp_path, {"capsule_root": "r" * 64})),
         "--standard", "amsterdam", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["standard"] == "amsterdam"
    assert "algorithm_name" in payload["manual_completion_required"]


def test_unknown_standard_exits_two(tmp_path):
    result = runner.invoke(
        app,
        ["export-transparency-register", str(_write(tmp_path, _DOC)), "--standard", "california"],
    )
    assert result.exit_code == 2


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(
        app,
        ["export-transparency-register", str(tmp_path / "nope.json"), "--standard", "atrs"],
    )
    assert result.exit_code == 2


def test_malformed_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(
        app, ["export-transparency-register", str(p), "--standard", "atrs"]
    )
    assert result.exit_code == 2
