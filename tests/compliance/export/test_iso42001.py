# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""ADR-0107 §NF-095 — ISO/IEC 42001 control-mapper + 42005 impact-assessment.

A pure projection over evidence NovaFabric already holds. The mapper never
fabricates a control as satisfied: a control whose mapped evidence is absent is an
honest ``not_evidenced`` gap, and an operator-asserted control carries no capsule
backing (``declared``). The 42005 object is a structured impact-assessment whose
each section records whether it was capsule-sourced or operator-declared.
"""

from __future__ import annotations

from novafabric.compliance.export.iso42001 import (
    Iso42001ControlStatus,
    build_iso42001_mapping,
    build_iso42005_impact_assessment,
)
from novafabric.compliance.export.provenance import EvidenceSource, EvidenceSourceRef

_REF = EvidenceSourceRef(
    capsule_id="cap-1",
    content_digest="sha256:" + "a" * 64,
    verified_at="2026-07-25T00:00:00Z",
)


class TestIso42001Mapping:
    def test_evidenced_control_is_marked_and_carries_ref(self) -> None:
        report = build_iso42001_mapping(
            catalog=[{"control_id": "ISO42-8.5-RESPONSIBLE-AI", "evidence_kind": "eval_gate"}],
            present_evidence={"eval_gate"},
            capsule_ref=_REF,
        )
        entry = report.controls[0]
        assert entry.status is Iso42001ControlStatus.evidenced
        assert entry.evidence_ref == _REF
        # An evidenced control is operator_asserted (ref carried, not re-performed).
        assert entry.provenance is EvidenceSource.operator_asserted
        assert report.evidenced_count == 1

    def test_absent_evidence_is_an_honest_gap(self) -> None:
        report = build_iso42001_mapping(
            catalog=[{"control_id": "ISO42-9.1-MONITORING-EVAL", "evidence_kind": "monitoring"}],
            present_evidence=set(),
        )
        entry = report.controls[0]
        assert entry.status is Iso42001ControlStatus.not_evidenced
        assert entry.evidence_ref is None
        assert entry.provenance is EvidenceSource.unverifiable
        assert report.evidenced_count == 0

    def test_operator_declared_control_has_no_capsule_backing(self) -> None:
        report = build_iso42001_mapping(
            catalog=[{"control_id": "ISO42-4.1-CONTEXT"}],
            present_evidence={"eval_gate"},
            declared_controls={"ISO42-4.1-CONTEXT"},
        )
        entry = report.controls[0]
        assert entry.status is Iso42001ControlStatus.declared
        assert entry.evidence_ref is None
        assert entry.provenance is EvidenceSource.operator_asserted

    def test_evidenced_requires_a_ref_else_unverifiable(self) -> None:
        # Evidence present but no re-performable ref supplied → cannot claim evidenced.
        report = build_iso42001_mapping(
            catalog=[{"control_id": "ISO42-8.5-RESPONSIBLE-AI", "evidence_kind": "eval_gate"}],
            present_evidence={"eval_gate"},
            capsule_ref=None,
        )
        entry = report.controls[0]
        assert entry.status is Iso42001ControlStatus.not_evidenced
        assert entry.provenance is EvidenceSource.unverifiable

    def test_standard_label_and_counts(self) -> None:
        report = build_iso42001_mapping(
            catalog=[
                {"control_id": "ISO42-8.5-RESPONSIBLE-AI", "evidence_kind": "eval_gate"},
                {"control_id": "ISO42-9.1-MONITORING-EVAL", "evidence_kind": "monitoring"},
            ],
            present_evidence={"eval_gate"},
            capsule_ref=_REF,
        )
        assert report.standard.startswith("ISO/IEC 42001")
        assert len(report.controls) == 2
        assert report.evidenced_count == 1


class TestIso42005ImpactAssessment:
    def test_sections_default_to_operator_declared(self) -> None:
        ia = build_iso42005_impact_assessment(system_name="triage-agent")
        assert ia.standard.startswith("ISO/IEC 42005")
        assert ia.system_name == "triage-agent"
        # Canonical impact-assessment sections are always present as a skeleton.
        assert len(ia.sections) >= 5
        assert all(s.content_source is EvidenceSource.operator_asserted for s in ia.sections)
        assert all(s.populated is False for s in ia.sections)

    def test_capsule_sourced_section_is_marked_and_populated(self) -> None:
        ia = build_iso42005_impact_assessment(
            system_name="triage-agent",
            sourced_sections={"potential_harms": _REF},
        )
        harms = next(s for s in ia.sections if s.section_id == "potential_harms")
        assert harms.content_source is EvidenceSource.capsule_verified
        assert harms.capsule_ref == _REF
        assert harms.populated is True

    def test_operator_populated_section_carries_no_ref(self) -> None:
        ia = build_iso42005_impact_assessment(
            system_name="triage-agent",
            operator_sections={"intended_purpose"},
        )
        purpose = next(s for s in ia.sections if s.section_id == "intended_purpose")
        assert purpose.content_source is EvidenceSource.operator_asserted
        assert purpose.capsule_ref is None
        assert purpose.populated is True

    def test_unknown_section_id_is_ignored_not_fabricated(self) -> None:
        # A caller naming a non-canonical section must not silently inject one.
        ia = build_iso42005_impact_assessment(
            system_name="triage-agent",
            operator_sections={"not_a_real_section"},
        )
        assert all(s.section_id != "not_a_real_section" for s in ia.sections)
