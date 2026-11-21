"""A served non-streamed model response is the SDK's own response type.

Mocked replay used to rebuild a non-streamed Chat Completion and an Anthropic
``Message`` as ``types.SimpleNamespace``. Code that checks the type -- the OpenAI
Agents SDK's ``OpenAIChatCompletionsModel`` does ``isinstance(ret, ChatCompletion)``
and treats anything else as a stream -- or that calls the SDK's pydantic helpers
(``model_dump``, ``to_dict``) broke on the first served call. Both builders now go
through the SDK's ``construct_type``, as the Responses builder and every stream
builder already did, and still fall back to plain attribute objects without the SDK.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest

from novafabric.replay._dispatcher import build_served_response

CALL = {"id": "call_1", "name": "get_weather", "arguments": {"city": "Paris"}}


def record(
    content: str | None = "hello",
    calls: list[Any] | None = None,
    *,
    finish: str | None = "stop",
    system: str = "openai",
    ext: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return {
        "gen_ai.system": system,
        "gen_ai.response.id": "rid-1",
        "gen_ai.response.model": "m-1",
        "gen_ai.usage.input_tokens": 3,
        "gen_ai.usage.output_tokens": 5,
        "gen_ai.response.choices": [
            {"index": 0, "message": message, "finish_reason": finish}
        ],
        "extensions": ext or {},
    }


def chat(rec: dict[str, Any]) -> Any:
    return build_served_response("openai", rec, stream=False, asynchronous=False)


def messages(rec: dict[str, Any]) -> Any:
    return build_served_response("anthropic", rec, stream=False, asynchronous=False)


# ── OpenAI Chat Completions ──────────────────────────────────────────────────


def test_a_served_chat_completion_is_the_sdks_chat_completion() -> None:
    pytest.importorskip("openai")
    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.chat.chat_completion import Choice
    from openai.types.completion_usage import CompletionUsage

    resp = chat(record("hi there"))
    assert isinstance(resp, ChatCompletion)
    assert isinstance(resp.choices[0], Choice)
    assert isinstance(resp.choices[0].message, ChatCompletionMessage)
    assert isinstance(resp.usage, CompletionUsage)
    assert resp.object == "chat.completion"
    assert (resp.id, resp.model) == ("rid-1", "m-1")
    assert resp.choices[0].message.content == "hi there"
    assert resp.choices[0].message.tool_calls is None
    assert resp.choices[0].finish_reason == "stop"
    assert (resp.usage.prompt_tokens, resp.usage.completion_tokens,
            resp.usage.total_tokens) == (3, 5, 8)


def test_served_tool_calls_are_the_sdks_tool_call_type() -> None:
    pytest.importorskip("openai")
    from openai.types.chat import ChatCompletionMessageFunctionToolCall

    unparsed = {"id": "call_2", "name": "lookup", "arguments": {"_unparsed": '{"q": "x'}}
    malformed = {"id": "call_3", "arguments": {}}  # no name: cannot be rebuilt
    resp = chat(record(None, [CALL, malformed, unparsed], finish="tool_calls"))
    calls = resp.choices[0].message.tool_calls
    assert all(isinstance(tc, ChatCompletionMessageFunctionToolCall) for tc in calls)
    assert [(tc.id, tc.type, tc.function.name) for tc in calls] == [
        ("call_1", "function", "get_weather"), ("call_2", "function", "lookup"),
    ]
    assert json.loads(calls[0].function.arguments) == {"city": "Paris"}
    assert calls[1].function.arguments == '{"q": "x'  # served verbatim
    assert resp.choices[0].message.content is None


def test_only_malformed_tool_call_refs_serve_tool_calls_none() -> None:
    pytest.importorskip("openai")
    resp = chat(record("x", [{"id": "c", "arguments": {}}, "junk"]))
    assert resp.choices[0].message.tool_calls is None


def test_a_missing_finish_reason_is_served_as_none_never_substituted() -> None:
    pytest.importorskip("openai")
    resp = chat(record("cut off", finish=None))
    assert resp.choices[0].finish_reason is None
    without_key = record("cut off")
    del without_key["gen_ai.response.choices"][0]["finish_reason"]
    assert chat(without_key).choices[0].finish_reason is None


def test_a_served_chat_completion_round_trips_through_the_sdks_helpers() -> None:
    pytest.importorskip("openai")
    from openai._models import construct_type
    from openai.types.chat import ChatCompletion

    resp = chat(record("hi", [CALL], finish="tool_calls"))
    dumped = resp.model_dump()
    assert dumped["choices"][0]["message"]["tool_calls"] == [{
        "id": "call_1", "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
    }]
    assert dumped["choices"][0]["finish_reason"] == "tool_calls"
    assert dumped["usage"]["total_tokens"] == 8
    assert json.loads(resp.model_dump_json()) == json.loads(json.dumps(dumped))
    assert resp.to_dict()["object"] == "chat.completion"
    assert construct_type(type_=ChatCompletion, value=dumped) == resp


def test_the_chat_completion_payload_is_the_api_body_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins what is handed to the SDK: exactly the recorded values, no invented
    ``created`` (the record does not hold one)."""
    import novafabric.replay._dispatcher as dispatcher

    seen: list[tuple[str, str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        dispatcher, "_construct",
        lambda sdk, mod, name, data: seen.append((sdk, mod, name, data)) or data,
    )
    chat(record("hi", [CALL], finish="tool_calls"))
    assert seen == [("openai", "openai.types.chat", "ChatCompletion", {
        "id": "rid-1",
        "object": "chat.completion",
        "model": "m-1",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant", "content": "hi",
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
    })]


def test_without_the_openai_sdk_the_chat_completion_keeps_attribute_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "openai._models", None)  # import fails
    resp = chat(record("hi", [CALL], finish="tool_calls"))
    assert isinstance(resp, types.SimpleNamespace)
    assert (resp.id, resp.model) == ("rid-1", "m-1")
    (choice,) = resp.choices
    assert (choice.index, choice.finish_reason) == (0, "tool_calls")
    assert (choice.message.role, choice.message.content) == ("assistant", "hi")
    (tc,) = choice.message.tool_calls
    assert (tc.id, tc.type, tc.function.name) == ("call_1", "function", "get_weather")
    assert json.loads(tc.function.arguments) == {"city": "Paris"}
    assert (resp.usage.prompt_tokens, resp.usage.completion_tokens,
            resp.usage.total_tokens) == (3, 5, 8)
    assert chat(record("x")).choices[0].message.tool_calls is None
    assert chat(record("x", finish=None)).choices[0].finish_reason is None
    empty = chat({})
    assert (empty.id, empty.model, empty.choices) == ("replay-mocked", "", [])


