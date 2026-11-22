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

AWS Bedrock ``Converse`` / ``ConverseStream`` (``boto3`` -> botocore ->
``urllib3``): the response is recorded too (:mod:`._bedrock`, experimental).
botocore asks for ``preload_content=False`` and reads the body itself (at once for
``Converse``, as the workload iterates for ``ConverseStream``), so the hook never
reads it: the response's own ``read`` / ``read1`` / ``read_chunked`` / ``stream`` /
``close`` are shadowed on that one instance by wrappers that return the bytes
unchanged, in the workload's own read, and copy them to a parser. The record is
written once when that read ends (exhausted, closed or garbage-collected), bound to
the capture active when the response arrived (``CaptureBinding``).
"""
from __future__ import annotations

import functools
import json
import time
import weakref
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from novafabric.capture._ulid import new_ulid
from novafabric.capture.event_recorder import _is_ai_api, get_current_recorder, get_current_writer
from novafabric.capture.hooks._bedrock import (
    BEDROCK_CONVERSE_STREAM_SURFACE,
    BEDROCK_REQUEST_EXT,
    INVOKE_NOT_RECORDED,
    BodySink,
    apply_response,
    bedrock_surface,
    http_error_of,
    is_invoke,
    model_path,
    recorded_headers,
    request_body,
    request_extension,
)
from novafabric.capture.hooks._fail_open import (
    MODEL_CALLS_STREAM,
    fail_open,
    note_capture_failure,
)
from novafabric.capture.hooks._layering import is_inside_higher_layer
from novafabric.capture.hooks._ollama import RESPONSE_NOT_RECORDED_EXT
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
    if url_path.startswith(("http://", "https://")):
        # An absolute request target (a request sent through an HTTP proxy).
        return url_path
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
        #: Set by :meth:`uninstall` (see ``CaptureBinding``).
        self._torn_down = False

    def install(self) -> None:
        try:
            from urllib3 import connectionpool
            self._original_urlopen = connectionpool.HTTPConnectionPool.urlopen
            self._torn_down = False
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

    def is_live(self) -> bool:
        """False once :meth:`uninstall` has run (see ``CaptureBinding``)."""
        return not self._torn_down

    def uninstall(self) -> None:
        self._torn_down = True
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
        raised: BaseException | None = None
        try:
            if original is not None:
                response = original(method, url_path, *args, **kwargs)
        except BaseException as exc:
            # BaseException: a cancelled task (CancelledError) or Ctrl-C never got a
            # response either; recording it as a success is review capture-runners-05.
            error_status = 599
            raised = exc
            raise
        finally:
            self._record(method, full_url, _named_arguments(args, kwargs), response, started,
                         int((time.monotonic() - t0) * 1000), error_status, raised, t0)
        return response

    def _record(
        self, method: str, full_url: str, kwargs: dict[str, Any], response: Any,
        started: str, duration_ms: int, error_status: int | None,
        raised: BaseException | None = None, t0: float | None = None,
    ) -> None:
        """Write the model-call record and the network event. Never raises:
        the response or the transport's exception passes through unchanged.

        A Bedrock Converse response whose body is still unread gets its
        model-call record when the workload's read ends
        (:meth:`_defer_bedrock_record`); the network event is written now."""
        status_code = 0
        with fail_open(_LIBRARY, MODEL_CALLS_STREAM):
            body = _extract_body_from_kwargs(kwargs)
            status_code = (
                error_status
                if error_status is not None
                else getattr(response, "status", 200) if response is not None else 0
            )
            system = self._registry.gen_ai_system(full_url)
            surface = bedrock_surface(full_url, system)
            if surface is None:
                self._write_plain(full_url, body, system, status_code, started, duration_ms)
            elif not self._defer_bedrock_record(
                surface, full_url, body, response, status_code, started, t0
            ):
                self._write_bedrock(
                    surface, full_url, body, status_code, started, duration_ms,
                    headers=getattr(response, "headers", None), raised=raised,
                    sink=self._buffered_sink(surface, response, status_code),
                )

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

    def _envelope(self, status_code: int, started: str, duration_ms: int) -> dict[str, Any]:
        return build_record_envelope(
            model_call_id=new_ulid(),
            parent_span_id=self._parent_span_id,
            started_at=started,
            finished_at=_now(),
            duration_ms=duration_ms,
            status="success" if 0 < status_code < 400 else "error",
        )

    def _write_plain(
        self, full_url: str, body: dict[str, Any], system: str, status_code: int,
        started: str, duration_ms: int,
    ) -> None:
        record = self._envelope(status_code, started, duration_ms)
        record.update(extract_request_attributes(body, url=full_url, gen_ai_system=system))
        parsed = model_path(full_url) if system == "aws.bedrock" else None
        if parsed is not None and record.get("gen_ai.request.model") in (None, "unknown"):
            record["gen_ai.request.model"] = record["gen_ai.response.model"] = parsed[0]
        if is_invoke(full_url, system):
            record.setdefault("extensions", {})[RESPONSE_NOT_RECORDED_EXT] = INVOKE_NOT_RECORDED
        get_current_writer(self._writer).append_model_call(stamp_wire_record(record))

    def _write_bedrock(
        self, surface: str, full_url: str, body: dict[str, Any], status_code: int,
        started: str, duration_ms: int, *, headers: Any = None,
        raised: BaseException | None = None, sink: BodySink | None = None,
        exhausted: bool = True, first_ms: int | None = None,
        read_error: BaseException | None = None,
    ) -> None:
        """A Bedrock Converse / ConverseStream record (request and response).

        ``read_error``: reading the body raised part-way; the record keeps what
        was delivered and fails (``status: error``), so replay refuses it."""
        record = self._envelope(status_code, started, duration_ms)
        record.update(extract_request_attributes(
            request_body(body, full_url), url=full_url, gen_ai_system="aws.bedrock",
        ))
        extras = request_extension(body)
        if extras is not None:
            record.setdefault("extensions", {})[BEDROCK_REQUEST_EXT] = extras
        kept_headers = recorded_headers(headers)
        if raised is not None or response_status_missing(status_code):
            # No response: the error block makes the record hold its queue position,
            # so mocked replay refuses it in order instead of serving the next call's
            # answer to it.
            apply_response(record, surface, None,
                           not_recorded="the request raised before a response")
            record["status"] = "error"
            record["error"] = {
                "type": type(raised).__name__ if raised is not None else "NoResponse",
                "message": str(raised) if raised is not None else "no response",
                "traceback_ref": None,
            }
        elif status_code >= 300:
            error_body = (
                sink.body() if sink is not None and sink.not_recorded is None and exhausted
                else None
            )
            apply_response(record, surface, None, headers=kept_headers,
                           http_error=http_error_of(status_code, headers, error_body))
        elif sink is None:
            apply_response(record, surface, None, headers=kept_headers,
                           not_recorded="the response body was not read")
        else:
            json_body = surface != BEDROCK_CONVERSE_STREAM_SURFACE
            folded = sink.folded() if exhausted or not json_body else None
            why = sink.not_recorded or (
                None if folded is not None else "the response body was not read to its end"
            )
            apply_response(
                record, surface, folded, headers=kept_headers, not_recorded=why,
                complete=exhausted and folded is not None and folded.stop_reason is not None,
                first_ms=first_ms,
            )
            if read_error is not None:
                record["status"] = "error"
                record["error"] = {"type": type(read_error).__name__,
                                   "message": str(read_error), "traceback_ref": None}
        get_current_writer(self._writer).append_model_call(stamp_wire_record(record))

    @staticmethod
    def _buffered_sink(surface: str, response: Any, status_code: int) -> BodySink | None:
        """A sink holding a body urllib3 already read (``preload_content=True``)."""
        if response is None:
            return None
        body = _buffered_body(response)
        if body is None:
            return None
        sink = BodySink(surface, status_code, _content_encoding(response))
        sink.feed(body)
        return sink

    def _defer_bedrock_record(
        self, surface: str, full_url: str, body: dict[str, Any], response: Any,
        status_code: int, started: str, t0: float | None,
    ) -> bool:
        """Copy an unread Bedrock body as the workload reads it; record at the end.

        True when the record is now deferred; False to record now (no response,
        a body already read, or a response whose methods cannot be shadowed)."""
        if response is None or status_code == 0 or _buffered_body(response) is not None:
            return False
        start_t = t0 if t0 is not None else time.monotonic()
        headers = getattr(response, "headers", None)
        sink = BodySink(surface, status_code, _content_encoding(response))
        first: list[int] = []

        def feed(data: Any) -> None:
            if data and not first:
                first.append(int((time.monotonic() - start_t) * 1000))
            sink.feed(data)

        def done(
            _sink: Any, _count: int, _first_ms: int | None, complete: bool,
            error: BaseException | None = None,
        ) -> None:
            with fail_open(_LIBRARY, MODEL_CALLS_STREAM):
                self._write_bedrock(
                    surface, full_url, body, status_code, started,
                    int((time.monotonic() - start_t) * 1000), headers=headers, sink=sink,
                    exhausted=complete, first_ms=first[0] if first else None,
                    read_error=error,
                )

        state = _StreamState(_SinkBox(sink), done, CaptureBinding.capture(self.is_live))
        return _shadow_body_reads(response, feed, state.finish)


def response_status_missing(status_code: int) -> bool:
    return status_code == 0 or status_code == 599


class _SinkBox:
    """``_StreamState`` accumulator adapter: the sink is folded by the writer."""

    def __init__(self, sink: BodySink) -> None:
        self._sink = sink

    def add(self, item: Any) -> None:  # pragma: no cover -- items are fed directly
        pass

    def response(self) -> BodySink:
        return self._sink


def _buffered_body(response: Any) -> bytes | None:
    """The body urllib3 already read (``preload_content=True``), without I/O."""
    try:
        value = vars(response).get("_body")
    except TypeError:
        return None
    return bytes(value) if isinstance(value, (bytes, bytearray)) else None


def _content_encoding(response: Any) -> str:
    try:
        headers = getattr(response, "headers", None) or {}
        return str(headers.get("content-encoding", "") or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


#: The ``HTTPResponse`` methods through which a body can be read, shadowed on the
#: one response instance a Bedrock call returned.
_SHADOWED = ("read", "read1", "read_chunked", "stream", "close")


def _shadow_body_reads(
    response: Any,
    feed: Callable[[Any], None],
    finish: Callable[..., None],
) -> bool:
    """Shadow *response*'s body-reading methods with copying wrappers.

    Each wrapper calls the class's own method (so behaviour, return values and
    exceptions are unchanged), copies the bytes it returns to *feed*, and calls
    ``finish(complete=…, error=…)`` once the body is exhausted, the read raised, or
    the response is closed or collected. Wrappers hold the response weakly (no
    reference cycle); nothing is read ahead. False when the instance cannot take
    attributes (nothing is changed then)."""
    cls = type(response)
    try:
        vars(response)
    except TypeError:
        return False
    if any(name in vars(response) for name in _SHADOWED):
        return False  # already shadowed (or patched by someone else): leave it
    ref = weakref.ref(response)

    def _end(exc: BaseException | None = None, *, exhausted: bool = False) -> None:
        try:
            finish(complete=exhausted, error=exc)
        except Exception:  # noqa: BLE001 -- capture must never fail the workload
            pass

    def _copy(data: Any) -> None:
        try:
            feed(data)
        except Exception:  # noqa: BLE001
            pass

    def _amount(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        return args[0] if args else kwargs.get("amt")

    def read(*args: Any, **kwargs: Any) -> Any:
        try:
            data = cls.read(ref(), *args, **kwargs)
        except Exception as exc:
            _end(exc)
            raise
        _copy(data)
        amt = _amount(args, kwargs)
        if amt is None or (amt != 0 and not data):
            _end(exhausted=True)
        return data

    def read1(*args: Any, **kwargs: Any) -> Any:
        try:
            data = cls.read1(ref(), *args, **kwargs)
        except Exception as exc:
            _end(exc)
            raise
        _copy(data)
        if not data and _amount(args, kwargs) != 0:
            _end(exhausted=True)
        return data

    def read_chunked(*args: Any, **kwargs: Any) -> Iterator[Any]:
        try:
            for chunk in cls.read_chunked(ref(), *args, **kwargs):
                _copy(chunk)
                yield chunk
        except Exception as exc:
            _end(exc)
            raise
        _end(exhausted=True)

    def stream(*args: Any, **kwargs: Any) -> Iterator[Any]:
        yield from cls.stream(ref(), *args, **kwargs)
        _end(exhausted=True)

    def close() -> Any:
        _end()
        return cls.close(ref())

    try:
        for name, wrapper in (("read", read), ("read1", read1), ("read_chunked", read_chunked),
                              ("stream", stream), ("close", close)):
            if hasattr(cls, name):
                setattr(response, name, wrapper)
        weakref.finalize(response, _end)
    except (AttributeError, TypeError):
        for name in _SHADOWED:
            vars(response).pop(name, None)
        return False
    return True
