from __future__ import annotations

import functools
import importlib
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from novafabric.capture._ulid import new_ulid
from novafabric.capture.event_recorder import get_current_writer
from novafabric.capture.hooks._finish_reason import (
    attach_provider_finish_reasons,
    canonical_finish_reason,
)
from novafabric.capture.hooks._otel_genai import (
    build_record_envelope,
    extract_request_attributes,
)
from novafabric.capture.hooks._sdk_errors import (
    SDK_ERROR_EXT,
    describe_sdk_error,
    mark_failed_by,
)
from novafabric.capture.hooks._sdk_streams import (
    API_SURFACE_EXT,
    OPENAI_CHAT_SURFACE,
    OPENAI_RESPONSES_SURFACE,
    RESPONSE_STATUS_EXT,
    STREAM_ERROR_EVENT_EXT,
    AsyncRecordingStream,
    OpenAIChatStreamAccumulator,
    OpenAIResponsesStreamAccumulator,
    RecordingStream,
    attach_stream_info,
    is_async_stream,
    is_raw_response_call,
    is_sync_stream,
    response_status_detail,
    stream_error_event_detail,
)
from novafabric.capture.hooks._tool_call_refs import (
    note_dropped_tool_calls,
    openai_tool_call_refs_with_dropped,
    parse_tool_arguments,
)
from novafabric.capture.record_roles import sdk_call_scope, stamp_logical_record
from novafabric.cost.usage_types import usage_from_openai

if TYPE_CHECKING:
    from novafabric.capture.capsule import CapsuleWriter


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _base_url(resource: Any) -> str:
    """Best-effort base URL of the client behind an openai SDK resource.

    The SDK hook patches ``Completions.create``, so unlike the HTTP-transport
    hooks it never sees a request URL and left ``endpoint`` empty on every
    record. That erases the only field distinguishing an Azure deployment from
    api.openai.com — precisely the claim ``examples/azure-openai`` exists to
    demonstrate. Never raises: capture must not fail because a client object
    has an unexpected shape.
    """
    try:
        return str(getattr(resource._client, "base_url", "") or "")
    except Exception:  # noqa: BLE001
        return ""


#: The SDK methods the hook wraps: (module, class, attribute, surface, async).
#: ``chat`` records the Chat Completions shape; ``responses`` the Responses API
#: (ADR-0304: async and Responses API calls used to be recorded by the wire hook
#: only, with no response, so mocked replay had nothing to serve).
_TARGETS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("openai.resources.chat.completions", "Completions", "create", "chat", False),
    ("openai.resources.chat.completions", "AsyncCompletions", "create", "chat", True),
    ("openai.resources.responses", "Responses", "create", "responses", False),
    ("openai.resources.responses", "AsyncResponses", "create", "responses", True),
)


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def responses_choice(response: Any) -> tuple[dict[str, Any] | None, int]:
    """The canonical ``Choice`` of a Responses API ``Response`` (ADR-0304).

    Text from ``message`` output items becomes ``message.content``;
    ``function_call`` items become ``message.tool_calls`` with ``id`` = the
    item's ``call_id`` (what the workload echoes back in
    ``function_call_output``). Other item types (reasoning, hosted tools) are
    not represented. Returns ``(None, 0)`` for a response with no output.
    ``finish_reason`` is ``None`` for the partial fold of a stream that ended
    before its terminal event: the provider delivered no status to map.
    """
    output = _get(response, "output")
    if not isinstance(output, (list, tuple)) or not output:
        return None, 0
    texts: list[str] = []
    calls: list[dict[str, Any]] = []
    dropped = 0
    refused = False
    for item in output:
        kind = _get(item, "type")
        if kind == "message":
            for part in _get(item, "content") or []:
                if _get(part, "type") == "output_text":
                    texts.append(str(_get(part, "text") or ""))
                elif _get(part, "type") == "refusal":
                    texts.append(str(_get(part, "refusal") or ""))
                    refused = True
        elif kind == "function_call":
            name = _get(item, "name")
            if not isinstance(name, str) or not name:
                dropped += 1
                continue
            calls.append({
                "id": str(_get(item, "call_id") or _get(item, "id") or ""),
                "name": name,
                "arguments": parse_tool_arguments(_get(item, "arguments")),
            })
    reason = _get(_get(response, "incomplete_details"), "reason")
    finish: str | None
    if _get(response, "nf_partial_fold"):
        finish = None
    elif calls:
        finish = "tool_calls"
    elif reason == "max_output_tokens":
        finish = "length"
    elif reason == "content_filter" or refused:
        finish = "content_filter"
    else:
        finish = "stop"
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(texts) if texts else None,
    }
    if calls:
        message["tool_calls"] = calls
    return {"index": 0, "message": message, "finish_reason": finish}, dropped


