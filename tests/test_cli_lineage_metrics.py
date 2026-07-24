"""ADR-0212 — `nova lineage metrics` CLI smoke tests."""
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
            source={"kind": "run", "run_id": "run-cli"},
            target={"kind": "asset", "registry": "local", "asset_ref": "m@v1"},
            confidence="observed",
            capsule_run_id="cap-cli",
        )
    )
    return db


def test_help_smoke():
    result = runner.invoke(app, ["lineage", "metrics", "--help"])
    assert result.exit_code == 0
    assert "hubs" in result.output.lower()


def test_table_output(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(_seed_db(tmp_path)))
    result = runner.invoke(app, ["lineage", "metrics"])
    assert result.exit_code == 0, result.output
    assert "m@v1" in result.output


def test_json_output_parses(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(_seed_db(tmp_path)))
    result = runner.invoke(app, ["lineage", "metrics", "--output", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["node_count"] == 2
    assert payload["top_hubs"]


def test_empty_store_exits_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(tmp_path / "fresh.db"))
    result = runner.invoke(app, ["lineage", "metrics"])
    assert result.exit_code == 0, result.output
    assert "empty" in result.output.lower()
