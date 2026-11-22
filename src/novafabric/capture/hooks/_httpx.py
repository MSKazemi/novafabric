"""httpx / httpx2 capture hooks (wire-level layer).

Wraps ``Client.send`` (sync) and ``AsyncClient.send`` (async) and records
every request whose URL is matched by the active URL registry (see
:mod:`._url_registry`): one ``model-calls.jsonl`` record per HTTP attempt and
one ``network_events.jsonl`` event.

Two client libraries share this code:

* :class:`HttpxHook` patches ``httpx`` -- the client under the ``anthropic``
  SDK, ``openai`` < 3, and most other Python LLM clients;
* :class:`Httpx2Hook` patches ``httpx2`` -- the client ``openai`` 3.x builds
  by default (``openai.OpenAI()`` -> ``SyncHttpxClientWrapper``, whose MRO
  resolves to ``httpx2.Client``, not ``httpx.Client``). Without it an OpenAI
  call left no transport record and no network event at all.

``httpx2`` is never a dependency: :class:`Httpx2Hook` is installed only when
it is importable, and is a no-op otherwise.

Under an SDK call (``record_roles.sdk_call_scope``) the record is stamped
``transport`` of that logical call (ADR-0305), so an SDK call is still counted
once whichever client library carried it.

Wire-level capture is the primary growth axis per RFC-0001 Option C.
The async path is required for LLM SDKs that use ``AsyncClient``
(e.g. ``langchain_ollama`` when invoked via ``ainvoke`` / ``asyncio.run``).

Recording is fail-open (:mod:`._fail_open`): the response or the transport's
exception passes through unchanged even when building or writing the record
fails.

Ollama ``/api/chat`` and ``/api/generate`` (ADR-0320, experimental): the
response is recorded too (:mod:`._ollama`). A buffered body (``send(stream=False)``,
e.g. ``client.post``) is parsed at ``send``. A streamed body (``client.stream``,
``send(stream=True)``; what the ``ollama`` SDK does for ``stream=True`` and
``langchain_ollama`` always) is never read by the hook: ``response.stream`` is
wrapped so the chunks are copied as the workload reads them, and the record is
written once, when that read ends -- exhausted, closed, or garbage-collected; a
stream closed before Ollama's ``done`` object is recorded as delivered and
flagged ``io.novafabric.stream_complete: false``.
"""
from __future__ import annotations

import functools
import importlib
import json
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

from novafabric.capture._ulid import new_ulid
from novafabric.capture.event_recorder import _is_ai_api, get_current_recorder, get_current_writer
from novafabric.capture.hooks._fail_open import (
    MODEL_CALLS_STREAM,
    fail_open,
    note_capture_failure,
)
from novafabric.capture.hooks._ollama import (
    NdjsonSplitter,
    OllamaAccumulator,
    apply_response,
    ollama_surface,
    operation_name,
    request_body,
    request_streams,
)
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

NETWORK_EVENTS_STREAM = "network_events.jsonl"

#: True while an httpx-family send is being recorded in this context. A nested
#: send -- ``httpx2.Client`` subclassing or delegating to a patched
#: ``httpx.Client``, or both hooks patching one inherited ``send`` -- passes
#: through unrecorded, so one HTTP attempt is never recorded twice.
_recording_send: ContextVar[bool] = ContextVar("novafabric_httpx_recording_send", default=False)


