# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Tests for the HIPAA Safe Harbor proof exporter."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from novafabric.compliance.export.hipaa_safe_harbor import (
    SAFE_HARBOR_IDENTIFIERS,
    STATUS_NOT_ASSESSABLE,
    STATUS_NOT_PRESENT,
    STATUS_REDACTED,
    HIPAASafeHarborExporter,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FIXED_DT = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def minimal_capsule(tmp_path: Path) -> Path:
    """A capsule directory with only capsule.yaml."""
    d = tmp_path / "cap-hipaa-001"
    d.mkdir()
    yaml.dump({"run_id": "run-hipaa-001", "schema_version": "1.0.0"}, (d / "capsule.yaml").open("w"))
    return d


@pytest.fixture()
def capsule_with_email_redaction(minimal_capsule: Path) -> Path:
    """Capsule with a redaction_manifest.json containing EMAIL entry."""
    redaction = {
        "total_detections": 1,
        "redacted_count": 1,
        "redacted_items": [
            {"pii_type": "EMAIL", "redacted": True},
        ],
    }
    (minimal_capsule / "redaction_manifest.json").write_text(json.dumps(redaction))
    return minimal_capsule


@pytest.fixture()
def capsule_with_multi_redaction(minimal_capsule: Path) -> Path:
    """Capsule with multiple PII types in redaction_manifest.json."""
    redaction = {
        "redacted_items": [
            {"pii_type": "PERSON"},
            {"pii_type": "PHONE"},
            {"pii_type": "US_SSN"},
            {"pii_type": "IP_ADDRESS"},
            {"pii_type": "URL"},
        ],
    }
    (minimal_capsule / "redaction_manifest.json").write_text(json.dumps(redaction))
    return minimal_capsule


@pytest.fixture()
def capsule_with_unknown_pii(minimal_capsule: Path) -> Path:
    """Capsule with an unrecognised PII type that should map to other_unique_identifiers."""
    redaction = {
        "redacted_items": [
            {"pii_type": "ALIEN_REGISTRY_NUMBER"},
        ],
    }
    (minimal_capsule / "redaction_manifest.json").write_text(json.dumps(redaction))
    return minimal_capsule


@pytest.fixture()
def capsule_with_manifest_format(minimal_capsule: Path) -> Path:
    """Capsule with RedactionManifest-schema format (entries / detection_rule_id)."""
    manifest = {
        "capsule_id": "cap-hipaa-001",
        "entries": [
            {
                "field_path": "model_calls[0].messages[0].content",
                "detection_rule_id": "EMAIL",
                "legal_basis": "operator_declared",
                "subject_id_hmac": "sha256:abc123",
                "redacted_at_utc": "2026-05-27T00:00:00+00:00",
            },
        ],
        "legal_hold_mode": True,
    }
    (minimal_capsule / "redaction_manifest.json").write_text(json.dumps(manifest))
    return minimal_capsule


@pytest.fixture()
def empty_capsule(tmp_path: Path) -> Path:
    """Capsule directory with no files at all."""
    d = tmp_path / "cap-empty"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Test: basic proof structure
# ---------------------------------------------------------------------------


class TestHIPAASafeHarborProofStructure:
    def test_all_18_identifiers_present(self, minimal_capsule: Path) -> None:
        exporter = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT)
        proof = exporter.build_proof(minimal_capsule)
        assert len(proof.identifiers) == 18
        returned_ids = [a.identifier for a in proof.identifiers]
        assert returned_ids == SAFE_HARBOR_IDENTIFIERS

    def test_framework_field_present(self, minimal_capsule: Path) -> None:
        proof = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT).build_proof(minimal_capsule)
        assert proof.framework == "HIPAA Privacy Rule 45 CFR 164.514(b) Safe Harbor"

    def test_capsule_id_is_directory_name(self, minimal_capsule: Path) -> None:
        proof = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT).build_proof(minimal_capsule)
        assert proof.capsule_id == minimal_capsule.name

    def test_run_id_from_capsule_yaml(self, minimal_capsule: Path) -> None:
        proof = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT).build_proof(minimal_capsule)
        assert proof.run_id == "run-hipaa-001"

    def test_disclaimer_present(self, minimal_capsule: Path) -> None:
        proof = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT).build_proof(minimal_capsule)
        assert "NOT legal advice" in proof.legal_disclaimer
        assert len(proof.legal_disclaimer) > 50

    def test_limitations_present(self, minimal_capsule: Path) -> None:
        proof = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT).build_proof(minimal_capsule)
        assert len(proof.limitations) >= 3
        assert any("legal advice" in lim.lower() or "technical evidence" in lim.lower() for lim in proof.limitations)

    def test_schema_version(self, minimal_capsule: Path) -> None:
        proof = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT).build_proof(minimal_capsule)
        assert proof.schema_version == "1.0"

    def test_assessment_mode(self, minimal_capsule: Path) -> None:
        proof = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT).build_proof(minimal_capsule)
        assert proof.assessment_mode == "evidence_artifact_only"

    def test_expert_determination_ref_is_none_by_default(self, minimal_capsule: Path) -> None:
        proof = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT).build_proof(minimal_capsule)
        assert proof.expert_determination_ref is None

    def test_source_files_includes_capsule_yaml(self, minimal_capsule: Path) -> None:
        proof = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT).build_proof(minimal_capsule)
        assert "capsule.yaml" in proof.source_files


