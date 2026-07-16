"""ADR-0159 D2 — the ``nova export-model-risk`` CLI.

Read-only. Loads a JSON of per-pillar evidence refs and renders the SR 26-2 / SR 11-7 model-risk
evidence file: each pillar complete/partial/missing, no rating. Exit 0 on render; 2 on
missing/malformed input.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_DOC = {
    "model_id": "agent-7",
    "development": ["capsule://eval-card-1"],
    "independent_validation": ["capsule://val-1"],
    "ongoing_monitoring": [],
    "model_inventory": ["capsule://inv-1"],
}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "ev.json"
    p.write_text(json.dumps(doc))
    return p


def test_renders_pillars_and_regime(tmp_path):
    result = runner.invoke(app, ["export-model-risk", str(_write(tmp_path, _DOC))])
    assert result.exit_code == 0, result.output
    assert "SR 26-2" in result.output
    assert "ongoing_monitoring" in result.output
    assert "missing" in result.output.lower()  # the empty pillar is surfaced as missing


def test_json_output(tmp_path):
    result = runner.invoke(app, ["export-model-risk", str(_write(tmp_path, _DOC)), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["model_id"] == "agent-7"
    assert payload["regime"].startswith("SR 26-2")
    assert payload["summary"] == {"complete": 3, "partial": 0, "missing": 1}
    assert "rating" not in payload  # assemble, never assess


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["export-model-risk", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["export-model-risk", str(p)])
    assert result.exit_code == 2
