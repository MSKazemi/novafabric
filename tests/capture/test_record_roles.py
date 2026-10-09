"""ADR-0305: wire records under an SDK call are transport, not model calls.

Every OpenAI/Anthropic SDK call used to land in ``model-calls.jsonl`` twice --
the ``httpx`` wire record (first, no response) and the SDK record -- so every
count of model calls was doubled. Both are still written (evidence is kept), but
the wire record now says it is ``transport`` and links to the SDK record, and
every counting consumer goes through :func:`logical_model_calls`.

These tests drive the real ``openai`` SDK over an ``httpx.MockTransport`` with
the real SDK and wire hooks installed in-process.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.record_roles import (
    LOGICAL_CALL_ID_EXT,
    RECORD_ROLE_EXT,
    classify_model_calls,
    count_logical_model_calls,
    count_logical_model_calls_in_file,
    current_sdk_call_id,
    is_transport_record,
    logical_model_calls,
    read_logical_model_calls,
    sdk_call_scope,
    stamp_wire_record,
)

httpx = pytest.importorskip("httpx")

URL = "https://api.openai.com/v1/chat/completions"
MSGS = [{"role": "user", "content": "hi"}]


def _body(rid: str = "chatcmpl-1", content: str = "hello") -> dict[str, Any]:
    return {
        "id": rid, "object": "chat.completion", "created": 1, "model": "gpt-4o",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
    }


def _sse(content: str = "hello") -> bytes:
    base = {"id": "chatcmpl-s", "object": "chat.completion.chunk", "created": 1,
            "model": "gpt-4o"}
    events = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                              "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {"content": content},
                              "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    lines = []
    for e in events:
        lines += ["data: " + json.dumps(e), ""]
    lines += ["data: [DONE]", ""]
    return ("\n".join(lines) + "\n").encode()


@pytest.fixture
def capsule(tmp_path: Path) -> Iterator[Path]:
    """A capsule with the OpenAI SDK hook and the httpx wire hook installed."""
    from novafabric.capture.hooks._httpx import HttpxHook
    from novafabric.capture.hooks._openai import OpenAIHook

    pytest.importorskip("openai")
    writer = CapsuleWriter("run-roles", tmp_path)
    writer.open()
    hooks: list[Any] = [
        HttpxHook(writer=writer, parent_span_id="0" * 16),
        OpenAIHook(writer=writer, parent_span_id="0" * 16),
    ]
    for h in hooks:
        h.install()
    try:
        yield writer.capsule_dir
    finally:
        for h in reversed(hooks):
            h.uninstall()


def _records(capsule: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (capsule / "model-calls.jsonl").read_text().splitlines()]


def _client(handler: Callable[[Any], Any], **kw: Any) -> Any:
    import openai

    return openai.OpenAI(
        api_key="sk-test", http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kw,
    )


def _async_client(handler: Callable[[Any], Any], **kw: Any) -> Any:
    import openai

    async def ahandler(request: Any) -> Any:
        return handler(request)

    return openai.AsyncOpenAI(
        api_key="sk-test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(ahandler)), **kw,
    )


def _ok(request: Any) -> Any:
    return httpx.Response(200, json=_body())


def _roles(records: list[dict[str, Any]]) -> list[str]:
    return [r["extensions"][RECORD_ROLE_EXT] for r in records]


def _assert_one_logical(records: list[dict[str, Any]], transports: int) -> dict[str, Any]:
    """``transports`` wire records, then one SDK record they all link to."""
    assert _roles(records) == ["transport"] * transports + ["logical"]
    sdk = records[-1]
    assert sdk["gen_ai.response.choices"], "the SDK record carries the response"
    assert sdk["extensions"][LOGICAL_CALL_ID_EXT] == sdk["model_call_id"]
    for wire in records[:-1]:
        assert wire["gen_ai.response.choices"] == []
        assert wire["extensions"][LOGICAL_CALL_ID_EXT] == sdk["model_call_id"]
    assert count_logical_model_calls(records) == 1
    assert logical_model_calls(records) == [sdk]
    return sdk


# ── capture: the four SDK call shapes ────────────────────────────────────────


def test_sync_sdk_call_is_one_logical_call(capsule: Path) -> None:
    _client(_ok).chat.completions.create(model="gpt-4o", messages=MSGS)
    _assert_one_logical(_records(capsule), transports=1)


def test_async_sdk_call_is_one_logical_call(capsule: Path) -> None:
    async def run() -> None:
        await _async_client(_ok).chat.completions.create(model="gpt-4o", messages=MSGS)

    asyncio.run(run())
    _assert_one_logical(_records(capsule), transports=1)


def test_streamed_sdk_call_is_one_logical_call(capsule: Path) -> None:
    def sse(request: Any) -> Any:
        return httpx.Response(200, content=_sse(),
                              headers={"content-type": "text/event-stream"})

    stream = _client(sse).chat.completions.create(model="gpt-4o", messages=MSGS, stream=True)
    assert "".join(c.choices[0].delta.content or "" for c in stream if c.choices) == "hello"
    sdk = _assert_one_logical(_records(capsule), transports=1)
    assert sdk["nova.streaming"]["streamed"] is True


def test_async_streamed_sdk_call_is_one_logical_call(capsule: Path) -> None:
    def sse(request: Any) -> Any:
        return httpx.Response(200, content=_sse(),
                              headers={"content-type": "text/event-stream"})

    async def run() -> None:
        stream = await _async_client(sse).chat.completions.create(
            model="gpt-4o", messages=MSGS, stream=True
        )
        async for _ in stream:
            pass

    asyncio.run(run())
    _assert_one_logical(_records(capsule), transports=1)


def test_retried_sdk_call_two_wire_attempts_one_logical_call(capsule: Path) -> None:
    attempts: list[int] = []

    def flaky(request: Any) -> Any:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(500, json={"error": {"message": "boom"}},
                                  headers={"retry-after-ms": "1"})
        return httpx.Response(200, json=_body())

    _client(flaky, max_retries=2).chat.completions.create(model="gpt-4o", messages=MSGS)
    records = _records(capsule)
    _assert_one_logical(records, transports=2)
    assert [r["status"] for r in records] == ["error", "success", "success"]
    # The two attempts are one logical call in every count-based consumer.
    assert count_logical_model_calls_in_file(capsule / "model-calls.jsonl") == 1


def test_failed_sdk_call_is_one_logical_error_record(capsule: Path) -> None:
    import openai

    def bad(request: Any) -> Any:
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    with pytest.raises(openai.BadRequestError):
        _client(bad).chat.completions.create(model="gpt-4o", messages=MSGS)
    records = _records(capsule)
    assert _roles(records) == ["transport", "logical"]
    wire, sdk = records
    assert sdk["status"] == "error" and sdk["error"]["type"] == "BadRequestError"
    # Error records name their surface too, so a consumer can tell the SDK
    # record of a failed call from a wire record without guessing.
    assert sdk["extensions"]["io.novafabric.api_surface"] == "openai.chat.completions"
    assert wire["extensions"][LOGICAL_CALL_ID_EXT] == sdk["model_call_id"]
    assert count_logical_model_calls(records) == 1


def test_raw_httpx_call_without_sdk_stays_logical(capsule: Path) -> None:
    with httpx.Client(transport=httpx.MockTransport(_ok)) as client:
        client.post(URL, json={"model": "gpt-4o", "messages": MSGS})
    (record,) = _records(capsule)
    assert record["extensions"][RECORD_ROLE_EXT] == "logical"
    assert record["extensions"][LOGICAL_CALL_ID_EXT] == record["model_call_id"]
    assert count_logical_model_calls([record]) == 1


def test_anthropic_hook_marks_the_wire_records_it_covers(tmp_path: Path) -> None:
    """The anthropic SDK is not a dependency: drive the hook with a stand-in
    ``create`` that makes the HTTP call through a hooked ``httpx.Client``."""
    from novafabric.capture.hooks._anthropic import AnthropicHook
    from novafabric.capture.hooks._httpx import HttpxHook

    writer = CapsuleWriter("run-a", tmp_path)
    writer.open()
    wire = HttpxHook(writer=writer, parent_span_id="0" * 16)
    wire.install()
    try:
        hook = AnthropicHook(writer=writer, parent_span_id="0" * 16)

        def create(**kwargs: Any) -> Any:
            def handler(request: Any) -> Any:
                return httpx.Response(200, json={})

            with httpx.Client(transport=httpx.MockTransport(handler)) as c:
                c.post("https://api.anthropic.com/v1/messages", json=kwargs)
            import types

            return types.SimpleNamespace(
                id="msg_1", model="claude-x", stop_reason="end_turn",
                content=[types.SimpleNamespace(type="text", text="hi")],
                usage=types.SimpleNamespace(input_tokens=1, output_tokens=1),
            )

        hook._intercept(create, model="claude-x", messages=MSGS, max_tokens=5)
    finally:
        wire.uninstall()
    _assert_one_logical(_records(writer.capsule_dir), transports=1)


# ── the scope itself ─────────────────────────────────────────────────────────


def test_scope_is_restored_and_does_not_leak() -> None:
    assert current_sdk_call_id() is None
    with sdk_call_scope("outer"):
        with sdk_call_scope("inner"):
            assert current_sdk_call_id() == "inner"
        assert current_sdk_call_id() == "outer"
    assert current_sdk_call_id() is None
    with pytest.raises(RuntimeError), sdk_call_scope("x"):
        raise RuntimeError
    assert current_sdk_call_id() is None


def test_concurrent_async_calls_do_not_cross_link() -> None:
    async def one(cid: str) -> dict[str, Any]:
        with sdk_call_scope(cid):
            await asyncio.sleep(0)
            return stamp_wire_record({"model_call_id": f"w-{cid}"})

    async def run() -> list[dict[str, Any]]:
        return list(await asyncio.gather(one("a"), one("b")))

    a, b = asyncio.run(run())
    assert a["extensions"][LOGICAL_CALL_ID_EXT] == "a"
    assert b["extensions"][LOGICAL_CALL_ID_EXT] == "b"


# ── read side: orphans and mixed capsules ────────────────────────────────────


def _rec(cid: str, role: str | None = None, link: str | None = None, **kw: Any) -> dict:
    r: dict[str, Any] = {"model_call_id": cid, "gen_ai.request.model": "m",
                         "gen_ai.request.messages": MSGS, **kw}
    if role:
        r["extensions"] = {RECORD_ROLE_EXT: role, LOGICAL_CALL_ID_EXT: link or cid}
    return r


def test_orphaned_transport_records_count_once() -> None:
    """The SDK record was never written (call cancelled): the last attempt counts."""
    records = [_rec("w1", "transport", "L"), _rec("w2", "transport", "L")]
    assert [r["model_call_id"] for r in logical_model_calls(records)] == ["w2"]
    assert is_transport_record(records[0])


def test_marked_capsule_keeps_unmarked_records_logical() -> None:
    """Adapter / proxy / OTel-ingest records carry no marker: they are logical."""
    records = [
        _rec("w", "transport", "s"), _rec("s", "logical"),
        _rec("adapter", **{"gen_ai.response.choices": []}),
    ]
    assert [r["model_call_id"] for r in logical_model_calls(records)] == ["s", "adapter"]


def test_malformed_lines_still_count(tmp_path: Path) -> None:
    path = tmp_path / "model-calls.jsonl"
    path.write_text(
        json.dumps(_rec("w", "transport", "s")) + "\n{not json\n"
        + json.dumps(_rec("s", "logical")) + "\n\n"
    )
    assert count_logical_model_calls_in_file(path) == 2
    assert [r["model_call_id"] for r in read_logical_model_calls(path)] == ["s"]
    assert count_logical_model_calls_in_file(tmp_path / "missing.jsonl") == 0


# ── pre-ADR-0305 capsules: the reader-side fallback ──────────────────────────

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pre-adr0305-double-record"


def test_legacy_fixture_capsule_counts_each_sdk_call_once() -> None:
    """A capsule captured before ADR-0305 (markers stripped from a real capture):
    sync, streamed, retried (2 wire attempts) and failed SDK calls, a raw httpx
    call, and an async call recorded by the wire hook only (pre-ADR-0304)."""
    path = FIXTURE / "model-calls.jsonl"
    records = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    assert not any(RECORD_ROLE_EXT in (r.get("extensions") or {}) for r in records)
    expected = json.loads((FIXTURE / "expected.json").read_text())
    assert len(records) == expected["records"]
    roles = classify_model_calls(records)
    assert [r["model_call_id"] for r in logical_model_calls(records)] == expected["logical"]
    assert all(role.inferred for role in roles if role.role == "transport")
    assert count_logical_model_calls_in_file(path) == len(expected["logical"])


def test_legacy_fallback_without_timestamps_needs_adjacency() -> None:
    wire = _rec("w", **{"gen_ai.response.choices": []})
    sdk = _rec("s", **{"gen_ai.response.choices": [{"index": 0}]})
    other = _rec("o", **{"gen_ai.request.messages": [{"role": "user", "content": "x"}],
                         "gen_ai.response.choices": [{"index": 0}]})
    assert [r["model_call_id"] for r in logical_model_calls([wire, sdk])] == ["s"]
    # A retry run of the same request still pairs.
    assert [r["model_call_id"] for r in logical_model_calls([wire, dict(wire), sdk])] == ["s"]
    # Something else in between: not certain, so nothing is collapsed.
    assert len(logical_model_calls([wire, other, sdk])) == 3


def test_legacy_fallback_needs_temporal_containment() -> None:
    wire = _rec("w", started_at="2026-01-01T00:00:01.000000Z",
                finished_at="2026-01-01T00:00:02.000000Z",
                **{"gen_ai.response.choices": []})
    inside = _rec("s", started_at="2026-01-01T00:00:00.500000Z",
                  finished_at="2026-01-01T00:00:03.000000Z",
                  **{"gen_ai.response.choices": [{"index": 0}]})
    later = _rec("s2", started_at="2026-01-01T00:00:05.000000Z",
                 finished_at="2026-01-01T00:00:06.000000Z",
                 **{"gen_ai.response.choices": [{"index": 0}]})
    assert [r["model_call_id"] for r in logical_model_calls([wire, inside])] == ["s"]
    # A same-prompt SDK call that started after the wire call is not its cover.
    assert len(logical_model_calls([wire, later])) == 2
