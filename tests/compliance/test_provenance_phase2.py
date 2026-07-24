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
"""ADR-0197 phase 2 — provenance marking of the pure-projection export families.

Every one of these first-slice exporters projects supplied refs without
re-performing any binding, so every field-group is ``operator_asserted``; a
status that names a *checked gap* (evidence expected, absent) is ``unverifiable``
(I-2), never downgraded to an assertion. None is ever ``capsule_verified`` — a
supplied capsule ref is not a re-performed verification.
"""

from __future__ import annotations

from novafabric.compliance.export.provenance import EvidenceSource, source_for_status


class TestSourceForStatus:
    def test_gap_status_is_unverifiable(self) -> None:
        assert (
            source_for_status("missing", gap_states={"missing"})
            is EvidenceSource.unverifiable
        )

    def test_non_gap_status_is_operator_asserted(self) -> None:
        assert (
            source_for_status("complete", gap_states={"missing"})
            is EvidenceSource.operator_asserted
        )

    def test_empty_gap_states_never_unverifiable(self) -> None:
        assert source_for_status("anything") is EvidenceSource.operator_asserted


class TestTier1EntryFamilies:
    def test_part11_marks_present_and_missing(self) -> None:
        from novafabric.compliance.export.healthcare.part11 import build_part11_record

        rec = build_part11_record(capsule_root="r" * 64, elements={"signer_identity": "x"})
        by = {f.element: f for f in rec.fields}
        assert by["signer_identity"].evidence_source is EvidenceSource.operator_asserted
        assert by["audit_trail"].evidence_source is EvidenceSource.unverifiable  # missing

    def test_model_risk_marks_present_and_missing(self) -> None:
        from novafabric.compliance.export.finance.model_risk import build_model_risk_file

        f = build_model_risk_file(model_id="m", development=["d"])
        by = {p.pillar: p for p in f.pillars}
        assert by["development"].evidence_source is EvidenceSource.operator_asserted
        assert by["ongoing_monitoring"].evidence_source is EvidenceSource.unverifiable

    def test_rai_marks_supported_and_unsupported(self) -> None:
        from novafabric.compliance.rai.scorecard import build_rai_scorecard

        s = build_rai_scorecard(evidence={"fairness": ["f"]})
        by = {c.dimension: c for c in s.cells}
        assert by["fairness"].evidence_source is EvidenceSource.operator_asserted
        assert by["transparency"].evidence_source is EvidenceSource.unverifiable  # unsupported


class TestTier2CrosswalkFamilies:
    def test_annex_viii_field_and_unmapped(self) -> None:
        from novafabric.compliance.export.public.annex_viii import build_annex_viii_entry

        e = build_annex_viii_entry(
            capsule_root="r" * 64,
            operator_declared={"provider_name": "x"},
            capsule_evidence={"system_status": "ref"},
        )
        # capsule_evidence source must NOT become capsule_verified (no binding re-performed).
        for f in e.fields:
            assert f.evidence_source is EvidenceSource.operator_asserted
        assert e.unmapped_evidence_source is EvidenceSource.unverifiable

    def test_transparency_register_marks(self) -> None:
        from novafabric.compliance.export.public._transparency_register import (
            build_transparency_register,
        )

        r = build_transparency_register(
            standard="atrs", capsule_root="r" * 64, capsule_evidence={}
        )
        for f in r.fields:
            assert f.evidence_source is EvidenceSource.operator_asserted
        assert r.manual_evidence_source is EvidenceSource.unverifiable

    def test_public_sector_marks(self) -> None:
        from novafabric.compliance.export.public._public_sector import (
            build_public_sector_disclosure,
        )

        d = build_public_sector_disclosure(authority_ref="a")
        assert d.evidence_source is EvidenceSource.operator_asserted
        assert d.manual_evidence_source is EvidenceSource.unverifiable


class TestTier3FlatFamilies:
    def test_all_flat_families_operator_asserted(self) -> None:
        from novafabric.compliance.export.public._accessibility import (
            build_accessibility_claim,
        )
        from novafabric.compliance.export.public._citizen import build_citizen_explanation
        from novafabric.compliance.export.public._election import build_election_disclosure
        from novafabric.compliance.export.public._foia import build_foia_export
        from novafabric.compliance.export.public._public_incident import (
            build_public_incident_disclosure,
        )
        from novafabric.compliance.export.public._whistleblower import (
            WHISTLEBLOWER_EVIDENCE_SOURCE,
            build_whistleblower_attestation,
        )

        foia = build_foia_export(decision_ref="d", record_index=["a" * 64])
        election = build_election_disclosure(
            content_ref="c", provenance_receipt_ref="p", disclosure_label="ai_generated"
        )
        incident = build_public_incident_disclosure(incident_ref="i")
        citizen = build_citizen_explanation(
            decision_ref="d", factors=["credit history"], human_involvement="human_in_the_loop"
        )
        access = build_accessibility_claim(declared_standard="wcag_2_2_aa")
        for obj in (foia, election, incident, citizen, access):
            assert obj.evidence_source is EvidenceSource.operator_asserted
        # whistleblower carries its marker as a module constant (see model note).
        whistle = build_whistleblower_attestation(
            {"content_digest": "d", "authenticity_attestation": "s"}
        )
        assert whistle.authenticity_attestation == "s"
        assert WHISTLEBLOWER_EVIDENCE_SOURCE is EvidenceSource.operator_asserted

    def test_whistleblower_still_rejects_source_identifying_input(self) -> None:
        # The new internal marker must not weaken the anti-identification gate.
        import pytest

        from novafabric.compliance.export.public._whistleblower import (
            build_whistleblower_attestation,
        )

        with pytest.raises(ValueError):
            build_whistleblower_attestation(
                {
                    "content_digest": "d",
                    "authenticity_attestation": "s",
                    "evidence_source": "operator_asserted",  # matches "source" → rejected
                }
            )
