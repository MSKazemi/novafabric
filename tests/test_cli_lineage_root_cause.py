"""ADR-0213 — `nova lineage root-cause` CLI smoke tests."""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.lineage._store import LineageStore
from novafabric.lineage._types import LineageEdge

runner = CliRunner()


def _seed_db(tmp_path: Path) -> Path:
    db = tmp_path / "registry.db"
    store = LineageStore(db_path=db)
    store.insert_edge(
        LineageEdge(
            edge_type="consumed",
            source={"kind": "run", "run_id": "run-victim", "status": "failed"},
            target={
                "kind": "run", "run_id": "run-bad",
                "status": "failed", "error": "tool call timeout",
            },
            confidence="observed",
            capsule_run_id="cap-rc",
        )
    )
    return db


def test_help_smoke():
    result = runner.invoke(app, ["lineage", "root-cause", "--help"])
    assert result.exit_code == 0
    assert "root-cause" in result.output.lower() or "root cause" in result.output.lower()


def test_text_output_names_culprit(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(_seed_db(tmp_path)))
    result = runner.invoke(app, ["lineage", "root-cause", "run-victim"])
    assert result.exit_code == 0, result.output
    assert "run-bad" in result.output


def test_json_output_parses(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(_seed_db(tmp_path)))
    result = runner.invoke(
        app, ["lineage", "root-cause", "run-victim", "--output", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["responsible"]["ref"] == "run-bad"
    assert payload["taxonomy"] == "SYSTEM"


def test_unknown_run_exits_one(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(_seed_db(tmp_path)))
    result = runner.invoke(app, ["lineage", "root-cause", "nope"])
    assert result.exit_code == 1
    assert "Unknown run" in result.output
