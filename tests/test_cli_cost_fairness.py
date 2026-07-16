"""ADR-0146 D5 — the ``nova cost fairness`` CLI.

Read-only. Loads per-agent resource totals per dimension and prints the fairness statistic (share,
Gini, max/mean) — descriptive evidence, no verdict. Exit 0 on render; 2 on missing/malformed input.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_DOC = {"totals": {
    "cost": {"agent-a": 90.0, "agent-b": 10.0},
    "calls": {"agent-a": 5.0, "agent-b": 5.0},
}}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "totals.json"
    p.write_text(json.dumps(doc))
    return p


def test_renders_dimensions_and_gini(tmp_path):
    result = runner.invoke(app, ["cost", "fairness", str(_write(tmp_path, _DOC))])
    assert result.exit_code == 0, result.output
    assert "cost" in result.output
    assert "gini" in result.output.lower()


def test_json_output(tmp_path):
    result = runner.invoke(app, ["cost", "fairness", str(_write(tmp_path, _DOC)), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    dims = [m["dimension"] for m in payload["metrics"]]
    assert dims == ["calls", "cost"]  # sorted
    calls = next(m for m in payload["metrics"] if m["dimension"] == "calls")
    assert calls["gini"] == 0.0  # equal split


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["cost", "fairness", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["cost", "fairness", str(p)])
    assert result.exit_code == 2
