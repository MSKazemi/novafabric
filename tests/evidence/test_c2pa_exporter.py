"""Tests for C2PA manifest exporter — library + CLI (ADR-0074)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.evidence.c2pa_exporter import C2PAManifestExporter, export_c2pa


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    d = tmp_path / "capsules" / "01HTEST000000000000000001"
    d.mkdir(parents=True)
    meta = {
        "schema_version": "0.1.0",
        "run_id": "01HTEST000000000000000001",
        "created_at": "2024-01-01T00:00:00.000000Z",
        "finished_at": "2024-01-01T00:00:10.000000Z",
        "status": "success",
        "command": ["python", "agent.py"],
        "capture_mode": "cli-wrapper",
        "novafabric_version": "0.32.0",
        "model_call_count": 2,
        "tool_call_count": 1,
    }
    (d / "capsule.yaml").write_text(yaml.dump(meta))
    (d / "model-calls.jsonl").write_text(
        json.dumps({"model": "gpt-4o", "provider": "openai", "prompt": "hi"}) + "\n"
    )
    return d


class TestC2PAManifestExporterLibrary:
    def test_returns_manifest_store_structure(self, capsule_dir: Path) -> None:
        exporter = C2PAManifestExporter()
        doc = exporter.build_manifest(capsule_dir)
        assert "manifests" in doc
        assert "active_manifest" in doc
        manifest_id = doc["active_manifest"]
        assert manifest_id in doc["manifests"]

    def test_ai_generated_assertion_present(self, capsule_dir: Path) -> None:
        doc = C2PAManifestExporter().build_manifest(capsule_dir)
        manifest = next(iter(doc["manifests"].values()))
        labels = [a["label"] for a in manifest["assertions"]]
        assert "c2pa.ai.generated" in labels
        ai_gen = next(a for a in manifest["assertions"] if a["label"] == "c2pa.ai.generated")
        assert ai_gen["data"]["value"] is True

    def test_model_identity_captured(self, capsule_dir: Path) -> None:
        doc = C2PAManifestExporter().build_manifest(capsule_dir)
        manifest = next(iter(doc["manifests"].values()))
        run_assertion = next(
            (a for a in manifest["assertions"] if a["label"] == "novafabric.run"), None
        )
        assert run_assertion is not None
        assert run_assertion["data"]["model"] == "gpt-4o"
        assert run_assertion["data"]["provider"] == "openai"

    def test_training_mining_not_included_by_default(self, capsule_dir: Path) -> None:
        doc = C2PAManifestExporter().build_manifest(capsule_dir)
        manifest = next(iter(doc["manifests"].values()))
        labels = [a["label"] for a in manifest["assertions"]]
        assert "c2pa.training-mining" not in labels

    def test_training_mining_opt_in(self, capsule_dir: Path) -> None:
        doc = C2PAManifestExporter(include_training_mining=True).build_manifest(capsule_dir)
        manifest = next(iter(doc["manifests"].values()))
        labels = [a["label"] for a in manifest["assertions"]]
        assert "c2pa.training-mining" in labels
        tm = next(a for a in manifest["assertions"] if a["label"] == "c2pa.training-mining")
        assert tm["data"]["use"] == "notAllowed"

    def test_missing_capsule_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            C2PAManifestExporter().build_manifest(tmp_path / "nonexistent")

    def test_missing_capsule_yaml_raises(self, tmp_path: Path) -> None:
        d = tmp_path / "empty_capsule"
        d.mkdir()
        with pytest.raises(FileNotFoundError, match="capsule.yaml"):
            C2PAManifestExporter().build_manifest(d)

    def test_export_writes_json_file(self, capsule_dir: Path, tmp_path: Path) -> None:
        out = tmp_path / "manifest.json"
        result = export_c2pa(capsule_dir, out)
        assert result == out
        assert out.exists()
        doc = json.loads(out.read_text())
        assert "manifests" in doc

    def test_claim_generator_set(self, capsule_dir: Path) -> None:
        doc = C2PAManifestExporter().build_manifest(capsule_dir)
        manifest = next(iter(doc["manifests"].values()))
        assert "NovaFabric" in manifest["claim_generator"]

    def test_seal_ref_added_when_seal_dir_present(self, capsule_dir: Path) -> None:
        seal_dir = capsule_dir / ".seal"
        seal_dir.mkdir()
        (seal_dir / "merkle.json").write_text("{}")
        doc = C2PAManifestExporter().build_manifest(capsule_dir)
        manifest = next(iter(doc["manifests"].values()))
        labels = [a["label"] for a in manifest["assertions"]]
        assert "c2pa.digital-source-type" in labels


class TestExportC2PACli:
    def test_default_output(self, capsule_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["export-c2pa", str(capsule_dir)])
        assert result.exit_code == 0, result.output
        out = capsule_dir / "c2pa-manifest.json"
        assert out.exists()
        doc = json.loads(out.read_text())
        assert "manifests" in doc

    def test_explicit_output(self, capsule_dir: Path, tmp_path: Path) -> None:
        out = tmp_path / "my.c2pa.json"
        runner = CliRunner()
        result = runner.invoke(app, ["export-c2pa", str(capsule_dir), "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert out.exists()

    def test_training_mining_flag(self, capsule_dir: Path, tmp_path: Path) -> None:
        out = tmp_path / "tm.json"
        runner = CliRunner()
        result = runner.invoke(
            app, ["export-c2pa", str(capsule_dir), "--output", str(out), "--training-mining"]
        )
        assert result.exit_code == 0, result.output
        doc = json.loads(out.read_text())
        manifest = next(iter(doc["manifests"].values()))
        labels = [a["label"] for a in manifest["assertions"]]
        assert "c2pa.training-mining" in labels

    def test_missing_capsule_exits_1(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["export-c2pa", str(tmp_path / "ghost")])
        assert result.exit_code == 1

    def test_tsp_disclaimer_in_output(self, capsule_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["export-c2pa", str(capsule_dir)])
        assert result.exit_code == 0
        assert "TSP" in result.output or "Trust Service" in result.output
