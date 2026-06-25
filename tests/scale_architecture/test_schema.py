from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from novafabric.schemas.event_schema import (
    CapsuleEventType,
    CostFacet,
    RunEnvelope,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=timezone.utc)
_BASE_ENVELOPE = dict(
    run_id="run-001",
    event_type=CapsuleEventType.RunStarted,
    tenant_id="tenant-acme",
    capsule_id="cap-001",
    timestamp=_NOW,
)


# ---------------------------------------------------------------------------
# FR-1 tests
# ---------------------------------------------------------------------------


class TestCapsuleEventTypeEnum:
    """All 25 original event types (ADR-0066) must still exist in the enum.

    ADR-0082 (gap-011) appends 8 more; the extended set is covered by
    ``test_extended_span_taxonomy.py``. The originals are asserted here to
    guard backward compatibility (none renamed or removed).
    """

    EXPECTED = [
        "RunStarted",
        "RunCompleted",
        "RunFailed",
        "RunAborted",
        "ModelCallStarted",
        "ModelCallCompleted",
        "ModelCallFailed",
        "ToolCallStarted",
        "ToolCallCompleted",
        "ToolCallFailed",
        "ToolPermissionGranted",
        "ToolPermissionDenied",
        "FileReadEvent",
        "FileWriteEvent",
        "NetworkCallEvent",
        "ArtifactProduced",
        "ArtifactConsumed",
        "EnvironmentLocked",
        "SecretRedacted",
        "PIIDetected",
        "PolicyEvaluated",
        "HumanApprovalRequested",
        "HumanApprovalGranted",
        "HumanApprovalDenied",
        "NovaSealApplied",
    ]

    def test_original_25_present(self) -> None:
        # ADR-0066 baseline; total may grow additively (ADR-0082 -> 33).
        assert len(CapsuleEventType) >= 25
        for name in self.EXPECTED:
            assert CapsuleEventType(name).value == name

    @pytest.mark.parametrize("name", EXPECTED)
    def test_each_type_exists(self, name: str) -> None:
        assert CapsuleEventType(name).value == name


class TestRunEnvelopeValidation:
    def test_valid_envelope(self) -> None:
        env = RunEnvelope(**_BASE_ENVELOPE)
        assert env.run_id == "run-001"
        assert env.schema_version == "1.0.0"
        assert env.allowed_processing_regions == []

    def test_unknown_event_type_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            RunEnvelope(**{**_BASE_ENVELOPE, "event_type": "NonExistentEvent"})
        # The error message should identify the problematic field
        assert "event_type" in str(exc_info.value) or "NonExistentEvent" in str(exc_info.value)

    def test_allowed_processing_regions_is_list(self) -> None:
        env = RunEnvelope(**{**_BASE_ENVELOPE, "allowed_processing_regions": ["eu-west-1", "us-east-1"]})
        assert isinstance(env.allowed_processing_regions, list)
        assert len(env.allowed_processing_regions) == 2

    def test_string_event_type_coerced(self) -> None:
        env = RunEnvelope(**{**_BASE_ENVELOPE, "event_type": "RunCompleted"})
        assert env.event_type is CapsuleEventType.RunCompleted


class TestCostFacet:
    def test_all_mandatory_fields(self) -> None:
        facet = CostFacet(
            model_id="gpt-4o",
            provider="openai",
            input_tokens=100,
            completion_tokens=50,
            cost_usd_estimated=0.0025,
        )
        assert facet.cached_tokens == 0
        assert facet.energy_joules_estimated is None

    def test_missing_mandatory_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            CostFacet(  # type: ignore[call-arg]
                model_id="gpt-4o",
                provider="openai",
                input_tokens=100,
                # completion_tokens missing
                cost_usd_estimated=0.0025,
            )

    def test_energy_joules_optional(self) -> None:
        facet = CostFacet(
            model_id="claude-3-haiku",
            provider="anthropic",
            input_tokens=200,
            completion_tokens=80,
            cost_usd_estimated=0.00015,
            energy_joules_estimated=1.23,
        )
        assert facet.energy_joules_estimated == pytest.approx(1.23)
