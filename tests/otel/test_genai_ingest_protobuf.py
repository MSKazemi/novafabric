# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""OTLP/protobuf ingest — decodes to the same events as the JSON path (ADR-0177)."""
from __future__ import annotations

import json

import pytest

from novafabric.otel.genai_ingest import (
    OTLPIngestError,
    ingest_otlp_json,
    ingest_otlp_protobuf,
    parse_otlp_json,
    parse_otlp_protobuf,
)

# The 'otlp' extra provides the proto classes; skip cleanly if absent.
pytest.importorskip("opentelemetry.proto.collector.trace.v1.trace_service_pb2")

_TRACE_HEX = "0123456789abcdef0123456789abcdef"
_SPAN_HEX = "0123456789abcdef"


def _build_protobuf_request() -> bytes:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    ss = rs.scope_spans.add()
    sp = ss.spans.add()
    sp.name = "chat gpt-4o"
    sp.trace_id = bytes.fromhex(_TRACE_HEX)
    sp.span_id = bytes.fromhex(_SPAN_HEX)
    sp.start_time_unix_nano = 1_700_000_000_000_000_000
    sp.end_time_unix_nano = 1_700_000_001_000_000_000
    for key, val in (
        ("gen_ai.system", "openai"),
        ("gen_ai.request.model", "gpt-4o"),
        ("gen_ai.operation.name", "chat"),
    ):
        kv = sp.attributes.add()
        kv.key = key
        kv.value.string_value = val
    return req.SerializeToString()


def _equivalent_json_payload() -> dict:
    return {
        "resourceSpans": [{
            "scopeSpans": [{
                "spans": [{
                    "name": "chat gpt-4o",
                    "traceId": _TRACE_HEX,
                    "spanId": _SPAN_HEX,
                    "startTimeUnixNano": "1700000000000000000",
                    "endTimeUnixNano": "1700000001000000000",
                    "attributes": [
                        {"key": "gen_ai.system", "value": {"stringValue": "openai"}},
                        {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o"}},
                        {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                    ],
                }],
            }],
        }],
    }


def test_parse_protobuf_matches_parse_json() -> None:
    proto_spans = parse_otlp_protobuf(_build_protobuf_request())
    json_spans = parse_otlp_json(_equivalent_json_payload())
    assert proto_spans == json_spans
    # IDs are hex (not base64) after normalization
    assert proto_spans[0]["trace_id"] == _TRACE_HEX
    assert proto_spans[0]["span_id"] == _SPAN_HEX


def test_ingest_protobuf_matches_ingest_json() -> None:
    proto_result = ingest_otlp_protobuf(_build_protobuf_request())
    json_result = ingest_otlp_json(_equivalent_json_payload())
    assert proto_result.model_calls == json_result.model_calls
    assert proto_result.tool_calls == json_result.tool_calls
    assert proto_result.spans_seen == json_result.spans_seen == 1
    assert len(proto_result.model_calls) == 1


def test_invalid_protobuf_raises_ingest_error() -> None:
    with pytest.raises(OTLPIngestError):
        # Random bytes are not a valid ExportTraceServiceRequest with these fields.
        parse_otlp_protobuf(b"\xff\xff\xff\xff\x0f not otlp")


def test_empty_protobuf_request_is_valid_empty() -> None:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    spans = parse_otlp_protobuf(ExportTraceServiceRequest().SerializeToString())
    assert spans == []


def test_roundtrip_through_json_dump_is_stable() -> None:
    # The protobuf-derived normalized spans are JSON-serializable (no bytes leak through).
    spans = parse_otlp_protobuf(_build_protobuf_request())
    json.dumps(spans)  # must not raise
