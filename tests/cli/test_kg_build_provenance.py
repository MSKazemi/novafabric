"""CLI tests for `nova kg build-provenance` (SPKG P1, ADR-0111 — experimental)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("rdflib")
pytest.importorskip("pyshacl")

from typer.testing import CliRunner  # noqa: E402

from novafabric.cli.main import app  # noqa: E402

runner = CliRunner()


def _capsule(tmp_path: Path, created_at: str = "2026-07-02T14:00:00.000000Z") -> Path:
    cap = tmp_path / "run-123"
    cap.mkdir()
    edge = {
        "edge_type": "produces",
        "source": {"kind": "run", "ref": "run-123"},
        "target": {"kind": "artifact", "ref": "artifact:run-123:out.txt"},
        "created_at": created_at,
        "capsule_run_id": "run-123",
    }
    (cap / "lineage.jsonl").write_text(json.dumps(edge) + "\n", encoding="utf-8")
    return cap


def test_build_provenance_validates_and_prints_turtle(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    result = runner.invoke(app, ["kg", "build-provenance", str(cap)])
    assert result.exit_code == 0, result.output
    assert "SHACL-valid" in result.output
    assert "@prefix" in result.output  # turtle serialization


def test_output_file_written(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    out = tmp_path / "prov.ttl"
    result = runner.invoke(
        app, ["kg", "build-provenance", str(cap), "-o", str(out), "--format", "turtle"]
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "prov" in out.read_text()


def test_no_validate_skips_gate(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    result = runner.invoke(app, ["kg", "build-provenance", str(cap), "--no-validate"])
    assert result.exit_code == 0, result.output
    assert "SHACL-valid" not in result.output


def test_invalid_provenance_exits_1(tmp_path: Path) -> None:
    # A malformed created_at becomes an ill-typed xsd:dateTime -> SHACL rejects it (R11).
    cap = _capsule(tmp_path, created_at="not-a-real-date")
    result = runner.invoke(app, ["kg", "build-provenance", str(cap)])
    assert result.exit_code == 1
    assert "failed" in result.output.lower()


def test_help_smoke() -> None:
    result = runner.invoke(app, ["kg", "build-provenance", "--help"])
    assert result.exit_code == 0
    assert "--validate" in result.output