@contextmanager
def _owning_send() -> Iterator[None]:
    token = _recording_send.set(True)
    try:
        yield
    finally:
        _recording_send.reset(token)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class HttpxHook:
    """Records registry-matched requests sent through ``httpx`` clients."""

    #: The client library this hook patches (``Client``/``AsyncClient`` in it).
    module_name: ClassVar[str] = "httpx"
    #: ``NetworkEvent.library`` and the hook name in fail-open warnings.
    library: ClassVar[str] = "httpx"

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
        #: (class, attribute, original, whether the class defined it itself).
        self._patched: list[tuple[Any, str, Any, bool]] = []
        #: The patched client module (its byte-stream base classes are needed
        #: to wrap a streamed Ollama body) and the wrapper classes built for it.
        self._module: Any = None
        self._tee_types: tuple[type, type] | None = None
        #: Set by :meth:`uninstall` (see ``CaptureBinding``).
        self._torn_down = False

    def install(self) -> None:
        try:
            module = importlib.import_module(self.module_name)
            sync_cls = module.Client
            async_cls = module.AsyncClient
            original_sync = sync_cls.send
            original_async = async_cls.send
        except (ImportError, AttributeError):
            return
        hook_self = self
        self._module = module
        self._torn_down = False
        self._original_sync = original_sync
        self._original_async = original_async

        # ── sync path ────────────────────────────────────────────────────
        @functools.wraps(original_sync)
        def patched_send(client_self: Any, request: Any, **kwargs: Any) -> Any:
            original_bound = original_sync.__get__(client_self, type(client_self))
            return hook_self._wrapped_send(request, original=original_bound, **kwargs)

        # ── async path ───────────────────────────────────────────────────
        @functools.wraps(original_async)
        async def patched_async_send(client_self: Any, request: Any, **kwargs: Any) -> Any:
            original_bound = original_async.__get__(client_self, type(client_self))
            return await hook_self._wrapped_send_async(
                request, original=original_bound, **kwargs
            )

        for cls, original, patched in (
            (sync_cls, original_sync, patched_send),
            (async_cls, original_async, patched_async_send),
        ):
            self._patched.append((cls, "send", original, "send" in vars(cls)))
            cls.send = patched

    def is_live(self) -> bool:
        """False once :meth:`uninstall` has run (see ``CaptureBinding``)."""
        return not self._torn_down

    def uninstall(self) -> None:
        self._torn_down = True
        while self._patched:
            cls, attr, original, owned = self._patched.pop()
            try:
                if owned:
                    setattr(cls, attr, original)
                else:
                    # The class inherited ``send``: drop our override rather than
                    # pin a copy of whatever the base class held at install time.
                    delattr(cls, attr)
            except (AttributeError, TypeError):
                pass
        self._original_sync = None
        self._original_async = None

    @staticmethod
    def _status_code(response: Any, error_status: int | None) -> int:
        if error_status is not None:
            return error_status
        if response is None:
            return 0
        status = getattr(response, "status_code", 200)
        return status if isinstance(status, int) else 200

    def _record(
        self,
        request: Any,
        response: Any,
        started: str,
        duration_ms: int,
        error_status: int | None,
        error: BaseException | None = None,
        t0: float | None = None,
    ) -> None:
        """Write the model-call record and the network event. Never raises.

        An Ollama ``/api/chat`` / ``/api/generate`` response whose body is still
        unread gets its model-call record when the workload's read ends
        (:meth:`_defer_ollama_record`); the network event is written now.
        """
        url = ""
        status_code = 0
        with fail_open(self.library, MODEL_CALLS_STREAM):
            url = str(request.url)
            status_code = self._status_code(response, error_status)
            if self._generic_wire(url):
                if not self._defer_wire_record(
                    request, response, url, status_code, started, t0
                ):
                    self._write_wire_call(
                        request, url, status_code, started, duration_ms,
                        response=response, error=error,
                    )
            elif not self._defer_ollama_record(
                request, response, url, status_code, started, t0
            ):
                self._write_model_call(
                    request, url, status_code, started, duration_ms,
                    response=response, error=error,
                )
        if not url:
            return
        with fail_open(self.library, NETWORK_EVENTS_STREAM):
            self._write_network_event(request, response, url, status_code, duration_ms)

    @staticmethod
    def _request_body(request: Any) -> dict[str, Any]:
        try:
            body: Any = json.loads(getattr(request, "content", b"{}") or b"{}")
        except Exception:  # noqa: BLE001 -- a non-JSON body records no attributes
            body = {}
        return body if isinstance(body, dict) else {}

    def _ollama_surface(self, url: str) -> str | None:
        return ollama_surface(url, self._registry.gen_ai_system(url))

    def _write_model_call(
        self, request: Any, url: str, status_code: int, started: str, duration_ms: int,
        *, response: Any = None, error: BaseException | None = None,
        streamed_result: tuple[OllamaAccumulator, int, int | None, bool] | None = None,
        stream_error: BaseException | None = None,
        not_recorded: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> None:
        if body is None:
            body = self._request_body(request)
        surface = self._ollama_surface(url)
        record = build_record_envelope(
            model_call_id=new_ulid(),
            parent_span_id=self._parent_span_id,
            started_at=started,
            finished_at=_now(),
            duration_ms=duration_ms,
            status="success" if 0 < status_code < 400 else "error",
        )
        if surface is None:
            record.update(extract_request_attributes(
                body, url=url, gen_ai_system=self._registry.gen_ai_system(url),
            ))
        else:
            record.update(extract_request_attributes(
                request_body(body, surface), url=url, gen_ai_system="ollama",
                operation_name=operation_name(surface),
            ))
            self._apply_ollama(
                record, surface, body, status_code, response, error,
                streamed_result, stream_error, not_recorded,
            )
        get_current_writer(self._writer).append_model_call(stamp_wire_record(record))

    @staticmethod
    def _apply_ollama(
        record: dict[str, Any], surface: str, body: dict[str, Any],
        status_code: int, response: Any, error: BaseException | None,
        streamed_result: tuple[OllamaAccumulator, int, int | None, bool] | None,
        stream_error: BaseException | None, not_recorded: str | None,
    ) -> None:
        """The response side of an Ollama record (ADR-0320)."""
        streamed = request_streams(body)
        if streamed_result is not None:
            acc, count, first_ms, complete = streamed_result
            apply_response(record, acc, streamed=streamed, chunk_count=count,
                           first_ms=first_ms, error=stream_error, not_recorded=not_recorded,
                           complete=complete)
            return
        acc = OllamaAccumulator(surface)
        if error is not None or not 0 < status_code < 400:
            # No response to record. The error block makes the record hold its
            # queue position, so mocked replay refuses it in order instead of
            # serving the next call's answer to it.
            why = ("the request raised before a response" if error is not None
                   else f"HTTP status {status_code}")
            apply_response(record, acc, streamed=False, chunk_count=0, first_ms=None,
                           not_recorded=why)
            record["error"] = {
                "type": type(error).__name__ if error is not None else "HTTPStatusError",
                "message": str(error) if error is not None else f"HTTP {status_code}",
                "traceback_ref": None,
            }
            return
        content = _buffered_content(response)
        if content is None:
            apply_response(record, acc, streamed=False, chunk_count=0, first_ms=None,
                           not_recorded="the response body was not read")
            return
        splitter = NdjsonSplitter()
        for obj in [*splitter.feed(content), *splitter.flush()]:
            acc.add(obj)
        if splitter.failed is not None:
            apply_response(record, acc, streamed=False, chunk_count=0, first_ms=None,
                           not_recorded=splitter.failed)
            return
        apply_response(record, acc, streamed=streamed, chunk_count=acc.objects,
                       first_ms=None)

    def _defer_ollama_record(
        self, request: Any, response: Any, url: str, status_code: int,
        started: str, t0: float | None,
    ) -> bool:
        """Wrap an unread Ollama response body so it is recorded when read.

        True when the model-call record is now deferred to the end of the
        workload's read; False to record now (not Ollama, an error status, a
        body already buffered, or no wrappable stream).
        """
        if response is None or not 0 < status_code < 400:
            return False
        surface = self._ollama_surface(url)
        if surface is None or _buffered_content(response) is not None:
            return False
        tee_types = self._stream_wrappers()
        if tee_types is None:
            return False
        sync_tee, async_tee = tee_types
        inner = getattr(response, "stream", None)
        if isinstance(inner, (sync_tee, async_tee)):
            return False
        if isinstance(inner, self._module.SyncByteStream):
            tee_cls = sync_tee
        elif isinstance(inner, self._module.AsyncByteStream):
            tee_cls = async_tee
        else:
            return False
        body = self._request_body(request)
        headers = getattr(response, "headers", None) or {}
        encoding = str(headers.get("content-encoding", "") or "").strip().lower()
        start_t = t0 if t0 is not None else time.monotonic()
        acc = OllamaAccumulator(surface)
        sink_box: list[_OllamaSink] = []

        def done(
            folded: Any, count: int, first_ms: int | None, complete: bool,
            error: BaseException | None = None,
        ) -> None:
            with fail_open(self.library, MODEL_CALLS_STREAM):
                self._write_model_call(
                    request, url, status_code, started,
                    int((time.monotonic() - start_t) * 1000),
                    streamed_result=(acc, count, first_ms, complete), stream_error=error,
                    not_recorded=sink_box[0].not_recorded, body=body,
                )

        state = _StreamState(acc, done, CaptureBinding.capture(self.is_live))
        sink = _OllamaSink(state, acc, encoding)
        sink_box.append(sink)
        response.stream = tee_cls(inner, sink)
        return True

    # ── generic wire calls (ADR-0324) ────────────────────────────────────

    def _generic_wire(self, url: str) -> bool:
        """A logical call to a registry model host that no dedicated server claims."""
        return records_body(
            self._registry.gen_ai_system(url),
            in_sdk_call=current_sdk_call_id() is not None,
            dedicated=self._ollama_surface(url) is not None,
        )

    def _write_wire_call(
        self, request: Any, url: str, status_code: int, started: str, duration_ms: int,
        *, response: Any = None, error: BaseException | None = None,
        sink: WireBodySink | None = None, streamed: bool = False, complete: bool = True,
        not_recorded: str | None = None,
    ) -> None:
        record = build_record_envelope(
            model_call_id=new_ulid(),
            parent_span_id=self._parent_span_id,
            started_at=started,
            finished_at=_now(),
            duration_ms=duration_ms,
            status="success" if 0 < status_code < 400 else "error",
        )
        record.update(extract_request_attributes(
            self._request_body(request), url=url,
            gen_ai_system=self._registry.gen_ai_system(url),
        ))
        mark_generic(record, str(getattr(request, "method", "") or ""), url,
                     getattr(request, "content", None))
        if sink is None and not_recorded is None:
            if error is not None or response is None:
                not_recorded = "the request raised before a response"
            else:
                content = _buffered_content(response)
                if content is None:
                    not_recorded = "the response body was not read"
                else:
                    sink = WireBodySink()
                    sink.add(content)
        apply_wire_response(
            record, status=status_code,
            reason=str(getattr(response, "reason_phrase", "") or ""),
            headers=getattr(response, "headers", None), sink=sink, streamed=streamed,
            complete=complete, not_recorded=not_recorded,
        )
        get_current_writer(self._writer).append_model_call(stamp_wire_record(record))

    def _defer_wire_record(
        self, request: Any, response: Any, url: str, status_code: int,
        started: str, t0: float | None,
    ) -> bool:
        """Copy an unread generic-wire body as the workload reads it; record at the end.

        True when the record is now deferred; False to record now (no response, a body
        already read, recording off, a content encoding capture does not decode, or no
        wrappable stream)."""
        if response is None or status_code == 0 or recording_off_reason() is not None:
            return False
        if _buffered_content(response) is not None:
            return False
        tee_types = self._stream_wrappers()
        if tee_types is None:
            return False
        sync_tee, async_tee = tee_types
        inner = getattr(response, "stream", None)
        if isinstance(inner, (sync_tee, async_tee)):
            return False
        if isinstance(inner, self._module.SyncByteStream):
            tee_cls = sync_tee
        elif isinstance(inner, self._module.AsyncByteStream):
            tee_cls = async_tee
        else:
            return False
        body = WireBodySink(encoding=content_encoding(getattr(response, "headers", None)))
        if body.not_recorded is not None:
            return False
        start_t = t0 if t0 is not None else time.monotonic()

        def done(
            sink: WireBodySink, _count: int, _first_ms: int | None, complete: bool,
            error: BaseException | None = None,
        ) -> None:
            with fail_open(self.library, MODEL_CALLS_STREAM):
                self._write_wire_call(
                    request, url, status_code, started,
                    int((time.monotonic() - start_t) * 1000), response=response,
                    sink=sink, streamed=True, complete=complete,
                    not_recorded=(
                        f"the response stream failed part-way ({type(error).__name__})"
                        if error is not None else None
                    ),
                )

        state = _StreamState(body, done, CaptureBinding.capture(self.is_live))
        response.stream = tee_cls(inner, _WireTeeSink(state))
        return True

    def _stream_wrappers(self) -> tuple[type, type] | None:
        if self._tee_types is None and self._module is not None:
            self._tee_types = _make_stream_wrappers(self._module)
        return self._tee_types

    def _write_network_event(
        self, request: Any, response: Any, url: str, status_code: int, duration_ms: int,
    ) -> None:
        recorder = get_current_recorder()
        if recorder is None:
            return
        parsed = urlparse(url)
        recorder.record_network_event(
            method=getattr(request, "method", "UNKNOWN") or "UNKNOWN",
            url=url,
            host=parsed.hostname or "",
            port=parsed.port,
            status_code=status_code if status_code != 0 else None,
            response_size_bytes=safe_response_size(response),
            duration_ms=float(duration_ms),
            library=self.library,
            is_ai_api=_is_ai_api(url),
        )

    def _should_record(self, request: Any) -> bool:
        """False for a nested send or an unregistered URL. Never raises."""
        if _recording_send.get():
            return False
        try:
            return self._registry.is_known(str(request.url))
        except Exception as exc:  # noqa: BLE001 -- capture must never block the workload
            note_capture_failure(self.library, MODEL_CALLS_STREAM, exc)
            return False

    def _wrapped_send(self, request: Any, original: Any = None, **kwargs: Any) -> Any:
        if original is None:
            raise RuntimeError("original send function required")
        if not self._should_record(request):
            return original(request, **kwargs)

        started = _now()
        t0 = time.monotonic()
        response: Any = None
        error_status: int | None = None
        raised: BaseException | None = None
        try:
            with _owning_send():
                response = original(request, **kwargs)
        except BaseException as exc:
            # BaseException: a cancelled task (CancelledError) or Ctrl-C never got a
            # response either; recording it as a success is review capture-runners-05.
            error_status = 599
            raised = exc
            raise
        finally:
            self._record(request, response, started,
                         int((time.monotonic() - t0) * 1000), error_status, raised, t0)
        return response

    async def _wrapped_send_async(
        self, request: Any, original: Any = None, **kwargs: Any
    ) -> Any:
        if original is None:
            raise RuntimeError("original send function required")
        if not self._should_record(request):
            return await original(request, **kwargs)

        started = _now()
        t0 = time.monotonic()
        response: Any = None
        error_status: int | None = None
        raised: BaseException | None = None
        try:
            with _owning_send():
                response = await original(request, **kwargs)
        except BaseException as exc:
            # BaseException: a cancelled task (CancelledError) or Ctrl-C never got a
            # response either; recording it as a success is review capture-runners-05.
            error_status = 599
            raised = exc
            raise
        finally:
            self._record(request, response, started,
                         int((time.monotonic() - t0) * 1000), error_status, raised, t0)
        return response


def _buffered_content(response: Any) -> bytes | None:
    """The body an httpx response has already read, without doing any I/O.

    Read from the instance ``__dict__`` only (``_content``), so a property that
    would read the stream never runs; ``None`` when the body is still unread.
    """
    try:
        value = vars(response).get("_content")
    except TypeError:
        return None
    return bytes(value) if isinstance(value, (bytes, bytearray)) else None


class _OllamaSink:
    """Where a wrapped Ollama body's bytes go: split into objects, folded, and
    recorded once when the read ends (``_StreamState``). Never raises."""

    def __init__(self, state: _StreamState, acc: OllamaAccumulator, encoding: str) -> None:
        self._state = state
        self._acc = acc
        self._splitter = NdjsonSplitter()
        self.not_recorded: str | None = None
        if encoding not in ("", "identity"):
            self.not_recorded = f"content-encoding {encoding!r} is not decoded by capture"
        self._finished = False

    def feed(self, data: Any) -> None:
        if self.not_recorded is not None:
            return
        try:
            for obj in self._splitter.feed(bytes(data)):
                self._state.add(obj)
            if self._splitter.failed is not None:
                self.not_recorded = self._splitter.failed
        except Exception:  # noqa: BLE001 -- capture must never fail the workload
            self.not_recorded = "the response body could not be parsed"

    def finish(self, error: BaseException | None = None, *, exhausted: bool = False) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            if self.not_recorded is None:
                for obj in self._splitter.flush():
                    self._state.add(obj)
                if self._splitter.failed is not None:
                    self.not_recorded = self._splitter.failed
            # Complete: the workload read the body to its end and it held the
            # done object. Closed early (or failed): not complete, even when the
            # transport had already delivered the done object.
            self._state.finish(complete=exhausted and self._acc.done, error=error)
        except Exception:  # noqa: BLE001 -- capture must never fail the workload
            pass


class _WireTeeSink:
    """The tee-side face of a generic-wire body (ADR-0324): bytes go to the
    ``WireBodySink`` through ``_StreamState``, which records once when the read ends."""

    def __init__(self, state: _StreamState) -> None:
        self._state = state

    def feed(self, data: Any) -> None:
        self._state.add(data)

    def finish(self, error: BaseException | None = None, *, exhausted: bool = False) -> None:
        self._state.finish(complete=exhausted, error=error)


def _make_stream_wrappers(module: Any) -> tuple[type, type] | None:
    """Byte-stream wrappers deriving from *module*'s own stream base classes
    (``Response.iter_raw`` / ``aiter_raw`` check ``isinstance``). They yield the
    inner stream's bytes unchanged, in the workload's own read, and copy them
    to an :class:`_OllamaSink`; nothing is read ahead or buffered for the
    workload."""
    base_sync = getattr(module, "SyncByteStream", None)
    base_async = getattr(module, "AsyncByteStream", None)
    if not (isinstance(base_sync, type) and isinstance(base_async, type)):
        return None

    class _SyncTee(base_sync):  # type: ignore[misc, valid-type]
        def __init__(self, inner: Any, sink: _OllamaSink | _WireTeeSink) -> None:
            self._nf_inner = inner
            self._nf_sink = sink

        def __iter__(self) -> Iterator[bytes]:
            try:
                for chunk in self._nf_inner:
                    self._nf_sink.feed(chunk)
                    yield chunk
            except Exception as exc:  # the stream failed part-way: record why
                self._nf_sink.finish(error=exc)
                raise
            self._nf_sink.finish(exhausted=True)

        def close(self) -> None:
            try:
                self._nf_sink.finish()
            finally:
                self._nf_inner.close()

        def __del__(self) -> None:
            try:
                self._nf_sink.finish()
            except Exception:  # noqa: BLE001
                pass

    class _AsyncTee(base_async):  # type: ignore[misc, valid-type]
        def __init__(self, inner: Any, sink: _OllamaSink | _WireTeeSink) -> None:
            self._nf_inner = inner
            self._nf_sink = sink

        async def __aiter__(self) -> AsyncIterator[bytes]:
            try:
                async for chunk in self._nf_inner:
                    self._nf_sink.feed(chunk)
                    yield chunk
            except Exception as exc:  # the stream failed part-way: record why
                self._nf_sink.finish(error=exc)
                raise
            self._nf_sink.finish(exhausted=True)

        async def aclose(self) -> None:
            try:
                self._nf_sink.finish()
            finally:
                await self._nf_inner.aclose()

        def __del__(self) -> None:
            try:
                self._nf_sink.finish()
            except Exception:  # noqa: BLE001
                pass

    return _SyncTee, _AsyncTee


class Httpx2Hook(HttpxHook):
    """Records requests sent through ``httpx2`` clients (``openai`` 3.x default).

    Same records, same ADR-0305 roles as :class:`HttpxHook`; network events
    carry ``library="httpx2"``. A no-op when ``httpx2`` is not importable.
    """

    module_name: ClassVar[str] = "httpx2"
    library: ClassVar[str] = "httpx2"
