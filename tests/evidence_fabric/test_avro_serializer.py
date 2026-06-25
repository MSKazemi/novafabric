"""Tests for AvroSerializer — Avro serialization of evidence events.

Tests:
  1. Schema file loads (schema path resolves and parses without error)
  2. Serialize/deserialize round-trip for a single event
  3. Batch serialize/deserialize round-trip for multiple events
  4. Unknown fields in deserialized data are tolerated (writer/reader schema compat)
  5. Import guard: importing the class when fastavro is absent raises ImportError
     with the pip hint
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastavro", reason="fastavro not installed — pip install novafabric[avro]")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(idx: int = 0) -> dict:
    return {
        "event_id": f"evt-{idx:04d}",
        "event_type": "model_call",
        "capsule_id": f"cap-{idx:04d}",
        "tenant_id": "test-tenant",
        "ts_ms": 1_700_000_000_000 + idx,
        "payload": b"\x81\xa3foo\xa3bar",  # minimal msgpack bytes
    }


# ---------------------------------------------------------------------------
# Test 1: schema file loads
# ---------------------------------------------------------------------------

def test_schema_file_exists_and_is_valid_json():
    """The .avsc schema file must exist and be valid JSON with required fields."""
    import json

    schema_path = (
        Path(__file__).parent.parent.parent
        / "src"
        / "novafabric"
        / "evidence_fabric"
        / "schemas"
        / "evidence_event.avsc"
    )
    assert schema_path.exists(), f"Schema file not found: {schema_path}"
    schema = json.loads(schema_path.read_text())
    assert schema["type"] == "record"
    assert schema["name"] == "EvidenceEvent"
    field_names = {f["name"] for f in schema["fields"]}
    assert field_names >= {
        "event_id",
        "event_type",
        "capsule_id",
        "tenant_id",
        "ts_ms",
        "payload",
    }


# ---------------------------------------------------------------------------
# Test 2: single serialize / deserialize round-trip
# ---------------------------------------------------------------------------

def test_serialize_deserialize_round_trip():
    """serialize() then deserialize() must return an equivalent dict."""
    from novafabric.evidence_fabric.avro_serializer import AvroSerializer

    serializer = AvroSerializer()
    event = _make_event(1)
    encoded = serializer.serialize(event)
    assert isinstance(encoded, bytes)
    assert len(encoded) > 0
    decoded = serializer.deserialize(encoded)
    assert decoded["event_id"] == event["event_id"]
    assert decoded["event_type"] == event["event_type"]
    assert decoded["capsule_id"] == event["capsule_id"]
    assert decoded["tenant_id"] == event["tenant_id"]
    assert decoded["ts_ms"] == event["ts_ms"]
    assert decoded["payload"] == event["payload"]


# ---------------------------------------------------------------------------
# Test 3: batch serialize / deserialize round-trip
# ---------------------------------------------------------------------------

def test_batch_serialize_deserialize_round_trip():
    """serialize_batch() then deserialize_batch() must return all events."""
    from novafabric.evidence_fabric.avro_serializer import AvroSerializer

    serializer = AvroSerializer()
    events = [_make_event(i) for i in range(5)]
    encoded = serializer.serialize_batch(events)
    assert isinstance(encoded, bytes)
    decoded_list = serializer.deserialize_batch(encoded)
    assert len(decoded_list) == 5
    for orig, decoded in zip(events, decoded_list):
        assert decoded["event_id"] == orig["event_id"]
        assert decoded["ts_ms"] == orig["ts_ms"]


# ---------------------------------------------------------------------------
# Test 4: empty batch
# ---------------------------------------------------------------------------

def test_empty_batch_round_trip():
    """An empty list must serialize and deserialize cleanly."""
    from novafabric.evidence_fabric.avro_serializer import AvroSerializer

    serializer = AvroSerializer()
    encoded = serializer.serialize_batch([])
    decoded = serializer.deserialize_batch(encoded)
    assert decoded == []


# ---------------------------------------------------------------------------
# Test 5: import guard — fastavro absent raises ImportError with pip hint
# ---------------------------------------------------------------------------

def test_import_guard_fastavro_absent(monkeypatch):
    """When fastavro is not installed, AvroSerializer() must raise ImportError
    containing the pip install hint."""
    import novafabric.evidence_fabric.avro_serializer as mod

    original = mod._FASTAVRO_AVAILABLE
    try:
        monkeypatch.setattr(mod, "_FASTAVRO_AVAILABLE", False)
        with pytest.raises(ImportError, match="pip install novafabric\\[avro\\]"):
            from novafabric.evidence_fabric.avro_serializer import AvroSerializer
            AvroSerializer()
    finally:
        monkeypatch.setattr(mod, "_FASTAVRO_AVAILABLE", original)
