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
"""
from __future__ import annotations

import functools
import importlib
import json
import time
from collections.abc import Iterator
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

    def uninstall(self) -> None:
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
    ) -> None:
        """Write the model-call record and the network event. Never raises."""
        url = ""
        status_code = 0
        with fail_open(self.library, MODEL_CALLS_STREAM):
            url = str(request.url)
            status_code = self._status_code(response, error_status)
            self._write_model_call(request, url, status_code, started, duration_ms)
        if not url:
            return
        with fail_open(self.library, NETWORK_EVENTS_STREAM):
            self._write_network_event(request, response, url, status_code, duration_ms)

    def _write_model_call(
        self, request: Any, url: str, status_code: int, started: str, duration_ms: int,
    ) -> None:
        try:
            body: Any = json.loads(getattr(request, "content", b"{}") or b"{}")
        except Exception:  # noqa: BLE001 -- a non-JSON body records no attributes
            body = {}
        if not isinstance(body, dict):
            body = {}
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
        get_current_writer(self._writer).append_model_call(stamp_wire_record(record))

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
        try:
            with _owning_send():
                response = original(request, **kwargs)
        except BaseException:
            # BaseException: a cancelled task (CancelledError) or Ctrl-C never got a
            # response either; recording it as a success is review capture-runners-05.
            error_status = 599
            raise
        finally:
            self._record(request, response, started,
                         int((time.monotonic() - t0) * 1000), error_status)
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
        try:
            with _owning_send():
                response = await original(request, **kwargs)
        except BaseException:
            # BaseException: a cancelled task (CancelledError) or Ctrl-C never got a
            # response either; recording it as a success is review capture-runners-05.
            error_status = 599
            raise
        finally:
            self._record(request, response, started,
                         int((time.monotonic() - t0) * 1000), error_status)
        return response


class Httpx2Hook(HttpxHook):
    """Records requests sent through ``httpx2`` clients (``openai`` 3.x default).

    Same records, same ADR-0305 roles as :class:`HttpxHook`; network events
    carry ``library="httpx2"``. A no-op when ``httpx2`` is not importable.
    """

    module_name: ClassVar[str] = "httpx2"
    library: ClassVar[str] = "httpx2"
