"""openai 3.x sends over ``httpx2``: the wire hook must see it (ADR-0305 kept).

``openai`` 3.x builds its default client on ``httpx2``: ``openai.OpenAI()``
uses ``SyncHttpxClientWrapper``, whose MRO is ``_DefaultHttpxClient ->
httpx2.Client``, not ``httpx.Client``. The wire hook patched ``httpx`` only, so
an OpenAI call left **no transport record and no network event**: the capsule
had the SDK's logical record and nothing showing what went over the wire
(retries included), and ``network_events.jsonl`` had no AI egress at all.

These tests drive the real ``openai`` SDK over ``httpx2.MockTransport`` with the
hooks installed through :func:`install_all`, exactly as a capture does, and pin
ADR-0305: one SDK call is still **one** logical model call -- the wire copy is
transport, whichever client library carried it.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.event_recorder import set_current_recorder
from novafabric.capture.record_roles import (
    LOGICAL_CALL_ID_EXT,
    RECORD_ROLE_EXT,
    count_logical_model_calls,
    count_logical_model_calls_in_file,
)

openai = pytest.importorskip("openai")
httpx = pytest.importorskip("httpx")
httpx2 = pytest.importorskip("httpx2")

SPAN = "0" * 16
MSGS = [{"role": "user", "content": "hi"}]


def _body() -> dict[str, Any]:
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-4o",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
    }


@pytest.fixture(autouse=True)
def _clean_global_hook_state() -> Iterator[None]:
    from novafabric.capture.hooks import uninstall_all

    uninstall_all()
    set_current_recorder(None)
    yield
    uninstall_all()
    set_current_recorder(None)


@pytest.fixture
def capsule(tmp_path: Path) -> Iterator[Path]:
    """A capture with every built-in hook installed, as ``nova capture`` does."""
    from novafabric.capture.hooks import install_all, uninstall_all

    writer = CapsuleWriter("run-httpx2", tmp_path)
    writer.open()
    token = install_all(writer, SPAN)
    try:
        yield writer.capsule_dir
    finally:
        uninstall_all(token)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x]


def _model_calls(capsule: Path) -> list[dict[str, Any]]:
    return _jsonl(capsule / "model-calls.jsonl")


def _network_events(capsule: Path) -> list[dict[str, Any]]:
    return _jsonl(capsule / "network_events.jsonl")


def _sync_client(handler: Callable[[Any], Any], **kw: Any) -> Any:
    """The SDK's own default client class, over a mock transport."""
    return openai.OpenAI(
        api_key="sk-test",
        http_client=openai.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
        **kw,
    )


def _async_client(handler: Callable[[Any], Any]) -> Any:
    async def ahandler(request: Any) -> Any:
        return handler(request)

    return openai.AsyncOpenAI(
        api_key="sk-test",
        http_client=openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(ahandler)),
    )


def _ok(request: Any) -> Any:
    return httpx2.Response(200, json=_body())


def _assert_one_call_over(capsule: Path, library: str, attempts: int = 1) -> None:
    records = _model_calls(capsule)
    roles = [r["extensions"][RECORD_ROLE_EXT] for r in records]
    assert roles == ["transport"] * attempts + ["logical"], roles
    sdk = records[-1]
    assert sdk["gen_ai.response.choices"], "the SDK record carries the response"
    for wire in records[:-1]:
        assert wire["extensions"][LOGICAL_CALL_ID_EXT] == sdk["model_call_id"]
        assert wire["endpoint"] == "https://api.openai.com/v1/chat/completions"
    # ADR-0305: one SDK call is one model call, whatever carried it.
    assert count_logical_model_calls(records) == 1
    assert count_logical_model_calls_in_file(capsule / "model-calls.jsonl") == 1
    events = _network_events(capsule)
    assert len(events) == attempts
    for event in events:
        assert event["library"] == library
        assert event["host"] == "api.openai.com"
        assert event["is_ai_api"] is True


# ── the premise ──────────────────────────────────────────────────────────────


def test_openai_default_client_is_httpx2_not_httpx() -> None:
    """If this ever fails, openai moved client library again: re-check coverage."""
    client = _sync_client(_ok)
    assert isinstance(client._client, httpx2.Client)
    assert not isinstance(client._client, httpx.Client)


# ── one OpenAI call over httpx2 = one model call + one network event ─────────


def test_sync_openai_call_over_httpx2_is_one_model_call_and_one_network_event(
    capsule: Path,
) -> None:
    out = _sync_client(_ok).chat.completions.create(model="gpt-4o", messages=MSGS)
    assert out.choices[0].message.content == "hello"
    _assert_one_call_over(capsule, "httpx2")


def test_async_openai_call_over_httpx2_is_one_model_call_and_one_network_event(
    capsule: Path,
) -> None:
    async def run() -> Any:
        return await _async_client(_ok).chat.completions.create(model="gpt-4o", messages=MSGS)

    out = asyncio.run(run())
    assert out.choices[0].message.content == "hello"
    _assert_one_call_over(capsule, "httpx2")


