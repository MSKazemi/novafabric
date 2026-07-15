# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Tests for AI-SBOM / CycloneDX ML-BOM exporter (cap-008)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app


@pytest.fixture()
def capsule_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cap-aibom-001"
    d.mkdir()
    manifest = {
        "schema_version": "1.0.0",
        "run_id": "run-aibom-001",
        "model": "gpt-4o",
        "model_version": "2024-08-06",
        "provider": "openai",
        "tools": ["web_search", "code_interpreter"],
        "input_tokens": 1500,
        "output_tokens": 800,
    }
    with open(d / "capsule.yaml", "w") as f:
        yaml.dump(manifest, f)
    return d


@pytest.fixture()
def capsule_dir_with_eval(capsule_dir: Path) -> Path:
    eval_data = {
        "suite_id": "smoke-v1",
        "metrics": [
            {"name": "accuracy", "value": 0.92},
            {"name": "latency_p99_ms", "value": 450.0},
        ],
    }
    (capsule_dir / "eval_result.json").write_text(json.dumps(eval_data))
    return capsule_dir


class TestAIBOMExporter:
    def test_build_aibom_minimal(self, capsule_dir: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        doc = AIBOMExporter().build_aibom(capsule_dir)
        assert doc.bom_format == "CycloneDX"
        assert doc.spec_version == "1.7"
        assert doc.run_id == "run-aibom-001"
        assert len(doc.components) >= 1  # at least the model component

    def test_model_component_name(self, capsule_dir: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        doc = AIBOMExporter().build_aibom(capsule_dir)
        model_comp = next(c for c in doc.components if c.type == "machine-learning-model")
        assert model_comp.name == "gpt-4o"
        assert model_comp.version == "2024-08-06"

    def test_tool_components_extracted(self, capsule_dir: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        doc = AIBOMExporter().build_aibom(capsule_dir)
        tool_comps = [c for c in doc.components if c.type == "library"]
        assert len(tool_comps) == 2
        tool_names = {c.name for c in tool_comps}
        assert "web_search" in tool_names
        assert "code_interpreter" in tool_names

    def test_model_card_from_eval(self, capsule_dir_with_eval: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        doc = AIBOMExporter().build_aibom(capsule_dir_with_eval)
        model_comp = next(c for c in doc.components if c.type == "machine-learning-model")
        assert model_comp.model_card is not None
        assert "quantitativeAnalysis" in model_comp.model_card
        perf = model_comp.model_card["quantitativeAnalysis"]["performanceMetrics"]
        assert len(perf) == 2

    def test_export_json_valid_cyclonedx(
        self, capsule_dir: Path, tmp_path: Path
    ) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        exporter = AIBOMExporter()
        doc = exporter.build_aibom(capsule_dir)
        out = tmp_path / "aibom.json"
        exporter.export_json(doc, out)

        assert out.exists()
        data = json.loads(out.read_text())
        assert data["bomFormat"] == "CycloneDX"
        assert data["specVersion"] == "1.7"
        assert "serialNumber" in data
        assert len(data["components"]) >= 1

    def test_serial_number_is_urn_uuid(self, capsule_dir: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        doc = AIBOMExporter().build_aibom(capsule_dir)
        assert doc.serial_number.startswith("urn:uuid:")

    def test_bom_ref_unique_per_component(self, capsule_dir: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        doc = AIBOMExporter().build_aibom(capsule_dir)
        refs = [c.bom_ref for c in doc.components]
        assert len(refs) == len(set(refs))

    def test_empty_capsule_dir_returns_unknown_model(self, tmp_path: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        empty = tmp_path / "empty"
        empty.mkdir()
        doc = AIBOMExporter().build_aibom(empty)
        model = next(c for c in doc.components if c.type == "machine-learning-model")
        assert model.name == "unknown-model"

    def test_provider_in_properties(self, capsule_dir: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        doc = AIBOMExporter().build_aibom(capsule_dir)
        model = next(c for c in doc.components if c.type == "machine-learning-model")
        prop_names = {p["name"] for p in model.properties}
        assert "nova:provider" in prop_names


class TestExportAibomCli:
    def test_default_output(self, capsule_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["export-aibom", str(capsule_dir)])
        assert result.exit_code == 0, result.output
        out = capsule_dir / "aibom.json"
        assert out.exists()
        data = json.loads(out.read_text())
        assert data["bomFormat"] == "CycloneDX"

    def test_explicit_output(self, capsule_dir: Path, tmp_path: Path) -> None:
        out = tmp_path / "my-aibom.json"
        runner = CliRunner()
        result = runner.invoke(app, ["export-aibom", str(capsule_dir), "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert out.exists()
        data = json.loads(out.read_text())
        assert data["specVersion"] == "1.7"

    def test_output_contains_component_count(self, capsule_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["export-aibom", str(capsule_dir)])
        assert result.exit_code == 0, result.output
        assert "components" in result.output

    def test_cyclonedx_format_in_output(self, capsule_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["export-aibom", str(capsule_dir)])
        assert result.exit_code == 0, result.output
        out = capsule_dir / "aibom.json"
        data = json.loads(out.read_text())
        assert "serialNumber" in data
        assert data["serialNumber"].startswith("urn:uuid:")


class TestAIBOMExporterV17:
    """CycloneDX 1.7 (ECMA-424 2nd Edition) upgrade tests."""

    def test_spec_version_is_1_7(self, capsule_dir: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        doc = AIBOMExporter().build_aibom(capsule_dir)
        assert doc.spec_version == "1.7"

    def test_export_json_spec_version_1_7(self, capsule_dir: Path, tmp_path: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        exporter = AIBOMExporter()
        out = tmp_path / "aibom17.json"
        exporter.export_json(exporter.build_aibom(capsule_dir), out)
        data = json.loads(out.read_text())
        assert data["specVersion"] == "1.7"

    def test_metadata_tools_uses_components_object(self, capsule_dir: Path, tmp_path: Path) -> None:
        """CycloneDX 1.5+ requires metadata.tools as {components:[...]} not an array."""
        from novafabric.compliance.export.aibom import AIBOMExporter

        exporter = AIBOMExporter()
        out = tmp_path / "aibom17.json"
        exporter.export_json(exporter.build_aibom(capsule_dir), out)
        data = json.loads(out.read_text())
        tools = data["metadata"]["tools"]
        assert isinstance(tools, dict), "metadata.tools must be an object in CycloneDX 1.5+"
        assert "components" in tools
        assert isinstance(tools["components"], list)
        assert len(tools["components"]) >= 1

    def test_metadata_tools_component_has_type_field(self, capsule_dir: Path, tmp_path: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        exporter = AIBOMExporter()
        out = tmp_path / "aibom17.json"
        exporter.export_json(exporter.build_aibom(capsule_dir), out)
        data = json.loads(out.read_text())
        tool_comp = data["metadata"]["tools"]["components"][0]
        assert tool_comp.get("type") == "application"
        assert tool_comp.get("name") == "novafabric"

    def test_metadata_lifecycle_phase_present(self, capsule_dir: Path, tmp_path: Path) -> None:
        """CycloneDX 1.7 adds metadata.lifecycles to document the BOM phase."""
        from novafabric.compliance.export.aibom import AIBOMExporter

        exporter = AIBOMExporter()
        out = tmp_path / "aibom17.json"
        exporter.export_json(exporter.build_aibom(capsule_dir), out)
        data = json.loads(out.read_text())
        assert "lifecycles" in data["metadata"]
        lifecycles = data["metadata"]["lifecycles"]
        assert isinstance(lifecycles, list)
        phases = [lc.get("phase") for lc in lifecycles]
        assert "post-build" in phases  # default for completed runs

    def test_model_card_limitations_from_capsule(self, tmp_path: Path) -> None:
        """CycloneDX 1.7 modelCard.limitations documents known restrictions."""
        d = tmp_path / "cap-limits"
        d.mkdir()
        manifest = {
            "run_id": "run-limits",
            "model": "llama-3-8b",
            "limitations": [
                "Not safe for medical diagnosis",
                "English only; quality degrades on other languages",
            ],
        }
        import yaml
        with open(d / "capsule.yaml", "w") as f:
            yaml.dump(manifest, f)

        from novafabric.compliance.export.aibom import AIBOMExporter

        exporter = AIBOMExporter()
        out = tmp_path / "aibom17.json"
        exporter.export_json(exporter.build_aibom(d), out)
        data = json.loads(out.read_text())
        model_comp = next(c for c in data["components"] if c["type"] == "machine-learning-model")
        assert "modelCard" in model_comp
        assert "limitations" in model_comp["modelCard"]
        lims = model_comp["modelCard"]["limitations"]
        assert len(lims) == 2
        assert any("medical" in lim for lim in lims)

    def test_dataset_components_from_lineage_edges(self, tmp_path: Path) -> None:
        """CycloneDX 1.7 dataset components — populated from capsule lineage_datasets."""
        d = tmp_path / "cap-datasets"
        d.mkdir()
        manifest = {
            "run_id": "run-ds",
            "model": "llama-3",
            "lineage_datasets": [
                {"name": "openhermes-2.5", "version": "1.0", "type": "training"},
                {"name": "alpaca-eval", "version": "2.0", "type": "evaluation"},
            ],
        }
        import yaml
        with open(d / "capsule.yaml", "w") as f:
            yaml.dump(manifest, f)

        from novafabric.compliance.export.aibom import AIBOMExporter

        exporter = AIBOMExporter()
        out = tmp_path / "aibom17.json"
        exporter.export_json(exporter.build_aibom(d), out)
        data = json.loads(out.read_text())
        data_comps = [c for c in data["components"] if c["type"] == "data"]
        assert len(data_comps) == 2
        ds_names = {c["name"] for c in data_comps}
        assert "openhermes-2.5" in ds_names
        assert "alpaca-eval" in ds_names

    def test_tool_version_matches_package(self, capsule_dir: Path, tmp_path: Path) -> None:
        """metadata.tools[0].version must reflect the installed novafabric version."""
        from importlib.metadata import version as _ver

        from novafabric.compliance.export.aibom import AIBOMExporter

        exporter = AIBOMExporter()
        out = tmp_path / "aibom17.json"
        exporter.export_json(exporter.build_aibom(capsule_dir), out)
        data = json.loads(out.read_text())
        tool_comp = data["metadata"]["tools"]["components"][0]
        expected = _ver("novafabric")
        assert tool_comp["version"] == expected

    def test_backwards_compat_no_limitations_field(self, capsule_dir: Path) -> None:
        """Capsule without limitations → modelCard is None or has no limitations key."""
        from novafabric.compliance.export.aibom import AIBOMExporter

        doc = AIBOMExporter().build_aibom(capsule_dir)
        model = next(c for c in doc.components if c.type == "machine-learning-model")
        # capsule_dir fixture has no limitations — modelCard is None or lacks the key
        if model.model_card is not None:
            assert "limitations" not in model.model_card


class TestAibomStatusCli:
    def test_status_shows_deadline(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["aibom", "status"])
        assert result.exit_code == 0, result.output
        assert "2026-09-11" in result.output

    def test_status_shows_regulation(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["aibom", "status"])
        assert result.exit_code == 0, result.output
        assert "CRA" in result.output or "Cyber Resilience" in result.output

    def test_status_shows_spec_version(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["aibom", "status"])
        assert result.exit_code == 0, result.output
        assert "CycloneDX" in result.output

    def test_status_with_capsules_dir(self, tmp_path: Path, capsule_dir: Path) -> None:
        capsules_root = capsule_dir.parent
        runner = CliRunner()
        result = runner.invoke(app, ["aibom", "status", "--capsules-dir", str(capsules_root)])
        assert result.exit_code == 0, result.output
        assert "0/" in result.output or "1/" in result.output or "no capsules" in result.output

    def test_status_full_coverage_shown(self, tmp_path: Path) -> None:
        capsules_root = tmp_path / "capsules"
        capsules_root.mkdir()
        cap = capsules_root / "run-001"
        cap.mkdir()
        (cap / "capsule.yaml").write_text("run_id: run-001\n")
        (cap / "aibom.json").write_text('{"bomFormat":"CycloneDX"}')
        runner = CliRunner()
        result = runner.invoke(app, ["aibom", "status", "--capsules-dir", str(capsules_root)])
        assert result.exit_code == 0, result.output
        assert "complete" in result.output

    def test_status_partial_coverage_shown(self, tmp_path: Path) -> None:
        capsules_root = tmp_path / "capsules"
        capsules_root.mkdir()
        for i in range(3):
            cap = capsules_root / f"run-{i:03d}"
            cap.mkdir()
            (cap / "capsule.yaml").write_text(f"run_id: run-{i:03d}\n")
        (capsules_root / "run-000" / "aibom.json").write_text('{"bomFormat":"CycloneDX"}')
        runner = CliRunner()
        result = runner.invoke(app, ["aibom", "status", "--capsules-dir", str(capsules_root)])
        assert result.exit_code == 0, result.output
        assert "missing" in result.output


class TestAIBOMGenerate:
    """nova aibom generate — per-deployment automation (CRA)."""

    def _make_capsule(self, root: Path, name: str) -> Path:
        cap = root / name
        cap.mkdir(parents=True)
        manifest = {
            "schema_version": "1.0.0",
            "run_id": name,
            "model": "gpt-4o",
            "provider": "openai",
        }
        (cap / "capsule.yaml").write_text(yaml.dump(manifest))
        return cap

    def test_generate_single_capsule(self, tmp_path: Path) -> None:
        cap = self._make_capsule(tmp_path, "cap-gen-001")
        runner = CliRunner()
        result = runner.invoke(app, ["aibom", "generate", str(cap)])
        assert result.exit_code == 0, result.output
        aibom = cap / "aibom.json"
        assert aibom.exists()
        doc = json.loads(aibom.read_text())
        assert doc["specVersion"] == "1.7"

    def test_generate_skips_existing_without_force(self, tmp_path: Path) -> None:
        cap = self._make_capsule(tmp_path, "cap-gen-002")
        (cap / "aibom.json").write_text('{"stale": true}')
        runner = CliRunner()
        result = runner.invoke(app, ["aibom", "generate", str(cap)])
        assert result.exit_code == 0, result.output
        assert json.loads((cap / "aibom.json").read_text()) == {"stale": True}
        assert "already exists" in result.output

    def test_generate_force_overwrites(self, tmp_path: Path) -> None:
        cap = self._make_capsule(tmp_path, "cap-gen-003")
        (cap / "aibom.json").write_text('{"stale": true}')
        runner = CliRunner()
        result = runner.invoke(app, ["aibom", "generate", str(cap), "--force"])
        assert result.exit_code == 0, result.output
        doc = json.loads((cap / "aibom.json").read_text())
        assert doc.get("specVersion") == "1.7"

    def test_generate_all_batch(self, tmp_path: Path) -> None:
        root = tmp_path / "capsules"
        for i in range(3):
            self._make_capsule(root, f"run-{i:03d}")
        # one already covered
        (root / "run-000" / "aibom.json").write_text('{"specVersion":"1.7"}')
        runner = CliRunner()
        result = runner.invoke(app, ["aibom", "generate", "--all", "--capsules-dir", str(root)])
        assert result.exit_code == 0, result.output
        assert "2 written" in result.output
        assert "1 skipped" in result.output
        for i in range(3):
            assert (root / f"run-{i:03d}" / "aibom.json").exists()

    def test_generate_all_force_refreshes_all(self, tmp_path: Path) -> None:
        root = tmp_path / "capsules"
        for i in range(2):
            self._make_capsule(root, f"run-{i:03d}")
        (root / "run-000" / "aibom.json").write_text('{"stale":true}')
        runner = CliRunner()
        result = runner.invoke(
            app, ["aibom", "generate", "--all", "--capsules-dir", str(root), "--force"]
        )
        assert result.exit_code == 0, result.output
        assert "2 written" in result.output

    def test_generate_requires_arg_or_all(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["aibom", "generate"])
        assert result.exit_code == 2
        assert "--all" in result.output

    def test_generate_invalid_capsule(self, tmp_path: Path) -> None:
        bad = tmp_path / "not-a-capsule"
        bad.mkdir()
        runner = CliRunner()
        result = runner.invoke(app, ["aibom", "generate", str(bad)])
        assert result.exit_code == 1
        assert "Not a valid capsule" in result.output


class TestAIBOM056Extensions:
    """NF-056 CycloneDX 1.7 extensions: citations, TLP, model-card, validate."""

    def _cap(self, tmp_path: Path, extra: dict | None = None) -> Path:
        d = tmp_path / "cap-056"
        d.mkdir()
        manifest = {
            "schema_version": "1.0.0",
            "run_id": "run-056",
            "model": "summarizer",
            "model_version": "1.0.0",
            "provider": "local",
            "lineage_datasets": [
                {"name": "corpus", "version": "3", "type": "training", "hash": "sha256:aa11"}
            ],
        }
        if extra:
            manifest.update(extra)
        with open(d / "capsule.yaml", "w") as f:
            yaml.dump(manifest, f)
        return d

    def test_default_output_is_byte_stable(self, tmp_path: Path) -> None:
        """With no NF-056 flags, no citations/tlp/hashes/externalRefs are emitted."""
        from novafabric.compliance.export.aibom import AIBOMExporter

        exp = AIBOMExporter()
        payload = exp.to_payload(exp.build_aibom(self._cap(tmp_path)))
        for comp in payload["components"]:
            assert "citations" not in comp
            assert "hashes" not in comp
            assert "externalReferences" not in comp
        assert "properties" not in payload["metadata"]  # no TLP

    def test_schema_field_present(self, tmp_path: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        exp = AIBOMExporter()
        payload = exp.to_payload(exp.build_aibom(self._cap(tmp_path)))
        assert payload["$schema"] == "https://cyclonedx.org/schema/bom-1.7.schema.json"

    def test_tlp_marker_recorded(self, tmp_path: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        exp = AIBOMExporter()
        payload = exp.to_payload(exp.build_aibom(self._cap(tmp_path), tlp="TLP:AMBER"))
        props = payload["metadata"]["properties"]
        assert {"name": "novafabric:tlp", "value": "TLP:AMBER"} in props

    def test_invalid_tlp_raises(self, tmp_path: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        with pytest.raises(ValueError, match="invalid TLP"):
            AIBOMExporter().build_aibom(self._cap(tmp_path), tlp="TLP:PURPLE")

    def test_model_card_auto_ref(self, tmp_path: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        doc = AIBOMExporter().build_aibom(self._cap(tmp_path), model_card_ref="auto")
        model = next(c for c in doc.components if c.type == "machine-learning-model")
        assert model.external_references == [
            {"type": "model-card", "url": "registry://models/summarizer/1.0.0/card.md"}
        ]

    def test_model_card_explicit_path(self, tmp_path: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        doc = AIBOMExporter().build_aibom(
            self._cap(tmp_path), model_card_ref="./cards/summarizer.md"
        )
        model = next(c for c in doc.components if c.type == "machine-learning-model")
        assert model.external_references[0]["url"] == "./cards/summarizer.md"

    def test_citations_bind_capsule_and_dataset(self, tmp_path: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        doc = AIBOMExporter().build_aibom(self._cap(tmp_path), citations=True)
        model = next(c for c in doc.components if c.type == "machine-learning-model")
        assert model.citations and model.citations[0]["content"].startswith("sha256:")
        assert model.citations[0]["location"].startswith("capsule://run-056/")
        ds = next(c for c in doc.components if c.type == "data")
        assert ds.citations and ds.citations[0]["content"] == "sha256:aa11"
        assert ds.hashes == [{"alg": "SHA-256", "content": "aa11"}]

    def test_citations_include_inclusion_proof(self, tmp_path: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        cap = self._cap(
            tmp_path,
            {"inclusion_proof": {"log": "nf-tlog", "tree_size": 42, "proof": "rekor://x"}},
        )
        doc = AIBOMExporter().build_aibom(cap, citations=True)
        model = next(c for c in doc.components if c.type == "machine-learning-model")
        ev = model.citations[0]["evidence"]
        assert ev == {"log": "nf-tlog", "treeSize": 42, "proof": "rekor://x"}

    def test_model_hashes_from_manifest(self, tmp_path: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        cap = self._cap(tmp_path, {"model_digest": "sha256:deadbeef"})
        doc = AIBOMExporter().build_aibom(cap, citations=True)
        model = next(c for c in doc.components if c.type == "machine-learning-model")
        assert model.hashes == [{"alg": "SHA-256", "content": "deadbeef"}]

    def test_no_include_datasets_suppresses_data_components(self, tmp_path: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        doc = AIBOMExporter().build_aibom(self._cap(tmp_path), include_datasets=False)
        assert not any(c.type == "data" for c in doc.components)

    def test_validate_passes_on_generated_bom(self, tmp_path: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        exp = AIBOMExporter()
        payload = exp.to_payload(
            exp.build_aibom(
                self._cap(tmp_path), citations=True, tlp="TLP:GREEN", model_card_ref="auto"
            )
        )
        assert exp.validate(payload) == []

    def test_validate_catches_bad_spec_version_and_tlp(self) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        errors = AIBOMExporter.validate(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "serialNumber": "not-a-urn",
                "metadata": {"properties": [{"name": "novafabric:tlp", "value": "TLP:X"}]},
                "components": [{"type": "machine-learning-model"}],
            }
        )
        joined = " ".join(errors)
        assert "specVersion" in joined
        assert "serialNumber" in joined
        assert "tlp" in joined
        assert "missing name" in joined

    def test_cli_generate_with_all_flags(self, tmp_path: Path) -> None:
        cap = self._cap(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["aibom", "generate", str(cap), "--citations", "--tlp", "TLP:AMBER",
             "--model-card", "auto", "--force"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads((cap / "aibom.json").read_text())
        assert payload["metadata"]["properties"][0]["value"] == "TLP:AMBER"
        model = next(c for c in payload["components"] if c["type"] == "machine-learning-model")
        assert "citations" in model and "externalReferences" in model

    def test_cli_generate_rejects_bad_tlp(self, tmp_path: Path) -> None:
        cap = self._cap(tmp_path)
        runner = CliRunner()
        result = runner.invoke(app, ["aibom", "generate", str(cap), "--tlp", "TLP:NOPE"])
        assert result.exit_code == 2
        assert "Invalid --tlp" in result.output

    def test_cli_validate_ok_and_fail(self, tmp_path: Path) -> None:
        cap = self._cap(tmp_path)
        runner = CliRunner()
        gen = runner.invoke(app, ["aibom", "generate", str(cap), "--force"])
        assert gen.exit_code == 0, gen.output
        bom = cap / "aibom.json"
        ok = runner.invoke(app, ["aibom", "validate", str(bom)])
        assert ok.exit_code == 0, ok.output
        assert "Valid CycloneDX 1.7" in ok.output

        bad = tmp_path / "bad.json"
        bad.write_text('{"bomFormat": "CycloneDX", "specVersion": "1.4", "components": []}')
        res = runner.invoke(app, ["aibom", "validate", str(bad)])
        assert res.exit_code == 1
        assert "validation error" in res.output

    def test_cli_validate_json_output(self, tmp_path: Path) -> None:
        cap = self._cap(tmp_path)
        runner = CliRunner()
        runner.invoke(app, ["aibom", "generate", str(cap), "--force"])
        res = runner.invoke(app, ["aibom", "validate", str(cap / "aibom.json"), "--json"])
        assert res.exit_code == 0, res.output
        assert '"valid": true' in res.output

    def test_cli_validate_missing_file(self, tmp_path: Path) -> None:
        runner = CliRunner()
        res = runner.invoke(app, ["aibom", "validate", str(tmp_path / "nope.json")])
        assert res.exit_code == 2


class TestAIBOM056ValidateBranches:
    """Cover NF-056 validate() and citation edge branches."""

    def test_validate_components_not_list(self) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        errors = AIBOMExporter.validate(
            {"bomFormat": "CycloneDX", "specVersion": "1.7",
             "serialNumber": "urn:uuid:x", "components": "nope"}
        )
        assert any("components must be a list" in e for e in errors)

    def test_validate_unsupported_hash_alg(self) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        errors = AIBOMExporter.validate(
            {"bomFormat": "CycloneDX", "specVersion": "1.7", "serialNumber": "urn:uuid:x",
             "components": [{"type": "machine-learning-model", "name": "m", "bom-ref": "r",
                             "hashes": [{"alg": "MD5", "content": "x"}]}]}
        )
        assert any("unsupported hash alg" in e for e in errors)

    def test_validate_citation_missing_content(self) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        errors = AIBOMExporter.validate(
            {"bomFormat": "CycloneDX", "specVersion": "1.7", "serialNumber": "urn:uuid:x",
             "components": [{"type": "data", "name": "d", "bom-ref": "r",
                             "citations": [{"location": "capsule://x"}]}]}
        )
        assert any("citation missing 'content'" in e for e in errors)

    def test_inclusion_proof_alternate_keys(self, tmp_path: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        d = tmp_path / "cap-alt"
        d.mkdir()
        with open(d / "capsule.yaml", "w") as f:
            yaml.dump(
                {"run_id": "r", "model": "m", "model_version": "1",
                 "tlog": {"treeSize": 9, "proof_uri": "rekor://y"}}, f
            )
        doc = AIBOMExporter().build_aibom(d, citations=True)
        model = next(c for c in doc.components if c.type == "machine-learning-model")
        ev = model.citations[0]["evidence"]
        assert ev["treeSize"] == 9
        assert ev["proof"] == "rekor://y"
        assert ev["log"] == "novafabric-tlog"  # default when unset

    def test_dataset_without_hash_still_cited(self, tmp_path: Path) -> None:
        from novafabric.compliance.export.aibom import AIBOMExporter

        d = tmp_path / "cap-nohash"
        d.mkdir()
        with open(d / "capsule.yaml", "w") as f:
            yaml.dump(
                {"run_id": "r", "model": "m", "model_version": "1",
                 "lineage_datasets": [{"name": "ds"}]}, f
            )
        doc = AIBOMExporter().build_aibom(d, citations=True)
        ds = next(c for c in doc.components if c.type == "data")
        # no dataset hash → falls back to the capsule digest, no hashes[]
        assert ds.hashes == []
        assert ds.citations[0]["content"].startswith("sha256:")
