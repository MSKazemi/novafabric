"""Tests for CloudEvents v1.0 interop on EventEnvelope (ADR-0081, gap-009).

Covers:
  * to_cloudevents() produces a spec-valid CloudEvents structured-mode JSON
    object (required attributes present, conformant extension names).
  * from_cloudevents() round-trips back to an equivalent EventEnvelope.
  * Unknown CloudEvents extension attributes are preserved across a round-trip.

The attribute contract mirrors collector/pkg/envelope/cloudevents_mapping.go so
Python <-> Go interop holds over the same Kafka/NATS topic.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from ulid import ULID

from novafabric.envelope import EventEnvelope, EventType
from novafabric.envelope.cloudevents import from_cloudevents, to_cloudevents

_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
_SPAN_ID = "00f067aa0ba902b7"
_SIGNING_KEY_UUID = "12345678-1234-1234-1234-123456789abc"
_BATCH_SIG = "A" * 88

# CloudEvents required context attributes (spec v1.0.2).
_CE_REQUIRED = {"id", "source", "type", "specversion"}
# CloudEvents attribute-name rule: lowercase [a-z0-9], begins with a letter.
_CE_NAME_RE = re.compile(r"^[a-z][a-z0-9]*$")


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


def _full_event() -> EventEnvelope:
    return EventEnvelope(**_minimal_event(
        event_type="model_call",
        parent_run_id=_ulid(),
        cluster_id="hpc-cluster-01",
        tenant_id="acme-corp",
        emitter_node_id="node-gpu-042",
        payload={"gen_ai.system": "anthropic", "gen_ai.request.model": "claude-sonnet-4-6"},
        payload_hash="sha256:" + "a" * 64,
        **{
            "nova.batch.signature": _BATCH_SIG,
            "nova.batch.signing_key_id": _SIGNING_KEY_UUID,
        },
    ))


# ---------------------------------------------------------------------------
# to_cloudevents — required attributes + spec validity
# ---------------------------------------------------------------------------


def test_to_cloudevents_has_required_attributes() -> None:
    ce = to_cloudevents(EventEnvelope(**_minimal_event()))
    assert _CE_REQUIRED.issubset(ce.keys())
    assert ce["specversion"] == "1.0"


def test_to_cloudevents_id_is_event_id() -> None:
    e = EventEnvelope(**_minimal_event())
    ce = to_cloudevents(e)
    assert ce["id"] == e.event_id


def test_to_cloudevents_source_is_agent_uri() -> None:
    ce = to_cloudevents(EventEnvelope(**_minimal_event(agent_id="test-agent-v1")))
    assert ce["source"] == "nova://agent/test-agent-v1"


def test_to_cloudevents_type_is_event_type() -> None:
    ce = to_cloudevents(EventEnvelope(**_minimal_event(event_type="tool_call")))
    assert ce["type"] == "tool_call"


def test_to_cloudevents_time_is_started_at() -> None:
    e = EventEnvelope(**_minimal_event())
    ce = to_cloudevents(e)
    assert ce["time"] == e.started_at


def test_to_cloudevents_datacontenttype_and_dataschema() -> None:
    ce = to_cloudevents(EventEnvelope(**_minimal_event()))
    assert ce["datacontenttype"] == "application/json"
    assert ce["dataschema"] == "https://novafabric.io/schemas/event-envelope/1"


def test_to_cloudevents_traceparent_extension() -> None:
    ce = to_cloudevents(EventEnvelope(**_minimal_event()))
    assert ce["traceparent"] == f"00-{_TRACE_ID}-{_SPAN_ID}-01"


def test_to_cloudevents_run_extensions_present() -> None:
    e = EventEnvelope(**_minimal_event())
    ce = to_cloudevents(e)
    assert ce["novarunglobal"] == e.global_run_id
    assert ce["novarun"] == e.run_id


def test_to_cloudevents_payload_is_data() -> None:
    payload = {"k": "v", "n": 1}
    ce = to_cloudevents(EventEnvelope(**_minimal_event(event_type="model_call", payload=payload)))
    assert ce["data"] == payload


def test_to_cloudevents_omits_null_optionals() -> None:
    # minimal event has null parent_run_id/cluster_id/tenant_id/payload etc.
    ce = to_cloudevents(EventEnvelope(**_minimal_event()))
    for absent in (
        "novaparentrun", "novacluster", "novatenant",
        "novaemitter", "novapayloadhash", "novabatchsig", "novabatchkid",
    ):
        assert absent not in ce, f"{absent} must be omitted when its field is null"


def test_to_cloudevents_full_event_optionals_present() -> None:
    ce = to_cloudevents(_full_event())
    assert ce["novaparentrun"]
    assert ce["novacluster"] == "hpc-cluster-01"
    assert ce["novatenant"] == "acme-corp"
    assert ce["novaemitter"] == "node-gpu-042"
    assert ce["novapayloadhash"].startswith("sha256:")
    assert ce["novabatchsig"] == _BATCH_SIG
    assert ce["novabatchkid"] == _SIGNING_KEY_UUID


def test_to_cloudevents_all_extension_names_are_ce_conformant() -> None:
    ce = to_cloudevents(_full_event())
    reserved = {
        "id", "source", "type", "specversion", "time",
        "datacontenttype", "dataschema", "subject", "data",
    }
    for key in ce:
        if key in reserved:
            continue
        assert _CE_NAME_RE.match(key), f"extension name {key!r} is not CloudEvents-conformant"


# ---------------------------------------------------------------------------
# from_cloudevents — round-trip
# ---------------------------------------------------------------------------


def test_round_trip_minimal() -> None:
    original = EventEnvelope(**_minimal_event())
    restored = from_cloudevents(to_cloudevents(original))
    assert restored.event_id == original.event_id
    assert restored.agent_id == original.agent_id
    assert restored.event_type == original.event_type
    assert restored.trace_id == original.trace_id
    assert restored.span_id == original.span_id
    assert restored.global_run_id == original.global_run_id
    assert restored.run_id == original.run_id
    assert restored.started_at == original.started_at


def test_round_trip_full() -> None:
    original = _full_event()
    restored = from_cloudevents(to_cloudevents(original))
    assert restored.parent_run_id == original.parent_run_id
    assert restored.cluster_id == original.cluster_id
    assert restored.tenant_id == original.tenant_id
    assert restored.emitter_node_id == original.emitter_node_id
    assert restored.payload == original.payload
    assert restored.payload_hash == original.payload_hash
    assert restored.nova_batch_signature == original.nova_batch_signature
    assert restored.nova_batch_signing_key_id == original.nova_batch_signing_key_id


def test_round_trip_yields_valid_envelope() -> None:
    restored = from_cloudevents(to_cloudevents(_full_event()))
    # A second to_wire must succeed (model invariants intact).
    assert restored.to_wire()["envelope_version"] == "1"
    assert restored.event_type == EventType.model_call


def test_from_cloudevents_rejects_bad_source() -> None:
    ce = to_cloudevents(EventEnvelope(**_minimal_event()))
    ce["source"] = "https://example.com/not-an-agent"
    with pytest.raises(ValueError, match="nova://agent/"):
        from_cloudevents(ce)


def test_from_cloudevents_missing_required_attribute_raises() -> None:
    ce = to_cloudevents(EventEnvelope(**_minimal_event()))
    del ce["id"]
    with pytest.raises(ValueError, match="id"):
        from_cloudevents(ce)


# ---------------------------------------------------------------------------
# Unknown extension attributes — preserved/ignored safely
# ---------------------------------------------------------------------------


def test_unknown_extension_preserved_across_round_trip() -> None:
    ce = to_cloudevents(EventEnvelope(**_minimal_event()))
    # A broker injects its own extension attribute.
    ce["partitionkey"] = "shard-7"
    ce["myvendorext"] = "abc"
    restored = from_cloudevents(ce)
    re_emitted = to_cloudevents(restored)
    assert re_emitted["partitionkey"] == "shard-7"
    assert re_emitted["myvendorext"] == "abc"


def test_unknown_extension_does_not_break_known_fields() -> None:
    original = EventEnvelope(**_minimal_event())
    ce = to_cloudevents(original)
    ce["partitionkey"] = "shard-7"
    restored = from_cloudevents(ce)
    assert restored.event_id == original.event_id
    assert restored.run_id == original.run_id


def test_no_unknown_extension_means_no_extras_block() -> None:
    restored = from_cloudevents(to_cloudevents(EventEnvelope(**_minimal_event())))
    # Re-emitting introduces no spurious extension attributes.
    re_emitted = to_cloudevents(restored)
    known = {
        "id", "source", "type", "specversion", "time",
        "datacontenttype", "dataschema", "data", "traceparent",
        "novarunglobal", "novarun",
    }
    assert set(re_emitted.keys()).issubset(known)