def test_retried_openai_call_over_httpx2_two_attempts_one_model_call(capsule: Path) -> None:
    attempts: list[int] = []

    def flaky(request: Any) -> Any:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx2.Response(500, json={"error": {"message": "boom"}},
                                   headers={"retry-after-ms": "1"})
        return httpx2.Response(200, json=_body())

    _sync_client(flaky, max_retries=2).chat.completions.create(model="gpt-4o", messages=MSGS)
    _assert_one_call_over(capsule, "httpx2", attempts=2)
    assert [e["status_code"] for e in _network_events(capsule)] == [500, 200]


def test_openai_call_over_plain_httpx_still_one_model_call_and_one_network_event(
    capsule: Path,
) -> None:
    """A caller-supplied ``httpx.Client`` (still accepted by openai 3.x)."""
    client = openai.OpenAI(
        api_key="sk-test",
        http_client=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_body())
        )),
    )
    client.chat.completions.create(model="gpt-4o", messages=MSGS)
    _assert_one_call_over(capsule, "httpx")


def test_raw_httpx2_call_without_sdk_is_one_logical_call(capsule: Path) -> None:
    with httpx2.Client(transport=httpx2.MockTransport(_ok)) as client:
        client.post("https://api.openai.com/v1/chat/completions",
                    json={"model": "gpt-4o", "messages": MSGS})
    (record,) = _model_calls(capsule)
    assert record["extensions"][RECORD_ROLE_EXT] == "logical"
    assert record["gen_ai.request.messages"] == MSGS
    (event,) = _network_events(capsule)
    assert event["library"] == "httpx2"


def test_unregistered_host_over_httpx2_is_not_recorded(capsule: Path) -> None:
    with httpx2.Client(transport=httpx2.MockTransport(
        lambda request: httpx2.Response(200, json={})
    )) as client:
        client.get("https://example.com/health")
    assert _model_calls(capsule) == []
    assert _network_events(capsule) == []


# ── installation: optional, reversible, never doubled ────────────────────────


def test_httpx2_hook_is_registered_as_optional_built_in() -> None:
    from novafabric.capture.hooks import _BUILT_IN_HOOKS

    assert ("httpx2", "novafabric.capture.hooks._httpx", "Httpx2Hook") in _BUILT_IN_HOOKS


def test_httpx2_hook_is_a_no_op_when_httpx2_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novafabric.capture.hooks._httpx import Httpx2Hook

    monkeypatch.setitem(sys.modules, "httpx2", None)  # import raises ImportError
    writer = CapsuleWriter("run-absent", tmp_path)
    writer.open()
    hook = Httpx2Hook(writer=writer, parent_span_id=SPAN)
    hook.install()
    hook.uninstall()
    assert hook._patched == []


def test_httpx2_hook_uninstall_restores_the_original_send(tmp_path: Path) -> None:
    from novafabric.capture.hooks._httpx import Httpx2Hook

    sync_send, async_send = httpx2.Client.send, httpx2.AsyncClient.send
    writer = CapsuleWriter("run-restore", tmp_path)
    writer.open()
    hook = Httpx2Hook(writer=writer, parent_span_id=SPAN)
    hook.install()
    assert httpx2.Client.send is not sync_send
    assert httpx2.AsyncClient.send is not async_send
    hook.uninstall()
    assert httpx2.Client.send is sync_send
    assert httpx2.AsyncClient.send is async_send


def test_a_client_inheriting_a_patched_send_is_recorded_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Should a future httpx2 subclass httpx (or both hooks patch one inherited
    ``send``), one HTTP attempt must still be one record, and uninstall must not
    leave a stale override on the subclass."""
    from novafabric.capture.hooks._httpx import HttpxHook

    fake = types.ModuleType("nf_fake_httpx_sub")
    fake.Client = type("Client", (httpx.Client,), {})  # type: ignore[attr-defined]
    fake.AsyncClient = type("AsyncClient", (httpx.AsyncClient,), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nf_fake_httpx_sub", fake)

    class SubHook(HttpxHook):
        module_name = "nf_fake_httpx_sub"
        library = "nf_fake_httpx_sub"

    writer = CapsuleWriter("run-sub", tmp_path)
    writer.open()
    base = HttpxHook(writer=writer, parent_span_id=SPAN)
    sub = SubHook(writer=writer, parent_span_id=SPAN)
    base.install()
    sub.install()
    try:
        with fake.Client(transport=httpx.MockTransport(  # type: ignore[attr-defined]
            lambda request: httpx.Response(200, json={})
        )) as client:
            client.post("https://api.openai.com/v1/chat/completions", json={"model": "m"})
    finally:
        sub.uninstall()
        base.uninstall()
    assert len(_model_calls(writer.capsule_dir)) == 1
    assert "send" not in vars(fake.Client)  # type: ignore[attr-defined]
    assert "send" not in vars(fake.AsyncClient)  # type: ignore[attr-defined]
