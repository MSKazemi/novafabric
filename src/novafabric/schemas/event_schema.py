"""Capsule event schema: CapsuleEventType enum, CostFacet, RunEnvelope.

cap-001 / ADR-0066.  Schema version 1.0.0.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, field_validator


class CapsuleEventType(str, Enum):
    """Canonical capsule event types.

    25 baseline types (cap-001, ADR-0066) plus 8 extended span-taxonomy types
    (gap-011, ADR-0082). Additions are append-only and backward-compatible:
    no baseline value is renamed or removed, and the schema version stays
    ``1.0.0`` because widening the enum keeps all prior documents valid.
    """

    # --- Baseline 25 (ADR-0066) -------------------------------------------
    RunStarted = "RunStarted"
    RunCompleted = "RunCompleted"
    RunFailed = "RunFailed"
    RunAborted = "RunAborted"
    ModelCallStarted = "ModelCallStarted"
    ModelCallCompleted = "ModelCallCompleted"
    ModelCallFailed = "ModelCallFailed"
    ToolCallStarted = "ToolCallStarted"
    ToolCallCompleted = "ToolCallCompleted"
    ToolCallFailed = "ToolCallFailed"
    ToolPermissionGranted = "ToolPermissionGranted"
    ToolPermissionDenied = "ToolPermissionDenied"
    FileReadEvent = "FileReadEvent"
    FileWriteEvent = "FileWriteEvent"
    NetworkCallEvent = "NetworkCallEvent"
    ArtifactProduced = "ArtifactProduced"
    ArtifactConsumed = "ArtifactConsumed"
    EnvironmentLocked = "EnvironmentLocked"
    SecretRedacted = "SecretRedacted"
    PIIDetected = "PIIDetected"
    PolicyEvaluated = "PolicyEvaluated"
    HumanApprovalRequested = "HumanApprovalRequested"
    HumanApprovalGranted = "HumanApprovalGranted"
    HumanApprovalDenied = "HumanApprovalDenied"
    NovaSealApplied = "NovaSealApplied"

    # --- Extended span taxonomy (ADR-0082, gap-011) -----------------------
    StateTransition = "StateTransition"  # working-memory before/after (src-109)
    MemoryOperation = "MemoryOperation"  # memory read/write/update (src-109)
    GuardrailEvaluated = "GuardrailEvaluated"  # guardrail check fired (src-203)
    EvaluatorScored = "EvaluatorScored"  # in-trace evaluator score (src-203)
    RerankerApplied = "RerankerApplied"  # reranker reordered docs (src-203)
    VectorRetrievalStarted = "VectorRetrievalStarted"  # vector-DB query (src-113)
    VectorRetrievalCompleted = "VectorRetrievalCompleted"
    VectorRetrievalFailed = "VectorRetrievalFailed"


class CostFacet(BaseModel):
    """Per-model-call cost attribution facet (cap-002, ADR-0066).

    ``usage`` (ADR-0132, additive, optional) carries the full provider-reported
    token usage-type breakdown (``nova.usage`` shape: cached / cache-write /
    reasoning / audio / image tokens plus an open ``extra`` map). ``None``
    means the provider reported no per-type breakdown — absent, not zero.
    The three legacy scalars are unchanged; ``usage`` is a superset view.
    """

    model_id: str
    provider: str
    input_tokens: int
    completion_tokens: int
    cached_tokens: int = 0
    cost_usd_estimated: float
    energy_joules_estimated: float | None = None
    usage: dict[str, Any] | None = None


class RunEnvelope(BaseModel):
    """Minimal envelope attached to every capsule event (cap-001, ADR-0066)."""

    run_id: str
    event_type: CapsuleEventType
    tenant_id: str
    capsule_id: str
    timestamp: datetime
    schema_version: str = "1.0.0"
    allowed_processing_regions: list[str] = []

    @field_validator("event_type", mode="before")
    @classmethod
    def _validate_event_type(cls, v: object) -> object:
        if isinstance(v, str):
            try:
                return CapsuleEventType(v)
            except ValueError:
                valid = [e.value for e in CapsuleEventType]
                raise ValueError(
                    f"Unknown event_type={v!r}. Must be one of: {valid}"
                )
        return v
