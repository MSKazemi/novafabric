from __future__ import annotations

import functools
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from novafabric.capture._ulid import new_ulid
from novafabric.capture.event_recorder import get_current_writer
from novafabric.capture.hooks._otel_genai import (
    build_record_envelope,
    extract_request_attributes,
)
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


class OpenAIHook:
    def __init__(self, writer: "CapsuleWriter", parent_span_id: str) -> None:
        self._writer = writer
        self._parent_span_id = parent_span_id
        self._original: Any = None

    def install(self) -> None:
        try:
            import openai.resources.chat.completions as _mod
            self._original = _mod.Completions.create
            hook_self = self

            @functools.wraps(self._original)
            def patched(inner_self: Any, *args: Any, **kwargs: Any) -> Any:
                original_bound = hook_self._original.__get__(inner_self, type(inner_self))
                return hook_self._intercept(
                    original_bound, _base_url(inner_self), **kwargs
                )

            _mod.Completions.create = patched  # type: ignore[method-assign, assignment]
        except (ImportError, AttributeError):
            pass

    def uninstall(self) -> None:
        if self._original is None:
            return
        try:
            import openai.resources.chat.completions as _mod
            _mod.Completions.create = self._original  # type: ignore[method-assign]
        except (ImportError, AttributeError):
            pass
        finally:
            self._original = None

    def _intercept(self, original_bound: Any, endpoint: str = "", **kwargs: Any) -> Any:
        started = _now()
        t0 = time.monotonic()
        try:
            response = original_bound(**kwargs)
            duration_ms = int((time.monotonic() - t0) * 1000)
            self._record(started, _now(), duration_ms, kwargs, response, "success",
                         endpoint)
            return response
        except Exception as exc:
            self._record_error(started, _now(), int((time.monotonic() - t0) * 1000),
                               kwargs, exc, endpoint)
            raise

    def _record(
        self,
        started: str,
        finished: str,
        duration_ms: int,
        kwargs: dict[str, Any],
        response: Any,
        status: str,
        endpoint: str = "",
    ) -> None:
        choices: list[dict[str, Any]] = []
        finish_reasons: list[str] = []
        for c in getattr(response, "choices", []):
            msg = c.message
            choice: dict[str, Any] = {
                "index": c.index,
                "message": {
                    "role": getattr(msg, "role", "assistant"),
                    "content": getattr(msg, "content", None),
                },
                "finish_reason": getattr(c, "finish_reason", "stop") or "stop",
            }
            choices.append(choice)
            finish_reasons.append(choice["finish_reason"])
        usage = getattr(response, "usage", None)
        record = build_record_envelope(
            model_call_id=new_ulid(),
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
        if finish_reasons:
            record["gen_ai.response.finish_reasons"] = finish_reasons
        response_id = getattr(response, "id", None)
        if response_id:
            record["gen_ai.response.id"] = str(response_id)
        get_current_writer(self._writer).append_model_call(record)

    def _record_error(
        self,
        started: str,
        finished: str,
        duration_ms: int,
        kwargs: dict[str, Any],
        exc: Exception,
        endpoint: str = "",
    ) -> None:
        record = build_record_envelope(
            model_call_id=new_ulid(),
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
        get_current_writer(self._writer).append_model_call(record)
