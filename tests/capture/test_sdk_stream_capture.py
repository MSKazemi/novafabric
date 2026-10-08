"""ADR-0304: the SDK hooks record async, streamed and Responses API calls with a
response mocked replay can serve -- and never disturb the workload doing so."""

from __future__ import annotations

import asyncio
import gc
import json
import types
from pathlib import Path
from typing import Any

import pytest

from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.hooks._anthropic import AnthropicHook
from novafabric.capture.hooks._openai import OpenAIHook, responses_choice
from novafabric.capture.hooks._sdk_streams import (
    AnthropicStreamAccumulator,
    OpenAIChatStreamAccumulator,
    OpenAIResponsesStreamAccumulator,
    is_raw_response_call,
)
from novafabric.replay._contract import is_replayable_model_call, model_queue_key

RUN_ID = "01HXAY7M5JZ8R7K4P9DPBYK2WX"


def ns(value: Any) -> Any:
    if isinstance(value, dict):
        return types.SimpleNamespace(**{k: ns(v) for k, v in value.items()})
    if isinstance(value, list):
        return [ns(v) for v in value]
    return value


@pytest.fixture
def writer(tmp_path: Path) -> CapsuleWriter:
    w = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    w.open()
    return w


def records(writer: CapsuleWriter) -> list[dict[str, Any]]:
    path = writer.capsule_dir / "model-calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def chat_chunk(delta: dict[str, Any], finish: str | None = None, **extra: Any) -> Any:
    return ns({"id": "c1", "model": "gpt-4o", "usage": None,
               "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra})


CHUNKS = [
    chat_chunk({"role": "assistant", "content": "Hel"}),
    chat_chunk({"content": "lo"}),
    chat_chunk({"tool_calls": [{"index": 0, "id": "call_1",
                                "function": {"name": "f", "arguments": '{"a"'}}]}),
    chat_chunk({"tool_calls": [{"index": 0, "id": None,
                                "function": {"name": None, "arguments": ": 1}"}}]}),
    chat_chunk({}, "tool_calls"),
]


class FakeStream:
    """The shape of an SDK ``Stream``: iterable, closable, with extra attributes."""

    def __init__(self, items: list[Any], fail_after: int | None = None) -> None:
        self._items = list(items)
        self._fail_after = fail_after
        self.closed = False
        self.response = "http-response"

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
        self.closed = True


class FakeAsyncStream:
    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)

    def __aiter__(self) -> FakeAsyncStream:
        return self

    async def __anext__(self) -> Any:
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


# ── accumulators ─────────────────────────────────────────────────────────────


def test_chat_accumulator_folds_content_and_split_tool_arguments() -> None:
    acc = OpenAIChatStreamAccumulator()
    for chunk in CHUNKS:
        acc.add(chunk)
    response = acc.response()
    (choice,) = response.choices
    assert choice.message.content == "Hello"
    assert choice.finish_reason == "tool_calls"
    (call,) = choice.message.tool_calls
    assert (call.id, call.function.name, call.function.arguments) == ("call_1", "f", '{"a": 1}')


def test_anthropic_accumulator_folds_text_and_tool_json() -> None:
    acc = AnthropicStreamAccumulator()
    for event in [
        {"type": "message_start", "message": {"id": "m1", "model": "claude-x",
                                              "usage": {"input_tokens": 4, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "t1", "name": "f", "input": {}}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": '{"q": '}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": '"x"}'}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
         "usage": {"output_tokens": 9}},
    ]:
        acc.add(ns(event))
    message = acc.response()
    assert (message.id, message.stop_reason) == ("m1", "tool_use")
    assert (message.usage.input_tokens, message.usage.output_tokens) == (4, 9)
    assert [b.type for b in message.content] == ["text", "tool_use"]
    assert message.content[1].input == {"q": "x"}


def test_anthropic_accumulator_keeps_invalid_tool_json_unparsed() -> None:
    acc = AnthropicStreamAccumulator()
    acc.add(ns({"type": "content_block_start", "index": 0,
                "content_block": {"type": "tool_use", "id": "t", "name": "f", "input": {}}}))
    acc.add(ns({"type": "content_block_delta", "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": "{not json"}}))
    assert acc.response().content[0].input == {"_unparsed": "{not json"}


