"""A streamed SDK call is recorded into the capture that MADE it — or nowhere.

A ``stream=True`` call is recorded when its stream ends (ADR-0304): exhausted,
closed, exited, or garbage-collected. That moment can come long after the call
was made, in another task, or after the capture that made it has finished and a
second capture has started. The recording path used to resolve the capsule
writer *when the stream ended*, so a stream opened in capture A and collected
while capture B ran wrote A's prompt and A's streamed answer into **B's**
capsule, and gave B a logical model call it never made (adversarial review
2026-10-22, ``a3_cross_capsule.py create``).

The contract pinned here:

* a stream binds to the capture that was active when the stream was **created**
  and records only there, whichever task, thread or capture finalises it;
* if that capture has already ended, the record is **dropped and counted**
  (fail-open, ADR-0021) — never written into another capsule, and never
  appended to a finished one;
* ADR-0305 counting is unchanged: one SDK call is one logical model call, in
  the capsule of the capture that made it.

Every finalisation path is covered for both SDK hooks — garbage collection,
``close()``, full iteration and context-manager exit, sync and async — for two
sequential captures and for two concurrent task-scoped captures (ADR-0224 D3).
"""

from __future__ import annotations

import asyncio
import gc
import json
import types
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from novafabric.capture import hooks
from novafabric.capture.capsule import CapsuleWriter
from novafabric.capture.event_recorder import (
    get_current_recorder,
    get_current_writer,
    set_current_recorder,
)
from novafabric.capture.hooks import _sdk_streams
from novafabric.capture.hooks._anthropic import AnthropicHook
from novafabric.capture.hooks._fail_open import MODEL_CALLS_STREAM
from novafabric.capture.hooks._openai import OpenAIHook
from novafabric.capture.record_roles import count_logical_model_calls_in_file

SPAN = "0" * 16


# ── fixtures and helpers ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    hooks.uninstall_all()
    set_current_recorder(None)
    yield
    leaked = get_current_writer(None)
    hooks.uninstall_all()
    set_current_recorder(None)
    assert leaked is None, "a capture binding leaked out of the test"


def ns(value: Any) -> Any:
    if isinstance(value, dict):
        return types.SimpleNamespace(**{k: ns(v) for k, v in value.items()})
    if isinstance(value, list):
        return [ns(v) for v in value]
    return value


def _writer(base: Path, run_id: str) -> CapsuleWriter:
    w = CapsuleWriter(run_id=run_id, base_dir=base)
    w.open()
    return w


def _text(writer: CapsuleWriter) -> str:
    path = writer.capsule_dir / "model-calls.jsonl"
    return path.read_text() if path.exists() else ""


def _records(writer: CapsuleWriter) -> list[dict[str, Any]]:
    return [json.loads(line) for line in _text(writer).splitlines() if line.strip()]


def _logical(writer: CapsuleWriter) -> int:
    return count_logical_model_calls_in_file(writer.capsule_dir / "model-calls.jsonl")


class FakeStream:
    """The shape of an SDK ``Stream``: iterable, closable, a context manager."""

    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)

    def __iter__(self) -> FakeStream:
        return self

    def __next__(self) -> Any:
        if not self._items:
            raise StopIteration
        return self._items.pop(0)

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def close(self) -> None:
        self._items.clear()


class FakeAsyncStream:
    """The shape of an SDK ``AsyncStream``."""

    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)

    def __aiter__(self) -> FakeAsyncStream:
        return self

    async def __anext__(self) -> Any:
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)

    async def __aenter__(self) -> FakeAsyncStream:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def close(self) -> None:
        self._items.clear()


def _chat_chunks(tag: str) -> list[Any]:
    return [
        ns({"id": f"c-{tag}", "model": "gpt-4o", "usage": None,
            "choices": [{"index": 0, "delta": {"role": "assistant",
                                               "content": f"ANSWER-{tag}-1 "},
                         "finish_reason": None}]}),
        ns({"id": f"c-{tag}", "model": "gpt-4o", "usage": None,
            "choices": [{"index": 0, "delta": {"content": f"ANSWER-{tag}-2"},
                         "finish_reason": "stop"}]}),
    ]


