"""ADR-0146 D3 / NF-148 — the ``nova cost attribute`` CLI (wasted-spend attribution)."""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_DOC = {
    "runs": [
        {"run_id": "r1", "status": "success", "cost": 2.0},
        {"run_id": "r2", "status": "failure", "cost": 1.0},
        {"run_id": "r3", "status": "aborted", "cost": 1.0},
    ]
}


def _write(tmp_path: Path, name: str, doc) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return p


def test_attribute_renders_exit_zero(tmp_path):
    result = runner.invoke(app, ["cost", "attribute", str(_write(tmp_path, "s.json", _DOC))])
    assert result.exit_code == 0, result.output
    assert "wasted" in result.output.lower()


def test_attribute_json(tmp_path):
    result = runner.invoke(
        app, ["cost", "attribute", str(_write(tmp_path, "s.json", _DOC)), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total_spend"] == 4.0
    assert payload["productive_spend"] == 2.0
    assert payload["wasted_spend"] == 2.0


def test_negative_cost_exits_two(tmp_path):
    doc = {"runs": [{"run_id": "r1", "status": "success", "cost": -1.0}]}
    result = runner.invoke(app, ["cost", "attribute", str(_write(tmp_path, "bad.json", doc))])
    assert result.exit_code == 2


def test_runs_not_array_exits_two(tmp_path):
    result = runner.invoke(
        app, ["cost", "attribute", str(_write(tmp_path, "bad.json", {"runs": "nope"}))]
    )
    assert result.exit_code == 2


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["cost", "attribute", str(tmp_path / "nope.json")])
    assert result.exit_code == 2
