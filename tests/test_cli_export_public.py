"""ADR-0169 D1 — the ``nova export-public-annex-viii`` CLI.

Read-only. Loads per-field sources and renders the DRAFT EU AI Act Annex VIII public-DB entry:
each field capsule_evidence (ref only) or operator_declared, unmapped required fields listed.
Exit 0 on render; 2 on missing/malformed input.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_DOC = {
    "capsule_root": "r" * 64,
    "capsule_evidence": {"provider_name": "capsule://root/provider#deadbeef"},
    "operator_declared": {"system_trade_name": "Acme Underwriter"},
}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "pub.json"
    p.write_text(json.dumps(doc))
    return p


def test_renders_draft_entry_and_unmapped(tmp_path):
    result = runner.invoke(app, ["export-public-annex-viii", str(_write(tmp_path, _DOC))])
    assert result.exit_code == 0, result.output
    assert "DRAFT" in result.output
    assert "provider_name" in result.output
    assert "unmapped" in result.output.lower()  # some required fields are unmapped here


def test_json_output(tmp_path):
    result = runner.invoke(
        app, ["export-public-annex-viii", str(_write(tmp_path, _DOC)), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "DRAFT"
    prov = next(f for f in payload["fields"] if f["name"] == "provider_name")
    assert prov["source"] == "capsule_evidence"
    assert prov["value"] is None  # ref only, never the raw value


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["export-public-annex-viii", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["export-public-annex-viii", str(p)])
    assert result.exit_code == 2
