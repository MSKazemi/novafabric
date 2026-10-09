"""In-process dispatchers for mocked replay (ADR-0300, ADR-0304).

Installed into the replayed Python process by the ``sitecustomize.py`` the replay
engine writes (see :func:`install_from_env`):

* :class:`MockModelDispatcher` serves recorded model responses on the supported
  surfaces (``SERVED_MODEL_SURFACES``: OpenAI Chat Completions and Responses
  API, Anthropic Messages -- sync and async, with and without ``stream=True``)
  and guards the unsupported ones (``UNSUPPORTED_MODEL_SURFACES``) so they
  cannot silently go live. A recorded call that FAILED is served by raising the
  same SDK exception class at its position (``_model_errors``, issue #16) --
  after the delivered chunks when it was raised mid-stream; a Responses API
  response the SDK *returned* with ``status: failed`` is returned as recorded;
* :class:`MockToolDispatcher` serves recorded tool results on the intercepted
  tool surfaces: MCP ``tools/call`` through ``mcp.ClientSession.call_tool``
  (ADR-0300), functions the workload declared with
  ``novafabric.capture.record.tool`` (ADR-0306, experimental) -- served before
  the function body runs -- and Google ADK tools through the NovaFabric ADK tool
  plugin (ADR-0306 slice 3, experimental).

Every action is appended to an event log the engine reads afterwards. Under the
default ``fail`` divergence policy a call with no recorded answer raises a
:class:`~novafabric.replay._errors.ReplayDivergenceError` subclass; under the
opt-in ``warn`` policy (``nova replay --permissive``) the pre-ADR-0300 behaviour
is kept -- an empty model response, live unsupported model surfaces -- and the
divergence is still recorded; an unmatched tool call runs live only if the
operator's safety-ladder flags permit its class, and never against a
``replay.yaml`` ``allow: false`` override (ADR-0306).
"""

from __future__ import annotations

import builtins
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

