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
"""Pydantic models for the compliance audit control-mapping engine."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class EvidenceRequirement(BaseModel):
    """A single evidence requirement for a compliance control."""

    evidence_type: str = Field(
        ...,
        description=(
            "Evidence type token: model_calls | tool_permissions | pii_scan | "
            "policy_events | seal_signature | lineage | tool_calls"
        ),
    )
    required: bool = Field(default=True, description="Whether this evidence is mandatory")
    description: str = Field(..., description="Human-readable description of this requirement")


class Control(BaseModel):
    """A single compliance control with its evidence requirements."""

    id: str = Field(..., description="Control identifier, e.g. NIST-MAP-1.1")
    title: str = Field(..., description="Short title of the control")
    description: str = Field(..., description="Full description of the control")
    framework: str = Field(..., description="Regulatory framework name, e.g. NIST AI RMF")
    evidence_requirements: list[EvidenceRequirement] = Field(
        default_factory=list,
        description="Evidence types required to satisfy this control",
    )
    weight: float = Field(default=1.0, description="Relative weight for scoring (>0)")


class ControlProfile(BaseModel):
    """A compliance profile: a named collection of controls for a framework."""

    id: str = Field(..., description="Machine-readable profile identifier")
    name: str = Field(..., description="Human-readable profile name")
    framework: str = Field(..., description="Regulatory framework name")
    version: str = Field(..., description="Framework version string")
    controls: list[Control] = Field(..., description="Controls in this profile")


class ControlCoverage(BaseModel):
    """Coverage result for a single control."""

    control_id: str = Field(..., description="Control identifier")
    control_title: str = Field(..., description="Control title")
    status: Literal["covered", "partial", "missing", "not_applicable"] = Field(
        ..., description="Coverage status"
    )
    coverage_score: float = Field(
        ..., ge=0.0, le=1.0, description="Coverage fraction from 0.0 to 1.0"
    )
    evidence_found: list[str] = Field(
        default_factory=list,
        description="Descriptions of evidence found for this control",
    )
    evidence_missing: list[str] = Field(
        default_factory=list,
        description="Descriptions of evidence missing for this control",
    )
    evidence_not_captured: list[str] = Field(
        default_factory=list,
        description=(
            "Evidence this NovaFabric version does not capture (no producer writes "
            "it) and no scanned capsule carries. Not scored as missing; a control "
            "made only of such evidence is 'not_applicable'."
        ),
    )
    capsule_ids: list[str] = Field(
        default_factory=list,
        description="Capsule IDs contributing evidence for this control",
    )


#: Finding codes that GATE: ``nova audit map|coverage`` exit 1 and the coverage route's
#: ``gate_passed`` is false. Every consumer asks :attr:`AuditFinding.blocking` (or
#: :attr:`AuditReport.blocking_findings`) instead of testing "is there a finding", so a new
#: code is non-gating until it is added here on purpose.
BLOCKING_FINDING_CODES: frozenset[str] = frozenset({"seal_invalid"})


class AuditFinding(BaseModel):
    """Something the audit found that is not a coverage gap (additive, optional).

    * ``seal_invalid`` (blocking): a capsule carries a ``.seal`` that fails verification
      (edited after sealing, a damaged envelope, a linked or non-directory ``.seal``).
      That is evidence of tampering, which "no seal evidence" does not say.
    * ``seal_unbound`` (not blocking): the ``.seal`` verifies but does not name some or all
      of the capsule's evidence streams (it was made before ADR-0251 and signs the manifest
      only, or a stream was added after sealing), so the seal could not show an edit to
      them. It does not say the capsule was edited.

    A capsule with no ``.seal`` at all is not a finding.
    """

    code: Literal["seal_invalid", "seal_unbound"] = Field(..., description="Finding code")
    capsule_id: str = Field(..., description="Name of the capsule directory the finding is about")
    message: str = Field(..., description="Human-readable explanation")

    @property
    def blocking(self) -> bool:
        """True when this finding fails a gate (see :data:`BLOCKING_FINDING_CODES`)."""
        return self.code in BLOCKING_FINDING_CODES


class AuditReport(BaseModel):
    """Full audit report mapping evidence to controls across a data directory."""

    report_id: str = Field(..., description="Unique report identifier (ULID)")
    generated_at: datetime = Field(..., description="UTC timestamp of report generation")
    profile_id: str = Field(..., description="Profile identifier used for this audit")
    profile_name: str = Field(..., description="Human-readable profile name")
    framework: str = Field(..., description="Regulatory framework")
    data_dir: str = Field(..., description="Absolute path to the scanned data directory")
    capsule_count: int = Field(..., ge=0, description="Number of capsules scanned")
    total_controls: int = Field(..., ge=0, description="Total controls in the profile")
    covered_controls: int = Field(..., ge=0, description="Controls with full coverage")
    partial_controls: int = Field(..., ge=0, description="Controls with partial coverage")
    missing_controls: int = Field(..., ge=0, description="Controls with no coverage")
    not_captured_controls: int = Field(
        default=0,
        ge=0,
        description=(
            "Controls reported 'not_applicable' because all their evidence is not "
            "captured by this version; excluded from overall_score"
        ),
    )
    overall_score: float = Field(
        ..., ge=0.0, le=1.0, description="Weighted overall coverage score"
    )
    coverages: list[ControlCoverage] = Field(
        ..., description="Per-control coverage details"
    )
    evidence_seal_verified: bool = Field(
        default=False,
        description=(
            "True if at least one capsule's NovaSeal seal verified when the report was "
            "generated (signature + content binding; signer self-asserted). A claim of "
            "the report: the report itself is not signed."
        ),
    )
    findings: list[AuditFinding] = Field(
        default_factory=list,
        description=(
            "Explicit findings beyond coverage gaps. A capsule whose .seal exists but "
            "fails verification is reported here as seal_invalid (blocking), separately "
            "from a capsule that was never sealed (which is only missing seal evidence). "
            "A seal that verifies but does not bind the evidence streams is reported as "
            "seal_unbound (not blocking)."
        ),
    )
    bundle_path: str | None = Field(
        default=None,
        description="Path to audit evidence bundle ZIP (if generated)",
    )

    # JSON-LD metadata
    context: str = Field(
        default="https://schema.org/",
        alias="@context",
        description="JSON-LD context",
    )
    document_type: str = Field(
        default="AuditReport",
        alias="@type",
        description="JSON-LD type",
    )

    model_config = {"populate_by_name": True}

    @property
    def blocking_findings(self) -> list[AuditFinding]:
        """The findings that fail a gate; ``seal_unbound`` is listed in ``findings`` only."""
        return [f for f in self.findings if f.blocking]
