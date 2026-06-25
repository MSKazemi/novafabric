"""Tests for W3C PROV-JSON export — library + CLI."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.compliance.export.prov_json import export_prov_json


class TestExportProvJsonLibrary:
    def test_returns_dict_with_prov_keys(self, minimal_capsule_dir: Path) -> None:
        doc = export_prov_json(minimal_capsule_dir)
        assert isinstance(doc, dict)
        for key in ("prefix", "entity", "activity", "wasGeneratedBy", "used", "wasDerivedFrom"):
            assert key in doc, f"missing key: {key}"

    def test_namespace_prefixes_present(self, minimal_capsule_dir: Path) -> None:
        doc = export_prov_json(minimal_capsule_dir)
        assert "prov" in doc["prefix"]
        assert "nf" in doc["prefix"]

    def test_empty_lineage_returns_skeleton(self, minimal_capsule_dir: Path) -> None:
        # minimal_capsule_dir has no lineage.jsonl — skeleton returned
        doc = export_prov_json(minimal_capsule_dir)
        assert doc["entity"] == {}
        assert doc["activity"] == {}

    def test_with_lineage_entries(self, minimal_capsule_dir: Path) -> None:
        lineage_path = minimal_capsule_dir / "lineage.jsonl"
        lineage_path.write_text(
            json.dumps({
                "source": "capsule-A",
                "target": "capsule-B",
                "run_id": "run-001",
                "edge_type": "derived",
            }) + "\n",
            encoding="utf-8",
        )
        doc = export_prov_json(minimal_capsule_dir)
        assert len(doc["entity"]) >= 2  # source + target entities
        assert len(doc["wasDerivedFrom"]) >= 1

    def test_structured_lineage_edges_do_not_crash(self, minimal_capsule_dir: Path) -> None:
        # Regression: lineage schema v0.1.0 uses structured dict source/target
        # ({"kind": "run", "run_id": ...}), which previously crashed _sanitize_id
        # (expected str, got dict). Found by the F10 conformance experiment.
        lineage_path = minimal_capsule_dir / "lineage.jsonl"
        lineage_path.write_text(
            json.dumps({
                "edge_type": "consumed",
                "source": {"kind": "run", "run_id": "01ABCDEF"},
                "target": {"kind": "asset", "asset_ref": "ns/thing@v1.0.0",
                           "registry": "local"},
                "capsule_run_id": "01ABCDEF",
                "created_at": "2026-06-11T01:04:43Z",
            }) + "\n",
            encoding="utf-8",
        )
        doc = export_prov_json(minimal_capsule_dir)
        assert len(doc["entity"]) >= 2          # run + asset registered as entities
        assert len(doc["activity"]) >= 1        # capsule_run_id → activity
        assert any(doc[k] for k in ("used", "wasGeneratedBy", "wasDerivedFrom"))

    def test_missing_capsule_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            export_prov_json(tmp_path / "nonexistent")

    def test_malformed_lineage_lines_skipped(self, minimal_capsule_dir: Path) -> None:
        lineage_path = minimal_capsule_dir / "lineage.jsonl"
        lineage_path.write_text("not-json\n{}\n", encoding="utf-8")
        # Should not raise — malformed lines are skipped
        doc = export_prov_json(minimal_capsule_dir)
        assert isinstance(doc, dict)


class TestExportProvJsonCli:
    def test_default_output(self, minimal_capsule_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["lineage", "export-prov", str(minimal_capsule_dir)])
        assert result.exit_code == 0, result.output
        out = minimal_capsule_dir / "prov.json"
        assert out.exists()
        doc = json.loads(out.read_text())
        assert "prefix" in doc

    def test_explicit_output(self, minimal_capsule_dir: Path, tmp_path: Path) -> None:
        out = tmp_path / "exported.prov.json"
        runner = CliRunner()
        result = runner.invoke(
            app, ["lineage", "export-prov", str(minimal_capsule_dir), "--output", str(out)]
        )
        assert result.exit_code == 0, result.output
        assert out.exists()

    def test_missing_capsule_exits_1(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["lineage", "export-prov", str(tmp_path / "ghost")])
        assert result.exit_code == 1