def _anthropic_events(tag: str) -> list[Any]:
    return [ns(e) for e in [
        {"type": "message_start", "message": {"id": f"m-{tag}", "model": "claude-x",
                                              "usage": {"input_tokens": 1}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": f"ANSWER-{tag}-1 "}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": f"ANSWER-{tag}-2"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 2}},
    ]]


@dataclass(frozen=True)
class Provider:
    """One SDK hook, driven through the exact entry point its patch calls."""

    name: str
    hook_cls: type

    def items(self, tag: str) -> list[Any]:
        return _chat_chunks(tag) if self.name == "openai" else _anthropic_events(tag)

    def kwargs(self, tag: str) -> dict[str, Any]:
        msgs = [{"role": "user", "content": f"PROMPT-{tag}"}]
        if self.name == "openai":
            return {"model": "gpt-4o", "messages": msgs, "stream": True}
        return {"model": "claude-x", "messages": msgs, "stream": True, "max_tokens": 8}

    def open_sync(self, hook: Any, tag: str) -> Any:
        inner = FakeStream(self.items(tag))
        if self.name == "openai":
            return hook._intercept(lambda **kw: inner, "", **self.kwargs(tag))
        return hook._intercept(lambda **kw: inner, **self.kwargs(tag))

    async def open_async(self, hook: Any, tag: str) -> Any:
        inner = FakeAsyncStream(self.items(tag))

        async def create(**kw: Any) -> FakeAsyncStream:
            return inner

        if self.name == "openai":
            return await hook._intercept_async("chat", create, "", self.kwargs(tag))
        return await hook._intercept_async(create, self.kwargs(tag))


PROVIDERS = [Provider("openai", OpenAIHook), Provider("anthropic", AnthropicHook)]
SYNC_FINALISERS = ["gc", "close", "iterate", "exit"]
ASYNC_FINALISERS = ["gc", "close", "iterate", "exit"]


def _finalise_sync(box: list[Any], how: str) -> None:
    """End the one stream in *box* the way *how* names, in the calling context."""
    stream = box.pop()
    if how == "gc":
        del stream
        gc.collect()
    elif how == "close":
        stream.close()
    elif how == "iterate":
        for _ in stream:
            pass
    elif how == "exit":
        stream.__exit__(None, None, None)
    else:  # pragma: no cover - a typo in the parametrisation
        raise AssertionError(how)


async def _finalise_async(box: list[Any], how: str) -> None:
    stream = box.pop()
    if how == "gc":
        del stream
        gc.collect()
    elif how == "close":
        await stream.close()
    elif how == "iterate":
        async for _ in stream:
            pass
    elif how == "exit":
        await stream.__aexit__(None, None, None)
    else:  # pragma: no cover
        raise AssertionError(how)


def _layer(provider: Provider, writer: CapsuleWriter) -> Any:
    """The hook as the owner's patch layer holds it: built with the owner's writer."""
    return provider.hook_cls(writer=writer, parent_span_id=SPAN)


