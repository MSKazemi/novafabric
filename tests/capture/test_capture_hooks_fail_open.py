"""Capture hooks are fail-open: a recorder failure never reaches the workload.

The invariant (CLAUDE.md, ADR-0021): *never block the user workload on a
component failure*. Every hook used to write its record **outside** its own
guard, so a recorder failure surfaced in the user's call:

* the OpenAI/Anthropic hooks serialised ``kwargs["messages"]`` verbatim, and an
  SDK object passed back into the next call -- the everyday
  ``messages.append(completion.choices[0].message)`` pattern -- made
  ``json.dumps`` raise ``TypeError`` *after* the provider had answered;
* a failed error-record write replaced the SDK's own exception;
* the MCP hook recorded inside its ``try``, so a write failure turned a
  successful tool call into an exception (and an error record for it);
* the wire hooks built their record in a ``finally`` outside the guard, so a
  build failure replaced the response or the transport's exception.

Each test asserts the three halves of fail-open: the call's own return value
(or exception) passes through **unchanged**, the loss is logged as a structured
warning, and it is counted in the run's ``capture-health.json`` drop counts.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import sys
import types
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.event_recorder import (
    EventRecorder,
    clear_current_recorder,
    set_current_recorder,
)

RUN_ID = "01HXFAILOPEN00000000000000"
SPAN = "0" * 16
MODEL_CALLS = "model-calls.jsonl"
TOOL_CALLS = "tool-calls.jsonl"


class _RecorderDown(OSError):
    """What a full disk or a revoked capsule directory raises from a write."""


class _SdkBoom(Exception):
    """The SDK's own exception -- must reach the caller as the same object."""


class _FailingWriter(CapsuleWriter):
    """A real capsule whose every model/tool-call write fails."""

    def append_model_call(self, record: dict) -> None:  # type: ignore[type-arg]
        raise _RecorderDown("disk full")

    def append_tool_call(self, record: dict) -> None:  # type: ignore[type-arg]
        raise _RecorderDown("disk full")


@pytest.fixture(autouse=True)
def _clean_global_hook_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from novafabric.capture.hooks import uninstall_all

    # Warnings are once per (hook, stream, error type) per process: start each
    # test with none issued so every test can observe its own.
    fail_open_mod = sys.modules.get("novafabric.capture.hooks._fail_open")
    if fail_open_mod is not None:
        monkeypatch.setattr(fail_open_mod, "_warned", set())
    uninstall_all()
    set_current_recorder(None)
    yield
    set_current_recorder(None)


@pytest.fixture
def writer(tmp_path: Path) -> CapsuleWriter:
    w = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    w.open()
    return w


@pytest.fixture
def failing(tmp_path: Path) -> CapsuleWriter:
    w = _FailingWriter(run_id=RUN_ID, base_dir=tmp_path)
    w.open()
    return w


@pytest.fixture
def recorder(tmp_path: Path) -> Iterator[EventRecorder]:
    """The run's recorder, whose drop counts become ``capture-health.json``."""
    rec = EventRecorder(tmp_path / RUN_ID, RUN_ID, "capsule-1")
    (tmp_path / RUN_ID).mkdir(parents=True, exist_ok=True)
    set_current_recorder(rec)
    yield rec
    clear_current_recorder(rec)


def _records(w: CapsuleWriter, name: str = MODEL_CALLS) -> list[dict[str, Any]]:
    text = (w.capsule_dir / name).read_text()
    return [json.loads(line) for line in text.splitlines() if line]


def _assert_flagged(
    caplog: pytest.LogCaptureFixture, rec: EventRecorder, hook: str, stream: str
) -> None:
    """The loss was logged as a structured warning and counted for the run."""
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and getattr(r, "nova_event", None) == "capture.record_dropped"
    ]
    assert warnings, "a dropped record must be logged as a structured warning"
    w = warnings[0]
    assert getattr(w, "hook", None) == hook
    assert getattr(w, "stream", None) == stream
    assert getattr(w, "error_type", None)
    # Type only: an exception message may quote the payload being recorded.
    assert "disk full" not in w.getMessage() and "record build failed" not in w.getMessage()
    assert rec.drop_counts.get(stream, 0) >= 1, "the loss must reach capture-health.json"


