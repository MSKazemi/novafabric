"""ADR-0304: what a served call returns, checked in-process.

The end-to-end tests drive these builders inside a replayed subprocess; here the
same code runs in the test process, against the real ``openai`` SDK types where the
SDK is installed and against plain attribute objects where it is not.
"""

from __future__ import annotations

import asyncio
import json
import types
from typing import Any

import pytest

from novafabric.replay._contract import ReplayEventLog
from novafabric.replay._dispatcher import (
    MockModelDispatcher,
    _AsyncReplayStream,
    _construct,
    _namespace,
    _ReplayStream,
    build_served_response,
)
from novafabric.replay._errors import (
    ReplayOrderMismatchError,
    ReplayQueueExhaustedError,
    ReplayUnsupportedSurfaceError,
)


def record(
    content: str | None = "hello",
    calls: list[dict[str, Any]] | None = None,
    *,
    surface: str = "openai.chat.completions",
    system: str = "openai",
    finish: str | None = None,
    ext: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {
        "model_call_id": "01HXAY7M5JZ8R7K4P9DPBYK2M0",
        "gen_ai.system": system,
        "gen_ai.response.id": "rid-1",
        "gen_ai.response.model": "m-1",
        "gen_ai.usage.input_tokens": 3,
        "gen_ai.usage.output_tokens": 5,
        "gen_ai.response.choices": [{
            "index": 0, "message": message,
            "finish_reason": finish or ("tool_calls" if calls else "stop"),
        }],
        "status": "success",
        "extensions": {"io.novafabric.api_surface": surface, **(ext or {})},
    }


CALL = {"id": "call_1", "name": "get_weather", "arguments": {"city": "Paris"}}


class Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, **fields: Any) -> None:
        self.items.append((event, fields))


# ── builders ─────────────────────────────────────────────────────────────────


def test_chat_stream_chunks_fold_back_into_the_record() -> None:
    pytest.importorskip("openai")
    stream = build_served_response(
        "openai", record("hi there", [CALL]), stream=True, asynchronous=False,
        kwargs={"stream_options": {"include_usage": True}},
    )
    chunks = list(stream)
    assert type(chunks[0]).__name__ == "ChatCompletionChunk"  # the SDK's own type
    content = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
    calls = [tc for c in chunks if c.choices for tc in (c.choices[0].delta.tool_calls or [])]
    finish = [c.choices[0].finish_reason for c in chunks if c.choices][-1]
    assert content == "hi there"
    assert [(tc.id, tc.function.name, json.loads(tc.function.arguments)) for tc in calls] == [
        ("call_1", "get_weather", {"city": "Paris"})
    ]
    assert finish == "tool_calls"
    assert chunks[-1].choices == [] and chunks[-1].usage.total_tokens == 8


def test_chat_stream_has_no_usage_chunk_unless_requested() -> None:
    chunks = list(build_served_response("openai", record(), stream=True, asynchronous=False))
    assert all(getattr(c, "usage", None) is None for c in chunks)


def test_responses_api_response_and_events() -> None:
    pytest.importorskip("openai")
    rec = record("answer", [CALL], surface="openai.responses")
    response = build_served_response("openai.responses", rec, stream=False, asynchronous=False)
    assert type(response).__name__ == "Response"
    assert response.output_text == "answer"
    (call,) = [i for i in response.output if i.type == "function_call"]
    assert (call.call_id, call.name, json.loads(call.arguments)) == (
        "call_1", "get_weather", {"city": "Paris"}
    )
    events = list(build_served_response("openai.responses", rec, stream=True,
                                        asynchronous=False))
    kinds = [e.type for e in events]
    assert kinds[0] == "response.created" and kinds[-1] == "response.completed"
    assert "response.function_call_arguments.done" in kinds
    assert "".join(e.delta for e in events if e.type == "response.output_text.delta") == "answer"
    assert [e.sequence_number for e in events] == list(range(len(events)))


def test_a_truncated_responses_recording_replays_as_incomplete() -> None:
    pytest.importorskip("openai")
    rec = record("cut", surface="openai.responses", finish="length")
    response = build_served_response("openai.responses", rec, stream=False, asynchronous=False)
    assert response.status == "incomplete"
    assert response.incomplete_details.reason == "max_output_tokens"
    events = list(build_served_response("openai.responses", rec, stream=True,
                                        asynchronous=False))
    assert events[-1].type == "response.incomplete"


def test_anthropic_events_rebuild_text_and_tool_use() -> None:
    rec = record("Checking.", [CALL], surface="anthropic.messages", system="anthropic",
                 ext={"io.novafabric.provider_finish_reasons": ["tool_use"]})
    events = list(build_served_response("anthropic", rec, stream=True, asynchronous=False))
    kinds = [e.type for e in events]
    assert kinds[0] == "message_start" and kinds[-1] == "message_stop"
    deltas = [e.delta for e in events if e.type == "content_block_delta"]
    assert deltas[0].text == "Checking."
    assert json.loads(deltas[1].partial_json) == {"city": "Paris"}
    (stop,) = [e for e in events if e.type == "message_delta"]
    assert stop.delta.stop_reason == "tool_use"
    assert stop.usage.output_tokens == 5


