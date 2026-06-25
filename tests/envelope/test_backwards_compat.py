"""Backwards compatibility regression contract for EventEnvelope v1.

These tests must pass for every v1-compatible reader.  They define the
forward-compatibility guarantee: a v1 reader MUST tolerate unknown additional
fields from future versions and MUST reject unknown envelope_version values.

See also: schemas/event-envelope-v1/envelope-v1.json (additionalProperties: true)
"""

from __future__ import annotations

import pytest

from novafabric.envelope.validator import EventEnvelopeValidationError, validate_event

# ---------------------------------------------------------------------------
# Minimal valid v1 envelope payload (all required fields).
# Derived from schemas/event-envelope-v1/envelope-v1.json required[] list.
# ---------------------------------------------------------------------------

_VALID_V1: dict[str, object] = {
    "envelope_version": "1",
    "schema_version": "event-envelope/1",
    "event_id": "01JVJD00000000000000000001",
    "global_run_id": "01JVJD00000000000000000002",
    "run_id": "01JVJD00000000000000000002",
    "parent_run_id": None,
    "event_type": "run.start",
    "trace_id": "0" * 32,
    "span_id": "0" * 16,
    "agent_id": "compat-test-agent",
    "cluster_id": None,
    "tenant_id": None,
    "started_at": "2026-01-01T00:00:00Z",
}


# ---------------------------------------------------------------------------
# Forward-compatibility: unknown fields from future versions are tolerated
# ---------------------------------------------------------------------------


def test_unknown_top_level_fields_tolerated() -> None:
    """v1 reader must accept envelopes with additional unknown top-level fields."""
    event = {**_VALID_V1, "future_v2_field": "some_value"}
    validate_event(event)  # must not raise


def test_unknown_nested_fields_tolerated() -> None:
    """v1 reader must accept envelopes with additional nested unknown fields."""
    event = {**_VALID_V1, "x_extension": {"nested_key": 42, "another": [1, 2, 3]}}
    validate_event(event)  # must not raise


def test_multiple_unknown_fields_tolerated() -> None:
    """Multiple unknown fields from a hypothetical v2 are all tolerated together."""
    event = {
        **_VALID_V1,
        "sampling_rate": 0.5,
        "compression": "zstd",
        "v2_extensions": {"priority": "high"},
    }
    validate_event(event)  # must not raise


# ---------------------------------------------------------------------------
# Version gating: unknown envelope_version is rejected
# ---------------------------------------------------------------------------


def test_unknown_envelope_version_rejected() -> None:
    """envelope_version != '1' must be rejected by the v1 reader."""
    event = {**_VALID_V1, "envelope_version": "2"}
    with pytest.raises(EventEnvelopeValidationError):
        validate_event(event)


def test_numeric_envelope_version_rejected() -> None:
    """Numeric envelope_version (type mismatch) must be rejected."""
    event = {**_VALID_V1, "envelope_version": 1}  # type: ignore[dict-item]
    with pytest.raises(EventEnvelopeValidationError):
        validate_event(event)


def test_empty_envelope_version_rejected() -> None:
    """Empty string envelope_version must be rejected."""
    event = {**_VALID_V1, "envelope_version": ""}
    with pytest.raises(EventEnvelopeValidationError):
        validate_event(event)


# ---------------------------------------------------------------------------
# Baseline: valid v1 envelope passes without modification
# ---------------------------------------------------------------------------


def test_valid_v1_envelope_accepted() -> None:
    """Baseline: a well-formed v1 envelope is accepted by the validator."""
    validate_event(dict(_VALID_V1))