# ---------------------------------------------------------------------------
# Test: identifier status mapping
# ---------------------------------------------------------------------------


class TestIdentifierStatusMapping:
    def test_email_redacted_when_in_manifest(self, capsule_with_email_redaction: Path) -> None:
        proof = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT).build_proof(
            capsule_with_email_redaction
        )
        email_assessment = next(a for a in proof.identifiers if a.identifier == "email_addresses")
        assert email_assessment.status == STATUS_REDACTED
        assert email_assessment.evidence_count == 1
        assert email_assessment.redaction_method == "redaction_manifest"

    def test_unrelated_categories_not_present(self, capsule_with_email_redaction: Path) -> None:
        proof = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT).build_proof(
            capsule_with_email_redaction
        )
        unrelated = [
            a for a in proof.identifiers
            if a.identifier not in ("email_addresses", "full_face_photos", "biometric_identifiers")
        ]
        for a in unrelated:
            assert a.status == STATUS_NOT_PRESENT, (
                f"Expected not_present for {a.identifier}, got {a.status}"
            )

    def test_multi_pii_types_map_correctly(self, capsule_with_multi_redaction: Path) -> None:
        proof = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT).build_proof(
            capsule_with_multi_redaction
        )
        by_id = {a.identifier: a for a in proof.identifiers}
        assert by_id["names"].status == STATUS_REDACTED, "PERSON -> names"
        assert by_id["telephone_numbers"].status == STATUS_REDACTED, "PHONE -> telephone_numbers"
        assert by_id["social_security_numbers"].status == STATUS_REDACTED, "US_SSN -> social_security_numbers"
        assert by_id["ip_addresses"].status == STATUS_REDACTED, "IP_ADDRESS -> ip_addresses"
        assert by_id["urls"].status == STATUS_REDACTED, "URL -> urls"

    def test_unknown_pii_type_maps_to_other_unique_identifiers(
        self, capsule_with_unknown_pii: Path
    ) -> None:
        proof = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT).build_proof(
            capsule_with_unknown_pii
        )
        other = next(a for a in proof.identifiers if a.identifier == "other_unique_identifiers")
        assert other.status == STATUS_REDACTED

    def test_biometrics_and_photos_not_assessable_by_default(self, minimal_capsule: Path) -> None:
        proof = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT).build_proof(minimal_capsule)
        by_id = {a.identifier: a for a in proof.identifiers}
        assert by_id["biometric_identifiers"].status == STATUS_NOT_ASSESSABLE
        assert by_id["full_face_photos"].status == STATUS_NOT_ASSESSABLE

    def test_manifest_entries_format_also_supported(self, capsule_with_manifest_format: Path) -> None:
        """Supports RedactionManifest schema (entries/detection_rule_id)."""
        proof = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT).build_proof(
            capsule_with_manifest_format
        )
        email = next(a for a in proof.identifiers if a.identifier == "email_addresses")
        assert email.status == STATUS_REDACTED


# ---------------------------------------------------------------------------
# Test: missing capsule.yaml handled gracefully
# ---------------------------------------------------------------------------


class TestMissingCapsuleYaml:
    def test_missing_capsule_yaml_returns_unknown_run_id(self, empty_capsule: Path) -> None:
        proof = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT).build_proof(empty_capsule)
        assert proof.run_id == "unknown"

    def test_missing_capsule_yaml_still_returns_18_identifiers(self, empty_capsule: Path) -> None:
        proof = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT).build_proof(empty_capsule)
        assert len(proof.identifiers) == 18

    def test_missing_capsule_yaml_capsule_id_is_dir_name(self, empty_capsule: Path) -> None:
        proof = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT).build_proof(empty_capsule)
        assert proof.capsule_id == empty_capsule.name


# ---------------------------------------------------------------------------
# Test: proof_digest
# ---------------------------------------------------------------------------


