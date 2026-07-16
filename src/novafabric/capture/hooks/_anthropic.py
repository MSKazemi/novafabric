from __future__ import annotations

import functools
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from novafabric.capture._ulid import new_ulid
from novafabric.capture.hooks._otel_genai import (
    build_record_envelope,
    extract_request_attributes,
)
from novafabric.cost.usage_types import usage_from_anthropic

if TYPE_CHECKING:
    from novafabric.capture.capsule import CapsuleWriter


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class AnthropicHook:
    def __init__(self, writer: "CapsuleWriter", parent_span_id: str) -> None:
        self._writer = writer
        self._parent_span_id = parent_span_id
        self._original: Any = None

    def install(self) -> None:
        try:
            import anthropic.resources.messages as _mod  # type: ignore[import-not-found]
            self._original = _mod.Messages.create
            hook_self = self

            @functools.wraps(self._original)
            def patched(inner_self: Any, *args: Any, **kwargs: Any) -> Any:
                original_bound = hook_self._original.__get__(inner_self, type(inner_self))
                return hook_self._intercept(original_bound, **kwargs)

            _mod.Messages.create = patched
        except (ImportError, AttributeError):
            pass

    def uninstall(self) -> None:
        if self._original is None:
            return
        try:
            import anthropic.resources.messages as _mod
            _mod.Messages.create = self._original
        except (ImportError, AttributeError):
            pass
        finally:
            self._original = None

    def _intercept(self, original_bound: Any, **kwargs: Any) -> Any:
        started = _now()
        t0 = time.monotonic()
        try:
            response = original_bound(**kwargs)
            duration_ms = int((time.monotonic() - t0) * 1000)
            self._record(started, _now(), duration_ms, kwargs, response, "success")
            return response
        except Exception as exc:
            self._record_error(started, _now(), int((time.monotonic() - t0) * 1000),
                               kwargs, exc)
            raise

    def _record(
        self,
        started: str,
        finished: str,
        duration_ms: int,
        kwargs: dict[str, Any],
        response: Any,
        status: str,
    ) -> None:
        parts = getattr(response, "content", [])
        text = " ".join(getattr(p, "text", "") for p in parts if hasattr(p, "text"))
        finish_reason = str(getattr(response, "stop_reason", "end_turn") or "stop")
        choices = [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": finish_reason,
        }]
        usage = getattr(response, "usage", None)
        record = build_record_envelope(
            model_call_id=new_ulid(),
            parent_span_id=self._parent_span_id,
            started_at=started,
            finished_at=finished,
            duration_ms=duration_ms,
            status=status,
        )
        record.update(extract_request_attributes(kwargs, gen_ai_system="anthropic"))
        record["gen_ai.response.model"] = getattr(response, "model", record["gen_ai.request.model"])
        record["gen_ai.response.choices"] = choices
        record["gen_ai.usage.input_tokens"] = getattr(usage, "input_tokens", 0) if usage else 0
        record["gen_ai.usage.output_tokens"] = getattr(usage, "output_tokens", 0) if usage else 0
        # ADR-0132: additive, optional per-type usage block (verbatim from the
        # provider payload; absent when the provider reports no breakdown).
        usage_block = usage_from_anthropic(usage)
        if usage_block is not None:
            record["nova.usage"] = usage_block
        record["gen_ai.response.finish_reasons"] = [finish_reason]
        response_id = getattr(response, "id", None)
        if response_id:
            record["gen_ai.response.id"] = str(response_id)
        self._writer.append_model_call(record)

    def _record_error(
        self,
        started: str,
        finished: str,
        duration_ms: int,
        kwargs: dict[str, Any],
        exc: Exception,
    ) -> None:
        record = build_record_envelope(
            model_call_id=new_ulid(),
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
        self._writer.append_model_call(record)
