"""ADR-0169 D5 / NF-380 — the ``nova export-accessibility-claim`` CLI.

Read-only. Renders a declared accessibility-conformance claim. The standard may come from the
document or the ``--standard`` flag (flag wins). Exit 0 on render; 2 on missing/malformed input or an
invalid/absent standard.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_DOC = {
    "declared_standard": "wcag_2_2_aa",
    "audit_digest": "audit://declared#d1",
    "export_format_check": True,
}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "a11y.json"
    p.write_text(json.dumps(doc))
    return p


def test_renders_from_document(tmp_path):
    result = runner.invoke(app, ["export-accessibility-claim", str(_write(tmp_path, _DOC))])
    assert result.exit_code == 0, result.output
    assert "wcag_2_2_aa" in result.output


def test_json_no_compliance_guarantee(tmp_path):
    result = runner.invoke(
        app, ["export-accessibility-claim", str(_write(tmp_path, _DOC)), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["declared_standard"] == "wcag_2_2_aa"
    assert payload["export_format_check"] is True
    assert "compliance_guaranteed" not in payload


def test_standard_flag_overrides_document(tmp_path):
    doc = dict(_DOC)
    doc["declared_standard"] = "wcag_2_2_aa"
    result = runner.invoke(
        app,
        ["export-accessibility-claim", str(_write(tmp_path, doc)),
         "--standard", "en_301_549_v4_1_1", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["declared_standard"] == "en_301_549_v4_1_1"


def test_invalid_standard_exits_two(tmp_path):
    doc = dict(_DOC)
    doc["declared_standard"] = "section_508"
    result = runner.invoke(app, ["export-accessibility-claim", str(_write(tmp_path, doc))])
    assert result.exit_code == 2


def test_absent_standard_exits_two(tmp_path):
    result = runner.invoke(
        app, ["export-accessibility-claim", str(_write(tmp_path, {"audit_digest": "x"}))]
    )
    assert result.exit_code == 2


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["export-accessibility-claim", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["export-accessibility-claim", str(p)])
    assert result.exit_code == 2
