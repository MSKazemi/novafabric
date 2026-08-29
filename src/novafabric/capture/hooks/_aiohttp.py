"""``aiohttp`` capture hook (wire-level layer, async).

Wraps :meth:`aiohttp.ClientSession._request` and records every outbound
request whose URL matches the active URL registry. Mirrors
:class:`HttpxHook` and :class:`RequestsHook` exactly — the only
difference is that ``_request`` is an async coroutine, so the wrapper
must also be async.

Coverage rationale: every code path in ``aiohttp``
(``session.get``, ``session.post``, ``session.request``, et al.)
ultimately calls ``ClientSession._request(method, url, **kwargs)``.
Patching that one method catches the long tail of async-first AI SDKs
(LangChain async paths, FastAPI agents, streaming clients).

This is RFC-0001 Option C, sub-track C-3.1.
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


def _extract_body(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Best-effort JSON-body extraction from aiohttp request kwargs.

    aiohttp accepts the body via ``json=``, ``data=``, or as raw bytes
    on a custom payload. We only try to recover a dict — anything else
    falls back to an empty dict, same as the other wire-level hooks.
    """
    if "json" in kwargs and isinstance(kwargs["json"], dict):
        return dict(kwargs["json"])
    raw = kwargs.get("data")
    if isinstance(raw, (bytes, str)):
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


class AiohttpHook:
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
        self._original_request: Any = None

    def install(self) -> None:
        try:
            import aiohttp
            self._original_request = aiohttp.ClientSession._request
            hook_self = self

            @functools.wraps(self._original_request)
            async def patched_request(
                session_self: Any, method: str, str_or_url: Any, **kwargs: Any
            ) -> Any:
                original_bound = hook_self._original_request.__get__(
                    session_self, type(session_self)
                )
                return await hook_self._wrapped_request(
                    method, str_or_url, original=original_bound, **kwargs
                )

            aiohttp.ClientSession._request = patched_request  # type: ignore[method-assign, assignment]
        except (ImportError, AttributeError):
            pass

    def uninstall(self) -> None:
        if self._original_request is None:
            return
        try:
            import aiohttp
            aiohttp.ClientSession._request = self._original_request  # type: ignore[method-assign]
        except (ImportError, AttributeError):
            pass
        finally:
            self._original_request = None

    async def _wrapped_request(
        self,
        method: str,
        str_or_url: Any,
        original: Any = None,
        **kwargs: Any,
    ) -> Any:
        url = str(str_or_url)
        if not self._registry.is_known(url):
            if original is not None:
                return await original(method, str_or_url, **kwargs)
            raise RuntimeError("original _request function required")

        started = _now()
        t0 = time.monotonic()
        response: Any = None
        error_status: int | None = None
        try:
            if original is not None:
                response = await original(method, str_or_url, **kwargs)
        except Exception:
            # Record the attempt even if the call failed, then re-raise.
            error_status = 599
            raise
        finally:
            duration_ms = int((time.monotonic() - t0) * 1000)
            body = _extract_body(kwargs)
            status_code = (
                error_status
                if error_status is not None
                else getattr(response, "status", 200)
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
                # Never let a bookkeeping failure surface to the caller.
                pass

            # Record a NetworkEvent in the active capsule (fail-open) —
            # ADR-0209 D2.4 parity with the httpx/requests hooks.
            try:
                _recorder = get_current_recorder()
                if _recorder is not None:
                    from urllib.parse import urlparse
                    _parsed = urlparse(url)
                    _recorder.record_network_event(
                        method=str(method or "UNKNOWN"),
                        url=url,
                        host=_parsed.hostname or "",
                        port=_parsed.port,
                        status_code=status_code if status_code != 0 else None,
                        response_size_bytes=safe_response_size(response),
                        duration_ms=float(duration_ms),
                        library="aiohttp",
                        is_ai_api=_is_ai_api(url),
                    )
            except Exception:
                pass  # fail-open
        return response
