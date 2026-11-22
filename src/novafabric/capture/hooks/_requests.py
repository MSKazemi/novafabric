"""``requests`` capture hook (wire-level layer).

Wraps :meth:`requests.Session.send` and records every outbound request whose
URL matches the active URL registry. Mirrors :class:`HttpxHook` exactly —
the only difference is the SDK boundary that's patched.

Coverage rationale: every code path in ``requests`` (``requests.get``,
``Session.post``, ``Session.request``, et al.) ultimately calls
``Session.send(prepared_request, **kwargs)``. Patching that one method
catches the long tail (LangChain HTTP adapters, LlamaIndex REST clients,
``boto3.client('bedrock-runtime')`` for AWS, vendor SDKs that ship over
``requests``).
"""
from __future__ import annotations

import functools
import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from novafabric.capture._ulid import new_ulid
from novafabric.capture.event_recorder import _is_ai_api, get_current_recorder, get_current_writer
from novafabric.capture.hooks._bedrock import bedrock_surface
from novafabric.capture.hooks._fail_open import (
    MODEL_CALLS_STREAM,
    fail_open,
    note_capture_failure,
)
from novafabric.capture.hooks._layering import claim_higher_layer
from novafabric.capture.hooks._otel_genai import (
    build_record_envelope,
    extract_request_attributes,
    safe_response_size,
)
from novafabric.capture.hooks._sdk_streams import CaptureBinding, _StreamState
from novafabric.capture.hooks._url_registry import (
    UrlRegistry,
    load_url_registry,
)
from novafabric.capture.hooks._wire_body import (
    WireBodySink,
    content_encoding,
    mark_generic,
    recording_off_reason,
    records_body,
)
from novafabric.capture.hooks._wire_body import apply_response as apply_wire_response
from novafabric.capture.record_roles import current_sdk_call_id, stamp_wire_record

if TYPE_CHECKING:
    from novafabric.capture.capsule import CapsuleWriter


