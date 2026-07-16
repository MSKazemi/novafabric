"""ADR-0169 D1 / NF-374 — the ``nova export-foia`` CLI.

Read-only. Renders a DRAFT FOIA/public-records export (ordered record_index, redactions as salted
digest + claimed exemption, custody_digest). Exit 0 on render; 2 on missing/malformed input.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_DOC = {
    "decision_ref": "capsule://root#dec",
    "record_index": ["z#1", "a#2", "m#3"],
    "redactions": [
        {"digest": "salted:abc123", "exemption_ref": "FOIA(b)(6) — personal privacy"},
    ],
}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "foia.json"
    p.write_text(json.dumps(doc))
    return p


def test_renders_draft_with_custody(tmp_path):
    result = runner.invoke(app, ["export-foia", str(_write(tmp_path, _DOC))])
    assert result.exit_code == 0, result.output
    assert "DRAFT" in result.output
    assert "custody" in result.output.lower()


def test_json_preserves_order_and_carries_redactions(tmp_path):
    result = runner.invoke(app, ["export-foia", str(_write(tmp_path, _DOC)), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "DRAFT"
    assert payload["record_index"] == ["z#1", "a#2", "m#3"]  # order preserved
    assert payload["redactions"][0]["exemption_ref"] == "FOIA(b)(6) — personal privacy"
    assert "value" not in payload["redactions"][0]  # withheld bytes absent
    assert len(payload["custody_digest"]) == 64


def test_missing_decision_ref_exits_two(tmp_path):
    result = runner.invoke(app, ["export-foia", str(_write(tmp_path, {"record_index": ["x#1"]}))])
    assert result.exit_code == 2


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["export-foia", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["export-foia", str(p)])
    assert result.exit_code == 2


def test_malformed_redaction_exits_two(tmp_path):
    doc = {"decision_ref": "d", "redactions": [{"digest": "x"}]}  # missing exemption_ref
    result = runner.invoke(app, ["export-foia", str(_write(tmp_path, doc))])
    assert result.exit_code == 2
