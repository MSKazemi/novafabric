"""ADR-0166 D4 — the ``nova assure-coverage`` CLI (NF-348).

Read-only. Renders the structural coverage of an assurance-case document. Because coverage is
**never a grade** (ADR-0166 D4), the command exits 0 whenever it renders — even with open defeaters
or unsupported leaves — and 2 only when the input is missing/malformed or a currency ledger is given
without ``--as-of``.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_DOC = {
    "case": {
        "case_id": "c1",
        "nodes": [
            {"id": "G", "type": "goal", "statement": "g", "supported_by": ["S"]},
            {
                "id": "S", "type": "solution", "statement": "s",
                "evidence_refs": [{"ref": "capsule://x", "digest": "d1"}],
            },
        ],
    },
    "resolvable_digests": ["d1"],
    "defeaters": [{"id": "def1", "target_node_id": "G", "statement": "challenge"}],
}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "case.json"
    p.write_text(json.dumps(doc))
    return p


def test_renders_and_exits_zero_even_with_open_defeater(tmp_path):
    result = runner.invoke(app, ["assure-coverage", str(_write(tmp_path, _DOC))])
    assert result.exit_code == 0, result.output
    assert "coverage" in result.output.lower()
    assert "goal" in result.output.lower()


def test_json_reports_structural_counts_no_grade(tmp_path):
    result = runner.invoke(app, ["assure-coverage", str(_write(tmp_path, _DOC)), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total_goals"] == 1
    assert payload["goals_with_resolvable_leaf"] == 1
    assert payload["open_defeaters"] == 1
    assert payload["unsupported_leaves"] == []
    for forbidden in ("grade", "score", "pass", "verdict", "ok"):
        assert forbidden not in payload


def test_currency_ledger_without_as_of_exits_two(tmp_path):
    doc = dict(_DOC)
    doc["currency"] = {"nodes": [
        {"node_id": "S", "last_refreshed": "2026-01-01T00:00:00Z", "evidence_window": "P30D"},
    ]}
    result = runner.invoke(app, ["assure-coverage", str(_write(tmp_path, doc))])
    assert result.exit_code == 2


def test_currency_overdue_reported_with_as_of(tmp_path):
    doc = dict(_DOC)
    doc["currency"] = {"nodes": [
        {"node_id": "S", "last_refreshed": "2026-01-01T00:00:00Z", "evidence_window": "P30D"},
    ]}
    result = runner.invoke(
        app,
        ["assure-coverage", str(_write(tmp_path, doc)), "--as-of", "2026-06-01T00:00:00Z", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["overdue_nodes"] == ["S"]


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["assure-coverage", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["assure-coverage", str(p)])
    assert result.exit_code == 2