class OpenAIHook:
    def __init__(self, writer: "CapsuleWriter", parent_span_id: str) -> None:
        self._writer = writer
        self._parent_span_id = parent_span_id
        #: The sync ``chat.completions.create`` original (kept for callers).
        self._original: Any = None
        self._patched: list[tuple[Any, str, Any]] = []

    def install(self) -> None:
        for module_name, class_name, attr, surface, is_async in _TARGETS:
            try:
                owner = getattr(importlib.import_module(module_name), class_name)
                original = getattr(owner, attr)
            except (ImportError, AttributeError):
                continue
            setattr(owner, attr, self._wrap(original, surface, is_async))
            self._patched.append((owner, attr, original))
            if (class_name, surface) == ("Completions", "chat"):
                self._original = original

    def _wrap(self, original: Any, surface: str, is_async: bool) -> Any:
        hook_self = self
        if is_async:

            @functools.wraps(original)
            async def patched_async(inner_self: Any, *args: Any, **kwargs: Any) -> Any:
                bound = original.__get__(inner_self, type(inner_self))
                return await hook_self._intercept_async(
                    surface, bound, _base_url(inner_self), kwargs
                )

            return patched_async

        @functools.wraps(original)
        def patched(inner_self: Any, *args: Any, **kwargs: Any) -> Any:
            bound = original.__get__(inner_self, type(inner_self))
            return hook_self._intercept_surface(
                surface, bound, _base_url(inner_self), kwargs
            )

        return patched

    def uninstall(self) -> None:
        while self._patched:
            owner, attr, original = self._patched.pop()
            try:
                setattr(owner, attr, original)
            except (AttributeError, TypeError):
                pass
        self._original = None

    def _intercept(self, original_bound: Any, endpoint: str = "", **kwargs: Any) -> Any:
        return self._intercept_surface("chat", original_bound, endpoint, kwargs)

    def _intercept_surface(
        self, surface: str, original_bound: Any, endpoint: str, kwargs: dict[str, Any]
    ) -> Any:
        started = _now()
        t0 = time.monotonic()
        # ADR-0305: every wire record written inside the SDK call (one per HTTP
        # attempt, retries included) is transport for this logical call id.
        call_id = new_ulid()
        try:
            with sdk_call_scope(call_id):
                response = original_bound(**kwargs)
        except Exception as exc:
            self._record_error(started, _now(), int((time.monotonic() - t0) * 1000),
                               kwargs, exc, endpoint, surface=surface, call_id=call_id)
            raise
        if kwargs.get("stream") is True and is_sync_stream(response):
            acc = self._accumulator(surface)
            return RecordingStream(
                response, acc,
                self._on_stream_done(surface, started, t0, kwargs, endpoint, call_id, acc),
            )
        self._record_for(surface, started, _now(), int((time.monotonic() - t0) * 1000),
                         kwargs, response, endpoint, call_id=call_id)
        return response

    async def _intercept_async(
        self, surface: str, original_bound: Any, endpoint: str, kwargs: dict[str, Any]
    ) -> Any:
        started = _now()
        t0 = time.monotonic()
        call_id = new_ulid()
        try:
            with sdk_call_scope(call_id):
                response = await original_bound(**kwargs)
        except Exception as exc:
            self._record_error(started, _now(), int((time.monotonic() - t0) * 1000),
                               kwargs, exc, endpoint, surface=surface, call_id=call_id)
            raise
        if kwargs.get("stream") is True and is_async_stream(response):
            acc = self._accumulator(surface)
            return AsyncRecordingStream(
                response, acc,
                self._on_stream_done(surface, started, t0, kwargs, endpoint, call_id, acc),
            )
        self._record_for(surface, started, _now(), int((time.monotonic() - t0) * 1000),
                         kwargs, response, endpoint, call_id=call_id)
        return response

    @staticmethod
    def _accumulator(surface: str) -> Any:
        if surface == "responses":
            return OpenAIResponsesStreamAccumulator()
        return OpenAIChatStreamAccumulator()

    def _on_stream_done(
        self, surface: str, started: str, t0: float, kwargs: dict[str, Any], endpoint: str,
        call_id: str | None = None, accumulator: Any = None,
    ) -> Any:
        def done(
            response: Any, count: int, first_ms: int | None, complete: bool,
            error: BaseException | None = None,
        ) -> None:
            self._record_for(
                surface, started, _now(), int((time.monotonic() - t0) * 1000),
                kwargs, response, endpoint, stream_info=(count, first_ms, complete),
                call_id=call_id, stream_error=error,
                error_event=getattr(accumulator, "error_event", None),
            )

        return done

    def _record_for(
        self, surface: str, started: str, finished: str, duration_ms: int,
        kwargs: dict[str, Any], response: Any, endpoint: str,
        stream_info: tuple[int, int | None, bool] | None = None,
        call_id: str | None = None,
        stream_error: BaseException | None = None,
        error_event: Any = None,
    ) -> None:
        if is_raw_response_call(kwargs):
            # An HTTP response wrapper, not a parsed response: nothing to fold,
            # so the record carries no choices and replay never serves it.
            response = None
        if surface == "responses":
            self._record_responses(started, finished, duration_ms, kwargs, response,
                                   endpoint, stream_info=stream_info, call_id=call_id,
                                   stream_error=stream_error, error_event=error_event)
        else:
            self._record(started, finished, duration_ms, kwargs, response, "success",
                         endpoint, stream_info=stream_info, call_id=call_id,
                         stream_error=stream_error)

    def _record_responses(
        self,
        started: str,
        finished: str,
        duration_ms: int,
        kwargs: dict[str, Any],
        response: Any,
        endpoint: str = "",
        *,
        stream_info: tuple[int, int | None, bool] | None = None,
        call_id: str | None = None,
        stream_error: BaseException | None = None,
        error_event: Any = None,
    ) -> None:
        choice, dropped = responses_choice(response)
        returned_failed = _get(response, "status") == "failed"
        # A delivered ``error`` event (yielded by the SDK, not raised) also makes
        # the call a failure; the event itself is kept verbatim below.
        failed = returned_failed or error_event is not None
        record = build_record_envelope(
            model_call_id=call_id or new_ulid(),
            parent_span_id=self._parent_span_id,
            started_at=started,
            finished_at=finished,
            duration_ms=duration_ms,
            status="error" if failed else "success",
        )
        record.update(
            extract_request_attributes(kwargs, url=endpoint, gen_ai_system="openai")
        )
        max_output = kwargs.get("max_output_tokens")
        if isinstance(max_output, int) and not isinstance(max_output, bool):
            record["gen_ai.request.max_tokens"] = max_output
        record["gen_ai.response.model"] = (
            _get(response, "model") or record["gen_ai.request.model"]
        )
        record["gen_ai.response.choices"] = [choice] if choice else []
        if choice and choice["finish_reason"] is not None:
            record["gen_ai.response.finish_reasons"] = [choice["finish_reason"]]
        usage = _get(response, "usage")
        record["gen_ai.usage.input_tokens"] = int(_get(usage, "input_tokens") or 0)
        record["gen_ai.usage.output_tokens"] = int(_get(usage, "output_tokens") or 0)
        if returned_failed:
            error = _get(response, "error")
            record["error"] = {
                "type": str(_get(error, "code") or "ResponseFailed"),
                "message": str(_get(error, "message") or "the response failed"),
                "traceback_ref": None,
            }
        elif failed:
            record["error"] = {
                "type": str(_get(error_event, "code") or _get(error_event, "type") or "error"),
                "message": str(_get(error_event, "message") or ""),
                "traceback_ref": None,
            }
        extensions = record.setdefault("extensions", {})
        extensions[API_SURFACE_EXT] = OPENAI_RESPONSES_SURFACE
        event_detail = stream_error_event_detail(error_event)
        if event_detail is not None:
            extensions[STREAM_ERROR_EVENT_EXT] = event_detail
        # Additive (issue #16): the provider's status verbatim, so mocked replay
        # returns a failed/incomplete Response exactly as the SDK returned it.
        status_detail = response_status_detail(response)
        if status_detail is not None:
            extensions[RESPONSE_STATUS_EXT] = status_detail
        note_dropped_tool_calls(record, dropped)
        attach_stream_info(record, stream_info)
        if stream_error is not None:
            mark_failed_by(record, stream_error, "openai")
        response_id = _get(response, "id")
        if response_id:
            record["gen_ai.response.id"] = str(response_id)
        stamp_logical_record(record)
        get_current_writer(self._writer).append_model_call(record)

    def _record(
        self,
        started: str,
        finished: str,
        duration_ms: int,
        kwargs: dict[str, Any],
        response: Any,
        status: str,
        endpoint: str = "",
        *,
        stream_info: tuple[int, int | None, bool] | None = None,
        call_id: str | None = None,
        stream_error: BaseException | None = None,
    ) -> None:
        choices: list[dict[str, Any]] = []
        finish_reasons: list[str | None] = []
        raw_finish_reasons: list[str] = []
        dropped = 0
        for c in getattr(response, "choices", []):
            msg = c.message
            # None when the provider delivered no finish reason (a stream
            # abandoned, failed or ended before it): recorded as null.
            delivered = getattr(c, "finish_reason", None)
            raw_finish = str(delivered) if delivered else None
            choice: dict[str, Any] = {
                "index": c.index,
                "message": {
                    "role": getattr(msg, "role", "assistant"),
                    "content": getattr(msg, "content", None),
                },
                "finish_reason": canonical_finish_reason("openai", raw_finish),
            }
            # Additive: the assistant's tool-call requests, so mocked replay can
            # serve them back (absent on a text-only turn).
            tool_calls, n_dropped = openai_tool_call_refs_with_dropped(msg)
            dropped += n_dropped
            if tool_calls:
                choice["message"]["tool_calls"] = tool_calls
            choices.append(choice)
            finish_reasons.append(choice["finish_reason"])
            if raw_finish is not None:
                raw_finish_reasons.append(raw_finish)
        usage = getattr(response, "usage", None)
        record = build_record_envelope(
            model_call_id=call_id or new_ulid(),
            parent_span_id=self._parent_span_id,
            started_at=started,
            finished_at=finished,
            duration_ms=duration_ms,
            status=status,
        )
        # Request-side semconv fields (temperature, max_tokens, top_p, seed, ...).
        record.update(
            extract_request_attributes(kwargs, url=endpoint, gen_ai_system="openai")
        )
        # Response-side fields the SDK lets us populate richly.
        record["gen_ai.response.model"] = getattr(response, "model", record["gen_ai.request.model"])
        record["gen_ai.response.choices"] = choices
        record["gen_ai.usage.input_tokens"] = (
            getattr(usage, "prompt_tokens", 0) if usage else 0
        )
        record["gen_ai.usage.output_tokens"] = (
            getattr(usage, "completion_tokens", 0) if usage else 0
        )
        # ADR-0132: additive, optional per-type usage block (verbatim from the
        # provider payload; absent when the provider reports no breakdown).
        usage_block = usage_from_openai(usage)
        if usage_block is not None:
            record["nova.usage"] = usage_block
        # One finish reason per choice, so written only when every choice
        # delivered one (the attribute is optional in OTel GenAI).
        delivered_reasons = [f for f in finish_reasons if f is not None]
        if finish_reasons and len(delivered_reasons) == len(finish_reasons):
            record["gen_ai.response.finish_reasons"] = delivered_reasons
            attach_provider_finish_reasons(record, raw_finish_reasons, delivered_reasons)
        record.setdefault("extensions", {})[API_SURFACE_EXT] = OPENAI_CHAT_SURFACE
        note_dropped_tool_calls(record, dropped)
        attach_stream_info(record, stream_info)
        if stream_error is not None:
            mark_failed_by(record, stream_error, "openai")
        response_id = getattr(response, "id", None)
        if response_id:
            record["gen_ai.response.id"] = str(response_id)
        stamp_logical_record(record)
        get_current_writer(self._writer).append_model_call(record)

    def _record_error(
        self,
        started: str,
        finished: str,
        duration_ms: int,
        kwargs: dict[str, Any],
        exc: Exception,
        endpoint: str = "",
        *,
        surface: str = "chat",
        call_id: str | None = None,
    ) -> None:
        record = build_record_envelope(
            model_call_id=call_id or new_ulid(),
            parent_span_id=self._parent_span_id,
            started_at=started,
            finished_at=finished,
            duration_ms=duration_ms,
            status="error",
        )
        record.update(
            extract_request_attributes(kwargs, url=endpoint, gen_ai_system="openai")
        )
        record["error"] = {
            "type": type(exc).__name__, "message": str(exc), "traceback_ref": None,
        }
        extensions = record.setdefault("extensions", {})
        extensions[API_SURFACE_EXT] = (
            OPENAI_RESPONSES_SURFACE if surface == "responses" else OPENAI_CHAT_SURFACE
        )
        # Additive: what mocked replay needs to raise the same exception again.
        extensions[SDK_ERROR_EXT] = describe_sdk_error(exc, "openai")
        stamp_logical_record(record)
        get_current_writer(self._writer).append_model_call(record)
