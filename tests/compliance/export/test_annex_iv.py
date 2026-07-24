# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Tests for AnnexIVExporter (cap-002)."""

from __future__ import annotations

from pathlib import Path

import jsonschema
import pytest

from novafabric.compliance.export.annex_iv import (
    AnnexIVExporter,
    _assess_completeness,
    _resolve,
)
from novafabric.compliance.export.models import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_MISSING,
    COMPLETENESS_PARTIAL,
)
from novafabric.compliance.export.provenance import EvidenceSource, validate_marked


class TestAnnexIVEvidenceSource:
    """ADR-0197: every Annex IV element must be honestly provenance-marked."""

    def test_every_element_marked(self, minimal_capsule_dir: Path) -> None:
        doc = AnnexIVExporter().build_annex_iv_document("DEP-1", minimal_capsule_dir)
        for elem in doc.elements:
            assert elem.evidence_source is not None, elem.element_id

    def test_completeness_summary_marked_and_valid(
        self, minimal_capsule_dir: Path
    ) -> None:
        doc = AnnexIVExporter().build_annex_iv_document("DEP-1", minimal_capsule_dir)
        summary = doc.completeness_summary()
        for entry in summary:
            assert entry.evidence_source is not None
        validate_marked(summary)  # must not raise

    def test_operator_declared_is_asserted(self, minimal_capsule_dir: Path) -> None:
        doc = AnnexIVExporter().build_annex_iv_document(
            "DEP-1", minimal_capsule_dir, operator_overrides={1: "operator text"}
        )
        overridden = next(e for e in doc.elements if e.element_id == 1)
        assert overridden.evidence_source is EvidenceSource.operator_asserted

    def test_capsule_verified_carries_ref(self, minimal_capsule_dir: Path) -> None:
        doc = AnnexIVExporter().build_annex_iv_document("DEP-1", minimal_capsule_dir)
        for elem in doc.elements:
            if elem.evidence_source is EvidenceSource.capsule_verified:
                assert elem.evidence_ref is not None
                assert elem.evidence_ref.content_digest.startswith("sha256:")
                break

_SCHEMA_PATH = (
    Path(__file__).parents[3]
    / "src" / "novafabric" / "schemas" / "annex-iv-document.schema.json"
)


class TestResolve:
    def test_simple_key(self) -> None:
        assert _resolve({"a": 1}, "a") == 1

    def test_nested_key(self) -> None:
        assert _resolve({"a": {"b": 2}}, "a.b") == 2

    def test_missing_key(self) -> None:
        assert _resolve({"a": 1}, "b") is None

    def test_non_dict_mid_path(self) -> None:
        assert _resolve({"a": "string_not_dict"}, "a.b") is None

    def test_none_obj_mid_path(self) -> None:
        assert _resolve({"a": None}, "a.b") is None


class TestAssessCompleteness:
    def test_operator_declared_always_missing(self) -> None:
        result = _assess_completeness({}, [], [], "operator_declared")
        assert result == COMPLETENESS_MISSING

    def test_no_required_no_optional_is_complete(self) -> None:
        result = _assess_completeness({}, [], [], "capsule_derived")
        assert result == COMPLETENESS_COMPLETE

    def test_no_required_optional_present_complete(self) -> None:
        result = _assess_completeness({"a": 1}, [], ["a"], "capsule_derived")
        assert result == COMPLETENESS_COMPLETE

    def test_no_required_optional_all_missing_partial(self) -> None:
        result = _assess_completeness({}, [], ["a"], "capsule_derived")
        assert result == COMPLETENESS_PARTIAL

    def test_all_required_present_complete(self) -> None:
        result = _assess_completeness({"a": 1, "b": 2}, ["a", "b"], [], "capsule_derived")
        assert result == COMPLETENESS_COMPLETE

    def test_required_missing_optional_present_partial(self) -> None:
        result = _assess_completeness({"b": 2}, ["a"], ["b"], "capsule_derived")
        assert result == COMPLETENESS_PARTIAL

    def test_required_missing_no_optional_missing(self) -> None:
        result = _assess_completeness({}, ["a"], [], "capsule_derived")
        assert result == COMPLETENESS_MISSING


