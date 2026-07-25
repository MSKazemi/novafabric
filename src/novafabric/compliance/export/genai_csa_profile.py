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
"""NIST GenAI Profile + CSA Agentic Profile mapper (ADR-0107 §NF-097).

**Extends** the shipped NIST-RMF evidence
(:class:`~novafabric.compliance.export.nist_rmf.NISTRMFReport`) — it does not restate it —
to two overlay profiles:

* the **NIST GenAI Profile** (NIST AI 600-1) four focus areas — Governance, Content
  Provenance, Pre-deployment Testing, Incident Disclosure — each backed by one of the
  base report's RMF functions, so a focus area is *auto-evidenced* when the base report
  carries a score for its RMF function; and
* the **CSA Agentic Profile** subcategory actions (agent identity, tool authorization,
  action governance, memory integrity, human oversight, incident response), mapped to the
  governance evidence present for the deployment.

Every mapping is ``evidenced`` / ``not_evidenced`` / ``declared`` with an ADR-0197
``evidence_source`` — a pure projection (reusing the ``control_attestation`` pattern),
never a fabrication. Pure-code and offline; zero new dependencies. This completes
ADR-0107's exporter set.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from novafabric.compliance.export.nist_rmf import NISTRMFReport
from novafabric.compliance.export.provenance import EvidenceSource

#: NIST GenAI Profile focus areas, each anchored to a base RMF function whose presence in
#: the shipped ``NISTRMFReport`` auto-evidences the area, plus a fallback evidence kind.
NIST_GENAI_FOCUS_AREAS: tuple[dict[str, str], ...] = (
    {"id": "governance", "title": "Governance", "rmf_function": "GOVERN",
     "evidence_kind": "governance_policy"},
    {"id": "content_provenance", "title": "Content Provenance", "rmf_function": "MANAGE",
     "evidence_kind": "content_marking"},
    {"id": "predeployment_testing", "title": "Pre-deployment Testing", "rmf_function": "MEASURE",
     "evidence_kind": "eval_gate"},
    {"id": "incident_disclosure", "title": "Incident Disclosure", "rmf_function": "MANAGE",
     "evidence_kind": "incident_record"},
)

#: CSA Agentic Profile subcategory actions, mapped to shipped governance-evidence surfaces.
CSA_AGENTIC_SUBCATEGORIES: tuple[dict[str, str], ...] = (
    {"id": "agent_identity", "title": "Agent identity & authentication",
     "evidence_kind": "agent_identity"},
    {"id": "tool_authorization", "title": "Tool & action authorization",
     "evidence_kind": "tool_permissions"},
    {"id": "action_governance", "title": "Action governance & approval",
     "evidence_kind": "approval_gate"},
    {"id": "memory_integrity", "title": "Memory & context integrity",
     "evidence_kind": "memory_seal"},
    {"id": "human_oversight", "title": "Human oversight",
     "evidence_kind": "hitl_facet"},
    {"id": "incident_response", "title": "Incident response",
     "evidence_kind": "incident_record"},
)

_RMF_SCORE_ATTR = {
    "GOVERN": "govern_score",
    "MAP": "map_score",
    "MEASURE": "measure_score",
    "MANAGE": "manage_score",
}


class ProfileMappingStatus(str, Enum):
    """The evidentiary status of a profile focus area / subcategory."""

    evidenced = "evidenced"
    not_evidenced = "not_evidenced"
    declared = "declared"


class ProfileMapping(BaseModel):
    """One focus area or subcategory mapped to the deployment's evidence."""

    area: str
    title: str
    profile: str  # "nist_genai" | "csa_agentic"
    status: ProfileMappingStatus
    provenance: EvidenceSource
    evidence_kind: str | None = None
    rmf_function: str | None = None


class GenAiCsaProfileReport(BaseModel):
    """NIST GenAI + CSA Agentic overlay over a base NIST-RMF report."""

    base_capsule_id: str
    base_run_id: str
    mappings: list[ProfileMapping] = Field(default_factory=list)
    evidenced_count: int = 0
    generated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve(
    *,
    area_id: str,
    evidence_kind: str,
    auto_evidenced: bool,
    present: set[str],
    declared: set[str],
) -> tuple[ProfileMappingStatus, EvidenceSource]:
    if area_id in declared:
        return ProfileMappingStatus.declared, EvidenceSource.operator_asserted
    if auto_evidenced or evidence_kind in present:
        return ProfileMappingStatus.evidenced, EvidenceSource.operator_asserted
    return ProfileMappingStatus.not_evidenced, EvidenceSource.unverifiable


def build_genai_csa_profile(
    rmf_report: NISTRMFReport,
    *,
    present_evidence: Iterable[str],
    declared: Iterable[str] | None = None,
) -> GenAiCsaProfileReport:
    """Map a base NIST-RMF report to the GenAI + CSA Agentic profiles.

    A GenAI focus area is ``evidenced`` when the base report carries a score for its RMF
    function (extending the shipped evidence) **or** its evidence kind is in
    ``present_evidence``. A CSA subcategory is ``evidenced`` when its evidence kind is
    present. Anything in ``declared`` is ``declared`` (operator-asserted, unbacked); the
    rest is an honest ``not_evidenced`` gap.
    """
    present = set(present_evidence)
    declared_set = set(declared or ())
    mappings: list[ProfileMapping] = []

    for area in NIST_GENAI_FOCUS_AREAS:
        rmf_function = area["rmf_function"]
        score = getattr(rmf_report, _RMF_SCORE_ATTR[rmf_function], None)
        status, provenance = _resolve(
            area_id=area["id"],
            evidence_kind=area["evidence_kind"],
            auto_evidenced=score is not None,
            present=present,
            declared=declared_set,
        )
        mappings.append(
            ProfileMapping(
                area=area["id"],
                title=area["title"],
                profile="nist_genai",
                status=status,
                provenance=provenance,
                evidence_kind=area["evidence_kind"],
                rmf_function=rmf_function,
            )
        )

    for sub in CSA_AGENTIC_SUBCATEGORIES:
        status, provenance = _resolve(
            area_id=sub["id"],
            evidence_kind=sub["evidence_kind"],
            auto_evidenced=False,
            present=present,
            declared=declared_set,
        )
        mappings.append(
            ProfileMapping(
                area=sub["id"],
                title=sub["title"],
                profile="csa_agentic",
                status=status,
                provenance=provenance,
                evidence_kind=sub["evidence_kind"],
            )
        )

    evidenced = sum(1 for m in mappings if m.status is ProfileMappingStatus.evidenced)
    return GenAiCsaProfileReport(
        base_capsule_id=rmf_report.capsule_id,
        base_run_id=rmf_report.run_id,
        mappings=mappings,
        evidenced_count=evidenced,
        generated_at=_now(),
    )


__all__: list[str] = [
    "CSA_AGENTIC_SUBCATEGORIES",
    "GenAiCsaProfileReport",
    "NIST_GENAI_FOCUS_AREAS",
    "ProfileMapping",
    "ProfileMappingStatus",
    "build_genai_csa_profile",
]
