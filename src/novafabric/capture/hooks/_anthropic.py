from __future__ import annotations

import functools
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
from novafabric.capture.hooks._sdk_errors import SDK_ERROR_EXT, describe_sdk_error
from novafabric.capture.hooks._sdk_streams import (
    ANTHROPIC_MESSAGES_SURFACE,
    API_SURFACE_EXT,
    AnthropicStreamAccumulator,
    AsyncRecordingStream,
    RecordingStream,
    attach_stream_info,
    is_async_stream,
    is_raw_response_call,
    is_sync_stream,
)
from novafabric.capture.hooks._tool_call_refs import (
    anthropic_tool_call_refs_with_dropped,
    note_dropped_tool_calls,
)
from novafabric.capture.record_roles import sdk_call_scope, stamp_logical_record
from novafabric.cost.usage_types import usage_from_anthropic

if TYPE_CHECKING:
    from novafabric.capture.capsule import CapsuleWriter


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class AnthropicHook:
    """Records ``messages.create`` -- sync and async, streamed or not (ADR-0304)."""

    def __init__(self, writer: "CapsuleWriter", parent_span_id: str) -> None:
        self._writer = writer
        self._parent_span_id = parent_span_id
        #: The sync ``Messages.create`` original (kept for callers).
        self._original: Any = None
        self._patched: list[tuple[Any, str, Any]] = []

    def install(self) -> None:
        try:
            import anthropic.resources.messages as _mod  # type: ignore[import-not-found]
        except (ImportError, AttributeError):
            return
        for class_name, is_async in (("Messages", False), ("AsyncMessages", True)):
            owner = getattr(_mod, class_name, None)
            original = getattr(owner, "create", None) if owner is not None else None
            if original is None:
                continue
            owner.create = self._wrap(original, is_async)  # type: ignore[union-attr]
            self._patched.append((owner, "create", original))
            if not is_async:
                self._original = original

    def _wrap(self, original: Any, is_async: bool) -> Any:
        hook_self = self
        if is_async:

            @functools.wraps(original)
            async def patched_async(inner_self: Any, *args: Any, **kwargs: Any) -> Any:
                bound = original.__get__(inner_self, type(inner_self))
                return await hook_self._intercept_async(bound, kwargs)

            return patched_async

        @functools.wraps(original)
        def patched(inner_self: Any, *args: Any, **kwargs: Any) -> Any:
            original_bound = original.__get__(inner_self, type(inner_self))
            return hook_self._intercept(original_bound, **kwargs)

        return patched

    def uninstall(self) -> None:
        while self._patched:
            owner, attr, original = self._patched.pop()
            try:
                setattr(owner, attr, original)
            except (AttributeError, TypeError):
                pass
        self._original = None

    def _intercept(self, original_bound: Any, **kwargs: Any) -> Any:
        started = _now()
        t0 = time.monotonic()
        # ADR-0305: wire records written inside the call are transport for it.
        call_id = new_ulid()
        try:
            with sdk_call_scope(call_id):
                response = original_bound(**kwargs)
        except Exception as exc:
            self._record_error(started, _now(), int((time.monotonic() - t0) * 1000),
                               kwargs, exc, call_id=call_id)
            raise
        if kwargs.get("stream") is True and is_sync_stream(response):
            return RecordingStream(
                response, AnthropicStreamAccumulator(),
                self._on_stream_done(started, t0, kwargs, call_id),
            )
        self._record(started, _now(), int((time.monotonic() - t0) * 1000), kwargs,
                     None if is_raw_response_call(kwargs) else response, "success",
                     call_id=call_id)
        return response

    async def _intercept_async(self, original_bound: Any, kwargs: dict[str, Any]) -> Any:
        started = _now()
        t0 = time.monotonic()
        # ADR-0305: wire records written inside the call are transport for it.
        call_id = new_ulid()
        try:
            with sdk_call_scope(call_id):
                response = await original_bound(**kwargs)
        except Exception as exc:
            self._record_error(started, _now(), int((time.monotonic() - t0) * 1000),
                               kwargs, exc, call_id=call_id)
            raise
        if kwargs.get("stream") is True and is_async_stream(response):
            return AsyncRecordingStream(
                response, AnthropicStreamAccumulator(),
                self._on_stream_done(started, t0, kwargs, call_id),
            )
        self._record(started, _now(), int((time.monotonic() - t0) * 1000), kwargs,
                     None if is_raw_response_call(kwargs) else response, "success",
                     call_id=call_id)
        return response

    def _on_stream_done(
        self, started: str, t0: float, kwargs: dict[str, Any], call_id: str | None = None
    ) -> Any:
        def done(response: Any, count: int, first_ms: int | None, complete: bool) -> None:
            self._record(
                started, _now(), int((time.monotonic() - t0) * 1000), kwargs, response,
                "success", stream_info=(count, first_ms, complete), call_id=call_id,
            )

        return done

    def _record(
        self,
        started: str,
        finished: str,
        duration_ms: int,
        kwargs: dict[str, Any],
        response: Any,
        status: str,
        *,
        stream_info: tuple[int, int | None, bool] | None = None,
        call_id: str | None = None,
    ) -> None:
        parts = getattr(response, "content", None) or []
        text = " ".join(getattr(p, "text", "") for p in parts if hasattr(p, "text"))
        # Anthropic's own stop_reason (end_turn, tool_use, ...) is outside the
        # schema enum: store the canonical value, keep the raw one additively.
        raw_finish = getattr(response, "stop_reason", None)
        finish_reason = canonical_finish_reason("anthropic", raw_finish)
        message: dict[str, Any] = {"role": "assistant", "content": text}
        # Additive: tool_use blocks as Message.tool_calls, so mocked replay can
        # serve the tool-calling turn back (absent on a text-only turn).
        tool_calls, dropped = anthropic_tool_call_refs_with_dropped(parts)
        if tool_calls:
            message["tool_calls"] = tool_calls
        choices = [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }]
        usage = getattr(response, "usage", None)
        record = build_record_envelope(
            model_call_id=call_id or new_ulid(),
            parent_span_id=self._parent_span_id,
            started_at=started,
            finished_at=finished,
            duration_ms=duration_ms,
            status=status,
        )
        record.update(extract_request_attributes(kwargs, gen_ai_system="anthropic"))
        record["gen_ai.response.model"] = getattr(response, "model", record["gen_ai.request.model"])
        # No response object (a with_raw_response call): nothing to fold, and a
        # record with no choices is never served by mocked replay.
        record["gen_ai.response.choices"] = choices if response is not None else []
        record["gen_ai.usage.input_tokens"] = getattr(usage, "input_tokens", 0) if usage else 0
        record["gen_ai.usage.output_tokens"] = getattr(usage, "output_tokens", 0) if usage else 0
        # ADR-0132: additive, optional per-type usage block (verbatim from the
        # provider payload; absent when the provider reports no breakdown).
        usage_block = usage_from_anthropic(usage)
        if usage_block is not None:
            record["nova.usage"] = usage_block
        if response is not None:
            record["gen_ai.response.finish_reasons"] = [finish_reason]
        if isinstance(raw_finish, str) and raw_finish:
            attach_provider_finish_reasons(record, [raw_finish], [finish_reason])
        record.setdefault("extensions", {})[API_SURFACE_EXT] = ANTHROPIC_MESSAGES_SURFACE
        note_dropped_tool_calls(record, dropped)
        attach_stream_info(record, stream_info)
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
        *,
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
        record.update(extract_request_attributes(kwargs, gen_ai_system="anthropic"))
        record["error"] = {
            "type": type(exc).__name__, "message": str(exc), "traceback_ref": None,
        }
        extensions = record.setdefault("extensions", {})
        extensions[API_SURFACE_EXT] = ANTHROPIC_MESSAGES_SURFACE
        # Additive: what mocked replay needs to raise the same exception again.
        extensions[SDK_ERROR_EXT] = describe_sdk_error(exc, "anthropic")
        stamp_logical_record(record)
        get_current_writer(self._writer).append_model_call(record)