def test_async_streams_and_context_managers() -> None:
    async def run() -> list[Any]:
        stream = build_served_response("openai", record("x"), stream=True, asynchronous=True)
        assert isinstance(stream, _AsyncReplayStream)
        async with stream as s:
            first = await s.__anext__()
        rest = [c async for c in stream]  # closed by the context manager
        await stream.response.aclose()
        return [first, *rest]

    assert len(asyncio.run(run())) == 1
    sync = _ReplayStream([1, 2, 3])
    with sync as s:
        assert next(s) == 1
    assert list(sync) == []
    sync.response.close()


def test_without_the_sdk_the_builders_fall_back_to_attribute_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _construct("no_such_sdk_xyz", "no_such_sdk_xyz.types", "T", {"a": {"b": [{"c": 1}]}})
    assert isinstance(built, types.SimpleNamespace) and built.a.b[0].c == 1
    assert _namespace([{"k": "v"}])[0].k == "v"

    import novafabric.replay._dispatcher as dispatcher

    monkeypatch.setattr(dispatcher, "_construct", lambda sdk, mod, name, data: _namespace(data))
    response = build_served_response(
        "openai.responses", record("plain", surface="openai.responses"),
        stream=False, asynchronous=False,
    )
    assert response.output_text == "plain"


# ── the dispatcher, installed in-process on the real openai SDK ─────────────


@pytest.fixture
def openai_client() -> Any:
    openai = pytest.importorskip("openai")
    import httpx

    def refuse(request: Any) -> Any:
        raise AssertionError(f"network reached: {request.url}")

    return openai.OpenAI(api_key="sk-test",
                         http_client=httpx.Client(transport=httpx.MockTransport(refuse)))


def test_installed_dispatcher_serves_stream_async_and_responses(openai_client: Any) -> None:
    import httpx
    import openai

    events = Events()
    recs = [record("a"), record("b"), record("c", surface="openai.responses")]
    d = MockModelDispatcher(recs, events=events)  # type: ignore[arg-type]
    d.install()
    try:
        chunks = list(openai_client.chat.completions.create(model="m", messages=[],
                                                            stream=True))
        assert "".join(c.choices[0].delta.content or "" for c in chunks) == "a"

        async def async_call() -> Any:
            client = openai.AsyncOpenAI(api_key="sk-test", http_client=httpx.AsyncClient())
            return await client.chat.completions.create(model="m", messages=[])

        assert asyncio.run(async_call()).choices[0].message.content == "b"
        assert openai_client.responses.create(model="m", input="x").output_text == "c"
        assert any("(async)" in s for s in d.installed_surfaces)
    finally:
        d.uninstall()
    served = [f for e, f in events.items if e == "model_served"]
    assert [(f["queue"], f["stream"], f["asynchronous"]) for f in served] == [
        ("openai", True, False), ("openai", False, True), ("openai.responses", False, False)
    ]


def test_installed_dispatcher_refuses_raw_response_and_legacy_capsule_streams(
    openai_client: Any,
) -> None:
    legacy = record("old")
    legacy.pop("extensions")  # captured before ADR-0304
    events = Events()
    d = MockModelDispatcher([legacy], events=events)  # type: ignore[arg-type]
    d.install()
    try:
        with pytest.raises(ReplayUnsupportedSurfaceError, match="with_raw_response"):
            openai_client.chat.completions.with_raw_response.create(model="m", messages=[])
        with pytest.raises(ReplayUnsupportedSurfaceError, match="captured before"):
            openai_client.chat.completions.create(model="m", messages=[], stream=True)
        assert openai_client.chat.completions.create(model="m", messages=[]) \
            .choices[0].message.content == "old"
    finally:
        d.uninstall()
    assert [f["kind"] for e, f in events.items if e == "divergence"] == [
        "unsupported_surface", "unsupported_surface"
    ]


def test_cross_surface_order_and_permissive_exhaustion_in_process(openai_client: Any) -> None:
    d = MockModelDispatcher(
        [record("chat"), record("resp", surface="openai.responses")],
        events=ReplayEventLog(None),
    )
    d.install()
    try:
        with pytest.raises(ReplayOrderMismatchError) as info:
            openai_client.responses.create(model="m", input="x")
        assert info.value.details["expected_surface"] == "openai.chat.completions.create"
    finally:
        d.uninstall()

    warn = MockModelDispatcher([], divergence_policy="warn")
    warn.install()
    try:
        empty = list(openai_client.chat.completions.create(model="m", messages=[],
                                                           stream=True))
        assert empty == []  # an empty stream, labelled, instead of a crash
    finally:
        warn.uninstall()

    strict = MockModelDispatcher([])
    strict.install()
    try:
        with pytest.raises(ReplayQueueExhaustedError):
            openai_client.responses.create(model="m", input="x", stream=True)
    finally:
        strict.uninstall()