class TestProofDigest:
    def test_proof_digest_is_sha256_prefixed(self, minimal_capsule: Path) -> None:
        proof = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT).build_proof(minimal_capsule)
        assert proof.proof_digest.startswith("sha256:")
        hex_part = proof.proof_digest[len("sha256:"):]
        assert len(hex_part) == 64
        assert all(c in "0123456789abcdef" for c in hex_part)

    def test_proof_digest_is_deterministic(self, minimal_capsule: Path) -> None:
        exporter = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT)
        proof1 = exporter.build_proof(minimal_capsule)
        proof2 = exporter.build_proof(minimal_capsule)
        assert proof1.proof_digest == proof2.proof_digest

    def test_proof_digest_changes_with_different_capsule(
        self, minimal_capsule: Path, tmp_path: Path
    ) -> None:
        d2 = tmp_path / "cap-hipaa-other"
        d2.mkdir()
        yaml.dump({"run_id": "run-other-999"}, (d2 / "capsule.yaml").open("w"))
        exporter = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT)
        proof1 = exporter.build_proof(minimal_capsule)
        proof2 = exporter.build_proof(d2)
        assert proof1.proof_digest != proof2.proof_digest


# ---------------------------------------------------------------------------
# Test: export_json
# ---------------------------------------------------------------------------


class TestExportJson:
    def test_export_writes_valid_json(self, minimal_capsule: Path, tmp_path: Path) -> None:
        exporter = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT)
        proof = exporter.build_proof(minimal_capsule)
        output = tmp_path / "hipaa-proof.json"
        exporter.export_json(proof, output)
        assert output.exists()
        data = json.loads(output.read_text())
        assert isinstance(data, dict)

    def test_export_includes_all_18_identifiers(self, minimal_capsule: Path, tmp_path: Path) -> None:
        exporter = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT)
        proof = exporter.build_proof(minimal_capsule)
        output = tmp_path / "hipaa-proof.json"
        exporter.export_json(proof, output)
        data = json.loads(output.read_text())
        assert len(data["identifiers"]) == 18

    def test_export_includes_framework(self, minimal_capsule: Path, tmp_path: Path) -> None:
        exporter = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT)
        proof = exporter.build_proof(minimal_capsule)
        output = tmp_path / "hipaa-proof.json"
        exporter.export_json(proof, output)
        data = json.loads(output.read_text())
        assert "HIPAA" in data["framework"]
        assert "164.514" in data["framework"]

    def test_export_includes_proof_digest(self, minimal_capsule: Path, tmp_path: Path) -> None:
        exporter = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT)
        proof = exporter.build_proof(minimal_capsule)
        output = tmp_path / "hipaa-proof.json"
        exporter.export_json(proof, output)
        data = json.loads(output.read_text())
        assert data["proof_digest"].startswith("sha256:")

    def test_export_includes_disclaimer(self, minimal_capsule: Path, tmp_path: Path) -> None:
        exporter = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT)
        proof = exporter.build_proof(minimal_capsule)
        output = tmp_path / "hipaa-proof.json"
        exporter.export_json(proof, output)
        data = json.loads(output.read_text())
        assert "NOT legal advice" in data["legal_disclaimer"]

    def test_export_creates_parent_dirs(self, minimal_capsule: Path, tmp_path: Path) -> None:
        exporter = HIPAASafeHarborExporter(assessment_datetime=_FIXED_DT)
        proof = exporter.build_proof(minimal_capsule)
        nested_output = tmp_path / "deep" / "nested" / "hipaa-proof.json"
        exporter.export_json(proof, nested_output)
        assert nested_output.exists()


# ---------------------------------------------------------------------------
# Test: customisation
# ---------------------------------------------------------------------------


class TestCustomisation:
    def test_custom_not_assessable_set(self, minimal_capsule: Path) -> None:
        """Operator can override which IDs are not_assessable."""
        exporter = HIPAASafeHarborExporter(
            not_assessable_ids=frozenset({"full_face_photos"}),
            assessment_datetime=_FIXED_DT,
        )
        proof = exporter.build_proof(minimal_capsule)
        by_id = {a.identifier: a for a in proof.identifiers}
        assert by_id["full_face_photos"].status == STATUS_NOT_ASSESSABLE
        # biometric_identifiers should be not_present (not not_assessable) since not in custom set
        assert by_id["biometric_identifiers"].status == STATUS_NOT_PRESENT

    def test_empty_not_assessable_set(self, minimal_capsule: Path) -> None:
        exporter = HIPAASafeHarborExporter(
            not_assessable_ids=frozenset(),
            assessment_datetime=_FIXED_DT,
        )
        proof = exporter.build_proof(minimal_capsule)
        by_id = {a.identifier: a for a in proof.identifiers}
        # With no not_assessable override, everything defaults to not_present (no evidence)
        assert by_id["biometric_identifiers"].status == STATUS_NOT_PRESENT
        assert by_id["full_face_photos"].status == STATUS_NOT_PRESENT
