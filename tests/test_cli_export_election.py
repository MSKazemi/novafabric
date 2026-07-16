"""ADR-0169 D5 / NF-379 — the ``nova export-election-disclosure`` CLI.

Read-only. Renders an election/democratic-process content-provenance disclosure. Exit 0 on render; 2
on missing/malformed input or an invalid ``disclosure_label``.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_DOC = {
    "content_ref": "content://ad#c1",
    "provenance_receipt_ref": "receipt://nf094#digest",
    "disclosure_label": "ai_generated",
    "capsule_refs": ["capsule://root/run1#d1", "capsule://root/run2#d2"],
}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "elec.json"
    p.write_text(json.dumps(doc))
    return p


def test_renders(tmp_path):
    result = runner.invoke(app, ["export-election-disclosure", str(_write(tmp_path, _DOC))])
    assert result.exit_code == 0, result.output
    assert "ai_generated" in result.output
    assert "receipt://nf094#digest" in result.output


def test_json_binds_receipt_no_verdict(tmp_path):
    result = runner.invoke(
        app, ["export-election-disclosure", str(_write(tmp_path, _DOC)), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["disclosure_label"] == "ai_generated"
    assert payload["provenance_receipt_ref"] == "receipt://nf094#digest"
    assert payload["capsule_refs"] == ["capsule://root/run1#d1", "capsule://root/run2#d2"]
    for forbidden in ("lawful", "deceptive", "verdict"):
        assert forbidden not in payload


def test_invalid_label_exits_two(tmp_path):
    doc = dict(_DOC)
    doc["disclosure_label"] = "deepfake"
    result = runner.invoke(app, ["export-election-disclosure", str(_write(tmp_path, doc))])
    assert result.exit_code == 2


def test_missing_content_ref_exits_two(tmp_path):
    doc = {"provenance_receipt_ref": "r", "disclosure_label": "ai_assisted"}
    result = runner.invoke(app, ["export-election-disclosure", str(_write(tmp_path, doc))])
    assert result.exit_code == 2


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["export-election-disclosure", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["export-election-disclosure", str(p)])
    assert result.exit_code == 2
