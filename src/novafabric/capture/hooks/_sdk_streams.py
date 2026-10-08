"""Recording wrappers for streamed SDK responses (ADR-0304).

A streamed SDK call (``stream=True``) returns an iterator of chunks/events, not a
response, so the SDK hooks used to record a response with no choices -- nothing
mocked replay could serve. This module wraps the SDK's stream object so that the
chunks are folded into one response-like object as the workload consumes them,
and the record is written **once**, when the stream ends: exhausted, closed, or
garbage-collected. A stream the workload abandoned early is recorded with what
it actually delivered and flagged ``extensions["io.novafabric.stream_complete"]:
false``.

The wrappers are transparent proxies: iteration yields the SDK's own chunk
objects unchanged, and every other attribute is delegated. Folding never raises
into the workload.

Accumulators (pure, provider-specific):

* :class:`OpenAIChatStreamAccumulator` -- ``chat.completions.create(stream=True)``
* :class:`OpenAIResponsesStreamAccumulator` -- ``responses.create(stream=True)``
* :class:`AnthropicStreamAccumulator` -- ``messages.create(stream=True)``
"""

from __future__ import annotations

import json
import logging
import time
import types
from collections.abc import Callable
from typing import Any

_log = logging.getLogger(__name__)

#: Reverse-DNS extension key: ``False`` when the workload stopped consuming a
#: stream before the provider ended it (the record holds what was delivered).
STREAM_COMPLETE_EXT = "io.novafabric.stream_complete"

#: Reverse-DNS extension key naming the API surface a record was captured on.
#: Absent (every record captured before ADR-0304) means Chat Completions
#: (OpenAI) or Messages (Anthropic), so older records keep their meaning.
API_SURFACE_EXT = "io.novafabric.api_surface"

#: ``API_SURFACE_EXT`` values the SDK hooks write (ADR-0304). Every SDK-hook
#: record carries one, so mocked replay can tell a capsule whose async and
#: streamed calls were recorded from one captured before they were.
OPENAI_CHAT_SURFACE = "openai.chat.completions"
OPENAI_RESPONSES_SURFACE = "openai.responses"
ANTHROPIC_MESSAGES_SURFACE = "anthropic.messages"

#: Header the SDKs set on ``with_raw_response`` / ``with_streaming_response``
#: calls: the return value is an HTTP response wrapper, not a parsed response.
RAW_RESPONSE_HEADER = "X-Stainless-Raw-Response"


def is_raw_response_call(kwargs: dict[str, Any]) -> bool:
    """True for a ``with_raw_response`` / ``with_streaming_response`` call."""
    headers = kwargs.get("extra_headers")
    if not isinstance(headers, dict):
        return False
    return any(str(k).lower() == RAW_RESPONSE_HEADER.lower() for k in headers)


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


# ── accumulators ─────────────────────────────────────────────────────────────


class OpenAIChatStreamAccumulator:
    """Fold ``ChatCompletionChunk`` objects into a ``ChatCompletion``-like object."""

    def __init__(self) -> None:
        self.id: str | None = None
        self.model: str | None = None
        self.usage: Any = None
        self._choices: dict[int, dict[str, Any]] = {}

    def add(self, chunk: Any) -> None:
        self.id = self.id or _get(chunk, "id")
        self.model = self.model or _get(chunk, "model")
        if _get(chunk, "usage") is not None:
            self.usage = _get(chunk, "usage")
        for choice in _get(chunk, "choices") or []:
            index = int(_get(choice, "index") or 0)
            state = self._choices.setdefault(
                index,
                {"role": "assistant", "content": [], "tool_calls": {}, "finish_reason": None},
            )
            delta = _get(choice, "delta")
            if _get(delta, "role"):
                state["role"] = _get(delta, "role")
            if isinstance(_get(delta, "content"), str):
                state["content"].append(_get(delta, "content"))
            for call in _get(delta, "tool_calls") or []:
                slot = state["tool_calls"].setdefault(
                    int(_get(call, "index") or 0), {"id": "", "name": "", "arguments": []}
                )
                if _get(call, "id"):
                    slot["id"] = _get(call, "id")
                function = _get(call, "function")
                if _get(function, "name") and not slot["name"]:
                    slot["name"] = _get(function, "name")
                if isinstance(_get(function, "arguments"), str):
                    slot["arguments"].append(_get(function, "arguments"))
            if _get(choice, "finish_reason"):
                state["finish_reason"] = _get(choice, "finish_reason")

    def response(self) -> Any:
        choices = []
        for index in sorted(self._choices):
            state = self._choices[index]
            calls = [
                types.SimpleNamespace(
                    id=slot["id"],
                    type="function",
                    function=types.SimpleNamespace(
                        name=slot["name"], arguments="".join(slot["arguments"])
                    ),
                )
                for _, slot in sorted(state["tool_calls"].items())
            ]
            choices.append(types.SimpleNamespace(
                index=index,
                message=types.SimpleNamespace(
                    role=state["role"],
                    content="".join(state["content"]) if state["content"] else None,
                    tool_calls=calls or None,
                ),
                finish_reason=state["finish_reason"],
            ))
        return types.SimpleNamespace(
            id=self.id, model=self.model, usage=self.usage, choices=choices
        )


