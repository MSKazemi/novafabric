"""ADR-0154 D2 / NF-229 — the ``nova eval cost`` CLI.

Read-only. Renders a self-reported eval-cost disclosure. Exit 0 on render; 2 on missing/malformed
input or a negative figure.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_DOC = {
    "wall_seconds": 12.5,
    "token_in": 1000,
    "token_out": 250,
    "usd_cost": 0.0123,
    "energy_wh": 4.2,
    "hardware_ref": "hw://a100#node3",
}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "cost.json"
    p.write_text(json.dumps(doc))
    return p


def test_renders(tmp_path):
    result = runner.invoke(app, ["eval", "cost", str(_write(tmp_path, _DOC))])
    assert result.exit_code == 0, result.output
    assert "self-reported" in result.output.lower()
    assert "12.5" in result.output


def test_json_carries_fields_and_self_reported(tmp_path):
    result = runner.invoke(app, ["eval", "cost", str(_write(tmp_path, _DOC)), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["self_reported"] is True
    assert payload["usd_cost"] == 0.0123
    assert payload["energy_wh"] == 4.2
    assert "measured" not in payload


def test_minimal_without_optional_energy(tmp_path):
    doc = {"wall_seconds": 1.0, "token_in": 10, "token_out": 5, "usd_cost": 0.001}
    result = runner.invoke(app, ["eval", "cost", str(_write(tmp_path, doc)), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["energy_wh"] is None


def test_negative_figure_exits_two(tmp_path):
    doc = dict(_DOC)
    doc["usd_cost"] = -1.0
    result = runner.invoke(app, ["eval", "cost", str(_write(tmp_path, doc))])
    assert result.exit_code == 2


def test_missing_required_exits_two(tmp_path):
    doc = {"wall_seconds": 1.0, "token_in": 10}  # missing token_out, usd_cost
    result = runner.invoke(app, ["eval", "cost", str(_write(tmp_path, doc))])
    assert result.exit_code == 2


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["eval", "cost", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["eval", "cost", str(p)])
    assert result.exit_code == 2