# ── two sequential captures ─────────────────────────────────────────────────


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)
@pytest.mark.parametrize("how", SYNC_FINALISERS)
def test_a_sync_stream_finalised_after_its_capture_ended_never_reaches_the_next(
    tmp_path: Path, provider: Provider, how: str
) -> None:
    wa = _writer(tmp_path, "run-A")
    ta = hooks.install_all(wa, SPAN)
    layer_a = _layer(provider, wa)
    box = [provider.open_sync(layer_a, "A")]
    next(box[0])  # A's workload read one chunk
    recorder_a = get_current_recorder()
    hooks.uninstall_all(ta)
    layer_a.uninstall()
    dropped_before = _sdk_streams.late_stream_records_dropped()

    wb = _writer(tmp_path, "run-B")
    tb = hooks.install_all(wb, SPAN)
    _finalise_sync(box, how)  # ...and the stream ends while B runs
    hooks.uninstall_all(tb)

    assert "PROMPT-A" not in _text(wb) and "ANSWER-A" not in _text(wb)
    assert _records(wb) == [] and _logical(wb) == 0, "B made no model call"
    # A had ended: the record is dropped, never appended to a finished capsule.
    assert _records(wa) == []
    assert _sdk_streams.late_stream_records_dropped() == dropped_before + 1
    assert recorder_a is not None
    assert recorder_a.drop_counts.get(MODEL_CALLS_STREAM) == 1


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)
@pytest.mark.parametrize("how", ASYNC_FINALISERS)
def test_an_async_stream_finalised_after_its_capture_ended_never_reaches_the_next(
    tmp_path: Path, provider: Provider, how: str
) -> None:
    wa, wb = _writer(tmp_path, "run-A"), _writer(tmp_path, "run-B")

    async def main() -> int:
        ta = hooks.install_all(wa, SPAN)
        layer_a = _layer(provider, wa)
        box = [await provider.open_async(layer_a, "A")]
        await box[0].__anext__()
        hooks.uninstall_all(ta)
        layer_a.uninstall()
        before = _sdk_streams.late_stream_records_dropped()
        tb = hooks.install_all(wb, SPAN)
        await _finalise_async(box, how)
        hooks.uninstall_all(tb)
        return before

    before = asyncio.run(main())
    assert "PROMPT-A" not in _text(wb) and "ANSWER-A" not in _text(wb)
    assert _records(wb) == [] and _logical(wb) == 0
    assert _records(wa) == []
    assert _sdk_streams.late_stream_records_dropped() == before + 1


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)
@pytest.mark.parametrize("how", SYNC_FINALISERS)
def test_a_stream_finalised_while_its_capture_is_live_is_recorded_there_once(
    tmp_path: Path, provider: Provider, how: str
) -> None:
    """The control: binding at creation must not lose the ordinary case."""
    wa = _writer(tmp_path, "run-A")
    ta = hooks.install_all(wa, SPAN)
    layer = _layer(provider, wa)
    box = [provider.open_sync(layer, "A")]
    next(box[0])
    _finalise_sync(box, how)
    hooks.uninstall_all(ta)
    (record,) = _records(wa)
    assert "PROMPT-A" in json.dumps(record)
    assert _logical(wa) == 1


def test_an_unbound_hook_drops_a_stream_that_ends_after_its_teardown(
    tmp_path: Path,
) -> None:
    """No capture scope at creation (deferred install, bare thread): the hook's
    own teardown is the end of its capture, so a later end records nothing."""
    wa = _writer(tmp_path, "run-A")
    layer = OpenAIHook(writer=wa, parent_span_id=SPAN)
    layer.install()
    box = [Provider("openai", OpenAIHook).open_sync(layer, "A")]
    layer.uninstall()
    before = _sdk_streams.late_stream_records_dropped()
    _finalise_sync(box, "gc")
    assert _records(wa) == []
    assert _sdk_streams.late_stream_records_dropped() == before + 1