class AnthropicStreamAccumulator:
    """Fold Anthropic raw stream events into a ``Message``-like object."""

    def __init__(self) -> None:
        self.id: str | None = None
        self.model: str | None = None
        self.stop_reason: str | None = None
        self.input_tokens = 0
        self.output_tokens = 0
        self._blocks: dict[int, dict[str, Any]] = {}

    def add(self, event: Any) -> None:
        kind = _get(event, "type")
        if kind == "message_start":
            message = _get(event, "message")
            self.id = _get(message, "id")
            self.model = _get(message, "model")
            usage = _get(message, "usage")
            self.input_tokens = int(_get(usage, "input_tokens") or 0)
            self.output_tokens = int(_get(usage, "output_tokens") or 0)
        elif kind == "content_block_start":
            block = _get(event, "content_block")
            self._blocks[int(_get(event, "index") or 0)] = {
                "type": _get(block, "type"),
                "text": [_get(block, "text") or ""],
                "id": _get(block, "id"),
                "name": _get(block, "name"),
                "input": _get(block, "input"),
                "partial_json": [],
            }
        elif kind == "content_block_delta":
            slot = self._blocks.setdefault(
                int(_get(event, "index") or 0),
                {"type": "text", "text": [], "id": None, "name": None, "input": None,
                 "partial_json": []},
            )
            delta = _get(event, "delta")
            if _get(delta, "type") == "text_delta":
                slot["text"].append(_get(delta, "text") or "")
            elif _get(delta, "type") == "input_json_delta":
                slot["partial_json"].append(_get(delta, "partial_json") or "")
        elif kind == "message_delta":
            delta = _get(event, "delta")
            if _get(delta, "stop_reason"):
                self.stop_reason = _get(delta, "stop_reason")
            usage = _get(event, "usage")
            if _get(usage, "output_tokens") is not None:
                self.output_tokens = int(_get(usage, "output_tokens") or 0)

    @staticmethod
    def _tool_input(slot: dict[str, Any]) -> Any:
        raw = "".join(slot["partial_json"])
        if raw.strip():
            try:
                return json.loads(raw)
            except ValueError:
                return {"_unparsed": raw}
        return slot["input"] if isinstance(slot["input"], dict) else {}

    def response(self) -> Any:
        content = []
        for index in sorted(self._blocks):
            slot = self._blocks[index]
            if slot["type"] == "tool_use":
                content.append(types.SimpleNamespace(
                    type="tool_use", id=slot["id"], name=slot["name"],
                    input=self._tool_input(slot),
                ))
            elif slot["type"] == "text":
                content.append(types.SimpleNamespace(type="text", text="".join(slot["text"])))
        return types.SimpleNamespace(
            id=self.id,
            model=self.model,
            stop_reason=self.stop_reason,
            content=content,
            usage=types.SimpleNamespace(
                input_tokens=self.input_tokens, output_tokens=self.output_tokens
            ),
        )


_RESPONSES_FINAL_EVENTS = frozenset(
    {"response.completed", "response.incomplete", "response.failed"}
)


class OpenAIResponsesStreamAccumulator:
    """Fold Responses API stream events into a ``Response``-like object.

    The provider sends the full response in its terminal event; that object is
    used verbatim. A stream that ended before it is rebuilt from the text
    deltas and the completed output items seen so far.
    """

    def __init__(self) -> None:
        self.final: Any = None
        self._seed: Any = None
        self._text: list[str] = []
        self._items: list[Any] = []

    def add(self, event: Any) -> None:
        kind = _get(event, "type")
        if kind in _RESPONSES_FINAL_EVENTS:
            self.final = _get(event, "response")
        elif kind == "response.created":
            self._seed = _get(event, "response")
        elif kind == "response.output_text.delta":
            self._text.append(_get(event, "delta") or "")
        elif kind == "response.output_item.done":
            item = _get(event, "item")
            if _get(item, "type") == "function_call":
                self._items.append(item)

    def response(self) -> Any:
        if self.final is not None:
            return self.final
        output: list[Any] = []
        if self._text:
            output.append(types.SimpleNamespace(
                type="message", role="assistant",
                content=[types.SimpleNamespace(type="output_text", text="".join(self._text))],
            ))
        output.extend(self._items)
        return types.SimpleNamespace(
            id=_get(self._seed, "id"),
            model=_get(self._seed, "model"),
            status="incomplete",
            incomplete_details=None,
            usage=None,
            output=output,
        )


# ── stream proxies ───────────────────────────────────────────────────────────


#: Called once with (folded response, chunk count, ms to first chunk or None,
#: whether the provider ended the stream).
OnStreamDone = Callable[[Any, int, "int | None", bool], None]


