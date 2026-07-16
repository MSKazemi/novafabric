"""Tests for W3C PROV-N text export — library + CLI (ADR-0176)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.compliance.export.prov_n import export_prov_n, prov_json_to_prov_n


class TestExportProvNLibrary:
    def test_empty_lineage_is_valid_empty_document(self, minimal_capsule_dir: Path) -> None:
        text = export_prov_n(minimal_capsule_dir)
        assert text.startswith("document\n")
        assert text.rstrip().endswith("endDocument")
        # prefixes are always declared
        assert "prefix prov <http://www.w3.org/ns/prov#>" in text
        assert "prefix nf <https://novafabric.io/terms/>" in text

    def test_wasderivedfrom_edge_renders(self, minimal_capsule_dir: Path) -> None:
        (minimal_capsule_dir / "lineage.jsonl").write_text(
            json.dumps({
                "source": "capsule-A",
                "target": "capsule-B",
                "run_id": "run-001",
                "edge_type": "derived",
            }) + "\n",
            encoding="utf-8",
        )
        text = export_prov_n(minimal_capsule_dir)
        assert "entity(nf:capsule-A" in text
        assert "entity(nf:capsule-B" in text
        assert "activity(nf:run-001" in text
        # wasDerivedFrom(id; generatedEntity, usedEntity)
        assert "wasDerivedFrom(" in text
        assert "nf:capsule-B, nf:capsule-A)" in text  # target(gen), source(used)

    def test_generated_and_used_relations_render(self, minimal_capsule_dir: Path) -> None:
        (minimal_capsule_dir / "lineage.jsonl").write_text(
            json.dumps({
                "source": "act-1", "target": "ent-1",
                "run_id": "act-1", "edge_type": "generated",
            }) + "\n",
            encoding="utf-8",
        )
        text = export_prov_n(minimal_capsule_dir)
        # wasGeneratedBy(id; entity, activity)
        assert "wasGeneratedBy(" in text
        assert "nf:ent-1, nf:act-1)" in text

    def test_missing_capsule_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            export_prov_n(tmp_path / "nonexistent")

    def test_prov_type_is_single_quoted_qualified_name(self) -> None:
        # entities carry prov:type="nf:Capsule" as a QName → single-quoted in PROV-N
        doc = {
            "prefix": {"prov": "http://www.w3.org/ns/prov#", "nf": "https://novafabric.io/terms/"},
            "entity": {"nf:x": {"prov:type": "nf:Capsule"}},
            "activity": {}, "wasGeneratedBy": {}, "used": {}, "wasDerivedFrom": {},
        }
        text = prov_json_to_prov_n(doc)
        assert "entity(nf:x, [prov:type='nf:Capsule'])" in text

    def test_activity_start_time_emitted_only_when_datetime(self) -> None:
        doc = {
            "prefix": {"nf": "https://novafabric.io/terms/"},
            "entity": {},
            "activity": {
                "nf:good": {"prov:type": "nf:Run", "prov:startTime": "2026-06-11T01:04:43"},
                "nf:bad": {"prov:type": "nf:Run", "prov:startTime": "not-a-date"},
            },
            "wasGeneratedBy": {}, "used": {}, "wasDerivedFrom": {},
        }
        text = prov_json_to_prov_n(doc)
        assert "activity(nf:good, 2026-06-11T01:04:43, -, [prov:type='nf:Run'])" in text
        # invalid datetime falls back to '-'
        assert "activity(nf:bad, -, -, [prov:type='nf:Run'])" in text


class TestExportProvNCli:
    def test_cli_prov_n_writes_file(self, minimal_capsule_dir: Path, tmp_path: Path) -> None:
        (minimal_capsule_dir / "lineage.jsonl").write_text(
            json.dumps({"source": "a", "target": "b", "run_id": "r", "edge_type": "derived"}) + "\n",
            encoding="utf-8",
        )
        out = tmp_path / "out.provn"
        result = CliRunner().invoke(
            app, ["lineage", "export-prov", str(minimal_capsule_dir),
                  "--format", "prov-n", "-o", str(out)],
        )
        assert result.exit_code == 0, result.output
        assert out.read_text(encoding="utf-8").startswith("document\n")
        assert "PROV-N" in result.output

    def test_cli_default_is_prov_json(self, minimal_capsule_dir: Path, tmp_path: Path) -> None:
        out = tmp_path / "out.json"
        result = CliRunner().invoke(
            app, ["lineage", "export-prov", str(minimal_capsule_dir), "-o", str(out)],
        )
        assert result.exit_code == 0, result.output
        json.loads(out.read_text(encoding="utf-8"))  # valid JSON
        assert "PROV-JSON" in result.output

    def test_cli_rejects_unknown_format(self, minimal_capsule_dir: Path) -> None:
        result = CliRunner().invoke(
            app, ["lineage", "export-prov", str(minimal_capsule_dir), "--format", "turtle"],
        )
        assert result.exit_code == 2
