"""Evidence-Grounded Safety-Case Compiler core (ADR-0095, slices C0–C1).

Assembles a Claims-Arguments-Evidence (CAE) tree from artifacts NovaFabric already
produces, assigning every claim a backing state mechanically from evidence
statistics so honesty is structural, not editorial. Carried *inside* the existing
Evidence Bundle — not a third top-level format (ADR-0034).
"""

from __future__ import annotations

from novafabric.safetycase.models import (
    SAFETY_CASE_SCHEMA_VERSION,
    ArgumentStrategy,
    ArtifactRef,
    BackingState,
    Claim,
    ClaimKind,
    Confidence,
    ConfidenceMethod,
    EvalContext,
    Evidence,
    EvidenceKind,
    InferenceType,
    ProducerInfo,
    ResidualRisk,
    ResidualRiskBasis,
    SafetyCase,
    Subject,
    SubjectKind,
    canonical_json,
    not_quantified_risk,
)

__all__ = [
    "SAFETY_CASE_SCHEMA_VERSION",
    "ArgumentStrategy",
    "ArtifactRef",
    "BackingState",
    "Claim",
    "ClaimKind",
    "Confidence",
    "ConfidenceMethod",
    "EvalContext",
    "Evidence",
    "EvidenceKind",
    "InferenceType",
    "ProducerInfo",
    "ResidualRisk",
    "ResidualRiskBasis",
    "SafetyCase",
    "Subject",
    "SubjectKind",
    "canonical_json",
    "not_quantified_risk",
]