def _chat_response() -> Any:
    return types.SimpleNamespace(
        id="chatcmpl-1", model="gpt-4o",
        choices=[types.SimpleNamespace(
            index=0, finish_reason="stop",
            message=types.SimpleNamespace(role="assistant", content="hello", tool_calls=None),
        )],
        usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )


def _responses_response() -> Any:
    return {"id": "resp_1", "model": "gpt-4o", "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1}}


MSGS = [{"role": "user", "content": "hi"}]


# ── OpenAI: SDK objects passed back into the next call ───────────────────────


def test_openai_message_object_passed_back_is_recorded_not_raised(writer: CapsuleWriter) -> None:
    """The reproduced harm: the next turn carries the SDK's own message object."""
    openai_types = pytest.importorskip("openai.types.chat")
    from novafabric.capture.hooks._openai import OpenAIHook

    previous = openai_types.ChatCompletionMessage(role="assistant", content="hello")
    messages = [*MSGS, previous, {"role": "user", "content": "and again"}]
    response = _chat_response()

    hook = OpenAIHook(writer=writer, parent_span_id=SPAN)
    out = hook._intercept_surface(
        "chat", MagicMock(return_value=response), "",
        {"model": "gpt-4o", "messages": messages},
    )

    assert out is response
    (record,) = _records(writer)
    # Serialised the way the SDK itself sends it (model_dump, exclude_unset).
    assert record["gen_ai.request.messages"][1] == {"role": "assistant", "content": "hello"}
    assert record["gen_ai.request.messages"][2] == {"role": "user", "content": "and again"}
    # The caller's list and object are untouched.
    assert messages[1] is previous


def test_openai_real_sdk_multi_turn_with_message_object(writer: CapsuleWriter) -> None:
    """End to end: the real SDK over a mock transport, turn 2 reuses turn 1's message."""
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    from novafabric.capture.hooks._openai import OpenAIHook

    def handler(request: Any) -> Any:
        return httpx.Response(200, json={
            "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-4o",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    hook = OpenAIHook(writer=writer, parent_span_id=SPAN)
    hook.install()
    try:
        client = openai.OpenAI(
            api_key="sk-test", http_client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        first = client.chat.completions.create(model="gpt-4o", messages=MSGS)
        second = client.chat.completions.create(
            model="gpt-4o",
            messages=[*MSGS, first.choices[0].message, {"role": "user", "content": "more"}],
        )
    finally:
        hook.uninstall()
    assert second.choices[0].message.content == "hello"
    records = _records(writer)
    assert len(records) == 2
    assert records[1]["gen_ai.request.messages"][1]["role"] == "assistant"


# ── OpenAI: a recorder failure never reaches the workload ────────────────────


@pytest.mark.parametrize("surface", ["chat", "responses"])
def test_openai_recorder_failure_returns_the_response_unchanged(
    failing: CapsuleWriter, recorder: EventRecorder,
    caplog: pytest.LogCaptureFixture, surface: str,
) -> None:
    from novafabric.capture.hooks._openai import OpenAIHook

    response = _chat_response() if surface == "chat" else _responses_response()
    hook = OpenAIHook(writer=failing, parent_span_id=SPAN)
    with caplog.at_level(logging.WARNING):
        out = hook._intercept_surface(
            surface, MagicMock(return_value=response), "", {"model": "gpt-4o", "messages": MSGS}
        )
    assert out is response
    _assert_flagged(caplog, recorder, "openai", MODEL_CALLS)


@pytest.mark.parametrize("surface", ["chat", "responses"])
def test_openai_async_recorder_failure_returns_the_response_unchanged(
    failing: CapsuleWriter, recorder: EventRecorder,
    caplog: pytest.LogCaptureFixture, surface: str,
) -> None:
    from novafabric.capture.hooks._openai import OpenAIHook

    response = _chat_response()

    async def create(**kwargs: Any) -> Any:
        return response

    hook = OpenAIHook(writer=failing, parent_span_id=SPAN)
    with caplog.at_level(logging.WARNING):
        out = asyncio.run(hook._intercept_async(
            surface, create, "", {"model": "gpt-4o", "messages": MSGS}
        ))
    assert out is response
    _assert_flagged(caplog, recorder, "openai", MODEL_CALLS)


def test_openai_sdk_exception_passes_through_when_its_error_record_fails(
    failing: CapsuleWriter, recorder: EventRecorder, caplog: pytest.LogCaptureFixture,
) -> None:
    from novafabric.capture.hooks._openai import OpenAIHook

    boom = _SdkBoom("rate limited")
    hook = OpenAIHook(writer=failing, parent_span_id=SPAN)
    with caplog.at_level(logging.WARNING), pytest.raises(_SdkBoom) as raised:
        hook._intercept_surface(
            "chat", MagicMock(side_effect=boom), "", {"model": "gpt-4o", "messages": MSGS}
        )
    assert raised.value is boom
    assert boom.__context__ is None, "the recorder's failure must not chain onto the SDK's"
    _assert_flagged(caplog, recorder, "openai", MODEL_CALLS)


def test_openai_async_sdk_exception_passes_through_when_its_error_record_fails(
    failing: CapsuleWriter, recorder: EventRecorder, caplog: pytest.LogCaptureFixture,
) -> None:
    from novafabric.capture.hooks._openai import OpenAIHook

    boom = _SdkBoom("timeout")

    async def create(**kwargs: Any) -> Any:
        raise boom

    hook = OpenAIHook(writer=failing, parent_span_id=SPAN)
    with caplog.at_level(logging.WARNING), pytest.raises(_SdkBoom) as raised:
        asyncio.run(hook._intercept_async("chat", create, "",
                                          {"model": "gpt-4o", "messages": MSGS}))
    assert raised.value is boom
    _assert_flagged(caplog, recorder, "openai", MODEL_CALLS)


def test_openai_stream_recorder_failure_still_yields_every_chunk(
    failing: CapsuleWriter, recorder: EventRecorder, caplog: pytest.LogCaptureFixture,
) -> None:
    from novafabric.capture.hooks._openai import OpenAIHook

    chunks = [
        types.SimpleNamespace(id="c", model="gpt-4o", usage=None, choices=[types.SimpleNamespace(
            index=0, finish_reason=None, delta=types.SimpleNamespace(
                role="assistant", content=t, tool_calls=None))])
        for t in ("hel", "lo")
    ]
    hook = OpenAIHook(writer=failing, parent_span_id=SPAN)
    with caplog.at_level(logging.WARNING):
        stream = hook._intercept_surface(
            "chat", MagicMock(return_value=iter(chunks)), "",
            {"model": "gpt-4o", "messages": MSGS, "stream": True},
        )
        got = list(stream)
    assert got == chunks
    _assert_flagged(caplog, recorder, "openai", MODEL_CALLS)


def test_openai_success_path_writes_exactly_one_record_and_no_warning(
    writer: CapsuleWriter, recorder: EventRecorder, caplog: pytest.LogCaptureFixture,
) -> None:
    """The guard is invisible when nothing fails."""
    from novafabric.capture.hooks._openai import OpenAIHook

    hook = OpenAIHook(writer=writer, parent_span_id=SPAN)
    with caplog.at_level(logging.WARNING):
        hook._intercept_surface("chat", MagicMock(return_value=_chat_response()), "",
                                {"model": "gpt-4o", "messages": MSGS})
    assert len(_records(writer)) == 1
    assert recorder.drop_counts == {}
    assert not [r for r in caplog.records if getattr(r, "nova_event", None) == "capture.record_dropped"]


# ── Anthropic ────────────────────────────────────────────────────────────────


def _anthropic_response() -> Any:
    return types.SimpleNamespace(
        id="msg_1", model="claude-x", stop_reason="end_turn",
        content=[types.SimpleNamespace(type="text", text="hi")],
        usage=types.SimpleNamespace(input_tokens=1, output_tokens=1),
    )


def test_anthropic_message_object_passed_back_is_recorded(writer: CapsuleWriter) -> None:
    """Anthropic is not a dependency: a pydantic model stands in for its types."""
    pydantic = pytest.importorskip("pydantic")
    from novafabric.capture.hooks._anthropic import AnthropicHook

    class TextBlock(pydantic.BaseModel):
        type: str = "text"
        text: str

    class Message(pydantic.BaseModel):
        role: str
        content: list[TextBlock]

    response = _anthropic_response()
    hook = AnthropicHook(writer=writer, parent_span_id=SPAN)
    out = hook._intercept(
        MagicMock(return_value=response), model="claude-x", max_tokens=5,
        messages=[*MSGS, Message.model_validate(  # parsed from JSON, like an SDK response
            {"role": "assistant", "content": [{"type": "text", "text": "earlier"}]}
        )],
    )
    assert out is response
    (record,) = _records(writer)
    assert record["gen_ai.request.messages"][1] == {
        "role": "assistant", "content": [{"type": "text", "text": "earlier"}],
    }


def test_anthropic_recorder_failure_returns_the_response_unchanged(
    failing: CapsuleWriter, recorder: EventRecorder, caplog: pytest.LogCaptureFixture,
) -> None:
    from novafabric.capture.hooks._anthropic import AnthropicHook

    response = _anthropic_response()
    hook = AnthropicHook(writer=failing, parent_span_id=SPAN)
    with caplog.at_level(logging.WARNING):
        out = hook._intercept(MagicMock(return_value=response), model="claude-x", messages=MSGS)
    assert out is response
    _assert_flagged(caplog, recorder, "anthropic", MODEL_CALLS)


def test_anthropic_async_recorder_failure_returns_the_response_unchanged(
    failing: CapsuleWriter, recorder: EventRecorder, caplog: pytest.LogCaptureFixture,
) -> None:
    from novafabric.capture.hooks._anthropic import AnthropicHook

    response = _anthropic_response()

    async def create(**kwargs: Any) -> Any:
        return response

    hook = AnthropicHook(writer=failing, parent_span_id=SPAN)
    with caplog.at_level(logging.WARNING):
        out = asyncio.run(hook._intercept_async(create, {"model": "claude-x", "messages": MSGS}))
    assert out is response
    _assert_flagged(caplog, recorder, "anthropic", MODEL_CALLS)


@pytest.mark.parametrize("is_async", [False, True])
def test_anthropic_sdk_exception_passes_through_when_its_error_record_fails(
    failing: CapsuleWriter, recorder: EventRecorder,
    caplog: pytest.LogCaptureFixture, is_async: bool,
) -> None:
    from novafabric.capture.hooks._anthropic import AnthropicHook

    boom = _SdkBoom("overloaded")
    hook = AnthropicHook(writer=failing, parent_span_id=SPAN)

    async def create(**kwargs: Any) -> Any:
        raise boom

    with caplog.at_level(logging.WARNING), pytest.raises(_SdkBoom) as raised:
        if is_async:
            asyncio.run(hook._intercept_async(create, {"model": "claude-x", "messages": MSGS}))
        else:
            hook._intercept(MagicMock(side_effect=boom), model="claude-x", messages=MSGS)
    assert raised.value is boom
    assert boom.__context__ is None
    _assert_flagged(caplog, recorder, "anthropic", MODEL_CALLS)


# ── MCP ──────────────────────────────────────────────────────────────────────


class _FakeContent:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeSession:
    server_name = "fs"
    server_version = "1"

    async def call_tool(self, name: str, arguments: dict | None = None) -> Any:  # type: ignore[type-arg]
        if name == "broken":
            raise _SdkBoom("server unreachable")
        return types.SimpleNamespace(content=[_FakeContent("ok")], isError=False)


def _mcp_modules() -> dict[str, object]:
    session_mod = types.ModuleType("mcp.client.session")
    session_mod.ClientSession = _FakeSession  # type: ignore[attr-defined]
    client_mod = types.ModuleType("mcp.client")
    client_mod.session = session_mod  # type: ignore[attr-defined]
    mcp_mod = types.ModuleType("mcp")
    mcp_mod.client = client_mod  # type: ignore[attr-defined]
    return {"mcp": mcp_mod, "mcp.client": client_mod, "mcp.client.session": session_mod}


def test_mcp_recorder_failure_returns_the_tool_result_unchanged(
    failing: CapsuleWriter, recorder: EventRecorder, caplog: pytest.LogCaptureFixture,
) -> None:
    from novafabric.capture.hooks._mcp import MCPHook

    with patch.dict(sys.modules, _mcp_modules()):
        hook = MCPHook(writer=failing, parent_span_id=SPAN)
        hook.install()
        try:
            with caplog.at_level(logging.WARNING):
                result = asyncio.run(_FakeSession().call_tool("read", {"path": "/x"}))
        finally:
            hook.uninstall()
    assert result.content[0].text == "ok"
    _assert_flagged(caplog, recorder, "mcp", TOOL_CALLS)


def test_mcp_tool_exception_passes_through_when_its_error_record_fails(
    failing: CapsuleWriter, recorder: EventRecorder, caplog: pytest.LogCaptureFixture,
) -> None:
    from novafabric.capture.hooks._mcp import MCPHook

    with patch.dict(sys.modules, _mcp_modules()):
        hook = MCPHook(writer=failing, parent_span_id=SPAN)
        hook.install()
        try:
            with caplog.at_level(logging.WARNING), pytest.raises(_SdkBoom) as raised:
                asyncio.run(_FakeSession().call_tool("broken", {}))
        finally:
            hook.uninstall()
    assert str(raised.value) == "server unreachable"
    assert raised.value.__context__ is None
    _assert_flagged(caplog, recorder, "mcp", TOOL_CALLS)


def test_mcp_non_json_arguments_are_recorded(writer: CapsuleWriter) -> None:
    """A ``datetime`` argument used to make the write raise *inside* the try:
    the hook then recorded an error for its own failure and raised it."""
    from novafabric.capture.hooks._mcp import MCPHook

    when = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    with patch.dict(sys.modules, _mcp_modules()):
        hook = MCPHook(writer=writer, parent_span_id=SPAN)
        hook.install()
        try:
            result = asyncio.run(_FakeSession().call_tool("read", {"since": when}))
        finally:
            hook.uninstall()
    assert result.content[0].text == "ok"
    (record,) = _records(writer, TOOL_CALLS)
    assert record["status"] == "success"
    assert record["arguments"] == {"since": "2026-10-22T12:00:00+00:00"}


# ── record.tool (PythonToolHook) ─────────────────────────────────────────────


def test_python_tool_recorder_failure_is_flagged(
    failing: CapsuleWriter, recorder: EventRecorder, caplog: pytest.LogCaptureFixture,
) -> None:
    """Already fail-open, but the loss used to be silent (a bare ``pass``)."""
    from novafabric.capture import record as record_api
    from novafabric.capture.hooks._python_tool import PythonToolHook

    @record_api.tool
    def add(a: int, b: int) -> int:
        return a + b

    hook = PythonToolHook(writer=failing, parent_span_id=SPAN)
    hook.install()
    try:
        with caplog.at_level(logging.WARNING):
            assert add(2, 3) == 5
    finally:
        hook.uninstall()
    _assert_flagged(caplog, recorder, "record.tool", TOOL_CALLS)


# ── wire hooks: a record-build failure must not replace the response ─────────


def _explode(*args: Any, **kwargs: Any) -> Any:
    raise TypeError("record build failed")


def test_httpx_record_build_failure_returns_the_response(
    writer: CapsuleWriter, recorder: EventRecorder, caplog: pytest.LogCaptureFixture,
) -> None:
    from novafabric.capture.hooks import _httpx

    hook = _httpx.HttpxHook(writer=writer, parent_span_id=SPAN)
    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="https://api.openai.com/v1/chat/completions")
    req.content = b'{"model":"gpt-4o","messages":[]}'
    resp = MagicMock(status_code=200)
    with patch.object(_httpx, "extract_request_attributes", _explode), \
            caplog.at_level(logging.WARNING):
        assert hook._wrapped_send(req, original=MagicMock(return_value=resp)) is resp
    _assert_flagged(caplog, recorder, "httpx", MODEL_CALLS)


def test_httpx_transport_exception_passes_through_when_its_record_fails(
    writer: CapsuleWriter, recorder: EventRecorder,
) -> None:
    from novafabric.capture.hooks import _httpx

    hook = _httpx.HttpxHook(writer=writer, parent_span_id=SPAN)
    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="https://api.openai.com/v1/chat/completions")
    req.content = b"{}"
    boom = _SdkBoom("connect timeout")
    with patch.object(_httpx, "extract_request_attributes", _explode), \
            pytest.raises(_SdkBoom) as raised:
        hook._wrapped_send(req, original=MagicMock(side_effect=boom))
    assert raised.value is boom
    assert recorder.drop_counts.get(MODEL_CALLS, 0) == 1


def test_httpx_async_record_build_failure_returns_the_response(
    writer: CapsuleWriter, recorder: EventRecorder,
) -> None:
    from novafabric.capture.hooks import _httpx

    hook = _httpx.HttpxHook(writer=writer, parent_span_id=SPAN)
    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="https://api.openai.com/v1/chat/completions")
    req.content = b"{}"
    resp = MagicMock(status_code=200)

    async def send(request: Any, **kwargs: Any) -> Any:
        return resp

    with patch.object(_httpx, "extract_request_attributes", _explode):
        assert asyncio.run(hook._wrapped_send_async(req, original=send)) is resp
    assert recorder.drop_counts.get(MODEL_CALLS, 0) == 1


def test_requests_record_build_failure_returns_the_response(
    writer: CapsuleWriter, recorder: EventRecorder,
) -> None:
    from novafabric.capture.hooks import _requests

    hook = _requests.RequestsHook(writer=writer, parent_span_id=SPAN)
    prepared = types.SimpleNamespace(
        url="https://api.openai.com/v1/chat/completions", body=b"{}", method="POST"
    )
    resp = MagicMock(status_code=200)
    with patch.object(_requests, "extract_request_attributes", _explode):
        assert hook._wrapped_send(prepared, original=MagicMock(return_value=resp)) is resp
    assert recorder.drop_counts.get(MODEL_CALLS, 0) == 1


def test_aiohttp_record_build_failure_returns_the_response(
    writer: CapsuleWriter, recorder: EventRecorder,
) -> None:
    from novafabric.capture.hooks import _aiohttp

    hook = _aiohttp.AiohttpHook(writer=writer, parent_span_id=SPAN)
    resp = MagicMock(status=200)

    async def request(method: str, url: Any, **kwargs: Any) -> Any:
        return resp

    with patch.object(_aiohttp, "extract_request_attributes", _explode):
        out = asyncio.run(hook._wrapped_request(
            "POST", "https://api.openai.com/v1/chat/completions", original=request, json={}
        ))
    assert out is resp
    assert recorder.drop_counts.get(MODEL_CALLS, 0) == 1


def test_urllib3_record_build_failure_returns_the_response(
    writer: CapsuleWriter, recorder: EventRecorder,
) -> None:
    from novafabric.capture.hooks import _urllib3

    hook = _urllib3.Urllib3Hook(writer=writer, parent_span_id=SPAN)
    pool = types.SimpleNamespace(scheme="https", host="api.openai.com", port=443)
    resp = MagicMock(status=200)
    with patch.object(_urllib3, "extract_request_attributes", _explode):
        out = hook._wrapped_urlopen(
            pool, "POST", "/v1/chat/completions",
            original=MagicMock(return_value=resp), body=b"{}",
        )
    assert out is resp
    assert recorder.drop_counts.get(MODEL_CALLS, 0) == 1


# ── work a hook does BEFORE the call is fail-open too ────────────────────────
#
# The wire hooks classified the URL (``str(url)`` + ``registry.is_known``)
# before calling through, outside any guard: a failure there -- a custom
# registry, an odd request object, a non-str urllib3 path -- raised in place of
# the user's request, which was never sent.


class _ExplodingRegistry:
    """A registry whose lookup fails (a user-supplied or broken registry)."""

    def is_known(self, url: str) -> bool:
        raise _RecorderDown("registry unavailable")

    def gen_ai_system(self, url: str) -> str:
        raise _RecorderDown("registry unavailable")


def test_requests_classification_failure_sends_the_request_unrecorded(
    writer: CapsuleWriter, recorder: EventRecorder, caplog: pytest.LogCaptureFixture,
) -> None:
    from novafabric.capture.hooks import _requests

    hook = _requests.RequestsHook(writer, SPAN, registry=_ExplodingRegistry())  # type: ignore[arg-type]
    prepared = types.SimpleNamespace(url="https://api.openai.com/v1/x", body=b"{}", method="POST")
    resp = MagicMock(status_code=200)
    original = MagicMock(return_value=resp)
    with caplog.at_level(logging.WARNING):
        assert hook._wrapped_send(prepared, original=original) is resp
    original.assert_called_once_with(prepared)
    _assert_flagged(caplog, recorder, "requests", MODEL_CALLS)


def test_aiohttp_classification_failure_sends_the_request_unrecorded(
    writer: CapsuleWriter, recorder: EventRecorder,
) -> None:
    from novafabric.capture.hooks import _aiohttp

    hook = _aiohttp.AiohttpHook(writer, SPAN, registry=_ExplodingRegistry())  # type: ignore[arg-type]
    resp = MagicMock(status=200)
    calls: list[tuple[str, Any]] = []

    async def request(method: str, url: Any, **kwargs: Any) -> Any:
        calls.append((method, url))
        return resp

    out = asyncio.run(hook._wrapped_request("POST", "https://api.openai.com/v1/x",
                                            original=request, json={}))
    assert out is resp
    assert calls == [("POST", "https://api.openai.com/v1/x")]
    assert recorder.drop_counts.get(MODEL_CALLS, 0) == 1


@pytest.mark.parametrize(
    ("registry", "path"),
    [
        (_ExplodingRegistry(), "/v1/chat/completions"),
        # A bytes path makes the URL reconstruction itself raise.
        (None, b"/v1/chat/completions"),
    ],
)
def test_urllib3_classification_failure_sends_the_request_unrecorded(
    writer: CapsuleWriter, recorder: EventRecorder, registry: Any, path: Any,
) -> None:
    from novafabric.capture.hooks import _urllib3

    hook = _urllib3.Urllib3Hook(writer, SPAN, registry=registry)
    pool = types.SimpleNamespace(scheme="https", host="api.openai.com", port=443)
    resp = MagicMock(status=200)
    original = MagicMock(return_value=resp)
    assert hook._wrapped_urlopen(pool, "POST", path, original=original, body=b"{}") is resp
    original.assert_called_once_with("POST", path, body=b"{}")
    assert recorder.drop_counts.get(MODEL_CALLS, 0) == 1


def test_httpx_classification_failure_sends_the_request_unrecorded(
    writer: CapsuleWriter, recorder: EventRecorder,
) -> None:
    from novafabric.capture.hooks import _httpx

    hook = _httpx.HttpxHook(writer, SPAN, registry=_ExplodingRegistry())  # type: ignore[arg-type]
    req = MagicMock()
    req.url.__str__ = MagicMock(return_value="https://api.openai.com/v1/x")
    resp = MagicMock(status_code=200)
    assert hook._wrapped_send(req, original=MagicMock(return_value=resp)) is resp
    assert recorder.drop_counts.get(MODEL_CALLS, 0) == 1


class _CreateKeywordOnly:
    """An SDK resource whose ``create`` is keyword-only, as openai's and anthropic's are."""

    def create(self, *, model: str) -> str:
        return f"created {model}"


def _patched_create(hook_name: str, writer: CapsuleWriter) -> Any:
    original = _CreateKeywordOnly.create
    if hook_name == "openai":
        from novafabric.capture.hooks._openai import OpenAIHook

        return OpenAIHook(writer, SPAN)._wrap(original, "chat", False)
    from novafabric.capture.hooks._anthropic import AnthropicHook

    return AnthropicHook(writer, SPAN)._wrap(original, False)


@pytest.mark.parametrize("hook_name", ["openai", "anthropic"])
def test_sdk_hook_positional_arguments_reach_the_sdk_unchanged(
    writer: CapsuleWriter, hook_name: str,
) -> None:
    """The SDK hooks forwarded ``**kwargs`` only and silently DROPPED positional
    arguments: a call the SDK rejects (``create`` is keyword-only) succeeded
    under capture. A positional call now reaches the SDK exactly as written."""
    resource = _CreateKeywordOnly()
    with pytest.raises(TypeError) as unhooked:
        resource.create("gpt-4o", model="m")  # type: ignore[misc]
    patched = _patched_create(hook_name, writer)
    with pytest.raises(TypeError) as hooked:
        patched(resource, "gpt-4o", model="m")
    assert str(hooked.value) == str(unhooked.value)
    assert patched(resource, model="m") == "created m"


class _HostileStream:
    """A response whose attribute lookup raises something other than AttributeError."""

    def __getattr__(self, name: str) -> Any:
        raise _RecorderDown("proxy lookup failed")


def test_stream_predicates_never_raise() -> None:
    from novafabric.capture.hooks._sdk_streams import is_async_stream, is_sync_stream

    assert is_sync_stream(_HostileStream()) is False
    assert is_async_stream(_HostileStream()) is False


# ── the capsule writer serialises what the SDKs hand it ──────────────────────


@dataclasses.dataclass
class _Point:
    x: int
    y: int


class _Opaque:
    def __repr__(self) -> str:  # never written: may carry anything
        return "SECRET-REPR"


def test_capsule_writer_serialises_non_json_values(writer: CapsuleWriter) -> None:
    pydantic = pytest.importorskip("pydantic")

    class Part(pydantic.BaseModel):
        type: str
        text: str

    record = {
        "model_call_id": "m1",
        "gen_ai.request.messages": [
            {"role": "user", "content": [Part(type="text", text="hi")]},
            {"role": "tool", "content": _Point(1, 2)},
        ],
        "when": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "tags": {"a"},
        "blob": b"\x00\x01",
        "path": Path("/tmp/x"),
        "opaque": _Opaque(),
    }
    writer.append_model_call(record)
    (out,) = _records(writer)
    assert out["gen_ai.request.messages"][0]["content"] == [{"type": "text", "text": "hi"}]
    assert out["gen_ai.request.messages"][1]["content"] == {"x": 1, "y": 2}
    assert out["when"] == "2026-01-02T00:00:00+00:00"
    assert out["tags"] == ["a"]
    assert out["path"] == "/tmp/x"
    assert "SECRET-REPR" not in json.dumps(out)
    assert "_Opaque" in out["opaque"]
    assert "bytes" in out["blob"]


def test_capsule_writer_plain_records_are_byte_identical(writer: CapsuleWriter) -> None:
    record = {"model_call_id": "m1", "gen_ai.request.messages": MSGS, "n": 1.5, "ok": True}
    writer.append_model_call(dict(record))
    line = (writer.capsule_dir / MODEL_CALLS).read_text()
    assert line == json.dumps(record, separators=(",", ":")) + "\n"
