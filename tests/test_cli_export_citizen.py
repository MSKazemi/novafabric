"""ADR-0169 D1 / NF-377 — the ``nova export-citizen-explanation`` CLI.

Read-only. Renders a plain-language, subject-facing decision explanation. Exit 0 on render; 2 on
missing/malformed input, an invalid ``human_involvement``, or a factor exposing model internals or a
raw sensitive identifier (refused, never carried).
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_DOC = {
    "decision_ref": "capsule://root#dec",
    "factors": ["income below the assistance threshold", "no prior claim in 12 months"],
    "human_involvement": "human_in_the_loop",
    "contest_channel_ref": "https://appeal.example.gov/case",
    "logic_summary_ref": "doc://logic-summary#v3",
}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "cit.json"
    p.write_text(json.dumps(doc))
    return p


def test_renders(tmp_path):
    result = runner.invoke(app, ["export-citizen-explanation", str(_write(tmp_path, _DOC))])
    assert result.exit_code == 0, result.output
    assert "human_in_the_loop" in result.output
    assert "income below the assistance threshold" in result.output


def test_json_carries_factors_and_involvement(tmp_path):
    result = runner.invoke(
        app, ["export-citizen-explanation", str(_write(tmp_path, _DOC)), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["human_involvement"] == "human_in_the_loop"
    assert len(payload["factors"]) == 2
    assert "legally_sufficient" not in payload  # never claims legal sufficiency


def test_model_internal_factor_is_refused(tmp_path):
    doc = dict(_DOC)
    doc["factors"] = ["income low", "output logit 2.1 for class A"]
    result = runner.invoke(app, ["export-citizen-explanation", str(_write(tmp_path, doc))])
    assert result.exit_code == 2
    assert "logit" in result.output


def test_invalid_involvement_exits_two(tmp_path):
    doc = dict(_DOC)
    doc["human_involvement"] = "fully_manual"
    result = runner.invoke(app, ["export-citizen-explanation", str(_write(tmp_path, doc))])
    assert result.exit_code == 2


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(
        app, ["export-citizen-explanation", str(tmp_path / "nope.json")]
    )
    assert result.exit_code == 2


def test_malformed_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["export-citizen-explanation", str(p)])
    assert result.exit_code == 2
