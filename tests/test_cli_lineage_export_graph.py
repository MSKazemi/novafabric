"""ADR-0214 — `nova lineage export-graph` CLI smoke tests."""
from __future__ import annotations

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
            source={"kind": "run", "run_id": "run-x"},
            target={"kind": "asset", "registry": "local", "asset_ref": "m@v1"},
            confidence="observed",
            capsule_run_id="cap-x",
        )
    )
    return db


def test_help_smoke():
    result = runner.invoke(app, ["lineage", "export-graph", "--help"])
    assert result.exit_code == 0
    assert "graphml" in result.output.lower()


def test_graphml_stdout(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(_seed_db(tmp_path)))
    result = runner.invoke(app, ["lineage", "export-graph"])
    assert result.exit_code == 0, result.output
    assert "<graphml" in result.output


def test_cypher_to_file(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(_seed_db(tmp_path)))
    out = tmp_path / "export" / "lineage.cypher"
    result = runner.invoke(
        app, ["lineage", "export-graph", "--format", "cypher", "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    text = out.read_text()
    assert "MERGE (n:Lineage" in text and "[r:CONSUMED" in text


def test_unknown_ref_exits_one(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(_seed_db(tmp_path)))
    result = runner.invoke(app, ["lineage", "export-graph", "--ref", "nope"])
    assert result.exit_code == 1
    assert "Unknown ref" in result.output


def test_unknown_format_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(_seed_db(tmp_path)))
    result = runner.invoke(app, ["lineage", "export-graph", "--format", "dot"])
    assert result.exit_code != 0
