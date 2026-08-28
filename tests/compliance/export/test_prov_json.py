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

    def test_empty_lineage_still_emits_the_run_itself(
        self, minimal_capsule_dir: Path
    ) -> None:
        """A capsule with no lineage still has provenance: its own run.

        This assertion used to read ``doc["activity"] == {}`` — it encoded the
        defect rather than a requirement. ``lineage.jsonl`` holds *cross-capsule*
        edges and is empty for any run that is not downstream of another, which is
        the most common capsule of all. Exporting those as "no provenance" was
        wrong in the one format an external auditor is most likely to consume,
        while ``capsule.yaml`` held the answer the whole time.
        """
        doc = export_prov_json(minimal_capsule_dir)
        aid = "nf:01HTEST000000000000000001"
        assert aid in doc["activity"]
        activity = doc["activity"][aid]
        assert activity["prov:type"] == "nf:Run"
        assert activity["prov:startTime"] == "2024-01-01T00:00:00.000000Z"
        assert activity["prov:endTime"] == "2024-01-01T00:00:10.000000Z"
        assert activity["nf:command"] == "python agent.py"
        assert activity["nf:status"] == "success"

    def test_evidence_digests_become_entities_generated_by_the_run(
        self, minimal_capsule_dir: Path
    ) -> None:
        """Each evidence file is an entity ``wasGeneratedBy`` the run activity."""
        import yaml

        manifest_path = minimal_capsule_dir / "capsule.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["evidence_digests"] = {
            "outputs/stdout.txt": {"sha256": "sha256:aa", "size_bytes": 3},
            "trace.jsonl": {"sha256": "sha256:bb", "size_bytes": 42},
        }
        manifest_path.write_text(yaml.dump(manifest))

        doc = export_prov_json(minimal_capsule_dir)
        assert len(doc["entity"]) == 2
        assert len(doc["wasGeneratedBy"]) == 2

        stdout_eid = "nf:01HTEST000000000000000001_outputs_stdout.txt"
        assert doc["entity"][stdout_eid]["nf:sha256"] == "sha256:aa"
        assert doc["entity"][stdout_eid]["nf:sizeBytes"] == 3
        assert doc["entity"][stdout_eid]["nf:filename"] == "outputs/stdout.txt"

        rel = next(
            r for r in doc["wasGeneratedBy"].values() if r["prov:entity"] == stdout_eid
        )
        assert rel["prov:activity"] == "nf:01HTEST000000000000000001"

    def test_seeded_relation_ids_do_not_collide_with_lineage_ids(
        self, minimal_capsule_dir: Path
    ) -> None:
        """Manifest-seeded and lineage-derived relations share one dict.

        Both number their relations from 1, so a shared naming scheme would have
        one silently overwrite the other. The seeded ids use a separate namespace.
        """
        import yaml

        manifest_path = minimal_capsule_dir / "capsule.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["evidence_digests"] = {"trace.jsonl": {"sha256": "sha256:bb", "size_bytes": 1}}
        manifest_path.write_text(yaml.dump(manifest))
        (minimal_capsule_dir / "lineage.jsonl").write_text(
            json.dumps({
                "source": "capsule-A",
                "target": "capsule-B",
                "run_id": "run-001",
                "edge_type": "generated",
            }) + "\n",
            encoding="utf-8",
        )
        doc = export_prov_json(minimal_capsule_dir)
        # 1 seeded + 1 from the lineage edge; neither clobbered the other.
        assert len(doc["wasGeneratedBy"]) == 2

    def test_no_manifest_returns_skeleton(self, tmp_path: Path) -> None:
        """The documented skeleton behaviour survives when there is nothing to read."""
        capsule_dir = tmp_path / "bare"
        capsule_dir.mkdir()
        doc = export_prov_json(capsule_dir)
        assert doc["entity"] == {}
        assert doc["activity"] == {}
        assert "prov" in doc["prefix"]

    def test_manifest_without_run_id_returns_skeleton(self, tmp_path: Path) -> None:
        capsule_dir = tmp_path / "norun"
        capsule_dir.mkdir()
        (capsule_dir / "capsule.yaml").write_text("schema_version: 0.1.0\n")
        doc = export_prov_json(capsule_dir)
        assert doc["activity"] == {}

    def test_unreadable_manifest_does_not_raise(self, minimal_capsule_dir: Path) -> None:
        """A corrupt manifest degrades to lineage-only, it does not fail the export."""
        (minimal_capsule_dir / "capsule.yaml").write_text("{[not: valid: yaml")
        doc = export_prov_json(minimal_capsule_dir)
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
