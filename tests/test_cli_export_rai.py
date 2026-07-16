"""ADR-0158 D4 — the ``nova export-rai-scorecard`` CLI.

Read-only. Loads per-dimension evidence refs and prints the RAI coverage scorecard (supported /
partial / unsupported / not_applicable) — never a score. Exit 0 on render; 2 on missing/malformed.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_DOC = {
    "evidence": {"fairness": ["capsule://f#1"], "privacy": ["capsule://p#1"]},
    "not_applicable": ["accessibility"],
    "partial": ["privacy"],
}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "rai.json"
    p.write_text(json.dumps(doc))
    return p


def test_renders_coverage_states(tmp_path):
    result = runner.invoke(app, ["export-rai-scorecard", str(_write(tmp_path, _DOC))])
    assert result.exit_code == 0, result.output
    assert "fairness" in result.output
    assert "supported" in result.output.lower()
    assert "not_applicable" in result.output.lower()


def test_json_output_has_no_score(tmp_path):
    result = runner.invoke(app, ["export-rai-scorecard", str(_write(tmp_path, _DOC)), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "score" not in payload
    fairness = next(c for c in payload["cells"] if c["dimension"] == "fairness")
    assert fairness["coverage"] == "supported"
    privacy = next(c for c in payload["cells"] if c["dimension"] == "privacy")
    assert privacy["coverage"] == "partial"


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["export-rai-scorecard", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["export-rai-scorecard", str(p)])
    assert result.exit_code == 2