from novafabric.capture import _tool_codec
from novafabric.capture.hooks._sdk_streams import (
    RESPONSE_STATUS_EXT,
    STREAM_ERROR_EVENT_EXT,
    is_raw_response_call,
)
from novafabric.replay._contract import (
    ECHO_DIFFERS,
    ECHO_NO_COUNTERPART,
    ECHO_NOT_RECORDED,
    ECHO_NOT_SENT,
    MODEL_SURFACES,
    QUEUE_PROVIDER,
    TOOL_SURFACE_ADK,
    TOOL_SURFACE_MCP,
    TOOL_SURFACE_PYTHON,
    ReplayEventLog,
    ToolCallMatcher,
    interceptable_tool_calls,
    model_queues,
    nested_boundary_ids,
    nested_within,
    normalized_arg_hash,
    not_servable_reason,
    recorded_provider_order,
    records_async_and_streamed_calls,
    request_messages,
    tool_result_messages,
    tool_surface,
)
from novafabric.replay._errors import (
    ReplayDivergenceError,
    ReplayOrderMismatchError,
    ReplayProviderMismatchError,
    ReplayQueueExhaustedError,
    ReplayRecordedErrorUnreconstructableError,
    ReplayRecordedModelError,
    ReplayRecordedToolError,
    ReplayRecordMalformedError,
    ReplayToolRecordNotServableError,
    ReplayToolUnmatchedError,
    ReplayUnsupportedSurfaceError,
)
from novafabric.replay._flags import LADDER_FLAG, ladder_flag
from novafabric.replay._model_errors import (
    UnreconstructableError,
    is_recorded_model_error,
    is_returned_failed_response,
    raised_mid_stream,
    rebuild_sdk_error,
    recorded_error_type,
)
from novafabric.replay._policy import (
    INSTALL_REQUIRED_ENV,
    TOOL_POLICY_ENV,
    decide_intercepted,
    gating_mutation_class,
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


def _delivered_finish(choice: dict[str, Any]) -> str | None:
    """The finish reason a recorded choice holds; ``None`` when the provider
    delivered none (``finish_reason: null``, or a record without the key).
    Replay never substitutes one."""
    finish = choice.get("finish_reason")
    return finish if isinstance(finish, str) and finish else None


def _unfinished(stored: dict[str, Any]) -> bool:
    """A record of a stream that never delivered a finish reason for some choice:
    its closing events were never delivered either, so none is served."""
    return any(_delivered_finish(c) is None for c in _choices(stored))


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
            finish_reason=_delivered_finish(c),
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


def _openai_chat_chunks(
    stored: dict[str, Any], *, include_usage: bool, partial: bool = False
) -> list[Any]:
    """A recorded Chat Completions response as the chunk stream ``stream=True`` yields.

    Per choice: the role and full content in one delta, one delta per tool
    call (id, name and full arguments), then the finish reason. A usage chunk
    (``choices: []``) closes the stream when the request asked for it.
    ``partial`` (a stream that raised part-way): the delivered content only --
    no finish-reason or usage chunk, which capture cannot tell were delivered.
    A choice recorded without a finish reason gets no finish-reason chunk, and
    then no usage chunk is served (the provider sends it last).
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
        finish = _delivered_finish(c)
        if not partial and finish is not None:
            chunks.append({**base, "choices": [{
                "index": index, "delta": {}, "finish_reason": finish,
            }]})
    if include_usage and not partial and not _unfinished(stored):
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
    finish = _delivered_finish(choices[0]) if choices else "stop"
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
    prompt = int(stored.get("gen_ai.usage.input_tokens", 0) or 0)
    completion = int(stored.get("gen_ai.usage.output_tokens", 0) or 0)
    ext = stored.get("extensions")
    recorded = ext.get(RESPONSE_STATUS_EXT) if isinstance(ext, dict) else None
    if isinstance(recorded, dict) and isinstance(recorded.get("status"), str):
        # The provider's own status, incomplete_details and error (issue #16).
        status: dict[str, Any] = {
            "status": recorded["status"],
            "incomplete_details": recorded.get("incomplete_details"),
            "error": recorded.get("error"),
        }
    else:
        # Captured before the status was recorded: inferred from the finish reason.
        incomplete = {"length": "max_output_tokens", "content_filter": "content_filter"}
        status = {
            "status": "incomplete" if finish in incomplete else "completed",
            "incomplete_details": (
                {"reason": incomplete[finish]} if finish in incomplete else None
            ),
            "error": None,
        }
    return {
        "id": rid,
        "object": "response",
        "created_at": 0,
        "model": stored.get("gen_ai.response.model", ""),
        **status,
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


#: The terminal stream event for each final ``Response.status`` (the three
#: events ``OpenAIResponsesStreamAccumulator`` folds as final).
_RESPONSES_TERMINAL_EVENT = {
    "completed": "response.completed",
    "incomplete": "response.incomplete",
    "failed": "response.failed",
}


def _responses_events(stored: dict[str, Any], *, partial: bool = False) -> list[Any]:
    """A recorded Responses API call as the event stream ``stream=True`` yields.

    ``response.created`` → per output item: ``output_item.added``, its content
    (one text delta, or one arguments delta) and ``output_item.done`` →
    ``response.completed`` (or ``response.incomplete`` / ``response.failed``)
    carrying the full response. ``partial`` (a stream that raised part-way, or
    that never delivered a terminal event): a text item stops after its delta --
    capture cannot tell whether its done events were delivered -- and there is
    no terminal event. A function call is only folded at capture from its
    ``output_item.done``, so it is complete. An ``error`` event the stream
    delivered (``io.novafabric.stream_error_event``) is served verbatim after the
    delivered events, before any terminal event.
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
            ]
            if partial:
                continue
            events += [
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
    error_event = _recorded_error_event(stored)
    if error_event is not None:
        # Its recorded sequence number, unless that would run backwards: replay
        # folds the deltas, so the served events before it are never more.
        position = next(seq)
        recorded = error_event.get("sequence_number")
        events.append({**error_event, "sequence_number": (
            recorded if isinstance(recorded, int) and not isinstance(recorded, bool)
            and recorded >= position else position
        )})
    if not partial:
        final = _RESPONSES_TERMINAL_EVENT.get(payload["status"], "response.completed")
        events.append({"type": final, "sequence_number": next(seq), "response": payload})
    return [
        _construct("openai", "openai.types.responses", "ResponseStreamEvent", event)
        for event in events
    ]


def _recorded_error_event(stored: dict[str, Any]) -> dict[str, Any] | None:
    """The Responses ``error`` event capture recorded verbatim, if servable."""
    ext = stored.get("extensions")
    event = ext.get(STREAM_ERROR_EVENT_EXT) if isinstance(ext, dict) else None
    if isinstance(event, dict) and isinstance(event.get("message"), str):
        return {**event, "type": "error"}
    return None


# A record carries the schema's finish-reason enum (model-call.schema.json); an
# Anthropic client expects Anthropic's own stop_reason vocabulary.
_ANTHROPIC_STOP_REASON = {
    "tool_calls": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def _anthropic_stop_reason(stored: dict[str, Any], finish_reason: str | None) -> str | None:
    # None: the stream delivered no stop_reason, and none is served.
    if finish_reason is None:
        return None
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
    finish_reason = _delivered_finish(choices[0]) if choices else "end_turn"
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


def _anthropic_events(stored: dict[str, Any], *, partial: bool = False) -> list[Any]:
    """A recorded Messages call as the raw event stream ``stream=True`` yields.

    ``partial`` (a stream that raised part-way): the blocks that carry
    delivered content, each closed except the last (blocks stream one after
    another), and no ``message_delta`` / ``message_stop``.
    """
    message = _mock_anthropic_response(stored)
    blocks = list(message.content)
    if partial:
        blocks = [b for b in blocks if b.type == "tool_use" or b.text]
    events: list[dict[str, Any]] = [{
        "type": "message_start",
        "message": {
            "id": message.id, "type": "message", "role": "assistant",
            "model": message.model, "content": [], "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": message.usage.input_tokens, "output_tokens": 0},
        },
    }]
    for index, block in enumerate(blocks):
        if block.type == "tool_use":
            start = {"type": "tool_use", "id": block.id, "name": block.name, "input": {}}
            # An argument string cut off mid-stream was kept verbatim under
            # "_unparsed"; _arguments_json serves it back as it was delivered.
            has_content = bool(block.input)
            delta = {"type": "input_json_delta", "partial_json": _arguments_json(block.input)}
        else:
            start = {"type": "text", "text": ""}
            has_content = bool(block.text)
            delta = {"type": "text_delta", "text": block.text}
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        if has_content or not partial:
            events.append({"type": "content_block_delta", "index": index, "delta": delta})
        if not partial or index < len(blocks) - 1:
            events.append({"type": "content_block_stop", "index": index})
    if not partial:
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


class _RecordedItems:
    """The recorded chunks of a served stream, then the recorded error if any.

    ``error``: the exception the recorded stream raised after those chunks
    (issue #16); raised once, when the consumer asks for the next chunk. A
    stream closed before then never raises it.
    """

    def __init__(self, items: list[Any], error: BaseException | None = None) -> None:
        self._iterator: Iterator[Any] = iter(items)
        self._error = error

    def next_item(self) -> Any:
        try:
            return next(self._iterator)
        except StopIteration:
            error, self._error = self._error, None
            if error is not None:
                raise error from None
            raise

    def clear(self) -> None:
        self._iterator = iter(())
        self._error = None


class _ReplayStream:
    """What a served ``stream=True`` call returns: the recorded chunks, in order."""

    response = _NoHTTPResponse()

    def __init__(self, items: list[Any], error: BaseException | None = None) -> None:
        self._items = _RecordedItems(items, error)

    def __iter__(self) -> _ReplayStream:
        return self

    def __next__(self) -> Any:
        return self._items.next_item()

    def __enter__(self) -> _ReplayStream:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._items.clear()


class _AsyncReplayStream:
    """The async counterpart of :class:`_ReplayStream`."""

    response = _NoHTTPResponse()

    def __init__(self, items: list[Any], error: BaseException | None = None) -> None:
        self._items = _RecordedItems(items, error)

    def __aiter__(self) -> _AsyncReplayStream:
        return self

    async def __anext__(self) -> Any:
        try:
            return self._items.next_item()
        except StopIteration:
            raise StopAsyncIteration from None

    async def __aenter__(self) -> _AsyncReplayStream:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        self._items.clear()


def _include_usage(kwargs: dict[str, Any]) -> bool:
    options = kwargs.get("stream_options")
    return isinstance(options, dict) and bool(options.get("include_usage"))


def build_served_response(
    queue: str, stored: dict[str, Any], *, stream: bool, asynchronous: bool,
    kwargs: dict[str, Any] | None = None, error: BaseException | None = None,
) -> Any:
    """The object a served call returns: a response, or a recorded stream.

    ``error`` (streamed only): the recorded stream raised part-way (issue #16).
    The stream then delivers what capture recorded as delivered -- nothing when
    it raised before the first chunk -- with no closing or terminal event, and
    raises ``error`` when the consumer asks for more. A record without a finish
    reason (the stream never delivered one) is served the same way, minus the
    exception: its closing events were never delivered.
    """
    if not stream:
        if queue == "openai.responses":
            return _mock_responses_response(stored)
        if queue == "anthropic":
            return _mock_anthropic_response(stored)
        return _mock_openai_response(stored)
    partial = error is not None
    streaming = stored.get("nova.streaming")
    if partial and isinstance(streaming, dict) and not streaming.get("chunk_count"):
        items: list[Any] = []
    elif queue == "openai.responses":
        items = _responses_events(stored, partial=partial or _unfinished(stored))
    elif queue == "anthropic":
        items = _anthropic_events(stored, partial=partial or _unfinished(stored))
    else:
        items = _openai_chat_chunks(
            stored, include_usage=_include_usage(kwargs or {}), partial=partial
        )
    if asynchronous:
        return _AsyncReplayStream(items, error)
    return _ReplayStream(items, error)


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
        #: Global recorded position of each queue record (queue -> [position]).
        self._global_pos: dict[str, list[int]] = {q: [] for q in self._queues}
        for position, queue in enumerate(self._order):
            self._global_pos[queue].append(position)
        #: Records covered by a served nested ``record.tool`` boundary (ADR-0306
        #: slice 4): skipped, never served -- the boundary's body never ran.
        self._covered: dict[str, set[int]] = {q: set() for q in self._queues}
        self._global_covered: set[int] = set()
        #: Tool-result ids the D10 echo check already reported (each once).
        self._echo_seen: set[str] = set()
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

    def cover_nested(self, boundary_ids: set[str]) -> int:
        """Skip, without serving, every not-yet-served record written inside one of
        *boundary_ids* (ADR-0306 slice 4). Returns how many were covered.

        Called when a nested ``record.tool`` boundary is served: its body never
        runs, so the model calls it made at capture are never requested. Each
        is reported as ``model_covered``, so it is neither served nor left over.
        """
        count = 0
        for queue, records in self._queues.items():
            covered = self._covered[queue]
            for idx in range(self._index[queue], len(records)):
                record = records[idx]
                within = nested_within(record)
                if idx in covered or within not in boundary_ids:
                    continue
                covered.add(idx)
                self._global_covered.add(self._global_pos[queue][idx])
                count += 1
                self._events.emit(
                    "model_covered",
                    provider=QUEUE_PROVIDER[queue],
                    queue=queue,
                    call_index=idx,
                    model_call_id=record.get("model_call_id"),
                    within_tool_call_id=within,
                )
        return count

    def _skip_covered(self, queue: str) -> None:
        covered = self._covered[queue]
        while self._index[queue] in covered:
            self._index[queue] += 1
        while self._global_index in self._global_covered:
            self._global_index += 1

    def _check_echo(
        self, queue: str, idx: int, record: dict[str, Any], request: dict[str, Any]
    ) -> None:
        """The D10 echo check (ADR-0306 slice 4): report-only, never raises.

        Compares each tool result this request sends back to the model with the
        one recorded in the request at the same served position, paired by
        ``tool_call_id``, by a digest over the secret-redacted value. A
        difference means the workload's own (undeclared) function returned
        something else than at capture. Each id is reported once -- at the
        first request that carries it -- and no value ever reaches the event
        log. A capsule that kept no request messages reports ``not_checked``,
        never ``matched``.
        """
        try:
            live = tool_result_messages(request_messages(request))
            if live is None:  # no readable message list: nothing to say either way
                return
            recorded_messages = tool_result_messages(record.get("gen_ai.request.messages"))
            recorded = recorded_messages or {}
            if not any(cid not in self._echo_seen for cid in (*live, *recorded)):
                return
            base = {
                "queue": queue,
                "surface": MODEL_SURFACES[queue],
                "call_index": idx,
                "model_call_id": record.get("model_call_id"),
            }
            for cid, value in live.items():
                if cid in self._echo_seen:
                    continue
                self._echo_seen.add(cid)
                if recorded_messages is None:
                    outcome, reason = "not_checked", ECHO_NOT_RECORDED
                elif cid not in recorded:
                    outcome, reason = "not_checked", ECHO_NO_COUNTERPART
                elif _tool_codec.echo_digest(value) == _tool_codec.echo_digest(recorded[cid]):
                    outcome, reason = "matched", None
                else:
                    outcome, reason = "mismatched", ECHO_DIFFERS
                self._events.emit(
                    "tool_result_echo", tool_call_id=cid, outcome=outcome,
                    **({"reason": reason} if reason else {}), **base,
                )
            for cid in recorded:
                if cid in live or cid in self._echo_seen:
                    continue
                self._echo_seen.add(cid)
                self._events.emit(
                    "tool_result_echo", tool_call_id=cid, outcome="mismatched",
                    reason=ECHO_NOT_SENT,
                    **base,
                )
        except Exception:  # noqa: BLE001 -- a report-only check never breaks a replay
            pass

    def _serve(self, queue: str, kwargs: dict[str, Any], *, asynchronous: bool) -> Any:
        stream = bool(kwargs.get("stream"))
        record, error = self._take(
            queue, stream=stream, asynchronous=asynchronous, request=kwargs
        )
        return build_served_response(
            queue, record, stream=stream, asynchronous=asynchronous, kwargs=kwargs,
            error=error,
        )

    def _next_response(self, queue: str) -> Any:
        """Serve the next recorded response, non-streaming (pre-ADR-0304 entry)."""
        return build_served_response(
            queue, self._next_record(queue), stream=False, asynchronous=False
        )

    def _next_record(
        self, queue: str, *, stream: bool = False, asynchronous: bool = False
    ) -> dict[str, Any]:
        """The next recorded response on ``queue`` (see :meth:`_take`); raises
        the recorded SDK exception when the recorded call failed."""
        record, error = self._take(queue, stream=stream, asynchronous=asynchronous)
        if error is not None:
            raise error
        return record

    def _take(
        self, queue: str, *, stream: bool = False, asynchronous: bool = False,
        request: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], BaseException | None]:
        """The next recorded response on ``queue``; ``{}`` once a divergence is
        tolerated (``warn``). Raises under ``fail``.

        When the recorded call at this position FAILED, this raises the
        recorded SDK exception instead of returning (see
        :meth:`_recorded_exception`) -- except for a ``stream=True`` call whose
        recorded stream raised part-way: the record is returned with the
        exception, to be raised after its delivered chunks. A Responses API
        response the SDK returned with ``status: failed``, or a Responses
        stream that delivered an ``error`` event, is served as recorded, never
        raised (issue #16; ADR-0304 follow-on)."""
        provider = QUEUE_PROVIDER[queue]
        surface = MODEL_SURFACES[queue]
        records = self._queues[queue]
        self._skip_covered(queue)
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
            return {}, None
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
        if request is not None:
            self._check_echo(queue, idx, record, request)
        if is_recorded_model_error(record) and not is_returned_failed_response(record):
            exc = self._recorded_exception(
                queue, idx, record, stream=stream, asynchronous=asynchronous
            )
            if stream and raised_mid_stream(record):
                return record, exc
            raise exc
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
        return record, None

    def _recorded_exception(
        self, queue: str, idx: int, record: dict[str, Any], *,
        stream: bool, asynchronous: bool,
    ) -> BaseException:
        """The exception the recorded call raised, to raise at this position.

        Built with the SDK's own class from the record's
        ``io.novafabric.sdk_error`` detail (``_model_errors``). When it cannot
        be rebuilt faithfully the call is refused (``fail``: the divergence is
        raised here, before any chunk is served, and the record is not
        consumed, as for a malformed response) or, under ``warn``, a
        :class:`ReplayRecordedModelError` stand-in with the recorded type and
        message is returned. Consumes the record otherwise.
        """
        provider = QUEUE_PROVIDER[queue]
        surface = MODEL_SURFACES[queue]
        error_type = recorded_error_type(record)
        faithful = True
        try:
            exc: BaseException = rebuild_sdk_error(record)
        except UnreconstructableError as why:
            refusal = ReplayRecordedErrorUnreconstructableError(
                f"recorded {surface} call #{idx + 1} failed with "
                f"{error_type or 'an error'}, which mocked replay cannot raise "
                f"faithfully: {why}; "
                + ("raising a stand-in ReplayRecordedModelError"
                   if self._policy == "warn" else "refusing the call"),
                provider=provider,
                call_index=idx,
                model_call_id=record.get("model_call_id"),
                error_type=error_type,
                reason=str(why),
                surface=surface,
            )
            _report_divergence(self._events, self._policy, refusal)
            error = record.get("error")
            message = str(error.get("message", "")) if isinstance(error, dict) else ""
            exc = ReplayRecordedModelError(error_type, message)
            faithful = False
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
            recorded_error=error_type or type(exc).__name__,
            faithful=faithful,
        )
        return exc


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


