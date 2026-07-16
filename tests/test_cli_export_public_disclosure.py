"""ADR-0169 D1 / NF-373 — the ``nova export-public-disclosure`` CLI.

Read-only. Renders a DRAFT public-sector agentic-AI disclosure record from supplied references.
Exit 0 on render; 2 on missing/malformed input.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_DOC = {
    "authority_ref": "gov://body/dwp",
    "agent_ref": "capsule://root/agent#a1",
    "decision_scope": "benefit-eligibility triage",
    "human_oversight_ref": "capsule://root/hitl#h1",
    "capsule_refs": ["capsule://root/run1#d1", "capsule://root/run2#d2"],
    "system_card_ref": "card://e7#digest",
}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "disc.json"
    p.write_text(json.dumps(doc))
    return p


def test_renders_draft(tmp_path):
    result = runner.invoke(app, ["export-public-disclosure", str(_write(tmp_path, _DOC))])
    assert result.exit_code == 0, result.output
    assert "DRAFT" in result.output
    assert "gov://body/dwp" in result.output


def test_json_carries_refs_and_no_gaps(tmp_path):
    result = runner.invoke(
        app, ["export-public-disclosure", str(_write(tmp_path, _DOC)), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "DRAFT"
    assert payload["capsule_refs"] == ["capsule://root/run1#d1", "capsule://root/run2#d2"]
    assert payload["system_card_ref"] == "card://e7#digest"
    assert payload["manual_completion_required"] == []


def test_missing_required_reported_not_fabricated(tmp_path):
    doc = {"agent_ref": "a", "decision_scope": "s"}
    result = runner.invoke(
        app, ["export-public-disclosure", str(_write(tmp_path, doc)), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["authority_ref"] is None
    assert "authority_ref" in payload["manual_completion_required"]
    assert "capsule_refs" in payload["manual_completion_required"]


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["export-public-disclosure", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["export-public-disclosure", str(p)])
    assert result.exit_code == 2


def test_non_object_exits_two(tmp_path):
    p = tmp_path / "arr.json"
    p.write_text("[1, 2, 3]")
    result = runner.invoke(app, ["export-public-disclosure", str(p)])
    assert result.exit_code == 2