_LIBRARY = "requests"
NETWORK_EVENTS_STREAM = "network_events.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class RequestsHook:
    def __init__(
        self,
        writer: "CapsuleWriter",
        parent_span_id: str,
        *,
        registry: UrlRegistry | None = None,
    ) -> None:
        self._writer = writer
        self._parent_span_id = parent_span_id
        self._registry = registry if registry is not None else load_url_registry()
        self._original_send: Any = None

    def install(self) -> None:
        try:
            import requests
            self._original_send = requests.Session.send
            hook_self = self

            @functools.wraps(self._original_send)
            def patched_send(
                session_self: Any, prepared_request: Any, **kwargs: Any
            ) -> Any:
                original_bound = hook_self._original_send.__get__(
                    session_self, type(session_self)
                )
                return hook_self._wrapped_send(
                    prepared_request, original=original_bound, **kwargs
                )

            requests.Session.send = patched_send  # type: ignore[method-assign, assignment]
        except (ImportError, AttributeError):
            pass

    def uninstall(self) -> None:
        if self._original_send is None:
            return
        try:
            import requests
            requests.Session.send = self._original_send  # type: ignore[method-assign]
        except (ImportError, AttributeError):
            pass
        finally:
            self._original_send = None

    def _target_url(self, prepared_request: Any) -> str | None:
        """The URL to record, or ``None`` to pass the call through. Never raises:
        a classification failure (a custom registry, an odd request object)
        loses the record, never the user's request (ADR-0021)."""
        try:
            url = str(getattr(prepared_request, "url", ""))
            return url if self._registry.is_known(url) else None
        except Exception as exc:  # noqa: BLE001 -- capture must never block the workload
            note_capture_failure(_LIBRARY, MODEL_CALLS_STREAM, exc)
            return None

    def _wrapped_send(
        self, prepared_request: Any, original: Any = None, **kwargs: Any
    ) -> Any:
        url = self._target_url(prepared_request)
        if url is None:
            if original is not None:
                # Even passthroughs must claim the layer so a lower hook
                # (e.g. Urllib3Hook) does not also record the same call.
                with claim_higher_layer():
                    return original(prepared_request, **kwargs)
            raise RuntimeError("original send function required")

        started = _now()
        t0 = time.monotonic()
        response: Any = None
        error_status: int | None = None
        try:
            with claim_higher_layer():
                response = original(prepared_request, **kwargs) if original else None
        except BaseException:
            # BaseException: a cancelled task (CancelledError) or Ctrl-C never got a
            # response either; recording it as a success is review capture-runners-05.
            # Connection failures, timeouts, TLS errors — record the attempt
            # so the audit trail is complete, then re-raise. Mirrors the
            # try/finally pattern in _aiohttp.py and _urllib3.py.
            error_status = 599
            raise
        finally:
            self._record(prepared_request, response, url, started,
                         int((time.monotonic() - t0) * 1000), error_status, t0)
        return response

    def _record(
        self, prepared_request: Any, response: Any, url: str, started: str,
        duration_ms: int, error_status: int | None, t0: float | None = None,
    ) -> None:
        """Write the model-call record and the network event. Never raises:
        the response or the transport's exception passes through unchanged."""
        status_code = 0
        with fail_open(_LIBRARY, MODEL_CALLS_STREAM):
            try:
                raw_body = getattr(prepared_request, "body", None) or b"{}"
                body: Any = json.loads(raw_body) if isinstance(raw_body, (bytes, str)) else {}
            except Exception:  # noqa: BLE001 -- a non-JSON body records no attributes
                body = {}
            if not isinstance(body, dict):
                body = {}
            # ``response is None``, never ``not response``: a requests
            # ``Response`` is falsy for every 4xx/5xx (``__bool__`` is ``.ok``),
            # which recorded a 503 as status 0 and ``"success"``.
            status_code = (
                error_status
                if error_status is not None
                else getattr(response, "status_code", 200) if response is not None else 0
            )
            system = self._registry.gen_ai_system(url)
            generic = records_body(
                system, in_sdk_call=current_sdk_call_id() is not None,
                dedicated=bedrock_surface(url, system) is not None,
            )
            if not generic:
                self._write_record(prepared_request, url, body, status_code, started,
                                   duration_ms)
            elif not self._defer_wire_record(
                prepared_request, url, body, response, status_code, started, t0
            ):
                self._write_record(
                    prepared_request, url, body, status_code, started, duration_ms,
                    wire=_buffered_wire(response, error_status),
                )

        # Record a NetworkEvent in the active capsule (fail-open).
        with fail_open(_LIBRARY, NETWORK_EVENTS_STREAM):
            _recorder = get_current_recorder()
            if _recorder is not None:
                _parsed = urlparse(url)
                _recorder.record_network_event(
                    method=getattr(prepared_request, "method", "UNKNOWN") or "UNKNOWN",
                    url=url,
                    host=_parsed.hostname or "",
                    port=_parsed.port,
                    status_code=status_code if status_code != 0 else None,
                    response_size_bytes=safe_response_size(response),
                    duration_ms=float(duration_ms),
                    library=_LIBRARY,
                    is_ai_api=_is_ai_api(url),
                )


    # ── records ──────────────────────────────────────────────────────────

    def _write_record(
        self, prepared_request: Any, url: str, body: dict[str, Any], status_code: int,
        started: str, duration_ms: int, *, wire: dict[str, Any] | None = None,
    ) -> None:
        """One model-call record; ``wire`` (ADR-0324): ``apply_response`` keywords of a
        generic-wire call, whose record also carries the match key."""
        record = build_record_envelope(
            model_call_id=new_ulid(),
            parent_span_id=self._parent_span_id,
            started_at=started,
            finished_at=_now(),
            duration_ms=duration_ms,
            status="success" if 0 < status_code < 400 else "error",
        )
        record.update(extract_request_attributes(
            body, url=url, gen_ai_system=self._registry.gen_ai_system(url),
        ))
        if wire is not None:
            mark_generic(record, str(getattr(prepared_request, "method", "") or ""), url,
                         getattr(prepared_request, "body", None))
            apply_wire_response(record, status=status_code, **wire)
        get_current_writer(self._writer).append_model_call(stamp_wire_record(record))

    def _defer_wire_record(
        self, prepared_request: Any, url: str, body: dict[str, Any], response: Any,
        status_code: int, started: str, t0: float | None,
    ) -> bool:
        """Copy a ``stream=True`` body as the workload reads it; record when the read ends.

        True when the record is now deferred; False to record now (no response, a body
        already read, recording off, a content encoding, or a raw body whose reads cannot
        be shadowed)."""
        from novafabric.capture.hooks._urllib3 import _shadow_body_reads

        if response is None or recording_off_reason() is not None:
            return False
        if isinstance(vars(response).get("_content"), (bytes, bytearray)):
            return False
        raw = getattr(response, "raw", None)
        if raw is None:
            return False
        headers = getattr(response, "headers", None)
        sink = WireBodySink(encoding=content_encoding(headers))
        if sink.not_recorded is not None:
            return False
        start_t = t0 if t0 is not None else time.monotonic()
        reason = str(getattr(response, "reason", "") or "")

        def done(
            folded: WireBodySink, _count: int, _first_ms: int | None, complete: bool,
            error: BaseException | None = None,
        ) -> None:
            with fail_open(_LIBRARY, MODEL_CALLS_STREAM):
                self._write_record(
                    prepared_request, url, body, status_code, started,
                    int((time.monotonic() - start_t) * 1000),
                    wire={
                        "reason": reason, "headers": headers, "sink": folded,
                        "streamed": True, "complete": complete,
                        "not_recorded": (
                            f"the response stream failed part-way ({type(error).__name__})"
                            if error is not None else None
                        ),
                    },
                )

        state = _StreamState(sink, done, CaptureBinding.capture())
        return _shadow_body_reads(raw, state.add, state.finish)


def _buffered_wire(response: Any, error_status: int | None) -> dict[str, Any]:
    """``apply_response`` keywords for a response whose body requests already read."""
    if response is None or error_status is not None:
        return {"reason": "", "headers": None, "sink": None, "streamed": False,
                "complete": False, "not_recorded": "the request raised before a response"}
    content = vars(response).get("_content")
    sink: WireBodySink | None = None
    why: str | None = None
    if isinstance(content, (bytes, bytearray)):
        sink = WireBodySink()
        sink.add(content)
    else:
        why = "the response body was not read"
    return {"reason": str(getattr(response, "reason", "") or ""),
            "headers": getattr(response, "headers", None), "sink": sink,
            "streamed": False, "complete": True, "not_recorded": why}