#: Env var carrying the mutation classes the operator's ladder flags permit
#: (``ReplayFlags.permits``), comma-separated; absent means ``none`` only.
TOOL_LADDER_ENV = "NOVAFABRIC_REPLAY_TOOL_LADDER"

#: The ladder flag that permits each mutation class (ADR-0012).
_LADDER_FLAG: dict[str, str] = LADDER_FLAG


def _ladder_from_env() -> frozenset[str]:
    raw = os.environ.get(TOOL_LADDER_ENV, "")
    classes = {c.strip() for c in raw.split(",") if c.strip()}
    return frozenset(classes | {"none"})


def _overrides_from_env() -> dict[str, bool]:
    """The ``replay.yaml`` override table the engine resolved (ADR-0306 D8):
    tool name -> ``allow``. Absent means no override. A table that cannot be
    read raises, so a strict replay (or one with an ``allow: false`` override)
    stops before the workload runs rather than enforce nothing."""
    path = os.environ.get(TOOL_POLICY_ENV, "")
    if not path:
        return {}
    table = json.loads(Path(path).read_text())
    overrides = table.get("overrides") if isinstance(table, dict) else None
    if not isinstance(overrides, dict):
        raise ValueError(f"{TOOL_POLICY_ENV}: no 'overrides' table")
    out: dict[str, bool] = {}
    for name, entry in overrides.items():
        allow = entry.get("allow") if isinstance(entry, dict) else None
        if not isinstance(allow, bool):
            raise ValueError(f"{TOOL_POLICY_ENV}: override {name!r} has no boolean 'allow'")
        out[str(name)] = allow
    return out


