"""Runtime guardrail & safety evidence (ADR-0145, experimental).

Record-only: NovaFabric records guardrail decisions made by other systems.
It never makes, enforces, or adjudicates them.
"""

from novafabric.safety.attempts import (
    AdversarialAttempt,
    EmptySpanError,
    InjectionAttempt,
    InvalidDigestError,
    JailbreakAttempt,
    RawAttemptTextRejectedError,
    SourceSpan,
    build_injection,
    build_jailbreak,
    digest_payload,
    resolve_artifact_linkage,
    verify_payload_binding,
)
from novafabric.safety.decisions import (
    DetectorProvenance,
    GuardrailDecision,
    SafetyFacet,
    UnmappableOutcomeError,
    attach_facet,
    build_facet,
    decision_from_event,
    digest_inputs,
    is_empty,
    verify_decision_binding,
)

__all__ = [
    "AdversarialAttempt",
    "DetectorProvenance",
    "EmptySpanError",
    "GuardrailDecision",
    "InjectionAttempt",
    "InvalidDigestError",
    "JailbreakAttempt",
    "RawAttemptTextRejectedError",
    "SafetyFacet",
    "SourceSpan",
    "UnmappableOutcomeError",
    "attach_facet",
    "build_facet",
    "build_injection",
    "build_jailbreak",
    "decision_from_event",
    "digest_inputs",
    "digest_payload",
    "is_empty",
    "resolve_artifact_linkage",
    "verify_decision_binding",
    "verify_payload_binding",
]