# ── Anthropic Messages ───────────────────────────────────────────────────────


def anthropic_record(content: str | None = "Checking.", calls: list[Any] | None = None,
                     finish: str | None = "stop", raw: str | None = None) -> dict[str, Any]:
    ext = {"io.novafabric.provider_finish_reasons": [raw]} if raw else None
    return record(content, calls, finish=finish, system="anthropic", ext=ext)


def test_the_anthropic_payload_is_the_api_body_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs without the Anthropic SDK (not a dependency): pins what is handed to
    the SDK's ``construct_type``."""
    import novafabric.replay._dispatcher as dispatcher

    seen: list[tuple[str, str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        dispatcher, "_construct",
        lambda sdk, mod, name, data: seen.append((sdk, mod, name, data)) or data,
    )
    messages(anthropic_record("Checking.", [CALL], finish="tool_calls", raw="tool_use"))
    messages(anthropic_record("", finish=None))
    assert seen == [
        ("anthropic", "anthropic.types", "Message", {
            "id": "rid-1", "type": "message", "role": "assistant", "model": "m-1",
            "content": [
                {"type": "text", "text": "Checking."},
                {"type": "tool_use", "id": "call_1", "name": "get_weather",
                 "input": {"city": "Paris"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 3, "output_tokens": 5},
        }),
        ("anthropic", "anthropic.types", "Message", {
            "id": "rid-1", "type": "message", "role": "assistant", "model": "m-1",
            "content": [{"type": "text", "text": ""}],
            "stop_reason": None,
            "usage": {"input_tokens": 3, "output_tokens": 5},
        }),
    ]


def test_a_served_anthropic_message_is_the_sdks_message() -> None:
    pytest.importorskip("anthropic")
    from anthropic.types import Message, TextBlock, ToolUseBlock

    resp = messages(anthropic_record("Checking.", [CALL], finish="tool_calls", raw="tool_use"))
    assert isinstance(resp, Message)
    assert [type(b) for b in resp.content] == [TextBlock, ToolUseBlock]
    assert resp.content[0].text == "Checking."
    assert (resp.content[1].id, resp.content[1].name, resp.content[1].input) == (
        "call_1", "get_weather", {"city": "Paris"})
    assert (resp.id, resp.model, resp.role, resp.type) == ("rid-1", "m-1", "assistant",
                                                           "message")
    assert resp.stop_reason == "tool_use"
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (3, 5)
    assert resp.to_dict()["content"][1]["input"] == {"city": "Paris"}
    assert messages(anthropic_record("cut", finish=None)).stop_reason is None


def test_without_the_anthropic_sdk_the_message_keeps_attribute_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "anthropic._models", None)  # import fails
    resp = messages(anthropic_record("Checking.", [CALL, {"id": "bad"}], finish="tool_calls"))
    assert isinstance(resp, types.SimpleNamespace)
    assert (resp.id, resp.model, resp.role, resp.type) == ("rid-1", "m-1", "assistant",
                                                           "message")
    assert [b.type for b in resp.content] == ["text", "tool_use"]
    assert resp.content[0].text == "Checking."
    block = resp.content[1]
    assert (block.id, block.name, block.input) == ("call_1", "get_weather", {"city": "Paris"})
    assert isinstance(block.input, dict)  # a JSON object, as with the SDK
    assert resp.stop_reason == "tool_use"
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (3, 5)
    # The stream builder reads the same message: the arguments survive the fallback.
    events = list(build_served_response(
        "anthropic", anthropic_record("Checking.", [CALL], finish="tool_calls"),
        stream=True, asynchronous=False,
    ))
    deltas = [e.delta for e in events if e.type == "content_block_delta"]
    assert json.loads(deltas[1].partial_json) == {"city": "Paris"}
    empty = messages({})
    assert [(b.type, b.text) for b in empty.content] == [("text", "")]
    assert (empty.id, empty.stop_reason) == ("replay-mocked", "end_turn")
