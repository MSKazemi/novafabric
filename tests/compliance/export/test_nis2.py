# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Tests for NIS2Exporter (cap-005 Phase 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.compliance.export.models import (
    COMPLETENESS_MISSING,
    NIS2IncidentReport,
)
from novafabric.compliance.export.nis2 import NIS2Exporter
from novafabric.compliance.export.provenance import EvidenceSource, validate_marked


class TestNIS2EvidenceSource:
    """ADR-0197: every NIS2 field-group must be honestly provenance-marked."""

    def test_every_entry_is_marked(self, minimal_capsule_dir: Path) -> None:
        # I-1: no silent provenance.
        exporter = NIS2Exporter(cap006_available=False)
        report = exporter.build_nis2_report("INC-001", minimal_capsule_dir, phase=3)
        assert report.completeness_summary
        for entry in report.completeness_summary:
            assert entry.evidence_source is not None, entry.field_name

    def test_document_validates(self, minimal_capsule_dir: Path) -> None:
        exporter = NIS2Exporter(cap006_available=False)
        report = exporter.build_nis2_report("INC-001", minimal_capsule_dir, phase=3)
        validate_marked(report.completeness_summary)  # must not raise

    def test_cap006_blocked_is_unverifiable_not_asserted(
        self, minimal_capsule_dir: Path
    ) -> None:
        # I-2: an attempted-and-failed verification is 'unverifiable', never
        # downgraded to 'operator_asserted'.
        exporter = NIS2Exporter(cap006_available=False)
        report = exporter.build_nis2_report("INC-001", minimal_capsule_dir, phase=3)
        cap006 = [
            e for e in report.completeness_summary if "cap_006" in e.reason
        ]
        assert cap006
        for entry in cap006:
            assert entry.evidence_source is EvidenceSource.unverifiable

    def test_capsule_verified_entries_carry_ref(
        self, minimal_capsule_dir: Path
    ) -> None:
        # I-3: capsule_verified must be re-performable.
        exporter = NIS2Exporter(cap006_available=False)
        report = exporter.build_nis2_report("INC-001", minimal_capsule_dir, phase=2)
        for entry in report.completeness_summary:
            if entry.evidence_source is EvidenceSource.capsule_verified:
                assert entry.evidence_ref is not None
                assert entry.evidence_ref.content_digest.startswith("sha256:")