def _override_label(override: bool | None) -> str | None:
    return None if override is None else ("allow" if override else "deny")


def _divergence_outcome(
    *, live: bool, policy: str, override: bool | None, gating: str, permitted: bool,
    subject: str,
) -> str:
    """Why a diverging call ran live or was refused, for its error message."""
    if live:
        return (
            f"running {subject} live (--permissive; mutation_class {gating!r} is "
            "permitted by the operator's ladder flag)"
        )
    if override is False:
        return (
            f"{subject} was not run: replay.yaml tool_overrides `allow: false` -- "
            "never run live, even under --permissive"
        )
    if policy != "warn":
        return f"{subject} was not run"
    note = (
        "; replay.yaml `allow: true` is not honoured without it"
        if override is True and not permitted else ""
    )
    return (
        f"{subject} was not run: under --permissive an unmatched call runs live "
        f"only when the operator's ladder flag permits mutation_class {gating!r} "
        f"({ladder_flag(gating)}){note}"
    )


def _recorded_python_exception(record: dict[str, Any]) -> Exception:
    """The exception a recorded ``record.tool`` failure is re-raised as (ADR-0306 D4).

    The recorded class is used only when capture marked it as living in
    ``builtins`` **and** ``getattr(builtins, type)`` is an ``Exception``
    subclass -- never ``BaseException``, so a capsule cannot raise
    ``SystemExit`` or ``KeyboardInterrupt`` into the workload, and nothing is
    ever imported by name. Anything else is ``ReplayRecordedToolError``.
    """
    raw_error = record.get("error")
    error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
    type_name = str(error.get("type", ""))
    message = str(error.get("message", ""))
    ext = record.get("extensions")
    if isinstance(ext, dict) and ext.get(_tool_codec.EXCEPTION_BUILTIN_EXT) is True:
        cls = getattr(builtins, type_name, None) if type_name.isidentifier() else None
        if isinstance(cls, type) and issubclass(cls, Exception):
            try:
                return cls(message)
            except Exception:  # noqa: BLE001 -- needs other constructor args
                pass
    return ReplayRecordedToolError(type_name, message or "recorded tool call failed")


