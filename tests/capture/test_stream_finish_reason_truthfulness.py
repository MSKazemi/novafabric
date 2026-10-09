"""The SDK hooks never record a finish reason a stream did not deliver (ADR-0304
follow-on), and record a Responses ``error`` event the SDK yielded.

Unit level, over SDK-shaped fakes; the end-to-end round trips through the real
``openai`` SDK are in ``tests/replay/test_undelivered_stream_endings.py``.
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.hooks._anthropic import AnthropicHook
from novafabric.capture.hooks._finish_reason import canonical_finish_reason
from novafabric.capture.hooks._openai import OpenAIHook, responses_choice
from novafabric.capture.hooks._sdk_streams import (
    MAX_RESPONSE_STATUS_CHARS,
    OpenAIResponsesStreamAccumulator,
    stream_error_event_detail,
)

RUN_ID = "01HXAY7M5JZ8R7K4P9DPBYK2WX"
REPO = Path(__file__).resolve().parents[2]
SCHEMA = json.loads(
    (REPO / "src" / "novafabric" / "schemas" / "model-call.schema.json").read_text()
)


def ns(value: Any) -> Any:
    if isinstance(value, dict):
        return types.SimpleNamespace(**{k: ns(v) for k, v in value.items()})
    if isinstance(value, list):
        return [ns(v) for v in value]
    return value


class FakeStream:
    def __init__(self, items: list[Any], fail_after: int | None = None) -> None:
        self._items = list(items)
        self._fail_after = fail_after

    def __iter__(self) -> FakeStream:
        return self

    def __next__(self) -> Any:
        if self._fail_after is not None and self._fail_after == 0:
            raise ConnectionError("stream dropped")
        if not self._items:
            raise StopIteration
        if self._fail_after is not None:
            self._fail_after -= 1
        return self._items.pop(0)

    def close(self) -> None:
        return None


@pytest.fixture
def writer(tmp_path: Path) -> CapsuleWriter:
    w = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    w.open()
    return w


def only_record(writer: CapsuleWriter) -> dict[str, Any]:
    lines = (writer.capsule_dir / "model-calls.jsonl").read_text().splitlines()
    (record,) = [json.loads(line) for line in lines if line]
    jsonschema.validate(record, SCHEMA)
    return record


def chat_chunk(delta: dict[str, Any], finish: str | None = None) -> Any:
    return ns({"id": "c1", "model": "gpt-4o", "usage": None,
               "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})


TEXT = [chat_chunk({"role": "assistant", "content": "Hel"}), chat_chunk({"content": "lo"})]


def assert_no_finish_reason(record: dict[str, Any]) -> None:
    assert record["gen_ai.response.choices"][0]["finish_reason"] is None
    assert "gen_ai.response.finish_reasons" not in record
    assert "io.novafabric.provider_finish_reasons" not in record.get("extensions", {})


def _chat(writer: CapsuleWriter, stream: FakeStream) -> Any:
    hook = OpenAIHook(writer=writer, parent_span_id="0" * 16)
    return hook._intercept(lambda **kw: stream, "", model="m", messages=[], stream=True)


def test_a_chat_stream_closed_before_its_finish_reason_records_none(
    writer: CapsuleWriter,
) -> None:
    with _chat(writer, FakeStream([*TEXT, chat_chunk({}, "stop")])) as s:
        next(s)
    assert_no_finish_reason(only_record(writer))


def test_a_chat_stream_that_failed_part_way_records_none(writer: CapsuleWriter) -> None:
    stream = _chat(writer, FakeStream([*TEXT, chat_chunk({}, "stop")], fail_after=2))
    with pytest.raises(ConnectionError):
        list(stream)
    record = only_record(writer)
    assert record["status"] == "error"
    assert_no_finish_reason(record)


def test_a_chat_stream_that_ended_without_a_finish_reason_records_none(
    writer: CapsuleWriter,
) -> None:
    list(_chat(writer, FakeStream(TEXT)))
    record = only_record(writer)
    assert "io.novafabric.stream_complete" not in record["extensions"]
    assert_no_finish_reason(record)


def test_a_delivered_finish_reason_is_kept(writer: CapsuleWriter) -> None:
    list(_chat(writer, FakeStream([*TEXT, chat_chunk({}, "length")])))
    record = only_record(writer)
    assert record["gen_ai.response.choices"][0]["finish_reason"] == "length"
    assert record["gen_ai.response.finish_reasons"] == ["length"]


def test_an_anthropic_stream_without_a_stop_reason_records_none(
    writer: CapsuleWriter,
) -> None:
    events = [ns(e) for e in [
        {"type": "message_start", "message": {"id": "m", "model": "claude-x",
                                              "usage": {"input_tokens": 1}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "ok"}},
    ]]
    hook = AnthropicHook(writer=writer, parent_span_id="0" * 16)
    list(hook._intercept(lambda **kw: FakeStream(events), model="claude-x", stream=True,
                         max_tokens=5, messages=[]))
    assert_no_finish_reason(only_record(writer))


def test_a_responses_partial_fold_has_no_finish_reason() -> None:
    acc = OpenAIResponsesStreamAccumulator()
    acc.add(ns({"type": "response.created", "response": {"id": "r", "model": "m"}}))
    acc.add(ns({"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "c", "name": "f", "arguments": "{}"}}))
    choice, _ = responses_choice(acc.response())
    assert choice is not None and choice["finish_reason"] is None
    assert choice["message"]["tool_calls"] == [{"id": "c", "name": "f", "arguments": {}}]


def test_a_responses_error_event_is_recorded_verbatim(writer: CapsuleWriter) -> None:
    error = {"type": "error", "code": None, "message": "boom", "param": None,
             "sequence_number": 3}
    events = [ns(e) for e in [
        {"type": "response.created", "response": {"id": "r", "model": "m"}},
        {"type": "response.output_text.delta", "delta": "par"},
        error,
    ]]
    hook = OpenAIHook(writer=writer, parent_span_id="0" * 16)
    list(hook._intercept_surface("responses", lambda **kw: FakeStream(events), "",
                                 {"model": "m", "input": "hi", "stream": True}))
    record = only_record(writer)
    assert record["status"] == "error"
    # No code delivered: the error type names the event, it does not invent a code.
    assert record["error"] == {"type": "error", "message": "boom", "traceback_ref": None}
    assert record["extensions"]["io.novafabric.stream_error_event"] == error
    assert record["gen_ai.response.choices"][0]["message"]["content"] == "par"
    assert_no_finish_reason(record)


def test_an_oversized_error_event_is_replaced_by_an_omission_marker() -> None:
    event = ns({"type": "error", "code": None, "message": "x" * (MAX_RESPONSE_STATUS_CHARS + 1),
                "param": None, "sequence_number": 1})
    detail = stream_error_event_detail(event)
    assert detail is not None and detail["type"] == "error" and "omitted" in detail
    assert "message" not in detail
    assert stream_error_event_detail(None) is None


def test_canonical_finish_reason_still_maps_delivered_values() -> None:
    assert canonical_finish_reason("anthropic", "end_turn") == "stop"
    assert canonical_finish_reason("openai", "function_call") == "tool_calls"
