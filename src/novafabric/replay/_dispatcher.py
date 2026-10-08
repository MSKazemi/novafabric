"""In-process dispatchers for mocked replay (ADR-0300, ADR-0304).

Installed into the replayed Python process by the ``sitecustomize.py`` the replay
engine writes (see :func:`install_from_env`):

* :class:`MockModelDispatcher` serves recorded model responses on the supported
  surfaces (``SERVED_MODEL_SURFACES``: OpenAI Chat Completions and Responses
  API, Anthropic Messages -- sync and async, with and without ``stream=True``)
  and guards the unsupported ones (``UNSUPPORTED_MODEL_SURFACES``) so they
  cannot silently go live;
* :class:`MockToolDispatcher` serves recorded MCP ``tools/call`` results through
  ``mcp.ClientSession.call_tool`` -- the one tool surface NovaFabric intercepts.

Every action is appended to an event log the engine reads afterwards. Under the
default ``fail`` divergence policy a call with no recorded answer raises a
:class:`~novafabric.replay._errors.ReplayDivergenceError` subclass; under the
opt-in ``warn`` policy (``nova replay --permissive``) the pre-ADR-0300 behaviour
is kept -- an empty model response, live tools -- and the divergence is still
recorded.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import json
import os
import sys
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from novafabric.capture.hooks._sdk_streams import is_raw_response_call
from novafabric.replay._contract import (
    MODEL_SURFACES,
    QUEUE_PROVIDER,
    TOOL_SURFACE_MCP,
    ReplayEventLog,
    ToolCallMatcher,
    interceptable_tool_calls,
    model_queues,
    normalized_arg_hash,
    recorded_provider_order,
    records_async_and_streamed_calls,
)
from novafabric.replay._errors import (
    ReplayDivergenceError,
    ReplayOrderMismatchError,
    ReplayProviderMismatchError,
    ReplayQueueExhaustedError,
    ReplayRecordedToolError,
    ReplayRecordMalformedError,
    ReplayToolUnmatchedError,
    ReplayUnsupportedSurfaceError,
)

#: Exit status of a strict replay whose dispatcher could not be installed: the
#: process is stopped before the workload runs, because running it would send
#: every model call to the network under a "mocked" label.
REPLAY_DISPATCHER_UNAVAILABLE_EXIT = 86

#: Provider-finish-reason extension written at capture (issue #12).
_PROVIDER_FINISH_REASONS_EXT = "io.novafabric.provider_finish_reasons"

#: Model API surfaces mocked replay serves from the capsule (ADR-0304). Each is
#: (replay queue, module, class, attribute, async). A served ``create`` honours
#: ``stream=True`` by replaying the recorded response as a chunk/event stream.
SERVED_MODEL_SURFACES: tuple[tuple[str, str, str, str, bool], ...] = (
    ("openai", "openai.resources.chat.completions", "Completions", "create", False),
    ("openai", "openai.resources.chat.completions", "AsyncCompletions", "create", True),
    ("openai.responses", "openai.resources.responses", "Responses", "create", False),
    ("openai.responses", "openai.resources.responses", "AsyncResponses", "create", True),
    ("anthropic", "anthropic.resources.messages", "Messages", "create", False),
    ("anthropic", "anthropic.resources.messages", "AsyncMessages", "create", True),
)

#: Model API surfaces mocked replay does NOT serve. Strict replay refuses them;
#: permissive replay lets them run live and counts them. Each entry is
#: (provider, module, class, attribute, human-readable surface).
UNSUPPORTED_MODEL_SURFACES: tuple[tuple[str, str, str, str, str], ...] = (
    ("openai", "openai.resources.chat.completions", "Completions", "parse",
     "openai.chat.completions.parse (structured outputs)"),
    ("openai", "openai.resources.chat.completions", "AsyncCompletions", "parse",
     "openai.chat.completions.parse (async)"),
    ("openai", "openai.resources.responses", "Responses", "parse",
     "openai.responses.parse (Responses API)"),
    ("openai", "openai.resources.responses", "AsyncResponses", "parse",
     "openai.responses.parse (Responses API, async)"),
    ("openai", "openai.resources.completions", "Completions", "create",
     "openai.completions.create (legacy text completions)"),
    ("openai", "openai.resources.completions", "AsyncCompletions", "create",
     "openai.completions.create (legacy text completions, async)"),
    ("anthropic", "anthropic.resources.messages", "Messages", "stream",
     "anthropic.messages.stream"),
    ("anthropic", "anthropic.resources.messages", "AsyncMessages", "stream",
     "anthropic.messages.stream (async)"),
    ("anthropic", "anthropic.resources.beta.messages", "Messages", "create",
     "anthropic.beta.messages.create"),
    ("anthropic", "anthropic.resources.beta.messages", "AsyncMessages", "create",
     "anthropic.beta.messages.create (async)"),
    ("anthropic", "anthropic.resources.beta.messages", "Messages", "stream",
     "anthropic.beta.messages.stream"),
    ("anthropic", "anthropic.resources.beta.messages", "AsyncMessages", "stream",
     "anthropic.beta.messages.stream (async)"),
)


# ── response reconstruction ──────────────────────────────────────────────────


def _arg_hash(arguments: Any) -> str:
    """Kept for callers of the pre-ADR-0300 name; see ``normalized_arg_hash``."""
    return normalized_arg_hash(arguments)


def _arguments_json(arguments: Any) -> str:
    # Inverse of capture's parsing: an argument string the model emitted as
    # invalid JSON was kept under "_unparsed" and is served back verbatim.
    if isinstance(arguments, dict) and set(arguments) == {"_unparsed"}:
        return str(arguments["_unparsed"])
    return json.dumps(arguments if isinstance(arguments, dict) else {})


def _well_formed_ref(ref: Any) -> bool:
    return isinstance(ref, dict) and isinstance(ref.get("name"), str) and bool(ref["name"])


def _malformed_tool_call_refs(stored: dict[str, Any]) -> int:
    """Stored tool-call entries replay cannot rebuild (no ``name``)."""
    count = 0
    for choice in stored.get("gen_ai.response.choices") or []:
        message = choice.get("message", {}) if isinstance(choice, dict) else {}
        refs = message.get("tool_calls") if isinstance(message, dict) else None
        if isinstance(refs, list):
            count += sum(1 for ref in refs if not _well_formed_ref(ref))
    return count


def _namespace(value: Any) -> Any:
    """Plain attribute objects for a JSON-shaped value (no SDK available)."""
    if isinstance(value, dict):
        return types.SimpleNamespace(**{k: _namespace(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_namespace(v) for v in value]
    return value


def _construct(sdk: str, module: str, type_name: str, data: dict[str, Any]) -> Any:
    """Build the SDK's own response type when the SDK is importable.

    Uses the SDK's ``construct_type`` -- what the SDK itself uses to parse a
    response body, without validation -- so helpers that rely on real types
    (``Response.output_text``, stream accumulators) work. Falls back to plain
    attribute objects.
    """
    try:
        models = importlib.import_module(f"{sdk}._models")
        type_ = getattr(importlib.import_module(module), type_name)
        return models.construct_type(type_=type_, value=data)
    except Exception:  # noqa: BLE001 -- no SDK / a different SDK layout
        return _namespace(data)


def _choices(stored: dict[str, Any]) -> list[dict[str, Any]]:
    raw = stored.get("gen_ai.response.choices")
    return [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []


def _openai_tool_calls(message: dict[str, Any]) -> list[Any] | None:
    refs = message.get("tool_calls")
    if not isinstance(refs, list) or not refs:
        return None
    calls = [
        types.SimpleNamespace(
            id=ref.get("id", ""),
            type="function",
            function=types.SimpleNamespace(
                name=ref["name"],
                arguments=_arguments_json(ref.get("arguments")),
            ),
        )
        for ref in refs
        if _well_formed_ref(ref)
    ]
    return calls or None


def _mock_openai_response(stored: dict[str, Any]) -> Any:
    choices = []
    for c in stored.get("gen_ai.response.choices", []):
        message = c.get("message", {})
        msg = types.SimpleNamespace(
            role=message.get("role", "assistant"),
            content=message.get("content", ""),
            tool_calls=_openai_tool_calls(message),
        )
        choices.append(types.SimpleNamespace(
            index=c.get("index", 0),
            message=msg,
            finish_reason=c.get("finish_reason", "stop"),
        ))
    usage = types.SimpleNamespace(
        prompt_tokens=stored.get("gen_ai.usage.input_tokens", 0),
        completion_tokens=stored.get("gen_ai.usage.output_tokens", 0),
        total_tokens=(
            stored.get("gen_ai.usage.input_tokens", 0)
            + stored.get("gen_ai.usage.output_tokens", 0)
        ),
    )
    return types.SimpleNamespace(
        id=stored.get("gen_ai.response.id", "replay-mocked"),
        model=stored.get("gen_ai.response.model", ""),
        choices=choices,
        usage=usage,
    )


def _openai_chat_chunks(stored: dict[str, Any], *, include_usage: bool) -> list[Any]:
    """A recorded Chat Completions response as the chunk stream ``stream=True`` yields.

    Per choice: the role and full content in one delta, one delta per tool
    call (id, name and full arguments), then the finish reason. A usage chunk
    (``choices: []``) closes the stream when the request asked for it.
    """
    base = {
        "id": stored.get("gen_ai.response.id", "replay-mocked"),
        "object": "chat.completion.chunk",
        "created": 0,
        "model": stored.get("gen_ai.response.model", ""),
    }
    chunks: list[dict[str, Any]] = []
    for c in _choices(stored):
        index = int(c.get("index", 0) or 0)
        message = c.get("message") if isinstance(c.get("message"), dict) else {}
        assert isinstance(message, dict)
        delta: dict[str, Any] = {"role": message.get("role", "assistant")}
        if isinstance(message.get("content"), str):
            delta["content"] = message["content"]
        chunks.append({**base, "choices": [
            {"index": index, "delta": delta, "finish_reason": None}
        ]})
        refs = [r for r in message.get("tool_calls") or [] if _well_formed_ref(r)]
        for position, ref in enumerate(refs):
            chunks.append({**base, "choices": [{
                "index": index,
                "delta": {"tool_calls": [{
                    "index": position,
                    "id": ref.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": ref["name"],
                        "arguments": _arguments_json(ref.get("arguments")),
                    },
                }]},
                "finish_reason": None,
            }]})
        chunks.append({**base, "choices": [{
            "index": index, "delta": {}, "finish_reason": c.get("finish_reason", "stop"),
        }]})
    if include_usage:
        prompt = int(stored.get("gen_ai.usage.input_tokens", 0) or 0)
        completion = int(stored.get("gen_ai.usage.output_tokens", 0) or 0)
        chunks.append({**base, "choices": [], "usage": {
            "prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }})
    return [
        _construct("openai", "openai.types.chat", "ChatCompletionChunk", chunk)
        for chunk in chunks
    ]


def _responses_payload(stored: dict[str, Any]) -> dict[str, Any]:
    """The Responses API ``Response`` body a recorded call stands for (ADR-0304)."""
    rid = str(stored.get("gen_ai.response.id", "resp_replay_mocked"))
    choices = _choices(stored)
    message = choices[0].get("message") if choices else {}
    message = message if isinstance(message, dict) else {}
    finish = choices[0].get("finish_reason", "stop") if choices else "stop"
    output: list[dict[str, Any]] = []
    if isinstance(message.get("content"), str):
        output.append({
            "type": "message",
            "id": f"msg_{rid}",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": message["content"], "annotations": []}],
        })
    for ref in message.get("tool_calls") or []:
        if _well_formed_ref(ref):
            call_id = str(ref.get("id", ""))
            output.append({
                "type": "function_call",
                "id": f"fc_{call_id}",
                "call_id": call_id,
                "name": ref["name"],
                "arguments": _arguments_json(ref.get("arguments")),
                "status": "completed",
            })
    incomplete = {"length": "max_output_tokens", "content_filter": "content_filter"}
    prompt = int(stored.get("gen_ai.usage.input_tokens", 0) or 0)
    completion = int(stored.get("gen_ai.usage.output_tokens", 0) or 0)
    return {
        "id": rid,
        "object": "response",
        "created_at": 0,
        "model": stored.get("gen_ai.response.model", ""),
        "status": "incomplete" if finish in incomplete else "completed",
        "incomplete_details": (
            {"reason": incomplete[finish]} if finish in incomplete else None
        ),
        "error": None,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": prompt,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": completion,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": prompt + completion,
        },
    }


def _mock_responses_response(stored: dict[str, Any]) -> Any:
    payload = _responses_payload(stored)
    built = _construct("openai", "openai.types.responses", "Response", payload)
    if isinstance(built, types.SimpleNamespace):
        built.output_text = "".join(
            part.text
            for item in built.output if getattr(item, "type", None) == "message"
            for part in item.content if getattr(part, "type", None) == "output_text"
        )
    return built


def _responses_events(stored: dict[str, Any]) -> list[Any]:
    """A recorded Responses API call as the event stream ``stream=True`` yields.

    ``response.created`` → per output item: ``output_item.added``, its content
    (one text delta, or one arguments delta) and ``output_item.done`` →
    ``response.completed`` (or ``response.incomplete``) carrying the full response.
    """
    payload = _responses_payload(stored)
    seq = iter(range(1_000_000))
    events: list[dict[str, Any]] = [{
        "type": "response.created", "sequence_number": next(seq),
        "response": {**payload, "status": "in_progress", "output": [],
                     "incomplete_details": None},
    }]
    for index, item in enumerate(payload["output"]):
        if item["type"] == "message":
            text = item["content"][0]["text"]
            empty = {**item, "status": "in_progress", "content": []}
            part = {"type": "output_text", "text": "", "annotations": []}
            events += [
                {"type": "response.output_item.added", "sequence_number": next(seq),
                 "output_index": index, "item": empty},
                {"type": "response.content_part.added", "sequence_number": next(seq),
                 "item_id": item["id"], "output_index": index, "content_index": 0,
                 "part": part},
                {"type": "response.output_text.delta", "sequence_number": next(seq),
                 "item_id": item["id"], "output_index": index, "content_index": 0,
                 "delta": text, "logprobs": []},
                {"type": "response.output_text.done", "sequence_number": next(seq),
                 "item_id": item["id"], "output_index": index, "content_index": 0,
                 "text": text, "logprobs": []},
                {"type": "response.content_part.done", "sequence_number": next(seq),
                 "item_id": item["id"], "output_index": index, "content_index": 0,
                 "part": {**part, "text": text}},
            ]
        else:
            events += [
                {"type": "response.output_item.added", "sequence_number": next(seq),
                 "output_index": index,
                 "item": {**item, "arguments": "", "status": "in_progress"}},
                {"type": "response.function_call_arguments.delta",
                 "sequence_number": next(seq), "item_id": item["id"],
                 "output_index": index, "delta": item["arguments"]},
                {"type": "response.function_call_arguments.done",
                 "sequence_number": next(seq), "item_id": item["id"],
                 "output_index": index, "name": item["name"],
                 "arguments": item["arguments"]},
            ]
        events.append({"type": "response.output_item.done", "sequence_number": next(seq),
                       "output_index": index, "item": item})
    final = "response.incomplete" if payload["status"] == "incomplete" else "response.completed"
    events.append({"type": final, "sequence_number": next(seq), "response": payload})
    return [
        _construct("openai", "openai.types.responses", "ResponseStreamEvent", event)
        for event in events
    ]


# A record carries the schema's finish-reason enum (model-call.schema.json); an
# Anthropic client expects Anthropic's own stop_reason vocabulary.
_ANTHROPIC_STOP_REASON = {
    "tool_calls": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def _anthropic_stop_reason(stored: dict[str, Any], finish_reason: str) -> str:
    # Capture keeps Anthropic's raw value additively (issue #12); serve exactly
    # that when present, so e.g. stop_sequence is not flattened to end_turn.
    ext = stored.get("extensions")
    if isinstance(ext, dict):
        raw = ext.get(_PROVIDER_FINISH_REASONS_EXT)
        if isinstance(raw, list) and raw and isinstance(raw[0], str):
            return raw[0]
    return _ANTHROPIC_STOP_REASON.get(finish_reason, finish_reason)


def _mock_anthropic_response(stored: dict[str, Any]) -> Any:
    choices = stored.get("gen_ai.response.choices", [])
    message = choices[0].get("message", {}) if choices else {}
    content_text = message.get("content", "") or ""
    finish_reason = choices[0].get("finish_reason", "end_turn") if choices else "end_turn"
    blocks: list[Any] = []
    if content_text:
        blocks.append(types.SimpleNamespace(type="text", text=content_text))
    for ref in message.get("tool_calls") or []:
        if _well_formed_ref(ref):
            arguments = ref.get("arguments")
            blocks.append(types.SimpleNamespace(
                type="tool_use",
                id=ref.get("id", ""),
                name=ref["name"],
                input=arguments if isinstance(arguments, dict) else {},
            ))
    if not blocks:
        blocks.append(types.SimpleNamespace(type="text", text=""))
    usage = types.SimpleNamespace(
        input_tokens=stored.get("gen_ai.usage.input_tokens", 0),
        output_tokens=stored.get("gen_ai.usage.output_tokens", 0),
    )
    return types.SimpleNamespace(
        id=stored.get("gen_ai.response.id", "replay-mocked"),
        model=stored.get("gen_ai.response.model", ""),
        content=blocks,
        stop_reason=_anthropic_stop_reason(stored, finish_reason),
        usage=usage,
        type="message",
        role="assistant",
    )


def _anthropic_events(stored: dict[str, Any]) -> list[Any]:
    """A recorded Messages call as the raw event stream ``stream=True`` yields."""
    message = _mock_anthropic_response(stored)
    events: list[dict[str, Any]] = [{
        "type": "message_start",
        "message": {
            "id": message.id, "type": "message", "role": "assistant",
            "model": message.model, "content": [], "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": message.usage.input_tokens, "output_tokens": 0},
        },
    }]
    for index, block in enumerate(message.content):
        if block.type == "tool_use":
            start = {"type": "tool_use", "id": block.id, "name": block.name, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block.input)}
        else:
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block.text}
        events += [
            {"type": "content_block_start", "index": index, "content_block": start},
            {"type": "content_block_delta", "index": index, "delta": delta},
            {"type": "content_block_stop", "index": index},
        ]
    events += [
        {"type": "message_delta",
         "delta": {"stop_reason": message.stop_reason, "stop_sequence": None},
         "usage": {"output_tokens": message.usage.output_tokens}},
        {"type": "message_stop"},
    ]
    return [
        _construct("anthropic", "anthropic.types", "RawMessageStreamEvent", event)
        for event in events
    ]


class _NoHTTPResponse:
    """Stands in for the HTTP response of a served stream: there is none.

    SDK stream helpers (``chat.completions.stream()``, ``responses.stream()``)
    close the underlying response when they finish; closing this does nothing.
    """

    status_code = 200
    headers: dict[str, str] = {}
    request = None

    def close(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class _ReplayStream:
    """What a served ``stream=True`` call returns: the recorded chunks, in order."""

    response = _NoHTTPResponse()

    def __init__(self, items: list[Any]) -> None:
        self._iterator: Iterator[Any] = iter(items)

    def __iter__(self) -> _ReplayStream:
        return self

    def __next__(self) -> Any:
        return next(self._iterator)

    def __enter__(self) -> _ReplayStream:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._iterator = iter(())


class _AsyncReplayStream:
    """The async counterpart of :class:`_ReplayStream`."""

    response = _NoHTTPResponse()

    def __init__(self, items: list[Any]) -> None:
        self._iterator: Iterator[Any] = iter(items)

    def __aiter__(self) -> _AsyncReplayStream:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._iterator)
        except StopIteration:
            raise StopAsyncIteration from None

    async def __aenter__(self) -> _AsyncReplayStream:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        self._iterator = iter(())


def _include_usage(kwargs: dict[str, Any]) -> bool:
    options = kwargs.get("stream_options")
    return isinstance(options, dict) and bool(options.get("include_usage"))


def build_served_response(
    queue: str, stored: dict[str, Any], *, stream: bool, asynchronous: bool,
    kwargs: dict[str, Any] | None = None,
) -> Any:
    """The object a served call returns: a response, or a recorded stream."""
    if not stream:
        if queue == "openai.responses":
            return _mock_responses_response(stored)
        if queue == "anthropic":
            return _mock_anthropic_response(stored)
        return _mock_openai_response(stored)
    if queue == "openai.responses":
        items = _responses_events(stored)
    elif queue == "anthropic":
        items = _anthropic_events(stored)
    else:
        items = _openai_chat_chunks(stored, include_usage=_include_usage(kwargs or {}))
    return _AsyncReplayStream(items) if asynchronous else _ReplayStream(items)


#: Pre-ADR-0304 name: the non-streaming builder per provider queue.
_BUILDERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "openai": _mock_openai_response,
    "openai.responses": _mock_responses_response,
    "anthropic": _mock_anthropic_response,
}


# ── shared plumbing ──────────────────────────────────────────────────────────


def _report_divergence(
    events: ReplayEventLog, policy: str, error: ReplayDivergenceError
) -> None:
    """Record a divergence; raise it under ``fail``, warn on stderr under ``warn``."""
    events.emit("divergence", policy=policy, **error.as_dict())
    if policy != "warn":
        raise error
    print(f"[novafabric] mocked replay (permissive): {error}", file=sys.stderr)


class _Patcher:
    """Patch class attributes and restore them exactly (own vs inherited)."""

    def __init__(self) -> None:
        self._saved: list[tuple[type, str, Any, bool]] = []

    def patch(
        self, module_name: str, class_name: str, attr: str,
        factory: Callable[[Any], Any],
    ) -> bool:
        try:
            module = importlib.import_module(module_name)
            owner = getattr(module, class_name)
            original = getattr(owner, attr)
        except (ImportError, AttributeError):
            return False
        had_own = attr in vars(owner)
        setattr(owner, attr, factory(original))
        self._saved.append((owner, attr, original, had_own))
        return True

    def restore(self) -> None:
        while self._saved:
            owner, attr, original, had_own = self._saved.pop()
            if had_own:
                setattr(owner, attr, original)
            else:
                try:
                    delattr(owner, attr)
                except AttributeError:
                    pass


# ── model calls ──────────────────────────────────────────────────────────────


class MockModelDispatcher:
    """Serve recorded model responses in recorded order.

    One queue per served surface (``_contract.model_queues``: OpenAI Chat
    Completions, OpenAI Responses API, Anthropic Messages), built from the
    servable records only. Sync and async calls to a surface share its queue,
    and ``stream=True`` replays the recorded response as a stream.
    ``divergence_policy="fail"`` (the default) raises on a call with no recorded
    answer; ``"warn"`` serves an empty response and warns, as replay did before
    ADR-0300.
    """

    def __init__(
        self,
        model_calls: list[dict[str, Any]],
        *,
        divergence_policy: str = "fail",
        events: ReplayEventLog | None = None,
    ) -> None:
        self._queues = model_queues(model_calls)
        self._order = recorded_provider_order(model_calls)
        #: A capsule captured before ADR-0304 never recorded its async or
        #: streamed calls; serving one from the queue would hand it another
        #: call's record, so those calls stay refused (pre-0304 behaviour).
        self._legacy_capture = not records_async_and_streamed_calls(model_calls)
        self._index: dict[str, int] = dict.fromkeys(self._queues, 0)
        self._global_index = 0
        self._policy = divergence_policy
        self._events = events or ReplayEventLog(None)
        self._patcher = _Patcher()
        self.installed_surfaces: list[str] = []

    # Kept for introspection by existing callers/tests.
    @property
    def _openai_queue(self) -> list[dict[str, Any]]:
        return self._queues["openai"]

    @property
    def _anthropic_queue(self) -> list[dict[str, Any]]:
        return self._queues["anthropic"]

    @property
    def _openai_index(self) -> int:
        return self._index["openai"]

    @property
    def _anthropic_index(self) -> int:
        return self._index["anthropic"]

    def install(self) -> None:
        for queue, module_name, class_name, attr, is_async in SERVED_MODEL_SURFACES:
            if self._patcher.patch(
                module_name, class_name, attr,
                functools.partial(self._make_create, queue, is_async),
            ):
                label = f"{MODEL_SURFACES[queue]}{' (async)' if is_async else ''}"
                self.installed_surfaces.append(label)
        for provider, module_name, class_name, attr, surface in UNSUPPORTED_MODEL_SURFACES:
            self._patcher.patch(
                module_name, class_name, attr,
                functools.partial(self._make_guard, provider, surface),
            )

    def uninstall(self) -> None:
        self._patcher.restore()
        self.installed_surfaces = []

    def _make_create(self, queue: str, is_async: bool, original: Any) -> Any:
        dispatcher = self
        provider = QUEUE_PROVIDER[queue]

        if is_async:

            @functools.wraps(original)
            async def mock_create_async(inner_self: Any, *args: Any, **kwargs: Any) -> Any:
                refused = dispatcher._refusal(queue, kwargs, asynchronous=True)
                if refused:
                    dispatcher._unsupported(provider, refused)
                    return await original(inner_self, *args, **kwargs)
                return dispatcher._serve(queue, kwargs, asynchronous=True)

            return mock_create_async

        @functools.wraps(original)
        def mock_create(inner_self: Any, *args: Any, **kwargs: Any) -> Any:
            refused = dispatcher._refusal(queue, kwargs, asynchronous=False)
            if refused:
                dispatcher._unsupported(provider, refused)
                return original(inner_self, *args, **kwargs)
            return dispatcher._serve(queue, kwargs, asynchronous=False)

        return mock_create

    def _make_guard(self, provider: str, surface: str, original: Any) -> Any:
        dispatcher = self
        if inspect.iscoroutinefunction(original):

            @functools.wraps(original)
            async def async_guard(inner_self: Any, *args: Any, **kwargs: Any) -> Any:
                dispatcher._unsupported(provider, surface)
                return await original(inner_self, *args, **kwargs)

            return async_guard

        @functools.wraps(original)
        def guard(inner_self: Any, *args: Any, **kwargs: Any) -> Any:
            dispatcher._unsupported(provider, surface)
            return original(inner_self, *args, **kwargs)

        return guard

    def _unsupported(self, provider: str, surface: str) -> None:
        """Refuse (fail) or let through live and count it (warn)."""
        _report_divergence(self._events, self._policy, ReplayUnsupportedSurfaceError(
            f"{surface} is not served by mocked replay; "
            + ("refused" if self._policy != "warn" else "running it live"),
            provider=provider,
            surface=surface,
        ))
        self._events.emit("model_live", provider=provider, surface=surface)

    def _refusal(
        self, queue: str, kwargs: dict[str, Any], *, asynchronous: bool
    ) -> str | None:
        """The unsupported surface this call is on, or ``None`` if it is served."""
        surface = MODEL_SURFACES[queue]
        if is_raw_response_call(kwargs):
            return f"{surface} via with_raw_response / with_streaming_response"
        stream = bool(kwargs.get("stream"))
        if self._legacy_capture and (asynchronous or stream):
            how = " and ".join(
                [w for w, on in (("async", asynchronous), ("stream=True", stream)) if on]
            )
            return (
                f"{surface} ({how}) -- this capsule was captured before async and "
                "streamed calls were recorded (ADR-0304); re-capture to serve them"
            )
        return None

    def _serve(self, queue: str, kwargs: dict[str, Any], *, asynchronous: bool) -> Any:
        stream = bool(kwargs.get("stream"))
        record = self._next_record(queue, stream=stream, asynchronous=asynchronous)
        return build_served_response(
            queue, record, stream=stream, asynchronous=asynchronous, kwargs=kwargs
        )

    def _next_response(self, queue: str) -> Any:
        """Serve the next recorded response, non-streaming (pre-ADR-0304 entry)."""
        return build_served_response(
            queue, self._next_record(queue), stream=False, asynchronous=False
        )

    def _next_record(
        self, queue: str, *, stream: bool = False, asynchronous: bool = False
    ) -> dict[str, Any]:
        """The next recorded response on ``queue``; ``{}`` once a divergence is
        tolerated (``warn``). Raises under ``fail``."""
        provider = QUEUE_PROVIDER[queue]
        surface = MODEL_SURFACES[queue]
        records = self._queues[queue]
        idx = self._index[queue]
        if idx >= len(records):
            self._index[queue] += 1
            others = {
                q: len(r) - self._index[q]
                for q, r in self._queues.items()
                if q != queue and self._index[q] < len(r)
            }
            cls = ReplayProviderMismatchError if others else ReplayQueueExhaustedError
            detail = (
                f"; recorded responses remain unconsumed for "
                f"{sorted(MODEL_SURFACES[q] for q in others)}"
                if others else ""
            )
            _report_divergence(self._events, self._policy, cls(
                f"no recorded {provider} response left for call #{idx + 1} to "
                f"{surface} (capsule recorded {len(records)}){detail}"
                + ("; serving an empty response" if self._policy == "warn" else ""),
                provider=provider,
                call_index=idx,
                recorded_queue_length=len(records),
                surface=surface,
            ))
            return {}
        position = self._global_index
        if position < len(self._order) and self._order[position] != queue:
            expected = self._order[position]
            _report_divergence(self._events, self._policy, ReplayOrderMismatchError(
                f"call #{position + 1} went to {surface}; the capsule recorded "
                f"{MODEL_SURFACES[expected]} at that position",
                provider=provider,
                expected_provider=QUEUE_PROVIDER[expected],
                surface=surface,
                expected_surface=MODEL_SURFACES[expected],
                global_call_index=position,
            ))
        record = records[idx]
        malformed = _malformed_tool_call_refs(record)
        if malformed:
            _report_divergence(self._events, self._policy, ReplayRecordMalformedError(
                f"recorded {surface} response #{idx + 1} has {malformed} tool-call "
                "entr" + ("y" if malformed == 1 else "ies") + " without a name; "
                + ("served without them" if self._policy == "warn" else "refusing to serve it"),
                provider=provider,
                call_index=idx,
                model_call_id=record.get("model_call_id"),
            ))
        self._index[queue] += 1
        self._global_index += 1
        self._events.emit(
            "model_served",
            provider=provider,
            queue=queue,
            call_index=idx,
            model_call_id=record.get("model_call_id"),
            stream=stream,
            asynchronous=asynchronous,
        )
        return record


# ── tool calls ───────────────────────────────────────────────────────────────


def _raise_recorded_tool_error(record: dict[str, Any], envelope: Any) -> None:
    rpc_error = envelope.get("error") if isinstance(envelope, dict) else None
    if isinstance(rpc_error, dict):
        try:
            from mcp.shared.exceptions import McpError
            from mcp.types import ErrorData

            raise McpError(ErrorData(
                code=int(rpc_error.get("code", -32603)),
                message=str(rpc_error.get("message", "")),
                data=rpc_error.get("data"),
            ))
        except ImportError:
            pass
        raise ReplayRecordedToolError("McpError", str(rpc_error.get("message", "")))
    raw_error = record.get("error")
    error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
    raise ReplayRecordedToolError(
        str(error.get("type", "")), str(error.get("message", "recorded tool call failed"))
    )


def _mcp_result_from_record(record: dict[str, Any]) -> Any:
    """Rebuild the ``CallToolResult`` a recorded MCP ``tools/call`` returned.

    The JSON-RPC response envelope (verbatim under ``nova mcp-proxy``) is
    preferred over the hook's serialized ``result``.
    """
    raw_mcp = record.get("mcp")
    mcp: dict[str, Any] = raw_mcp if isinstance(raw_mcp, dict) else {}
    envelope = mcp.get("response_envelope")
    if record.get("status") not in (None, "success") or (
        isinstance(envelope, dict) and "error" in envelope
    ):
        _raise_recorded_tool_error(record, envelope)
    payload: Any = None
    if isinstance(envelope, dict) and isinstance(envelope.get("result"), dict):
        payload = envelope["result"]
    elif isinstance(record.get("result"), dict):
        payload = record["result"]
    payload = payload or {"content": []}
    try:
        from mcp.types import CallToolResult

        return CallToolResult.model_validate(payload)
    except Exception:  # noqa: BLE001 -- lossy record or no mcp SDK: rebuild by attribute
        content = [
            types.SimpleNamespace(**item) if isinstance(item, dict) else item
            for item in payload.get("content") or []
        ]
        return types.SimpleNamespace(
            content=content,
            isError=bool(payload.get("isError", False)),
            structuredContent=payload.get("structuredContent"),
            meta=None,
        )


class MockToolDispatcher:
    """Serve recorded MCP ``tools/call`` results, one-to-one (ADR-0300).

    Only records on the intercepted surface (``_contract.interceptable_tool_calls``)
    are matchable. A call with no unconsumed record is refused under ``fail``
    -- the live tool is never executed -- and runs live under ``warn``.
    """

    def __init__(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        divergence_policy: str = "fail",
        events: ReplayEventLog | None = None,
    ) -> None:
        self._matcher = ToolCallMatcher(interceptable_tool_calls(tool_calls))
        self._policy = divergence_policy
        self._events = events or ReplayEventLog(None)
        self._patcher = _Patcher()
        self.installed_surfaces: list[str] = []

    def lookup(
        self, tool_call_id: str | None, tool_name: str, arguments: Any
    ) -> dict[str, Any] | None:
        """Match and CONSUME one recorded call; ``None`` when unmatched."""
        return self._matcher.match(tool_call_id, tool_name, arguments).record

    def install(self) -> None:
        if self._patcher.patch(
            "mcp.client.session", "ClientSession", "call_tool", self._make_call_tool
        ):
            self.installed_surfaces.append(TOOL_SURFACE_MCP)

    def uninstall(self) -> None:
        self._patcher.restore()
        self.installed_surfaces = []

    def _make_call_tool(self, original: Any) -> Any:
        dispatcher = self

        @functools.wraps(original)
        async def mocked_call_tool(
            inner_self: Any, name: str, arguments: dict[str, Any] | None = None,
            *args: Any, **kwargs: Any,
        ) -> Any:
            return await dispatcher._call(original, inner_self, name, arguments, args, kwargs)

        return mocked_call_tool

    async def _call(
        self, original: Any, inner_self: Any, name: str, arguments: Any,
        args: tuple[Any, ...], kwargs: dict[str, Any],
    ) -> Any:
        match = self._matcher.match(None, name, arguments)
        if match.record is not None:
            self._events.emit(
                "tool_mocked",
                tool_name=name,
                record_id=match.record.get("tool_call_id"),
                how=match.how,
            )
            return _mcp_result_from_record(match.record)
        _report_divergence(self._events, self._policy, ReplayToolUnmatchedError(
            f"{TOOL_SURFACE_MCP}({name!r}) has no unconsumed recorded result: "
            f"{match.reason}; "
            + ("running the live tool" if self._policy == "warn" else "the live tool was not run"),
            tool_name=name,
            arguments_hash=normalized_arg_hash(arguments),
            reason=match.reason,
            surface=TOOL_SURFACE_MCP,
        ))
        self._events.emit("tool_live", tool_name=name)
        return await original(inner_self, name, arguments, *args, **kwargs)


# ── live network observation ─────────────────────────────────────────────────


#: Most ``network_live`` events one replayed process writes; beyond it a single
#: ``network_live_capped`` event says the count is a lower bound.
NETWORK_EVENT_CAP = 10_000


class NetworkObserver:
    """Report -- never block -- outbound connections the replayed process opens.

    Patches ``socket.socket.connect`` / ``connect_ex`` (the methods
    ``socket.create_connection``, ``httpx``/``requests`` and ``asyncio`` call)
    and logs one ``network_live`` event per IPv4/IPv6 connection attempt with its
    host and port -- never a payload. A mocked replay can therefore say whether
    anything outside the intercepted surfaces reached the network (ADR-0304).
    Connections made by C extensions that bypass the Python ``socket`` methods,
    and by non-Python child processes, are not seen.
    """

    def __init__(self, events: ReplayEventLog, *, cap: int = NETWORK_EVENT_CAP) -> None:
        self._events = events
        self._cap = cap
        self._count = 0
        self._patcher = _Patcher()

    def install(self) -> bool:
        installed = self._patcher.patch("socket", "socket", "connect", self._wrap)
        installed = self._patcher.patch("socket", "socket", "connect_ex", self._wrap) and installed
        return installed

    def uninstall(self) -> None:
        self._patcher.restore()

    def _wrap(self, original: Any) -> Any:
        observer = self

        @functools.wraps(original)
        def observed(sock: Any, address: Any, *args: Any, **kwargs: Any) -> Any:
            observer.seen(sock, address)
            return original(sock, address, *args, **kwargs)

        return observed

    def seen(self, sock: Any, address: Any) -> None:
        try:
            import socket

            if getattr(sock, "family", None) not in (socket.AF_INET, socket.AF_INET6):
                return
            if not isinstance(address, tuple) or len(address) < 2:
                return
            self._count += 1
            if self._count <= self._cap:
                self._events.emit("network_live", host=str(address[0]), port=int(address[1]))
            elif self._count == self._cap + 1:
                self._events.emit("network_live_capped", cap=self._cap)
        except Exception:  # noqa: BLE001 -- observation must never break the workload
            pass


# ── subprocess entry point ───────────────────────────────────────────────────


def install_from_env() -> None:
    """Install the dispatchers described by the engine's environment variables.

    Called from the generated ``sitecustomize.py``. A failure to install under
    the ``fail`` policy stops the process (exit
    ``REPLAY_DISPATCHER_UNAVAILABLE_EXIT``) before the workload runs.
    """
    model_path = os.environ.get("NOVAFABRIC_REPLAY_QUEUE_PATH", "")
    if not model_path:
        return
    events = ReplayEventLog(os.environ.get("NOVAFABRIC_REPLAY_EVENTS_PATH") or None)
    policy = "warn" if os.environ.get("NOVAFABRIC_REPLAY_DIVERGENCE_POLICY") == "warn" else "fail"
    try:
        model_calls = json.loads(Path(model_path).read_text())
        model_dispatcher = MockModelDispatcher(
            model_calls, divergence_policy=policy, events=events
        )
        model_dispatcher.install()
        surfaces = list(model_dispatcher.installed_surfaces)
        tool_path = os.environ.get("NOVAFABRIC_REPLAY_TOOL_QUEUE_PATH", "")
        if tool_path:
            tool_dispatcher = MockToolDispatcher(
                json.loads(Path(tool_path).read_text()),
                divergence_policy=policy,
                events=events,
            )
            tool_dispatcher.install()
            surfaces.extend(tool_dispatcher.installed_surfaces)
        if NetworkObserver(events).install():
            events.emit("network_observer_installed")
        events.emit("installed", policy=policy, surfaces=surfaces)
    except Exception as exc:  # noqa: BLE001 -- reported, then fail-closed below
        events.emit("install_failed", error=f"{type(exc).__name__}: {exc}")
        print(f"[novafabric] mock dispatcher install failed: {exc}", file=sys.stderr)
        if policy != "warn":
            os._exit(REPLAY_DISPATCHER_UNAVAILABLE_EXIT)