class _StreamState:
    def __init__(self, accumulator: Any, on_done: OnStreamDone) -> None:
        self._acc = accumulator
        self._on_done = on_done
        self._t0 = time.monotonic()
        self._first_ms: int | None = None
        self._count = 0
        self._done = False

    def add(self, item: Any) -> None:
        if self._first_ms is None:
            self._first_ms = int((time.monotonic() - self._t0) * 1000)
        self._count += 1
        try:
            self._acc.add(item)
        except Exception:  # noqa: BLE001 -- capture must never fail the workload
            _log.debug("novafabric: could not fold a stream chunk", exc_info=True)

    def finish(self, complete: bool) -> None:
        if self._done:
            return
        self._done = True
        try:
            self._on_done(self._acc.response(), self._count, self._first_ms, complete)
        except Exception:  # noqa: BLE001 -- capture must never fail the workload
            _log.debug("novafabric: could not record a streamed response", exc_info=True)


class RecordingStream:
    """Transparent proxy over a sync SDK stream that records it when it ends."""

    def __init__(self, inner: Any, accumulator: Any, on_done: OnStreamDone) -> None:
        self._nf_inner = inner
        self._nf_iter: Any = None
        self._nf_state = _StreamState(accumulator, on_done)

    def __iter__(self) -> RecordingStream:
        return self

    def __next__(self) -> Any:
        if self._nf_iter is None:
            self._nf_iter = iter(self._nf_inner)
        try:
            item = next(self._nf_iter)
        except StopIteration:
            self._nf_state.finish(complete=True)
            raise
        except BaseException:
            self._nf_state.finish(complete=False)
            raise
        self._nf_state.add(item)
        return item

    def __enter__(self) -> RecordingStream:
        enter = getattr(self._nf_inner, "__enter__", None)
        if enter is not None:
            enter()
        return self

    def __exit__(self, *exc: Any) -> Any:
        self._nf_state.finish(complete=False)
        exit_ = getattr(self._nf_inner, "__exit__", None)
        return exit_(*exc) if exit_ is not None else None

    def close(self) -> Any:
        self._nf_state.finish(complete=False)
        close = getattr(self._nf_inner, "close", None)
        return close() if close is not None else None

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_nf_"):
            raise AttributeError(name)
        return getattr(self._nf_inner, name)

    def __del__(self) -> None:
        try:
            self._nf_state.finish(complete=False)
        except Exception:  # noqa: BLE001
            pass


class AsyncRecordingStream:
    """Transparent proxy over an async SDK stream that records it when it ends."""

    def __init__(self, inner: Any, accumulator: Any, on_done: OnStreamDone) -> None:
        self._nf_inner = inner
        self._nf_iter: Any = None
        self._nf_state = _StreamState(accumulator, on_done)

    def __aiter__(self) -> AsyncRecordingStream:
        return self

    async def __anext__(self) -> Any:
        if self._nf_iter is None:
            self._nf_iter = self._nf_inner.__aiter__()
        try:
            item = await self._nf_iter.__anext__()
        except StopAsyncIteration:
            self._nf_state.finish(complete=True)
            raise
        except BaseException:
            self._nf_state.finish(complete=False)
            raise
        self._nf_state.add(item)
        return item

    async def __aenter__(self) -> AsyncRecordingStream:
        enter = getattr(self._nf_inner, "__aenter__", None)
        if enter is not None:
            await enter()
        return self

    async def __aexit__(self, *exc: Any) -> Any:
        self._nf_state.finish(complete=False)
        exit_ = getattr(self._nf_inner, "__aexit__", None)
        return (await exit_(*exc)) if exit_ is not None else None

    async def close(self) -> Any:
        self._nf_state.finish(complete=False)
        close = getattr(self._nf_inner, "close", None)
        return (await close()) if close is not None else None

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_nf_"):
            raise AttributeError(name)
        return getattr(self._nf_inner, name)

    def __del__(self) -> None:
        try:
            self._nf_state.finish(complete=False)
        except Exception:  # noqa: BLE001
            pass


def is_sync_stream(obj: Any) -> bool:
    """A sync SDK stream object (iterable, not a response with choices/content)."""
    return hasattr(obj, "__iter__") and hasattr(obj, "__next__") and not isinstance(
        obj, (str, bytes, dict, list, tuple)
    )


def is_async_stream(obj: Any) -> bool:
    return hasattr(obj, "__aiter__")


def streaming_block(chunk_count: int, first_token_ms: int | None) -> dict[str, Any]:
    """The ``nova.streaming`` block of a model-call record."""
    return {"streamed": True, "chunk_count": chunk_count, "first_token_ms": first_token_ms}


def attach_stream_info(
    record: dict[str, Any], stream_info: tuple[int, int | None, bool] | None
) -> None:
    """Mark a record folded from a stream (``nova.streaming``; ADR-0304)."""
    if stream_info is None:
        return
    count, first_ms, complete = stream_info
    record["nova.streaming"] = streaming_block(count, first_ms)
    if not complete:
        record.setdefault("extensions", {})[STREAM_COMPLETE_EXT] = False
