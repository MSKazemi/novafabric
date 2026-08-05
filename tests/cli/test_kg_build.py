"""CLI tests for `nova kg build` — SPKG canonical RDF + operational LPG (ADR-0111 P1)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("rdflib")
pytest.importorskip("pyshacl")
pytest.importorskip("kuzu")

from _help_assert import assert_flag_in_help
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


def test_build_populates_and_reports(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    store_path = tmp_path / "spkg.kuzu"
    result = runner.invoke(
        app, ["kg", "build", str(cap), "--path", str(store_path)]
    )
    assert result.exit_code == 0, result.output
    assert "SPKG built" in result.output
    assert "SHACL-valid" in result.output
    assert "LPG edges" in result.output


def test_build_invalid_exits_1(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, created_at="not-a-date")
    store_path = tmp_path / "spkg.kuzu"
    result = runner.invoke(
        app, ["kg", "build", str(cap), "--path", str(store_path)]
    )
    assert result.exit_code == 1
    assert "SHACL" in result.output


def test_build_no_validate(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    store_path = tmp_path / "spkg.kuzu"
    result = runner.invoke(
        app, ["kg", "build", str(cap), "--path", str(store_path), "--no-validate"]
    )
    assert result.exit_code == 0, result.output
    assert "unvalidated" in result.output


def test_build_help() -> None:
    result = runner.invoke(app, ["kg", "build", "--help"])
    assert result.exit_code == 0
    assert_flag_in_help(result, "--validate")
