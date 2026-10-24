"""``urllib3`` capture hook (wire-level layer, lowest tier).

Wraps :meth:`urllib3.connectionpool.HTTPConnectionPool.urlopen` and
records every outbound request whose URL matches the active URL
registry. Sits **below** ``requests`` (and ``boto3``, transitively) in
the HTTP-stack — patching ``urllib3`` catches the long tail of clients
that bypass higher-level libraries.

Layered with `_layering.claim_higher_layer()`: when ``RequestsHook``
processes a call that internally goes through ``urllib3``, it claims
the higher layer for the duration of the call, and this hook skips
recording. Without that guard, the same call would be recorded twice.

This is RFC-0001 Option C, sub-track C-3.2.
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
from novafabric.capture.hooks._fail_open import (
    MODEL_CALLS_STREAM,
    fail_open,
    note_capture_failure,
)
from novafabric.capture.hooks._layering import is_inside_higher_layer
from novafabric.capture.hooks._otel_genai import (
    build_record_envelope,
    extract_request_attributes,
    safe_response_size,
)
from novafabric.capture.hooks._url_registry import (
    UrlRegistry,
    load_url_registry,
)
from novafabric.capture.record_roles import stamp_wire_record

if TYPE_CHECKING:
    from novafabric.capture.capsule import CapsuleWriter


_LIBRARY = "urllib3"
NETWORK_EVENTS_STREAM = "network_events.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _build_url(pool_self: Any, url_path: str) -> str:
    """Reconstruct the full URL from a urllib3 pool + path.

    ``HTTPConnectionPool.urlopen`` is called with a path like ``/v1/chat/...``
    and the host/scheme live on the pool object. We need the full URL to
    match the registry.
    """
    scheme = getattr(pool_self, "scheme", "http")
    host = getattr(pool_self, "host", "")
    port = getattr(pool_self, "port", None)
    if port is not None and not (
        (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    ):
        host = f"{host}:{port}"
    if not url_path.startswith("/"):
        url_path = "/" + url_path
    return f"{scheme}://{host}{url_path}"


def _extract_body_from_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    body_raw = kwargs.get("body")
    if isinstance(body_raw, bytes):
        try:
            parsed = json.loads(body_raw)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(body_raw, str):
        try:
            parsed = json.loads(body_raw)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


#: urllib3 ``HTTPConnectionPool.urlopen`` positionals after ``method, url``, in order.
_URLOPEN_POSITIONALS = ("body", "headers")


def _named_arguments(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """*kwargs* plus ``body``/``headers`` given positionally, for the record only."""
    named = dict(kwargs)
    for name, value in zip(_URLOPEN_POSITIONALS, args):
        named.setdefault(name, value)
    return named


class Urllib3Hook:
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
        self._original_urlopen: Any = None

    def install(self) -> None:
        try:
            from urllib3 import connectionpool
            self._original_urlopen = connectionpool.HTTPConnectionPool.urlopen
            hook_self = self

            @functools.wraps(self._original_urlopen)
            def patched_urlopen(
                pool_self: Any, method: str, url: str, *args: Any, **kwargs: Any
            ) -> Any:
                # *args: urllib3 calls itself back POSITIONALLY on a retry or a
                # redirect (urlopen(method, url, body, headers, retries, …)); a
                # signature without them made every retry raise TypeError under
                # capture (review capture-runners-01).
                original_bound = hook_self._original_urlopen.__get__(
                    pool_self, type(pool_self)
                )
                return hook_self._wrapped_urlopen(
                    pool_self, method, url, *args, original=original_bound, **kwargs
                )

            connectionpool.HTTPConnectionPool.urlopen = patched_urlopen  # type: ignore[method-assign, assignment]
        except (ImportError, AttributeError):
            pass

    def uninstall(self) -> None:
        if self._original_urlopen is None:
            return
        try:
            from urllib3 import connectionpool
            connectionpool.HTTPConnectionPool.urlopen = self._original_urlopen  # type: ignore[method-assign]
        except (ImportError, AttributeError):
            pass
        finally:
            self._original_urlopen = None

    def _target_url(self, pool_self: Any, url_path: Any) -> str | None:
        """The URL to record, or ``None`` to pass the call through. Never raises:
        a classification failure loses the record, never the user's request."""
        try:
            full_url = _build_url(pool_self, url_path)
            return full_url if self._registry.is_known(full_url) else None
        except Exception as exc:  # noqa: BLE001 -- capture must never block the workload
            note_capture_failure(_LIBRARY, MODEL_CALLS_STREAM, exc)
            return None

    def _wrapped_urlopen(
        self,
        pool_self: Any,
        method: str,
        url_path: str,
        *args: Any,
        original: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Pass the call through unchanged — same positionals, same keywords — and
        record it when its URL is a known model endpoint."""
        # If a higher-layer hook (e.g. requests) is already recording
        # this call, do not double-record.
        if is_inside_higher_layer():
            if original is not None:
                return original(method, url_path, *args, **kwargs)
            raise RuntimeError("original urlopen function required")

        full_url = self._target_url(pool_self, url_path)
        if full_url is None:
            if original is not None:
                return original(method, url_path, *args, **kwargs)
            raise RuntimeError("original urlopen function required")

        started = _now()
        t0 = time.monotonic()
        response: Any = None
        error_status: int | None = None
        try:
            if original is not None:
                response = original(method, url_path, *args, **kwargs)
        except Exception:
            error_status = 599
            raise
        finally:
            self._record(method, full_url, _named_arguments(args, kwargs), response, started,
                         int((time.monotonic() - t0) * 1000), error_status)
        return response

    def _record(
        self, method: str, full_url: str, kwargs: dict[str, Any], response: Any,
        started: str, duration_ms: int, error_status: int | None,
    ) -> None:
        """Write the model-call record and the network event. Never raises:
        the response or the transport's exception passes through unchanged."""
        status_code = 0
        with fail_open(_LIBRARY, MODEL_CALLS_STREAM):
            body = _extract_body_from_kwargs(kwargs)
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
                body, url=full_url,
                gen_ai_system=self._registry.gen_ai_system(full_url),
            ))
            get_current_writer(self._writer).append_model_call(stamp_wire_record(record))

        # Record a NetworkEvent in the active capsule (fail-open) —
        # ADR-0209 D2.4 parity with the httpx/requests hooks. Reached only
        # when no higher-layer hook owns the call (see the layering guard
        # early-return in _wrapped_urlopen), so a requests-initiated urllib3
        # call is never double-recorded.
        with fail_open(_LIBRARY, NETWORK_EVENTS_STREAM):
            _recorder = get_current_recorder()
            if _recorder is not None:
                _parsed = urlparse(full_url)
                _recorder.record_network_event(
                    method=str(method or "UNKNOWN"),
                    url=full_url,
                    host=_parsed.hostname or "",
                    port=_parsed.port,
                    status_code=status_code if status_code != 0 else None,
                    response_size_bytes=safe_response_size(response),
                    duration_ms=float(duration_ms),
                    library=_LIBRARY,
                    is_ai_api=_is_ai_api(full_url),
                )