class TestAnnexIVExporter:
    def test_15_elements_returned(self, minimal_capsule_dir: Path) -> None:
        exporter = AnnexIVExporter()
        doc = exporter.build_annex_iv_document("deploy-001", minimal_capsule_dir)
        assert len(doc.elements) == 15

    def test_element_ids_1_through_15(self, minimal_capsule_dir: Path) -> None:
        exporter = AnnexIVExporter()
        doc = exporter.build_annex_iv_document("deploy-001", minimal_capsule_dir)
        ids = [e.element_id for e in doc.elements]
        assert ids == list(range(1, 16))

    def test_at_least_11_capsule_derived_elements_populated(self, minimal_capsule_dir: Path) -> None:
        """At least 11 of 15 capsule-derived elements must be non-missing.

        4 elements (5, 7, 8, 10) are marked operator_declared and will be
        'missing' until the operator supplies them via overrides. This tests
        the capsule-derived elements only.
        """
        exporter = AnnexIVExporter()
        doc = exporter.build_annex_iv_document("deploy-test", minimal_capsule_dir)
        capsule_derived = [
            e for e in doc.elements
            if e.population_method == "capsule_derived"
        ]
        populated = [
            e for e in capsule_derived
            if e.completeness_flag in (COMPLETENESS_COMPLETE, COMPLETENESS_PARTIAL)
        ]
        assert len(populated) >= 11, (
            f"Expected ≥11 capsule-derived elements populated, got {len(populated)}. "
            f"Missing: {[e.element_id for e in capsule_derived if e.completeness_flag == COMPLETENESS_MISSING]}"
        )
        # Operator-declared elements (5, 7, 8, 10) should have content with [OPERATOR REQUIRED]
        operator_elems = [e for e in doc.elements if e.population_method == "operator_declared"]
        assert len(operator_elems) == 4

    def test_schema_validation(self, minimal_capsule_dir: Path) -> None:
        """JSON output must validate against the JSON Schema."""
        import json

        schema = json.loads(_SCHEMA_PATH.read_text())
        exporter = AnnexIVExporter()
        doc = exporter.build_annex_iv_document("deploy-schema-test", minimal_capsule_dir)
        # Use by_alias=True so @context/@type are included, then remove unknown keys
        doc_dict = doc.model_dump(mode="json", by_alias=True)
        # Remove the internal alias keys that don't appear in the schema properties
        # (context/document_type are internal Pydantic fields; @context/@type are JSON-LD)
        doc_dict.pop("context", None)
        doc_dict.pop("document_type", None)
        jsonschema.validate(instance=doc_dict, schema=schema)

    def test_operator_override_marks_complete(self, minimal_capsule_dir: Path) -> None:
        exporter = AnnexIVExporter()
        doc = exporter.build_annex_iv_document(
            "deploy-override",
            minimal_capsule_dir,
            operator_overrides={7: "ISO/IEC 42001:2023 applied"},
        )
        elem7 = next(e for e in doc.elements if e.element_id == 7)
        assert elem7.completeness_flag == COMPLETENESS_COMPLETE
        assert "ISO/IEC 42001:2023" in elem7.content

    def test_missing_capsule_yaml_raises(self, tmp_path: Path) -> None:
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        exporter = AnnexIVExporter()
        with pytest.raises(FileNotFoundError, match="capsule.yaml"):
            exporter.build_annex_iv_document("deploy-fail", empty_dir)

    def test_deployment_id_in_document(self, minimal_capsule_dir: Path) -> None:
        exporter = AnnexIVExporter()
        doc = exporter.build_annex_iv_document("my-agent-v2", minimal_capsule_dir)
        assert doc.deployment_id == "my-agent-v2"

    def test_generated_at_present(self, minimal_capsule_dir: Path) -> None:
        exporter = AnnexIVExporter()
        doc = exporter.build_annex_iv_document("deploy-ts", minimal_capsule_dir)
        assert doc.generated_at

    def test_element_evidence_refs_contain_run_id(self, minimal_capsule_dir: Path) -> None:
        exporter = AnnexIVExporter()
        doc = exporter.build_annex_iv_document("deploy-ev", minimal_capsule_dir)
        # At least one element should reference the capsule run_id
        all_refs = [ref for e in doc.elements for ref in e.evidence_refs]
        capsule_refs = [r for r in all_refs if r.startswith("capsule:")]
        assert len(capsule_refs) > 0

    def test_completeness_summary_length_15(self, minimal_capsule_dir: Path) -> None:
        exporter = AnnexIVExporter()
        doc = exporter.build_annex_iv_document("deploy-cs", minimal_capsule_dir)
        summary = doc.completeness_summary()
        assert len(summary) == 15

    def test_capsule_yaml_not_dict_raises(self, tmp_path: Path) -> None:
        bad_dir = tmp_path / "bad_capsule"
        bad_dir.mkdir()
        (bad_dir / "capsule.yaml").write_text("- just a list\n- not a dict\n")
        exporter = AnnexIVExporter()
        with pytest.raises(ValueError, match="did not parse as a dict"):
            exporter.build_annex_iv_document("deploy-bad", bad_dir)

    def test_custom_mapping_path(self, minimal_capsule_dir: Path, tmp_path: Path) -> None:
        default_mapping_path = (
            Path(__file__).parents[3]
            / "src" / "novafabric" / "compliance" / "export" / "annex_iv_mapping.yaml"
        )
        custom_mapping = tmp_path / "custom_mapping.yaml"
        custom_mapping.write_text(default_mapping_path.read_text())
        exporter = AnnexIVExporter(mapping_path=custom_mapping)
        doc = exporter.build_annex_iv_document("deploy-custom-map", minimal_capsule_dir)
        assert len(doc.elements) == 15

    def test_renderer_json_ld_output(self, minimal_capsule_dir: Path, tmp_path: Path) -> None:
        """JSON-LD file must be created without requiring WeasyPrint."""
        exporter = AnnexIVExporter()
        doc = exporter.build_annex_iv_document("deploy-render", minimal_capsule_dir)
        from novafabric.compliance.export.renderer import DocumentRenderer
        renderer = DocumentRenderer()
        json_ld_path, pdf_path = renderer.render(doc, tmp_path / "out", pdf=False)
        assert json_ld_path.exists()
        assert pdf_path is None
        # Verify JSON-LD content
        import json
        data = json.loads(json_ld_path.read_text())
        assert data.get("@type") == "AnnexIVDocument"
        assert "elements" in data