def test_responses_accumulator_prefers_the_terminal_response() -> None:
    final = ns({"id": "r1", "output": []})
    acc = OpenAIResponsesStreamAccumulator()
    acc.add(ns({"type": "response.output_text.delta", "delta": "partial"}))
    acc.add(ns({"type": "response.completed", "response": final}))
    assert acc.response() is final


def test_responses_accumulator_rebuilds_an_unfinished_stream() -> None:
    acc = OpenAIResponsesStreamAccumulator()
    acc.add(ns({"type": "response.created", "response": {"id": "r9", "model": "gpt-4o"}}))
    acc.add(ns({"type": "response.output_text.delta", "delta": "par"}))
    acc.add(ns({"type": "response.output_text.delta", "delta": "tial"}))
    acc.add(ns({"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "c", "name": "f", "arguments": "{}"}}))
    response = acc.response()
    assert response.id == "r9" and response.status == "incomplete"
    choice, dropped = responses_choice(response)
    assert choice is not None and dropped == 0
    assert choice["message"]["content"] == "partial"
    assert choice["message"]["tool_calls"] == [{"id": "c", "name": "f", "arguments": {}}]


# ── Responses API record shape ───────────────────────────────────────────────


@pytest.mark.parametrize(("response", "finish", "content"), [
    ({"output": [{"type": "message", "content": [
        {"type": "output_text", "text": "a"}, {"type": "output_text", "text": "b"}]}]},
     "stop", "ab"),
    ({"incomplete_details": {"reason": "max_output_tokens"},
      "output": [{"type": "message", "content": [{"type": "output_text", "text": "cut"}]}]},
     "length", "cut"),
    ({"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]},
     "content_filter", "no"),
    ({"output": [{"type": "reasoning", "summary": []}, {"type": "function_call",
                                                        "call_id": "c", "name": "f",
                                                        "arguments": "{bad"}]},
     "tool_calls", None),
])
def test_responses_choice_maps_output_to_the_canonical_choice(
    response: dict[str, Any], finish: str, content: str | None
) -> None:
    choice, _ = responses_choice(ns(response))
    assert choice is not None
    assert choice["finish_reason"] == finish
    assert choice["message"]["content"] == content
    if finish == "tool_calls":
        assert choice["message"]["tool_calls"] == [
            {"id": "c", "name": "f", "arguments": {"_unparsed": "{bad"}}
        ]


def test_responses_choice_drops_nameless_calls_and_counts_them() -> None:
    choice, dropped = responses_choice(ns({"output": [
        {"type": "function_call", "call_id": "c", "name": "", "arguments": "{}"}]}))
    assert dropped == 1
    assert choice is not None and "tool_calls" not in choice["message"]
    assert responses_choice(ns({"output": []})) == (None, 0)


def test_responses_record_is_marked_and_servable(writer: CapsuleWriter) -> None:
    hook = OpenAIHook(writer=writer, parent_span_id="0" * 16)
    response = ns({"id": "resp_1", "model": "gpt-4o", "status": "completed",
                   "usage": {"input_tokens": 3, "output_tokens": 5},
                   "output": [{"type": "message", "content": [
                       {"type": "output_text", "text": "hi"}]}]})
    hook._intercept_surface("responses", lambda **kw: response, "",
                            {"model": "gpt-4o", "input": "hi", "max_output_tokens": 64})
    (record,) = records(writer)
    assert record["extensions"]["io.novafabric.api_surface"] == "openai.responses"
    assert record["gen_ai.request.max_tokens"] == 64
    assert (record["gen_ai.usage.input_tokens"], record["gen_ai.usage.output_tokens"]) == (3, 5)
    assert model_queue_key(record) == "openai.responses"
    assert is_replayable_model_call(record)


def test_failed_responses_call_is_an_error_record_never_served(writer: CapsuleWriter) -> None:
    hook = OpenAIHook(writer=writer, parent_span_id="0" * 16)
    response = ns({"id": "r", "status": "failed", "output": [],
                   "error": {"code": "server_error", "message": "boom"}})
    hook._intercept_surface("responses", lambda **kw: response, "", {"model": "m"})
    (record,) = records(writer)
    assert record["status"] == "error"
    assert record["error"]["type"] == "server_error"
    assert not is_replayable_model_call(record)


def test_unknown_surface_marker_is_never_served() -> None:
    record = {"gen_ai.system": "openai", "status": "success",
              "gen_ai.response.choices": [{"index": 0, "message": {}, "finish_reason": "stop"}],
              "extensions": {"io.novafabric.api_surface": "openai.realtime"}}
    assert model_queue_key(record) is None
    assert not is_replayable_model_call(record)


# ── stream proxies ───────────────────────────────────────────────────────────


def test_streamed_chat_is_recorded_once_when_exhausted(writer: CapsuleWriter) -> None:
    hook = OpenAIHook(writer=writer, parent_span_id="0" * 16)
    inner = FakeStream(CHUNKS)
    stream = hook._intercept(lambda **kw: inner, "", model="gpt-4o", messages=[], stream=True)
    assert stream.response == "http-response"  # other attributes are delegated
    assert records(writer) == []  # nothing until the stream ends
    seen = list(stream)
    assert seen == CHUNKS  # the SDK's own chunk objects, unchanged
    stream.close()  # a close after exhaustion does not record twice
    (record,) = records(writer)
    choice = record["gen_ai.response.choices"][0]
    assert choice["message"]["content"] == "Hello"
    assert choice["message"]["tool_calls"] == [{"id": "call_1", "name": "f", "arguments": {"a": 1}}]
    assert record["nova.streaming"]["chunk_count"] == len(CHUNKS)
    assert record["nova.streaming"]["first_token_ms"] is not None
    assert "io.novafabric.stream_complete" not in record.get("extensions", {})


def test_a_stream_closed_early_records_what_was_delivered(writer: CapsuleWriter) -> None:
    hook = OpenAIHook(writer=writer, parent_span_id="0" * 16)
    inner = FakeStream(CHUNKS)
    with hook._intercept(lambda **kw: inner, "", model="m", messages=[], stream=True) as s:
        next(s)
    (record,) = records(writer)
    assert record["gen_ai.response.choices"][0]["message"]["content"] == "Hel"
    assert record["extensions"]["io.novafabric.stream_complete"] is False


def test_an_abandoned_stream_is_recorded_when_collected(writer: CapsuleWriter) -> None:
    hook = OpenAIHook(writer=writer, parent_span_id="0" * 16)
    stream = hook._intercept(lambda **kw: FakeStream(CHUNKS), "", model="m", messages=[],
                             stream=True)
    next(stream)
    del stream
    gc.collect()
    (record,) = records(writer)
    assert record["extensions"]["io.novafabric.stream_complete"] is False


def test_a_stream_that_fails_midway_reraises_and_records_partial(
    writer: CapsuleWriter,
) -> None:
    hook = OpenAIHook(writer=writer, parent_span_id="0" * 16)
    stream = hook._intercept(lambda **kw: FakeStream(CHUNKS, fail_after=2), "",
                             model="m", messages=[], stream=True)
    with pytest.raises(ConnectionError):
        list(stream)
    (record,) = records(writer)
    assert record["gen_ai.response.choices"][0]["message"]["content"] == "Hello"
    assert record["extensions"]["io.novafabric.stream_complete"] is False


def test_a_chunk_that_cannot_be_folded_never_breaks_the_workload(
    writer: CapsuleWriter,
) -> None:
    hook = OpenAIHook(writer=writer, parent_span_id="0" * 16)
    odd = types.SimpleNamespace(choices=[types.SimpleNamespace(index="x")])
    stream = hook._intercept(lambda **kw: FakeStream([odd, *CHUNKS]), "", model="m",
                             messages=[], stream=True)
    assert len(list(stream)) == len(CHUNKS) + 1
    assert len(records(writer)) == 1


def test_async_streamed_anthropic_call_is_recorded(writer: CapsuleWriter) -> None:
    hook = AnthropicHook(writer=writer, parent_span_id="0" * 16)
    events = [ns(e) for e in [
        {"type": "message_start", "message": {"id": "m", "model": "claude-x",
                                              "usage": {"input_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 2}},
    ]]

    async def create(**kwargs: Any) -> FakeAsyncStream:
        return FakeAsyncStream(events)

    async def run() -> list[Any]:
        stream = await hook._intercept_async(create, {"model": "claude-x", "stream": True,
                                                      "max_tokens": 5, "messages": []})
        return [e async for e in stream]

    assert len(asyncio.run(run())) == 4
    (record,) = records(writer)
    assert record["gen_ai.response.choices"][0]["message"]["content"] == "ok"
    assert record["gen_ai.response.finish_reasons"] == ["stop"]
    assert record["nova.streaming"]["streamed"] is True


def test_async_non_streamed_openai_call_is_recorded(writer: CapsuleWriter) -> None:
    hook = OpenAIHook(writer=writer, parent_span_id="0" * 16)
    response = ns({"id": "r", "model": "gpt-4o", "usage": None, "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hi", "tool_calls": None},
         "finish_reason": "stop"}]})

    async def create(**kwargs: Any) -> Any:
        return response

    assert asyncio.run(hook._intercept_async("chat", create, "", {"model": "gpt-4o"})) is response
    (record,) = records(writer)
    assert record["gen_ai.response.choices"][0]["message"]["content"] == "hi"
    assert "nova.streaming" not in record


def test_raw_response_calls_are_recorded_without_choices(writer: CapsuleWriter) -> None:
    kwargs = {"model": "m", "messages": [], "extra_headers": {"X-Stainless-Raw-Response": "true"}}
    assert is_raw_response_call(kwargs)
    assert not is_raw_response_call({"extra_headers": {"x-other": "1"}})
    raw = object()
    OpenAIHook(writer=writer, parent_span_id="0" * 16)._intercept(lambda **kw: raw, "", **kwargs)
    AnthropicHook(writer=writer, parent_span_id="0" * 16)._intercept(lambda **kw: raw, **kwargs)
    assert [r["gen_ai.response.choices"] for r in records(writer)] == [[], []]
    assert not any(is_replayable_model_call(r) for r in records(writer))


def test_install_patches_async_and_responses_and_restores_them(writer: CapsuleWriter) -> None:
    pytest.importorskip("openai")
    import openai.resources.chat.completions as chat
    import openai.resources.responses as responses

    originals = (chat.Completions.create, chat.AsyncCompletions.create,
                 responses.Responses.create, responses.AsyncResponses.create)
    hook = OpenAIHook(writer=writer, parent_span_id="0" * 16)
    hook.install()
    try:
        patched = (chat.Completions.create, chat.AsyncCompletions.create,
                   responses.Responses.create, responses.AsyncResponses.create)
        assert all(p is not o for p, o in zip(patched, originals))
        assert hook._original is originals[0]
        assert asyncio.iscoroutinefunction(chat.AsyncCompletions.create)
    finally:
        hook.uninstall()
    assert (chat.Completions.create, chat.AsyncCompletions.create,
            responses.Responses.create, responses.AsyncResponses.create) == originals


def test_every_sdk_record_names_its_api_surface(writer: CapsuleWriter) -> None:
    from novafabric.replay._contract import records_async_and_streamed_calls

    chat = ns({"id": "c", "model": "gpt-4o", "usage": None, "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hi", "tool_calls": None},
         "finish_reason": "stop"}]})
    message = ns({"id": "m", "model": "claude-x", "stop_reason": "end_turn", "usage": None,
                  "content": [{"type": "text", "text": "hi"}]})
    OpenAIHook(writer=writer, parent_span_id="0" * 16)._intercept(
        lambda **kw: chat, "", model="gpt-4o", messages=[])
    AnthropicHook(writer=writer, parent_span_id="0" * 16)._intercept(
        lambda **kw: message, model="claude-x", messages=[], max_tokens=5)
    recs = records(writer)
    assert [r["extensions"]["io.novafabric.api_surface"] for r in recs] == [
        "openai.chat.completions", "anthropic.messages"
    ]
    assert [model_queue_key(r) for r in recs] == ["openai", "anthropic"]
    # Marked records come from hooks that record async and streamed calls ...
    assert records_async_and_streamed_calls(recs)
    # ... unmarked ones (every capsule captured before ADR-0304) do not.
    legacy = [{k: v for k, v in r.items() if k != "extensions"} for r in recs]
    assert [model_queue_key(r) for r in legacy] == ["openai", "anthropic"]
    assert not records_async_and_streamed_calls(legacy)
    assert records_async_and_streamed_calls([])