def test_a_drop_warning_is_structured_and_never_quotes_the_payload(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    wa = _writer(tmp_path, "run-A")
    ta = hooks.install_all(wa, SPAN)
    box = [Provider("openai", OpenAIHook).open_sync(_layer(PROVIDERS[0], wa), "A")]
    hooks.uninstall_all(ta)
    with caplog.at_level("DEBUG", logger=_sdk_streams.__name__):
        _finalise_sync(box, "close")
    events = [r for r in caplog.records
              if getattr(r, "nova_event", None) == "capture.stream_record_dropped"]
    assert events, "the drop must be logged"
    assert events[0].reason == "capture_ended"  # type: ignore[attr-defined]
    assert all("PROMPT-A" not in r.getMessage() for r in caplog.records)


# ── two concurrent task-scoped captures (ADR-0224 D3) ────────────────────────


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)
@pytest.mark.parametrize("how", SYNC_FINALISERS)
@pytest.mark.parametrize("kind", ["sync", "async"])
def test_concurrent_captures_each_keep_their_own_streams(
    tmp_path: Path, provider: Provider, how: str, kind: str
) -> None:
    """Each task's stream is ended inside the OTHER task, both captures live."""
    wa, wb = _writer(tmp_path, "run-A"), _writer(tmp_path, "run-B")
    owner_layer = _layer(provider, wa)  # the owner's patch layer serves both

    async def main() -> None:
        opened = {"A": asyncio.Event(), "B": asyncio.Event()}
        swapped = {"A": asyncio.Event(), "B": asyncio.Event()}
        boxes: dict[str, list[Any]] = {}

        async def capture(tag: str, writer: CapsuleWriter, other: str) -> str:
            token = hooks.install_all(writer, SPAN)
            try:
                if kind == "sync":
                    stream = provider.open_sync(owner_layer, tag)
                    next(stream)
                else:
                    stream = await provider.open_async(owner_layer, tag)
                    await stream.__anext__()
                boxes[tag] = [stream]
                del stream
                opened[tag].set()
                await opened[other].wait()
                # End the OTHER capture's stream here, in this capture's task.
                if kind == "sync":
                    _finalise_sync(boxes[other], how)
                else:
                    await _finalise_async(boxes[other], how)
                swapped[tag].set()
                await swapped[other].wait()
            finally:
                hooks.uninstall_all(token)
            return token

        ta = asyncio.create_task(capture("A", wa, "B"))
        await opened["A"].wait()  # A owns the hooks, B participates
        tb = asyncio.create_task(capture("B", wb, "A"))
        await asyncio.gather(ta, tb)

    asyncio.run(main())
    for tag, writer, other in (("A", wa, "B"), ("B", wb, "A")):
        text = _text(writer)
        assert f"PROMPT-{other}" not in text and f"ANSWER-{other}" not in text
        (record,) = _records(writer)
        assert f"PROMPT-{tag}" in json.dumps(record)
        assert record["gen_ai.response.id"].endswith(f"-{tag}")
        assert _logical(writer) == 1, f"{tag}: one SDK call is one logical model call"


# ── the reproduction, through the real openai SDK ────────────────────────────


def _sse(tag: str) -> bytes:
    lines = []
    for i in range(3):
        chunk = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
                 "choices": [{"index": 0, "delta": {"content": f"A-SECRET-{tag}-{i}"},
                              "finish_reason": None}]}
        lines.append(f"data: {json.dumps(chunk)}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def _sse_handler(request: Any) -> Any:
    httpx2 = pytest.importorskip("httpx2")
    return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                           content=_sse("A"))


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_the_real_sdk_stream_collected_in_the_next_capture_is_not_filed_there(
    tmp_path: Path, kind: str
) -> None:
    openai = pytest.importorskip("openai")
    httpx2 = pytest.importorskip("httpx2")
    wa, wb = _writer(tmp_path, "run-A"), _writer(tmp_path, "run-B")
    msgs = [{"role": "user", "content": "TENANT-A-PROMPT"}]

    if kind == "sync":
        client = openai.OpenAI(
            api_key="sk-test",
            http_client=openai.DefaultHttpxClient(transport=httpx2.MockTransport(_sse_handler)),
        )
        ta = hooks.install_all(wa, SPAN)
        box = [client.chat.completions.create(model="m", messages=msgs, stream=True)]
        next(iter(box[0]))
        hooks.uninstall_all(ta)
        tb = hooks.install_all(wb, SPAN)
        _finalise_sync(box, "gc")
        hooks.uninstall_all(tb)
    else:
        async def ahandler(request: Any) -> Any:
            return _sse_handler(request)

        async def main() -> None:
            client = openai.AsyncOpenAI(
                api_key="sk-test",
                http_client=openai.DefaultAsyncHttpxClient(
                    transport=httpx2.MockTransport(ahandler)),
            )
            ta = hooks.install_all(wa, SPAN)
            box = [await client.chat.completions.create(model="m", messages=msgs,
                                                        stream=True)]
            await box[0].__anext__()
            hooks.uninstall_all(ta)
            tb = hooks.install_all(wb, SPAN)
            await _finalise_async(box, "gc")
            hooks.uninstall_all(tb)

        asyncio.run(main())

    assert "TENANT-A-PROMPT" not in _text(wb) and "A-SECRET" not in _text(wb)
    assert _logical(wb) == 0, "B made no model call"
    # A's call is still one logical call in A: its wire record stands for it.
    assert _logical(wa) == 1
    assert "A-SECRET" not in _text(wa), "nothing was appended after A ended"

