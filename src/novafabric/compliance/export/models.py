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
"""Compliance export Pydantic models (cap-002, cap-005)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .provenance import EvidenceSource, EvidenceSourceRef


class CompletenessStatus(str):
    """Status of an Annex IV element or NIS2 field population."""
    complete = "complete"
    partial = "partial"
    missing = "missing"

    _VALUES = {"complete", "partial", "missing"}

    def __new__(cls, value: str) -> "CompletenessStatus":
        if value not in cls._VALUES:
            raise ValueError(f"Invalid CompletenessStatus: {value!r}")
        return super().__new__(cls, value)


COMPLETENESS_COMPLETE = "complete"
COMPLETENESS_PARTIAL = "partial"
COMPLETENESS_MISSING = "missing"
VALID_COMPLETENESS = frozenset({COMPLETENESS_COMPLETE, COMPLETENESS_PARTIAL, COMPLETENESS_MISSING})


class PopulationMethod(str):
    """How an Annex IV element was populated."""
    capsule_derived = "capsule_derived"
    operator_declared = "operator_declared"
    not_applicable = "not_applicable"

    _VALUES = {"capsule_derived", "operator_declared", "not_applicable"}


POPULATION_CAPSULE = "capsule_derived"
POPULATION_OPERATOR = "operator_declared"
POPULATION_NA = "not_applicable"
VALID_POPULATION = frozenset({POPULATION_CAPSULE, POPULATION_OPERATOR, POPULATION_NA})


class AnnexIVElement(BaseModel):
    """A single EU AI Act Annex IV element.

    Elements 1-15 per EU AI Act 2024/1689.
    """

    element_id: int = Field(..., ge=1, le=15, description="Annex IV element number 1-15")
    element_title: str = Field(..., description="Short title of the element")
    content: str | None = Field(
        default=None,
        description="Populated content for this element (None if not available)",
    )
    evidence_refs: list[str] = Field(
        default_factory=list,
        description="References to capsule IDs, eval results, or other evidence",
    )
    population_method: str = Field(
        ...,
        description=(
            "How this element was populated: "
            "capsule_derived|operator_declared|not_applicable"
        ),
    )
    completeness_flag: str = Field(
        ...,
        description="complete|partial|missing",
    )
    evidence_source: EvidenceSource | None = Field(
        default=None,
        description="ADR-0197 provenance marker for this element (I-1).",
    )
    evidence_ref: EvidenceSourceRef | None = Field(
        default=None,
        description="Re-performable reference; required when capsule_verified (I-3).",
    )


class AnnexIVDocument(BaseModel):
    """A complete EU AI Act Annex IV technical documentation package.

    Covers all 15 mandatory elements per EU AI Act 2024/1689 Annex IV.
    """

    deployment_id: str = Field(
        ...,
        description="Operator-assigned identifier for the AI system deployment",
    )
    generated_at: str = Field(
        ...,
        description="ISO 8601 UTC timestamp of document generation",
    )
    elements: list[AnnexIVElement] = Field(
        ...,
        min_length=15,
        max_length=15,
        description="All 15 Annex IV elements",
    )
    schema_ref: str = Field(
        default="novafabric/schemas/annex-iv-document.schema.json",
        description="JSON Schema reference for validation",
    )
    context: str = Field(
        default="https://schema.org/",
        alias="@context",
        description="JSON-LD context",
    )
    document_type: str = Field(
        default="AnnexIVDocument",
        alias="@type",
        description="JSON-LD type",
    )

    model_config = {"populate_by_name": True}

    def completeness_summary(self) -> list["CompletenessSummaryEntry"]:
        """Return a summary of element completeness."""
        return [
            CompletenessSummaryEntry(
                field_name=f"element_{e.element_id}",
                status=e.completeness_flag,
                reason=(
                    f"Element {e.element_id}: {e.element_title} — {e.population_method}"
                ),
                evidence_source=e.evidence_source,
                evidence_ref=e.evidence_ref,
            )
            for e in self.elements
        ]


class CompletenessSummaryEntry(BaseModel):
    """A single entry in a completeness summary.

    Carries the ADR-0197 ``evidence_source`` provenance marker. The marker is
    additive and optional on the wire (a pre-ADR-0197 envelope deserializes with
    ``evidence_source is None``); every shipped exporter populates it, and the
    export path enforces presence via
    :func:`novafabric.compliance.export.provenance.validate_marked`.
    """

    field_name: str
    status: str
    reason: str
    evidence_source: EvidenceSource | None = Field(
        default=None,
        description=(
            "ADR-0197 provenance marker: operator_asserted | capsule_verified | "
            "unverifiable. None only for legacy (pre-ADR-0197) envelopes."
        ),
    )
    evidence_ref: EvidenceSourceRef | None = Field(
        default=None,
        description="Re-performable reference; required when capsule_verified (I-3).",
    )


class NIS2IncidentReport(BaseModel):
    """NIS2 incident report (cap-005), Phases 1-3.

    Fields requiring cap-006 (cross-session lineage) are marked ``missing``
    with reason ``requires_cap_006_cross_session_lineage``.
    """

    # ------------------------------------------------------------------
    # Phase 1 (initial notification — NIS2 Art. 23(3))
    # ------------------------------------------------------------------

    incident_id: str = Field(..., description="Operator-assigned incident identifier")
    first_detection_timestamp: str = Field(
        ...,
        description="ISO 8601 UTC timestamp of first incident detection",
    )
    preliminary_classification: str = Field(
        ...,
        description=(
            "Preliminary incident classification per NIS2 taxonomy "
            "(e.g. 'unauthorized_tool_use', 'data_exfiltration_attempt')"
        ),
    )
    preliminary_severity_indicator: str = Field(
        ...,
        description="Severity: critical|high|medium|low",
    )
    cross_border_dimensions_flag: bool = Field(
        ...,
        description="True if the incident has potential cross-border impact (NIS2 Art. 23(6))",
    )

    # ------------------------------------------------------------------
    # Phase 2 (detailed report — NIS2 Art. 23(4))
    # ------------------------------------------------------------------

    initial_root_cause_path: list[str] | None = Field(
        default=None,
        description=(
            "Ordered list of capsule IDs forming the root-cause chain. "
            "Marked missing if cap-006 is not available."
        ),
    )
    initial_scope_estimate: str | None = Field(
        default=None,
        description="Narrative estimate of affected scope",
    )
    affected_systems_list: list[str] | None = Field(
        default=None,
        description="Asset names or IDs of affected systems",
    )
    actions_taken: list[str] | None = Field(
        default=None,
        description=(
            "Actions taken in response.  Populated from denied tool_permission_event "
            "records where available."
        ),
    )

    # ------------------------------------------------------------------
    # Phase 3 (final report — NIS2 Art. 23(5))
    # ------------------------------------------------------------------

    final_root_cause: str | None = Field(
        default=None,
        description="Final root cause determination",
    )
    cross_border_impact_assessment: str | None = Field(
        default=None,
        description="Assessment of cross-border impact (NIS2 Art. 23(6))",
    )
    lessons_learned: str | None = Field(
        default=None,
        description="Post-incident lessons learned narrative",
    )

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    completeness_summary: list[CompletenessSummaryEntry] = Field(
        default_factory=list,
        description="Completeness status of each field",
    )
    generated_at: str = Field(
        ...,
        description="ISO 8601 UTC timestamp of report generation",
    )
    report_phase: int = Field(
        ...,
        ge=1,
        le=3,
        description="NIS2 reporting phase: 1 (initial) | 2 (detailed) | 3 (final)",
    )