class _PythonToolServer:
    """Serves ``record.tool`` calls from python-surface records (ADR-0306 D5-D7).

    Registered as the façade's tool handler, so a decorated call asks it
    *before* the function body runs. Its matcher holds python-surface records
    only: a python record never answers an MCP call, and the reverse.
    """

    def __init__(
        self,
        records: list[dict[str, Any]],
        *,
        divergence_policy: str,
        events: ReplayEventLog,
        permitted: frozenset[str],
        overrides: dict[str, bool] | None = None,
        on_served: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._matcher = ToolCallMatcher(records)
        self._policy = divergence_policy
        self._events = events
        self._permitted = permitted
        self._overrides = dict(overrides or {})
        #: Called with each served record before its value is returned, so the
        #: records nested inside it are covered (ADR-0306 slice 4).
        self._on_served = on_served

    def call(
        self, spec: Any, fn: Callable[..., Any],
        args: tuple[Any, ...], kwargs: dict[str, Any],
    ) -> Any:
        served, value = self._resolve(spec, args, kwargs)
        return value if served else fn(*args, **kwargs)

    async def call_async(
        self, spec: Any, fn: Callable[..., Any],
        args: tuple[Any, ...], kwargs: dict[str, Any],
    ) -> Any:
        served, value = self._resolve(spec, args, kwargs)
        return value if served else await fn(*args, **kwargs)

    def _resolve(
        self, spec: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> tuple[bool, Any]:
        """``(True, value)`` to serve, ``(False, None)`` to run the body live;
        raises to refuse (a divergence) or to replay a recorded failure."""
        # A call that does not bind raises the TypeError the function itself
        # would raise -- before its body could run.
        call = _tool_codec.canonical_call(spec.signature, spec.ignore, args, kwargs)
        override = self._overrides.get(spec.name)
        mutation_class = str(spec.mutation_class)
        if override is True and mutation_class in self._permitted:
            # ADR-0306 D8: `allow: true` from the capsule plus the operator's
            # ladder flag -- re-execute. A matching record is consumed (it was
            # asked for), so leftover accounting stays exact; nothing is served.
            consumed = call.not_canonical is None and self._matcher.match_digest(
                None, spec.name, str(call.digest), raw_digest=call.raw_digest
            ).record is not None
            self._events.emit(
                "tool_live", tool_name=spec.name, surface=TOOL_SURFACE_PYTHON,
                mutation_class=mutation_class, override="allow", consumed=consumed,
            )
            return False, None
        if call.not_canonical is not None:
            return self._diverge(
                spec, ReplayToolRecordNotServableError, None,
                "cannot be matched", call.not_canonical, consumed=False,
            )
        digest = str(call.digest)
        match = self._matcher.match_digest(None, spec.name, digest, raw_digest=call.raw_digest)
        if match.record is None:
            return self._diverge(
                spec, ReplayToolUnmatchedError, digest,
                "has no unconsumed recorded result", str(match.reason),
            )
        reason = not_servable_reason(match.record)
        if reason is not None:
            return self._diverge(
                spec, ReplayToolRecordNotServableError, digest,
                "matched a recorded call that cannot be served", reason, consumed=True,
            )
        self._events.emit(
            "tool_mocked",
            tool_name=spec.name,
            record_id=match.record.get("tool_call_id"),
            how=match.how,
            surface=TOOL_SURFACE_PYTHON,
        )
        if self._on_served is not None:
            self._on_served(match.record)
        if match.record.get("status", "success") != "success":
            raise _recorded_python_exception(match.record)
        ok, value = _tool_codec.decode_result(match.record)
        if not ok:  # a slice-1 nested-only record: servable since slice 4, value kept
            result = match.record.get("result")
            value = result.get("value") if isinstance(result, dict) else None
        return True, value

    def _diverge(
        self, spec: Any, error_cls: type[ReplayDivergenceError], digest: str | None,
        what: str, reason: str, **extra: Any,
    ) -> tuple[bool, Any]:
        mutation_class = str(spec.mutation_class)
        override = self._overrides.get(spec.name)
        permitted = mutation_class in self._permitted
        live = decide_intercepted(
            override=override, servable_match=False, permitted=permitted,
            permissive=self._policy == "warn",
        ) == "live"
        outcome = _divergence_outcome(
            live=live, policy=self._policy, override=override,
            gating=mutation_class, permitted=permitted, subject="the function body",
        )
        label = _override_label(override)
        if label is not None:
            extra["override"] = label
        error = error_cls(
            f"{TOOL_SURFACE_PYTHON}({spec.name!r}) {what}: {reason}; {outcome}",
            tool_name=spec.name,
            arguments_hash=digest,
            reason=reason,
            surface=TOOL_SURFACE_PYTHON,
            mutation_class=mutation_class,
            **extra,
        )
        _report_divergence(self._events, self._policy, error)  # raises under ``fail``
        if live:
            self._events.emit(
                "tool_live", tool_name=spec.name, surface=TOOL_SURFACE_PYTHON,
                mutation_class=mutation_class,
            )
            return False, None
        self._events.emit(
            "tool_refused", tool_name=spec.name, surface=TOOL_SURFACE_PYTHON,
            mutation_class=mutation_class,
            **({"override": label} if label is not None else {}),
        )
        raise error


class MockToolDispatcher:
    """Serve recorded tool results, one-to-one (ADR-0300, ADR-0306).

    Two surfaces, each with its own matcher (a record answers only calls on the
    surface it was recorded on):

    * MCP ``tools/call`` through a patch of ``mcp.ClientSession.call_tool``;
    * ``record.tool`` functions through a server registered with the façade
      (ADR-0306, experimental), asked before the function body runs;
    * Google ADK tools through a server registered with the ADK tool seam
      (``adapters._adk_tool_seam``; ADR-0306 slice 3, experimental), asked by
      the NovaFabric ADK tool plugin's ``before_tool_callback``.

    A call with no unconsumed record is refused under ``fail`` -- the live tool
    is never executed. Under ``warn`` an unmatched call runs live only if the
    operator's ladder flags permit its mutation class -- the declared one for
    ``record.tool``, always ``unknown`` for MCP (ADR-0306 D7, Q3).

    ``overrides`` is the ``replay.yaml`` table (tool name -> ``allow``), applied
    on every surface through ``_policy.decide_intercepted`` (ADR-0306 D8):
    ``allow: false`` refuses an unmatched call even under ``warn``; ``allow:
    true`` re-executes a call only when the ladder permits its class.
    """

    def __init__(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        divergence_policy: str = "fail",
        events: ReplayEventLog | None = None,
        permitted_mutation_classes: frozenset[str] = frozenset({"none"}),
        overrides: dict[str, bool] | None = None,
        model_dispatcher: MockModelDispatcher | None = None,
    ) -> None:
        intercepted = interceptable_tool_calls(tool_calls)
        #: Every tool record, interceptable or not: nesting is a tree over all of
        #: them (a boundary nested in a boundary nested in ...).
        self._all_tool_calls = [r for r in tool_calls if isinstance(r, dict)]
        self._model = model_dispatcher
        self._matcher = ToolCallMatcher(
            [r for r in intercepted if tool_surface(r) == TOOL_SURFACE_MCP]
        )
        self._policy = divergence_policy
        self._events = events or ReplayEventLog(None)
        self._permitted = permitted_mutation_classes
        self._overrides = dict(overrides or {})
        self._python = _PythonToolServer(
            [r for r in intercepted if tool_surface(r) == TOOL_SURFACE_PYTHON],
            divergence_policy=divergence_policy,
            events=self._events,
            permitted=permitted_mutation_classes,
            overrides=self._overrides,
            on_served=self._cover_nested,
        )
        from novafabric.replay._adk_tool_server import AdkToolServer

        self._adk = AdkToolServer(
            [r for r in intercepted if tool_surface(r) == TOOL_SURFACE_ADK],
            divergence_policy=divergence_policy,
            events=self._events,
            permitted=permitted_mutation_classes,
            overrides=self._overrides,
        )
        self._previous_adk_handler: Any = None
        self._previous_handler: Any = None
        self._handler_registered = False
        self._patcher = _Patcher()
        self.installed_surfaces: list[str] = []

    def _cover_nested(self, record: dict[str, Any]) -> None:
        """Consume as covered every record written inside a served boundary.

        ADR-0306 slice 4 (D3's nested coverage): a served ``record.tool``
        boundary never runs its body, so the model calls, MCP calls and inner
        boundaries it made at capture are never requested. Records marked
        ``within_tool_call_id`` (transitively) are consumed as ``covered``
        instead of being reported unconsumed. A nested call capture could not
        mark (made from a raw thread) stays unconsumed: the replay fails closed.
        """
        ext = record.get("extensions")
        nested = ext.get(_tool_codec.NESTED_RECORDS_EXT) if isinstance(ext, dict) else None
        boundary = record.get("tool_call_id")
        if type(nested) is not int or nested <= 0 or not isinstance(boundary, str):
            return
        ids = nested_boundary_ids(boundary, self._all_tool_calls)
        for surface, matcher in (
            (TOOL_SURFACE_MCP, self._matcher), (TOOL_SURFACE_PYTHON, self._python._matcher),
        ):
            for rec in matcher.cover(ids):
                self._events.emit(
                    "tool_covered", tool_name=rec.get("tool_name"),
                    record_id=rec.get("tool_call_id"), surface=surface,
                    within_tool_call_id=nested_within(rec),
                )
        if self._model is not None:
            self._model.cover_nested(ids)

    def lookup(
        self, tool_call_id: str | None, tool_name: str, arguments: Any
    ) -> dict[str, Any] | None:
        """Match and CONSUME one recorded MCP call; ``None`` when unmatched."""
        return self._matcher.match(tool_call_id, tool_name, arguments).record

    def install(self) -> None:
        if self._patcher.patch(
            "mcp.client.session", "ClientSession", "call_tool", self._make_call_tool
        ):
            self.installed_surfaces.append(TOOL_SURFACE_MCP)
        from novafabric.adapters import _adk_tool_seam
        from novafabric.capture import record

        self._previous_handler = record._set_tool_handler(self._python)
        self._previous_adk_handler = _adk_tool_seam._set_handler(self._adk)
        self._handler_registered = True
        self.installed_surfaces.append(TOOL_SURFACE_PYTHON)
        self.installed_surfaces.append(TOOL_SURFACE_ADK)

    def uninstall(self) -> None:
        self._patcher.restore()
        if self._handler_registered:
            from novafabric.adapters import _adk_tool_seam
            from novafabric.capture import record

            if record._get_tool_handler() is self._python:
                record._set_tool_handler(self._previous_handler)
            if _adk_tool_seam._get_handler() is self._adk:
                _adk_tool_seam._set_handler(self._previous_adk_handler)
            self._previous_handler = None
            self._previous_adk_handler = None
            self._handler_registered = False
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
        override = self._overrides.get(name)
        # ADR-0306 Q3: an MCP call carries no trustworthy class -- always `unknown`.
        gating = gating_mutation_class(TOOL_SURFACE_MCP, "unknown")
        permitted = gating in self._permitted
        if override is True and permitted:
            # ADR-0306 D8: `allow: true` plus the operator's ladder flag --
            # re-execute; a matching record is consumed, never served.
            consumed = self._matcher.match(None, name, arguments).record is not None
            self._events.emit(
                "tool_live", tool_name=name, surface=TOOL_SURFACE_MCP,
                mutation_class=gating, override="allow", consumed=consumed,
            )
            return await original(inner_self, name, arguments, *args, **kwargs)
        match = self._matcher.match(None, name, arguments)
        if match.record is not None:
            self._events.emit(
                "tool_mocked",
                tool_name=name,
                record_id=match.record.get("tool_call_id"),
                how=match.how,
            )
            return _mcp_result_from_record(match.record)
        live = decide_intercepted(
            override=override, servable_match=False, permitted=permitted,
            permissive=self._policy == "warn",
        ) == "live"
        label = _override_label(override)
        error = ReplayToolUnmatchedError(
            f"{TOOL_SURFACE_MCP}({name!r}) has no unconsumed recorded result: "
            f"{match.reason}; "
            + _divergence_outcome(
                live=live, policy=self._policy, override=override, gating=gating,
                permitted=permitted, subject="the live tool",
            ),
            tool_name=name,
            # D12.3: the redact-then-hash digest, never one of a raw secret.
            arguments_hash=_tool_codec.redacted_arguments_digest(arguments),
            reason=match.reason,
            surface=TOOL_SURFACE_MCP,
            mutation_class=gating,
            **({"override": label} if label is not None else {}),
        )
        _report_divergence(self._events, self._policy, error)  # raises under ``fail``
        if live:
            self._events.emit(
                "tool_live", tool_name=name, surface=TOOL_SURFACE_MCP, mutation_class=gating,
            )
            return await original(inner_self, name, arguments, *args, **kwargs)
        self._events.emit(
            "tool_refused", tool_name=name, surface=TOOL_SURFACE_MCP, mutation_class=gating,
            **({"override": label} if label is not None else {}),
        )
        raise error


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
    # ADR-0306 D8: an `allow: false` override holds even under --permissive,
    # which it cannot do without the tool dispatcher -- so a failed install
    # stops the process whenever one is in force.
    install_required = policy != "warn" or os.environ.get(INSTALL_REQUIRED_ENV) == "1"
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
                permitted_mutation_classes=_ladder_from_env(),
                overrides=_overrides_from_env(),
                model_dispatcher=model_dispatcher,
            )
            tool_dispatcher.install()
            surfaces.extend(tool_dispatcher.installed_surfaces)
        if NetworkObserver(events).install():
            events.emit("network_observer_installed")
        events.emit("installed", policy=policy, surfaces=surfaces)
    except Exception as exc:  # noqa: BLE001 -- reported, then fail-closed below
        events.emit("install_failed", error=f"{type(exc).__name__}: {exc}")
        print(f"[novafabric] mock dispatcher install failed: {exc}", file=sys.stderr)
        if install_required:
            os._exit(REPLAY_DISPATCHER_UNAVAILABLE_EXIT)
