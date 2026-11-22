"""Bedrock records -> served bytes, and which Bedrock records are servable at all.

botocore's own rest-json parser and event-stream decoder are the oracle: what the
dispatcher serves must parse back to what capture recorded.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from novafabric.capture.hooks._aws_eventstream import encode_message
from novafabric.capture.hooks._bedrock import (
    BEDROCK_CONVERSE_STREAM_SURFACE,
    BEDROCK_CONVERSE_SURFACE,
    BodySink,
    apply_response,
)
from novafabric.replay._bedrock_serve import (
    BedrockServeError,
    request_queue,
    served_response,
    unservable_reason,
)
from novafabric.replay._contract import ReplayEventLog, model_queue_key
from novafabric.replay._dispatcher import MockModelDispatcher
from novafabric.replay._errors import ReplayRecordMalformedError
from novafabric.replay._servability import METADATA, NOT_SERVABLE, SERVABLE, classify

botocore = pytest.importorskip("botocore")
from botocore import parsers  # noqa: E402
from botocore.eventstream import EventStreamBuffer  # noqa: E402
from botocore.session import get_session  # noqa: E402

_MODEL = get_session().get_service_model("bedrock-runtime")
URL = "https://bedrock-runtime.eu-central-1.amazonaws.com/model/qwen.qwen3-32b-v1%3A0/"
BODY = {
    "metrics": {"latencyMs": 9},
    "output": {"message": {"role": "assistant", "content": [
        {"text": "Hel"}, {"toolUse": {"toolUseId": "t1", "name": "f", "input": {"a": 1}}},
        {"reasoningContent": {"reasoningText": {"text": "hm", "signature": "s"}}},
        {"text": "lo ünï"},
    ]}},
    "stopReason": "tool_use",
    "usage": {"inputTokens": 5, "outputTokens": 2, "totalTokens": 7},
}


def _event(kind: str, payload: dict[str, Any]) -> bytes:
    return encode_message([(":event-type", kind), (":content-type", "application/json"),
                           (":message-type", "event")], json.dumps(payload).encode())


EVENTS = [
    ("messageStart", {"role": "assistant"}),
    ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "Hi "}}),
    ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "there"}}),
    ("contentBlockStop", {"contentBlockIndex": 0}),
    ("contentBlockStart", {"contentBlockIndex": 1, "start": {
        "toolUse": {"toolUseId": "t9", "name": "get_weather"}}}),
    ("contentBlockDelta", {"contentBlockIndex": 1, "delta": {"toolUse": {"input": '{"ci'}}}),
    ("contentBlockDelta", {"contentBlockIndex": 1, "delta": {"toolUse": {"input": 'ty": "X"}'}}}),
    ("contentBlockStop", {"contentBlockIndex": 1}),
    ("messageStop", {"stopReason": "tool_use"}),
    ("metadata", {"usage": {"inputTokens": 3, "outputTokens": 4, "totalTokens": 7},
                  "metrics": {"latencyMs": 1}}),
]


def _record(surface: str, data: bytes) -> dict[str, Any]:
    sink = BodySink(surface, 200, "")
    sink.feed(data)
    record: dict[str, Any] = {"gen_ai.system": "aws.bedrock", "status": "success",
                              "endpoint": URL + "converse"}
    apply_response(record, surface, sink.folded())
    return record


def _parse_converse(body: bytes, status: int = 200) -> dict[str, Any]:
    parser = parsers.create_parser("rest-json")
    shape = _MODEL.operation_model("Converse").output_shape
    return parser.parse({"status_code": status, "headers": {}, "body": body}, shape)


def _decode(body: bytes) -> list[tuple[str, dict[str, Any]]]:
    buffer = EventStreamBuffer()
    buffer.add_data(body)
    return [(m.headers[":event-type"], json.loads(m.payload)) for m in buffer]


def test_a_converse_record_is_served_back_as_botocore_parsed_it() -> None:
    record = _record(BEDROCK_CONVERSE_SURFACE, json.dumps(BODY).encode())
    served = served_response(BEDROCK_CONVERSE_SURFACE, record)
    assert served.status == 200
    assert json.loads(served.body) == BODY
    parsed = _parse_converse(served.body)
    expected = _parse_converse(json.dumps(BODY).encode())
    parsed.pop("ResponseMetadata", None)
    expected.pop("ResponseMetadata", None)
    assert parsed == expected


def test_a_converse_stream_record_is_served_back_event_for_event() -> None:
    data = b"".join(_event(k, p) for k, p in EVENTS)
    record = _record(BEDROCK_CONVERSE_STREAM_SURFACE, data)
    message = record["gen_ai.response.choices"][0]["message"]
    assert message["content"] == "Hi there"
    assert message["tool_calls"] == [{"id": "t9", "name": "get_weather",
                                      "arguments": {"city": "X"}}]
    served = served_response(BEDROCK_CONVERSE_STREAM_SURFACE, record)
    assert _decode(served.body) == EVENTS
    assert dict(served.headers)["content-type"] == "application/vnd.amazon.eventstream"


def test_a_stream_without_its_event_map_is_served_as_a_standard_sequence() -> None:
    record = _record(BEDROCK_CONVERSE_STREAM_SURFACE, b"".join(_event(k, p) for k, p in EVENTS))
    del record["extensions"]["io.novafabric.bedrock.events"]
    events = _decode(served_response(BEDROCK_CONVERSE_STREAM_SURFACE, record).body)
    text = "".join(p["delta"].get("text", "") for k, p in events if k == "contentBlockDelta")
    raw = "".join(p["delta"].get("toolUse", {}).get("input", "")
                  for k, p in events if k == "contentBlockDelta")
    assert text == "Hi there" and json.loads(raw) == {"city": "X"}
    assert events[-2] == ("messageStop", {"stopReason": "tool_use"})


@pytest.mark.parametrize("damage", ["layout", "events", "content"])
def test_a_record_that_does_not_add_up_is_not_served(damage: str) -> None:
    record = _record(BEDROCK_CONVERSE_STREAM_SURFACE, b"".join(_event(k, p) for k, p in EVENTS))
    ext = record["extensions"]
    if damage == "layout":
        ext["io.novafabric.bedrock.blocks"][0]["chars"] = 99
    elif damage == "events":
        ext["io.novafabric.bedrock.events"][1][3] = 1
    else:
        record["gen_ai.response.choices"][0]["message"]["content"] = "Hi THERE!"
    with pytest.raises(BedrockServeError):
        served_response(BEDROCK_CONVERSE_STREAM_SURFACE, record)
    assert unservable_reason(record) is not None


def test_a_recorded_http_error_is_served_with_its_status_and_body() -> None:
    record = {
        "gen_ai.system": "aws.bedrock", "status": "error",
        "error": {"type": "ThrottlingException", "message": "slow down"},
        "extensions": {
            "io.novafabric.api_surface": BEDROCK_CONVERSE_SURFACE,
            "io.novafabric.bedrock.http_error": {"status": 429, "body": '{"message":"slow down"}'},
            "io.novafabric.bedrock.response_headers": [
                ["x-amzn-ErrorType", "ThrottlingException:http://x/"], ["content-length", "1"]],
        },
    }
    served = served_response(BEDROCK_CONVERSE_SURFACE, record)
    assert served.status == 429 and served.body == b'{"message":"slow down"}'
    assert ("content-length", str(len(served.body))) in served.headers
    parser = parsers.create_parser("rest-json")
    shape = _MODEL.operation_model("Converse").output_shape
    from botocore.awsrequest import HeadersDict

    parsed = parser.parse({"status_code": 429, "headers": HeadersDict(served.headers),
                           "body": served.body}, shape)
    assert parsed["Error"]["Code"] == "ThrottlingException"


def test_only_converse_posts_are_served() -> None:
    hosts: frozenset[str] = frozenset({"127.0.0.1:9"})
    assert request_queue("POST", URL + "converse", "aws.bedrock", frozenset()) == (
        BEDROCK_CONVERSE_SURFACE)
    assert request_queue("POST", URL + "converse-stream", "aws.bedrock", frozenset()) == (
        BEDROCK_CONVERSE_STREAM_SURFACE)
    assert request_queue("POST", URL + "invoke", "aws.bedrock", frozenset()) is None
    assert request_queue("GET", URL + "converse", "aws.bedrock", frozenset()) is None
    assert request_queue("POST", "http://127.0.0.1:9/model/m/converse", "unknown", hosts) == (
        BEDROCK_CONVERSE_SURFACE)
    assert request_queue("POST", "http://127.0.0.1:8/model/m/converse", "unknown", hosts) is None


def _wire(path: str, **extra: Any) -> dict[str, Any]:
    return {"gen_ai.system": "aws.bedrock", "status": "success", "gen_ai.response.choices": [],
            "endpoint": f"https://bedrock-runtime.eu-central-1.amazonaws.com{path}", **extra}


@pytest.mark.parametrize("path, expected", [
    ("/foundation-models", METADATA),
    ("/inference-profiles", METADATA),
    ("/model-invocation-jobs", METADATA),
    ("/model/m/converse", NOT_SERVABLE),
    ("/model/m/invoke", NOT_SERVABLE),
    ("/model/m/count-tokens", NOT_SERVABLE),
    ("/async-invoke", NOT_SERVABLE),
    ("/guardrail/g/version/1/apply", NOT_SERVABLE),
])
def test_bedrock_servability_by_path(path: str, expected: str) -> None:
    assert classify([_wire(path)]) == [expected]


def test_a_recorded_converse_record_is_servable_and_an_unmarked_one_is_not() -> None:
    record = _record(BEDROCK_CONVERSE_SURFACE, json.dumps(BODY).encode())
    assert model_queue_key(record) == BEDROCK_CONVERSE_SURFACE
    assert classify([record]) == [SERVABLE]
    del record["extensions"]["io.novafabric.api_surface"]
    assert model_queue_key(record) is None
    assert classify([record]) == [NOT_SERVABLE]


@pytest.mark.parametrize("policy", ["fail", "warn"])
def test_an_unservable_record_is_refused_before_it_is_consumed(
    policy: str, tmp_path: Any
) -> None:
    record = _record(BEDROCK_CONVERSE_SURFACE, json.dumps(BODY).encode())
    record["extensions"]["io.novafabric.bedrock.blocks"][0]["chars"] = 99
    record["extensions"]["io.novafabric.record_role"] = "logical"
    log = tmp_path / "events.jsonl"
    dispatcher = MockModelDispatcher([record], divergence_policy=policy,
                                     events=ReplayEventLog(log))
    if policy == "fail":
        with pytest.raises(ReplayRecordMalformedError):
            dispatcher._serve_bedrock(BEDROCK_CONVERSE_SURFACE, {})
        assert dispatcher._index[BEDROCK_CONVERSE_SURFACE] == 0
    else:
        assert dispatcher._serve_bedrock(BEDROCK_CONVERSE_SURFACE, {}) is None
        assert dispatcher._index[BEDROCK_CONVERSE_SURFACE] == 1
    kinds = [json.loads(line)["event"] for line in log.read_text().splitlines()]
    assert "model_served" not in kinds and "divergence" in kinds


def test_an_in_stream_exception_and_other_blocks_are_served_as_delivered() -> None:
    frames = [
        _event("messageStart", {"role": "assistant"}),
        _event("contentBlockDelta", {"contentBlockIndex": 0, "delta": {
            "reasoningContent": {"text": "thinking"}}}),
        _event("contentBlockDelta", {"contentBlockIndex": 1, "delta": {"text": "ok"}}),
        encode_message([(":exception-type", "throttlingException"),
                        (":content-type", "application/json"),
                        (":message-type", "exception")], b'{"message":"slow"}'),
    ]
    record = _record(BEDROCK_CONVERSE_STREAM_SURFACE, b"".join(frames))
    assert record["status"] == "error"
    from novafabric.replay._model_errors import is_returned_failed_response

    assert is_returned_failed_response(record)
    buffer = EventStreamBuffer()
    buffer.add_data(served_response(BEDROCK_CONVERSE_STREAM_SURFACE, record).body)
    served = list(buffer)
    assert [m.headers.get(":event-type") for m in served[:3]] == [
        "messageStart", "contentBlockDelta", "contentBlockDelta"]
    assert served[3].headers[":exception-type"] == "throttlingException"
    assert served[3].payload == b'{"message":"slow"}'


def test_a_converse_record_without_its_body_extras_is_served_from_the_canonical_fields() -> None:
    record = _record(BEDROCK_CONVERSE_SURFACE, json.dumps(BODY).encode())
    del record["extensions"]["io.novafabric.bedrock.final"]
    body = json.loads(served_response(BEDROCK_CONVERSE_SURFACE, record).body)
    assert body["output"]["message"]["content"] == BODY["output"]["message"]["content"]
    assert body["stopReason"] == "tool_use"
    assert body["usage"] == {"inputTokens": 5, "outputTokens": 2, "totalTokens": 7}
    del record["extensions"]["io.novafabric.bedrock.blocks"]
    body = json.loads(served_response(BEDROCK_CONVERSE_SURFACE, record).body)
    assert body["output"]["message"]["content"][0] == {"text": "Hello ünï"}


def test_a_block_too_large_to_keep_makes_the_record_unservable() -> None:
    record = _record(BEDROCK_CONVERSE_SURFACE, json.dumps(BODY).encode())
    record["extensions"]["io.novafabric.bedrock.blocks"][2]["block"] = None
    assert "too large" in str(unservable_reason(record))
    failed = {"gen_ai.system": "aws.bedrock", "status": "error",
              "extensions": {"io.novafabric.api_surface": BEDROCK_CONVERSE_SURFACE}}
    assert unservable_reason(failed) is not None
