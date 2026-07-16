"""ADR-0169 D1 / NF-378 — the ``nova export-public-incident`` CLI.

Read-only. Renders a DRAFT public-interest incident summary. Exit 0 on render; 2 on missing/malformed
input or a per-subject identifier in the summary/scope (refused — aggregate only).
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_DOC = {
    "incident_ref": "incident://root#i1",
    "public_summary": "An automated triage agent misclassified a subset of applications.",
    "affected_scope": "approximately 12,000 applications in region X",
    "remediation_ref": "capsule://root/fix#r1",
}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "inc.json"
    p.write_text(json.dumps(doc))
    return p


def test_renders_draft(tmp_path):
    result = runner.invoke(app, ["export-public-incident", str(_write(tmp_path, _DOC))])
    assert result.exit_code == 0, result.output
    assert "DRAFT" in result.output
    assert "12,000" in result.output


def test_json_is_draft_and_aggregate(tmp_path):
    result = runner.invoke(
        app, ["export-public-incident", str(_write(tmp_path, _DOC)), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["draft"] is True
    assert payload["incident_ref"] == "incident://root#i1"
    assert "compliance_guaranteed" not in payload


def test_per_subject_identifier_refused(tmp_path):
    doc = dict(_DOC)
    doc["affected_scope"] = "applicant SSN 123-45-6789 affected"
    result = runner.invoke(app, ["export-public-incident", str(_write(tmp_path, doc))])
    assert result.exit_code == 2
    assert "affected_scope" in result.output


def test_missing_incident_ref_exits_two(tmp_path):
    result = runner.invoke(
        app, ["export-public-incident", str(_write(tmp_path, {"public_summary": "x"}))]
    )
    assert result.exit_code == 2


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["export-public-incident", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["export-public-incident", str(p)])
    assert result.exit_code == 2
