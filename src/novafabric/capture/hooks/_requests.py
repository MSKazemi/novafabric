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

from novafabric.capture._ulid import new_ulid
from novafabric.capture.event_recorder import _is_ai_api, get_current_recorder, get_current_writer
from novafabric.capture.hooks._layering import claim_higher_layer
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

    def _wrapped_send(
        self, prepared_request: Any, original: Any = None, **kwargs: Any
    ) -> Any:
        url = str(getattr(prepared_request, "url", ""))
        if not self._registry.is_known(url):
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
        except Exception:
            # Connection failures, timeouts, TLS errors — record the attempt
            # so the audit trail is complete, then re-raise. Mirrors the
            # try/finally pattern in _aiohttp.py and _urllib3.py.
            error_status = 599
            raise
        finally:
            duration_ms = int((time.monotonic() - t0) * 1000)
            try:
                raw_body = getattr(prepared_request, "body", None) or b"{}"
                if isinstance(raw_body, bytes):
                    body = json.loads(raw_body)
                elif isinstance(raw_body, str):
                    body = json.loads(raw_body)
                else:
                    body = {}
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
                # Never let a bookkeeping failure surface to the caller.
                pass

            # Record a NetworkEvent in the active capsule (fail-open).
            try:
                _recorder = get_current_recorder()
                if _recorder is not None:
                    from urllib.parse import urlparse
                    _parsed = urlparse(url)
                    _recorder.record_network_event(
                        method=getattr(prepared_request, "method", "UNKNOWN") or "UNKNOWN",
                        url=url,
                        host=_parsed.hostname or "",
                        port=_parsed.port,
                        status_code=status_code if status_code != 0 else None,
                        response_size_bytes=safe_response_size(response),
                        duration_ms=float(duration_ms),
                        library="requests",
                        is_ai_api=_is_ai_api(url),
                    )
            except Exception:
                pass  # fail-open
        return response
