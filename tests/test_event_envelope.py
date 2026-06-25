"""Tests for EventEnvelope v1 — Pydantic models, JSON Schema validation, sha256 pin."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from ulid import ULID

from novafabric.envelope import (
    EventEnvelope,
    EventEnvelopeValidationError,
    EventType,
    validate_event,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
_SPAN_ID = "00f067aa0ba902b7"
_SIGNING_KEY_UUID = "12345678-1234-1234-1234-123456789abc"
# 88-char base64 string (valid length for Ed25519 signature)
_BATCH_SIG = "A" * 88


def _ulid() -> str:
    return str(ULID())


def _minimal_event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "envelope_version": "1",
        "schema_version": "event-envelope/1",
        "event_id": _ulid(),
        "global_run_id": _ulid(),
        "run_id": _ulid(),
        "parent_run_id": None,
        "event_type": "run.start",
        "trace_id": _TRACE_ID,
        "span_id": _SPAN_ID,
        "agent_id": "test-agent-v1",
        "cluster_id": None,
        "tenant_id": None,
        "started_at": "2026-05-12T09:00:00.123Z",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# EventType enum
# ---------------------------------------------------------------------------


def test_event_type_values() -> None:
    assert EventType.run_start == "run.start"
    assert EventType.run_end == "run.end"
    assert EventType.model_call == "model_call"
    assert EventType.tool_call == "tool_call"
    assert EventType.span == "span"
    assert EventType.capsule_finalize == "capsule.finalize"


def test_event_type_is_str_enum() -> None:
    assert isinstance(EventType.model_call, str)
    assert EventType.model_call == "model_call"


# ---------------------------------------------------------------------------
# Valid event construction
# ---------------------------------------------------------------------------


def test_minimal_event_valid() -> None:
    e = EventEnvelope(**_minimal_event())
    assert e.envelope_version == "1"
    assert e.schema_version == "event-envelope/1"
    assert e.event_type == EventType.run_start
    assert e.parent_run_id is None
    assert e.cluster_id is None
    assert e.tenant_id is None


def test_full_event_valid() -> None:
    uid = _ulid()
    parent_uid = _ulid()
    e = EventEnvelope(**_minimal_event(
        event_id=uid,
        parent_run_id=parent_uid,
        event_type="model_call",
        cluster_id="hpc-cluster-01",
        tenant_id="acme-corp",
        emitter_node_id="node-gpu-042",
        payload={"gen_ai.system": "anthropic", "gen_ai.request.model": "claude-sonnet-4-6"},
        payload_hash="sha256:" + "a" * 64,
    ))
    assert e.event_id == uid
    assert e.parent_run_id == parent_uid
    assert e.event_type == EventType.model_call
    assert e.cluster_id == "hpc-cluster-01"
    assert e.emitter_node_id == "node-gpu-042"
    assert e.payload is not None
    assert e.payload_hash is not None


def test_global_run_id_accepts_ulid() -> None:
    uid = _ulid()
    e = EventEnvelope(**_minimal_event(global_run_id=uid))
    assert e.global_run_id == uid


def test_global_run_id_accepts_uuid_v7() -> None:
    uuid_v7 = "01958b3d-7a2f-7000-8001-123456789abc"
    e = EventEnvelope(**_minimal_event(global_run_id=uuid_v7))
    assert e.global_run_id == uuid_v7


def test_batch_signature_both_set() -> None:
    e = EventEnvelope(**_minimal_event(**{
        "nova.batch.signature": _BATCH_SIG,
        "nova.batch.signing_key_id": _SIGNING_KEY_UUID,
    }))
    assert e.nova_batch_signature == _BATCH_SIG
    assert e.nova_batch_signing_key_id == _SIGNING_KEY_UUID


def test_batch_signature_both_none() -> None:
    e = EventEnvelope(**_minimal_event())
    assert e.nova_batch_signature is None
    assert e.nova_batch_signing_key_id is None


def test_all_event_types_accepted() -> None:
    for et in EventType:
        e = EventEnvelope(**_minimal_event(event_type=et.value))
        assert e.event_type == et


def test_unknown_fields_tolerated() -> None:
    data = _minimal_event()
    data["future_field_v2"] = "some-value"
    data["x_custom"] = 42
    e = EventEnvelope(**data)
    assert e.envelope_version == "1"


def test_to_wire_uses_dot_aliases() -> None:
    e = EventEnvelope(**_minimal_event(**{
        "nova.batch.signature": _BATCH_SIG,
        "nova.batch.signing_key_id": _SIGNING_KEY_UUID,
    }))
    wire = e.to_wire()
    assert "nova.batch.signature" in wire
    assert "nova.batch.signing_key_id" in wire
    assert wire["nova.batch.signature"] == _BATCH_SIG


# ---------------------------------------------------------------------------
# Field validation — required field failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", [
    "envelope_version",
    "schema_version",
    "event_id",
    "global_run_id",
    "run_id",
    "parent_run_id",
    "event_type",
    "trace_id",
    "span_id",
    "agent_id",
    "cluster_id",
    "tenant_id",
    "started_at",
])
def test_missing_required_field_raises(field: str) -> None:
    data = _minimal_event()
    del data[field]
    with pytest.raises(ValidationError):
        EventEnvelope(**data)


# ---------------------------------------------------------------------------
# envelope_version / schema_version constraints
# ---------------------------------------------------------------------------


def test_wrong_envelope_version_rejected() -> None:
    with pytest.raises(ValidationError, match="envelope_version"):
        EventEnvelope(**_minimal_event(envelope_version="2"))


def test_wrong_schema_version_rejected() -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        EventEnvelope(**_minimal_event(schema_version="event-envelope/2"))


# ---------------------------------------------------------------------------
# ULID validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_ulid", [
    "",
    "not-a-ulid",
    "01HWXYZ123456789012345",  # too short (22 chars)
    "01HWXYZ12345678901234567",  # 24 chars
    "8HWXYZ1234567890ABCDEFGH",  # first char > 7
    "01HWXYZ1234567890ABCDEFGI",  # I is not Crockford Base32
])
def test_invalid_event_id_rejected(bad_ulid: str) -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(**_minimal_event(event_id=bad_ulid))


def test_invalid_run_id_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(**_minimal_event(run_id="not-valid"))


def test_invalid_parent_run_id_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(**_minimal_event(parent_run_id="not-valid"))


def test_invalid_global_run_id_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(**_minimal_event(global_run_id="bad-id"))


# ---------------------------------------------------------------------------
# trace_id / span_id validation
# ---------------------------------------------------------------------------


def test_invalid_trace_id_format_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(**_minimal_event(trace_id="UPPERCASE1234567890123456789012"))


def test_all_zeros_trace_id_rejected() -> None:
    with pytest.raises(ValidationError, match="all-zeros"):
        EventEnvelope(**_minimal_event(trace_id="0" * 32))


def test_invalid_span_id_format_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(**_minimal_event(span_id="BADSPAN1"))


def test_all_zeros_span_id_rejected() -> None:
    with pytest.raises(ValidationError, match="all-zeros"):
        EventEnvelope(**_minimal_event(span_id="0" * 16))


def test_trace_id_wrong_length_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(**_minimal_event(trace_id="abc123"))


def test_span_id_wrong_length_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(**_minimal_event(span_id="abc"))


# ---------------------------------------------------------------------------
# agent_id validation
# ---------------------------------------------------------------------------


def test_agent_id_with_space_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(**_minimal_event(agent_id="agent with space"))


def test_agent_id_with_tab_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(**_minimal_event(agent_id="agent\twith\ttab"))


def test_agent_id_with_control_char_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(**_minimal_event(agent_id="agent\x00null"))


def test_agent_id_empty_string_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(**_minimal_event(agent_id=""))


# ---------------------------------------------------------------------------
# cluster_id / tenant_id validation
# ---------------------------------------------------------------------------


def test_cluster_id_empty_string_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(**_minimal_event(cluster_id=""))


def test_tenant_id_empty_string_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(**_minimal_event(tenant_id=""))


def test_cluster_id_null_accepted() -> None:
    e = EventEnvelope(**_minimal_event(cluster_id=None))
    assert e.cluster_id is None


def test_tenant_id_null_accepted() -> None:
    e = EventEnvelope(**_minimal_event(tenant_id=None))
    assert e.tenant_id is None


# ---------------------------------------------------------------------------
# payload_hash validation
# ---------------------------------------------------------------------------


def test_invalid_payload_hash_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(**_minimal_event(payload_hash="md5:abc123"))


def test_payload_hash_valid() -> None:
    h = "sha256:" + "a" * 64
    e = EventEnvelope(**_minimal_event(payload_hash=h))
    assert e.payload_hash == h


def test_payload_hash_none_accepted() -> None:
    e = EventEnvelope(**_minimal_event(payload_hash=None))
    assert e.payload_hash is None


# ---------------------------------------------------------------------------
# nova.batch.* validation
# ---------------------------------------------------------------------------


def test_batch_sig_wrong_length_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(**_minimal_event(**{
            "nova.batch.signature": "A" * 87,
            "nova.batch.signing_key_id": _SIGNING_KEY_UUID,
        }))


def test_batch_sig_set_without_key_id_rejected() -> None:
    with pytest.raises(ValidationError, match="both be set or both be null"):
        EventEnvelope(**_minimal_event(**{
            "nova.batch.signature": _BATCH_SIG,
            "nova.batch.signing_key_id": None,
        }))


def test_batch_key_id_set_without_sig_rejected() -> None:
    with pytest.raises(ValidationError, match="both be set or both be null"):
        EventEnvelope(**_minimal_event(**{
            "nova.batch.signature": None,
            "nova.batch.signing_key_id": _SIGNING_KEY_UUID,
        }))


def test_invalid_signing_key_id_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(**_minimal_event(**{
            "nova.batch.signature": _BATCH_SIG,
            "nova.batch.signing_key_id": "not-a-uuid",
        }))


# ---------------------------------------------------------------------------
# event_type validation
# ---------------------------------------------------------------------------


def test_invalid_event_type_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(**_minimal_event(event_type="unknown_type"))


# ---------------------------------------------------------------------------
# JSON Schema validation
# ---------------------------------------------------------------------------


def test_validate_event_valid() -> None:
    validate_event(_minimal_event())


def test_validate_event_with_payload() -> None:
    data = _minimal_event(
        event_type="model_call",
        payload={
            "gen_ai.system": "anthropic",
            "gen_ai.request.model": "claude-sonnet-4-6",
            "gen_ai.usage.input_tokens": 1024,
        },
        emitter_node_id="node-gpu-042",
    )
    validate_event(data)


def test_validate_event_missing_required_field_raises() -> None:
    data = _minimal_event()
    del data["event_id"]
    with pytest.raises(EventEnvelopeValidationError):
        validate_event(data)


def test_validate_event_wrong_envelope_version_raises() -> None:
    data = _minimal_event(envelope_version="2")
    with pytest.raises(EventEnvelopeValidationError):
        validate_event(data)


def test_validate_event_invalid_trace_id_raises() -> None:
    data = _minimal_event(trace_id="UPPERCASE12345678901234567890AB")
    with pytest.raises(EventEnvelopeValidationError):
        validate_event(data)


def test_validate_event_invalid_event_type_raises() -> None:
    data = _minimal_event(event_type="bad_type")
    with pytest.raises(EventEnvelopeValidationError):
        validate_event(data)


def test_validate_event_unknown_fields_tolerated() -> None:
    data = _minimal_event()
    data["future_field"] = "allowed"
    validate_event(data)


def test_validate_event_invalid_ulid_raises() -> None:
    data = _minimal_event(event_id="not-a-ulid")
    with pytest.raises(EventEnvelopeValidationError):
        validate_event(data)


def test_validate_event_error_has_path() -> None:
    data = _minimal_event(envelope_version="99")
    with pytest.raises(EventEnvelopeValidationError) as exc_info:
        validate_event(data)
    assert exc_info.value.path == "envelope_version" or exc_info.value.path == ""


# ---------------------------------------------------------------------------
# SHA-256 version pin integrity
# ---------------------------------------------------------------------------


def test_schema_sha256_pin_matches_file() -> None:
    schema_dir = Path(__file__).parent.parent / "schemas" / "event-envelope-v1"
    json_file = schema_dir / "envelope-v1.json"
    sha_file = schema_dir / "envelope-v1.sha256"
    assert json_file.exists(), "envelope-v1.json must exist"
    assert sha_file.exists(), "envelope-v1.sha256 must exist"

    content = json_file.read_bytes()
    if content.endswith(b"\n"):
        content = content[:-1]
    actual_hash = hashlib.sha256(content).hexdigest()

    expected_hash = sha_file.read_text(encoding="utf-8").strip()
    assert actual_hash == expected_hash, (
        f"SHA-256 pin mismatch: expected {expected_hash!r}, got {actual_hash!r}. "
        "Update envelope-v1.sha256 to match the current schema."
    )


# ---------------------------------------------------------------------------
# Schema file structure
# ---------------------------------------------------------------------------


def test_schema_file_is_valid_json() -> None:
    schema_dir = Path(__file__).parent.parent / "schemas" / "event-envelope-v1"
    schema = json.loads((schema_dir / "envelope-v1.json").read_text())
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["title"] == "EventEnvelope"


def test_schema_required_fields_complete() -> None:
    schema_dir = Path(__file__).parent.parent / "schemas" / "event-envelope-v1"
    schema = json.loads((schema_dir / "envelope-v1.json").read_text())
    required = set(schema["required"])
    expected = {
        "envelope_version", "schema_version", "event_id", "global_run_id",
        "run_id", "parent_run_id", "event_type", "trace_id", "span_id",
        "agent_id", "cluster_id", "tenant_id", "started_at",
    }
    assert required == expected


def test_proto_file_exists() -> None:
    proto = Path(__file__).parent.parent / "schemas" / "event-envelope-v1" / "envelope-v1.proto"
    assert proto.exists()
    content = proto.read_text()
    assert "message EventEnvelope" in content
    assert "syntax = \"proto3\"" in content