class TestNIS2Exporter:
    def test_phase1_basic_fields(self, minimal_capsule_dir: Path) -> None:
        exporter = NIS2Exporter(cap006_available=False)
        report = exporter.build_nis2_report(
            incident_id="INC-2024-001",
            capsule_dir=minimal_capsule_dir,
            phase=1,
        )
        assert report.incident_id == "INC-2024-001"
        assert report.first_detection_timestamp
        assert report.preliminary_classification == "unclassified"
        assert report.preliminary_severity_indicator == "medium"
        assert isinstance(report.cross_border_dimensions_flag, bool)

    def test_completeness_summary_present(self, minimal_capsule_dir: Path) -> None:
        exporter = NIS2Exporter(cap006_available=False)
        report = exporter.build_nis2_report("INC-001", minimal_capsule_dir, phase=1)
        assert len(report.completeness_summary) > 0

    def test_cap006_fields_marked_missing(self, minimal_capsule_dir: Path) -> None:
        """Fields requiring cap-006 must be marked missing with correct reason."""
        exporter = NIS2Exporter(cap006_available=False)
        report = exporter.build_nis2_report("INC-001", minimal_capsule_dir, phase=3)
        cap006_entries = [
            e for e in report.completeness_summary
            if e.status == COMPLETENESS_MISSING
            and "cap_006" in e.reason
        ]
        assert len(cap006_entries) >= 1, (
            "Expected ≥1 cap-006-blocked fields in completeness_summary"
        )

    def test_cap006_reason_string(self, minimal_capsule_dir: Path) -> None:
        exporter = NIS2Exporter(cap006_available=False)
        report = exporter.build_nis2_report("INC-001", minimal_capsule_dir, phase=2)
        cap006_missing = [
            e for e in report.completeness_summary
            if e.reason == "requires_cap_006_cross_session_lineage"
        ]
        assert len(cap006_missing) >= 1

    def test_phase1_p2_p3_fields_none(self, minimal_capsule_dir: Path) -> None:
        exporter = NIS2Exporter(cap006_available=False)
        report = exporter.build_nis2_report("INC-001", minimal_capsule_dir, phase=1)
        assert report.initial_root_cause_path is None
        assert report.final_root_cause is None
        assert report.lessons_learned is None

    def test_phase2_includes_p1_fields(self, minimal_capsule_dir: Path) -> None:
        exporter = NIS2Exporter(cap006_available=False)
        report = exporter.build_nis2_report("INC-001", minimal_capsule_dir, phase=2)
        assert report.incident_id == "INC-001"
        assert report.first_detection_timestamp
        assert report.report_phase == 2

    def test_phase3_completeness_summary_has_p3_entries(
        self, minimal_capsule_dir: Path
    ) -> None:
        exporter = NIS2Exporter(cap006_available=False)
        report = exporter.build_nis2_report("INC-001", minimal_capsule_dir, phase=3)
        p3_fields = {"final_root_cause", "cross_border_impact_assessment", "lessons_learned"}
        entry_names = {e.field_name for e in report.completeness_summary}
        assert p3_fields.issubset(entry_names), (
            f"Phase 3 fields missing from completeness_summary: "
            f"{p3_fields - entry_names}"
        )

    def test_operator_inputs_used(self, minimal_capsule_dir: Path) -> None:
        exporter = NIS2Exporter(cap006_available=False)
        report = exporter.build_nis2_report(
            "INC-002",
            minimal_capsule_dir,
            phase=1,
            operator_inputs={
                "preliminary_classification": "unauthorized_tool_use",
                "preliminary_severity_indicator": "high",
                "cross_border_dimensions_flag": True,
            },
        )
        assert report.preliminary_classification == "unauthorized_tool_use"
        assert report.preliminary_severity_indicator == "high"
        assert report.cross_border_dimensions_flag is True

    def test_invalid_phase_raises(self, minimal_capsule_dir: Path) -> None:
        exporter = NIS2Exporter()
        with pytest.raises(ValueError, match="phase must be"):
            exporter.build_nis2_report("INC-001", minimal_capsule_dir, phase=4)

    def test_json_roundtrip(self, minimal_capsule_dir: Path) -> None:
        exporter = NIS2Exporter(cap006_available=False)
        report = exporter.build_nis2_report("INC-001", minimal_capsule_dir, phase=1)
        restored = NIS2IncidentReport.model_validate_json(report.model_dump_json())
        assert restored.incident_id == report.incident_id

    def test_actions_taken_from_tool_permission_events(
        self, tmp_path: Path
    ) -> None:
        """Denied tool_permission_events should populate actions_taken."""
        import json

        import yaml
        capsule_dir = tmp_path / "cap"
        capsule_dir.mkdir()
        manifest = {
            "schema_version": "0.1.0",
            "run_id": "CAP-INCIDENT",
            "created_at": "2024-01-01T00:00:00Z",
            "finished_at": "2024-01-01T00:00:05Z",
            "status": "failure",
            "command": ["python", "agent.py"],
            "exit_code": 1,
        }
        (capsule_dir / "capsule.yaml").write_text(yaml.dump(manifest))
        # Write a denied tool permission event
        event = {
            "entity_type": "tool_permission_event",
            "capsule_id": "CAP-INCIDENT",
            "event_id": "00000000-0000-0000-0000-000000000001",
            "tool_name": "rm",
            "tool_id": "tool:rm:v1",
            "policy_id": "policy/safety.rego",
            "permission_level": "filesystem",
            "decision": "denied",
            "authorising_identity": "user:alice",
            "human_approval_required": False,
            "human_approval_obtained": None,
            "approval_principal": None,
            "decision_latency_ms": 2,
            "capsule_timestamp_utc": "2024-01-01T00:00:01Z",
        }
        (capsule_dir / "tool-permission-events.jsonl").write_text(
            json.dumps(event) + "\n"
        )
        exporter = NIS2Exporter(cap006_available=False)
        report = exporter.build_nis2_report("INC-003", capsule_dir, phase=2)
        assert report.actions_taken is not None
        assert len(report.actions_taken) >= 1
        assert "DENIED" in report.actions_taken[0]
        assert "rm" in report.actions_taken[0]

    def test_malformed_json_line_skipped(self, tmp_path: Path) -> None:
        """Malformed JSON lines in tool-permission-events.jsonl should be silently skipped."""
        import yaml as yaml_mod

        capsule_dir = tmp_path / "cap_malformed"
        capsule_dir.mkdir()
        manifest = {
            "schema_version": "0.1.0",
            "run_id": "CAP-MAL",
            "created_at": "2024-01-01T00:00:00Z",
        }
        (capsule_dir / "capsule.yaml").write_text(yaml_mod.dump(manifest))
        (capsule_dir / "tool-permission-events.jsonl").write_text(
            "not valid json\n"
        )
        exporter = NIS2Exporter(cap006_available=False)
        report = exporter.build_nis2_report("INC-MAL", capsule_dir, phase=1)
        assert report.incident_id == "INC-MAL"
        assert report.actions_taken is None

    def test_operator_actions_taken_used_when_no_denied(self, tmp_path: Path) -> None:
        """operator_inputs.actions_taken should be used when no denied events exist."""
        import yaml as yaml_mod

        capsule_dir = tmp_path / "cap_opactions"
        capsule_dir.mkdir()
        manifest = {
            "schema_version": "0.1.0",
            "run_id": "CAP-OPACT",
            "created_at": "2024-01-01T00:00:00Z",
        }
        (capsule_dir / "capsule.yaml").write_text(yaml_mod.dump(manifest))
        exporter = NIS2Exporter(cap006_available=False)
        report = exporter.build_nis2_report(
            "INC-OPACT",
            capsule_dir,
            phase=2,
            operator_inputs={"actions_taken": ["Contacted CSIRT", "Isolated system"]},
        )
        assert report.actions_taken == ["Contacted CSIRT", "Isolated system"]

    def test_no_capsule_yaml_returns_report(self, tmp_path: Path) -> None:
        """NIS2Exporter should handle missing capsule.yaml gracefully."""
        capsule_dir = tmp_path / "cap_nocapsule"
        capsule_dir.mkdir()
        exporter = NIS2Exporter(cap006_available=False)
        report = exporter.build_nis2_report("INC-NOCAP", capsule_dir, phase=1)
        assert report.incident_id == "INC-NOCAP"
