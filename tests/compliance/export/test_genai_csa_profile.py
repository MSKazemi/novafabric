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
"""ADR-0107 §NF-097 — NIST GenAI Profile + CSA Agentic Profile mapper.

Extends the *shipped* NIST-RMF evidence (``NISTRMFReport``) to the NIST GenAI Profile's
four focus areas and the CSA Agentic Profile subcategory actions. A focus area is
auto-evidenced from the base report's RMF-function score; anything else is mapped
against the governance evidence present, and marked evidenced / not_evidenced / declared
— never fabricated.
"""

from __future__ import annotations

from novafabric.compliance.export.genai_csa_profile import (
    CSA_AGENTIC_SUBCATEGORIES,
    NIST_GENAI_FOCUS_AREAS,
    ProfileMappingStatus,
    build_genai_csa_profile,
)
from novafabric.compliance.export.nist_rmf import NISTRMFReport
from novafabric.compliance.export.provenance import EvidenceSource


def _report(**scores: float) -> NISTRMFReport:
    return NISTRMFReport(capsule_id="cap-1", run_id="run-1", **scores)


class TestGenAiFocusAreas:
    def test_governance_area_auto_evidenced_from_base_govern_score(self) -> None:
        report = build_genai_csa_profile(_report(govern_score=0.8), present_evidence=set())
        gov = next(m for m in report.mappings if m.area == "governance")
        assert gov.status is ProfileMappingStatus.evidenced
        assert gov.provenance is EvidenceSource.operator_asserted

    def test_area_not_evidenced_without_score_or_evidence(self) -> None:
        report = build_genai_csa_profile(_report(), present_evidence=set())
        gov = next(m for m in report.mappings if m.area == "governance")
        assert gov.status is ProfileMappingStatus.not_evidenced
        assert gov.provenance is EvidenceSource.unverifiable

    def test_content_provenance_evidenced_from_present_evidence(self) -> None:
        report = build_genai_csa_profile(_report(), present_evidence={"content_marking"})
        cp = next(m for m in report.mappings if m.area == "content_provenance")
        assert cp.status is ProfileMappingStatus.evidenced

    def test_all_four_focus_areas_present(self) -> None:
        report = build_genai_csa_profile(_report(), present_evidence=set())
        areas = {m.area for m in report.mappings if m.profile == "nist_genai"}
        assert areas == {a["id"] for a in NIST_GENAI_FOCUS_AREAS}
        assert len(areas) == 4


class TestCsaAgenticSubcategories:
    def test_subcategory_evidenced_from_present_evidence(self) -> None:
        # tool_authorization maps to the shipped tool-permission evidence.
        report = build_genai_csa_profile(_report(), present_evidence={"tool_permissions"})
        ta = next(m for m in report.mappings if m.area == "tool_authorization")
        assert ta.profile == "csa_agentic"
        assert ta.status is ProfileMappingStatus.evidenced

    def test_all_csa_subcategories_present(self) -> None:
        report = build_genai_csa_profile(_report(), present_evidence=set())
        subs = {m.area for m in report.mappings if m.profile == "csa_agentic"}
        assert subs == {s["id"] for s in CSA_AGENTIC_SUBCATEGORIES}


class TestDeclared:
    def test_declared_area_is_operator_asserted_without_backing(self) -> None:
        report = build_genai_csa_profile(
            _report(govern_score=0.9),
            present_evidence=set(),
            declared={"human_oversight"},
        )
        ho = next(m for m in report.mappings if m.area == "human_oversight")
        assert ho.status is ProfileMappingStatus.declared
        assert ho.provenance is EvidenceSource.operator_asserted


def test_report_references_the_base_rmf() -> None:
    report = build_genai_csa_profile(_report(govern_score=0.5), present_evidence=set())
    assert report.base_capsule_id == "cap-1"
    assert report.base_run_id == "run-1"
    assert report.evidenced_count >= 1
