"""ADR-0170 D5 / NF-387 — the ``nova export-control-attestation`` CLI.

Read-only. Renders a governance-control attestation pack — each control ``evidenced`` /
``not_evidenced`` / ``declared``. Exit 0 on render; 2 on missing/malformed input.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_DOC = {
    "capsule_root": "r" * 64,
    "catalog": [
        {"control_id": "GOV-1", "evidence_kind": "sealing"},
        {"control_id": "GOV-2", "evidence_kind": "redaction"},
        {"control_id": "GOV-3"},
    ],
    "present_evidence": {"sealing": "capsule://root/seal#d1"},
    "declared": ["GOV-3"],
}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "ctrl.json"
    p.write_text(json.dumps(doc))
    return p


def test_renders(tmp_path):
    result = runner.invoke(
        app, ["export-control-attestation", str(_write(tmp_path, _DOC))]
    )
    assert result.exit_code == 0, result.output
    assert "GOV-1" in result.output
    assert "evidenced" in result.output


def test_json_maps_statuses(tmp_path):
    result = runner.invoke(
        app, ["export-control-attestation", str(_write(tmp_path, _DOC)), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    status = {e["control_id"]: e["status"] for e in payload["entries"]}
    assert status == {"GOV-1": "evidenced", "GOV-2": "not_evidenced", "GOV-3": "declared"}
    assert payload["summary"]["evidenced"] == 1
    assert "certified" not in payload


def test_missing_capsule_root_exits_two(tmp_path):
    result = runner.invoke(
        app, ["export-control-attestation", str(_write(tmp_path, {"catalog": []}))]
    )
    assert result.exit_code == 2


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(
        app, ["export-control-attestation", str(tmp_path / "nope.json")]
    )
    assert result.exit_code == 2


def test_malformed_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["export-control-attestation", str(p)])
    assert result.exit_code == 2
