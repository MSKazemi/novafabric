"""httpx capture hook (wire-level layer).

Wraps both ``httpx.Client.send`` (sync) and ``httpx.AsyncClient.send``
(async) and records every request whose URL is matched by the active URL
registry (see :mod:`._url_registry`).

Wire-level capture is the primary growth axis per RFC-0001 Option C.
The async path is required for LLM SDKs that use ``httpx.AsyncClient``
(e.g. ``langchain_ollama`` when invoked via ``ainvoke`` / ``asyncio.run``).
"""
from __future__ import annotations

import functools
import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from novafabric.capture._ulid import new_ulid
from novafabric.capture.event_recorder import _is_ai_api, get_current_recorder, get_current_writer
from novafabric.capture.hooks._otel_genai import (
    build_record_envelope,
    extract_request_attributes,
    safe_response_size,
)
from novafabric.capture.hooks._url_registry import (
    UrlRegistry,
    load_url_registry,
)

if TYPE_CHECKING:
    from novafabric.capture.capsule import CapsuleWriter


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class HttpxHook:
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
        self._original_sync: Any = None
        self._original_async: Any = None

    def install(self) -> None:
        try:
            import httpx
            hook_self = self

            # ── sync path ────────────────────────────────────────────────────
            self._original_sync = httpx.Client.send

            @functools.wraps(self._original_sync)
            def patched_send(client_self: Any, request: Any, **kwargs: Any) -> Any:
                original_bound = hook_self._original_sync.__get__(client_self, type(client_self))
                return hook_self._wrapped_send(request, original=original_bound, **kwargs)

            httpx.Client.send = patched_send  # type: ignore[assignment]

            # ── async path ───────────────────────────────────────────────────
            self._original_async = httpx.AsyncClient.send

            @functools.wraps(self._original_async)
            async def patched_async_send(
                client_self: Any, request: Any, **kwargs: Any
            ) -> Any:
                original_bound = hook_self._original_async.__get__(
                    client_self, type(client_self)
                )
                return await hook_self._wrapped_send_async(
                    request, original=original_bound, **kwargs
                )

            httpx.AsyncClient.send = patched_async_send  # type: ignore[assignment]

        except (ImportError, AttributeError):
            pass

    def uninstall(self) -> None:
        try:
            import httpx
            if self._original_sync is not None:
                httpx.Client.send = self._original_sync  # type: ignore[method-assign]
            if self._original_async is not None:
                httpx.AsyncClient.send = self._original_async  # type: ignore[method-assign]
        except (ImportError, AttributeError):
            pass
        finally:
            self._original_sync = None
            self._original_async = None

    def _record(
        self,
        request: Any,
        response: Any,
        started: str,
        duration_ms: int,
        error_status: int | None,
    ) -> None:
        url = str(request.url)
        try:
            body: dict[str, Any] = json.loads(
                getattr(request, "content", b"{}") or b"{}"
            )
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        status_code = (
            error_status
            if error_status is not None
            else getattr(response, "status_code", 200) if response else 0
        )
        record = build_record_envelope(
            model_call_id=new_ulid(),
            parent_span_id=self._parent_span_id,
            started_at=started,
            finished_at=_now(),
            duration_ms=duration_ms,
            status="success" if status_code < 400 else "error",
        )
        record.update(extract_request_attributes(
            body, url=url, gen_ai_system=self._registry.gen_ai_system(url),
        ))
        try:
            get_current_writer(self._writer).append_model_call(record)
        except Exception:
            pass

        # Record a NetworkEvent in the active capsule (fail-open).
        try:
            _recorder = get_current_recorder()
            if _recorder is not None:
                from urllib.parse import urlparse
                _parsed = urlparse(url)
                _recorder.record_network_event(
                    method=getattr(request, "method", "UNKNOWN") or "UNKNOWN",
                    url=url,
                    host=_parsed.hostname or "",
                    port=_parsed.port,
                    status_code=status_code if status_code != 0 else None,
                    response_size_bytes=safe_response_size(response),
                    duration_ms=float(duration_ms),
                    library="httpx",
                    is_ai_api=_is_ai_api(url),
                )
        except Exception:
            pass  # fail-open

    def _wrapped_send(self, request: Any, original: Any = None, **kwargs: Any) -> Any:
        url = str(request.url)
        if not self._registry.is_known(url):
            if original is not None:
                return original(request, **kwargs)
            raise RuntimeError("original send function required")

        started = _now()
        t0 = time.monotonic()
        response: Any = None
        error_status: int | None = None
        try:
            response = original(request, **kwargs) if original else None
        except Exception:
            error_status = 599
            raise
        finally:
            duration_ms = int((time.monotonic() - t0) * 1000)
            self._record(request, response, started, duration_ms, error_status)
        return response

    async def _wrapped_send_async(
        self, request: Any, original: Any = None, **kwargs: Any
    ) -> Any:
        url = str(request.url)
        if not self._registry.is_known(url):
            if original is not None:
                return await original(request, **kwargs)
            raise RuntimeError("original send function required")

        started = _now()
        t0 = time.monotonic()
        response: Any = None
        error_status: int | None = None
        try:
            response = await original(request, **kwargs) if original else None
        except Exception:
            error_status = 599
            raise
        finally:
            duration_ms = int((time.monotonic() - t0) * 1000)
            self._record(request, response, started, duration_ms, error_status)
        return response
