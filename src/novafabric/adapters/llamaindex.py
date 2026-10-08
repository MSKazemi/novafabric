"""NovaFabric adapter for LlamaIndex.

Wraps a query engine, chat engine, or agent so that each invocation writes a
Run Capsule. Wire-level hooks capture the model calls the framework makes.

LlamaIndex is an **optional** dependency — this module must stay importable
without it; :func:`wrap_engine` raises :class:`ImportError` at call time.

LlamaIndex has no single entry point across its object types: a query engine
exposes ``query``, a chat engine ``chat``, and an agent ``chat`` or ``run``. The
wrapper therefore patches the first method it finds from an explicit,
ordered list rather than guessing a name, and says which one it patched.

Several call shapes finish *after* the patched method returns, and each used to
be captured as an empty, successful capsule written before any model call ran:

* An agent's ``run`` (``FunctionAgent``, ``AgentWorkflow``, …) is a plain method
  that returns a ``WorkflowHandler`` and the work happens in a background task.
  In llama-index-workflows < 2 the handler *is* an :class:`asyncio.Future`; from
  2.x it is a plain awaitable, so a ``Future`` check alone misses every current
  install. The capsule closes when the workflow settles — via the future's
  done-callback, or by watching the handler's public ``stop_event_result()`` —
  so it covers the whole run, including one read through ``stream_events()``.
* The async twins ``aquery`` / ``achat`` are coroutine functions. When the
  detected entry point has one, it is patched too, so async callers are not
  silently uncaptured.
* Streaming. A chat engine's ``stream_chat`` / ``astream_chat`` are patched
  alongside ``chat``; a query engine built with ``streaming=True`` streams from
  ``query`` itself. Either way the returned response is guarded so the capsule
  closes when the stream ends: exhausted -> ``success``, raised -> ``failure``,
  closed or dropped unread -> ``partial``. See :mod:`._streaming`.

A context-variable guard keeps a nested call (an ``aquery`` reached from inside
a wrapped agent run, say) recording into the capsule already open instead of
opening a second one that would steal the wire hooks from the first.
"""
from __future__ import annotations

import asyncio
import contextvars
import inspect
import threading
from pathlib import Path
from typing import Any

from novafabric.adapters._capsule import AdapterCapture, begin_capture, require
from novafabric.adapters._streaming import (
    CANCELLED,
    Closer,
    close_when_collected,
    guard_async_iterator,
    guard_iterator,
    watch_awaitable,
    watch_thread,
)

#: Checked in order. ``query`` first: on an object exposing both, the query
#: path is the one that runs a retrieval + synthesis round-trip.
_ENTRY_POINTS = ("query", "chat", "run")

#: Sync entry point -> its async twin, patched alongside it when present.
_ASYNC_TWINS = {"query": "aquery", "chat": "achat"}

#: Entry point -> the streaming variants patched alongside it when present. A
#: query engine has none: it streams from ``query`` itself when built with
#: ``streaming=True``, and the returned response is what gets guarded.
_STREAMING_TWINS: dict[str, tuple[str, ...]] = {"chat": ("stream_chat", "astream_chat")}

#: Set while a capture is in flight on this task; a nested call records into it.
_in_flight: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "novafabric_llamaindex_in_flight", default=False
)


def wrap_engine(
    engine: Any,
    *,
    run_name: str | None = None,
    data_dir: Path | None = None,
    method: str | None = None,
) -> Any:
    """Wrap a LlamaIndex engine or agent for NovaFabric capture.

    Patches the entry-point method — plus its async twin and, for a chat
    engine, ``stream_chat`` / ``astream_chat`` — in place and returns the same
    object, so existing references keep working.

    Args:
        engine: A LlamaIndex query engine, chat engine, or agent.
        run_name: Name recorded in the manifest. Defaults to the class name.
        data_dir: Base directory for capsules. Defaults to
            ``$NOVAFABRIC_HOME/runs`` or ``.novafabric/runs`` under the CWD.
        method: Force a specific method name instead of autodetecting.

    Raises:
        ImportError: If ``llama-index-core`` is not installed.
        AttributeError: If no known entry point is present.

    Usage::

        from novafabric.adapters.llamaindex import wrap_engine
        engine = wrap_engine(index.as_query_engine())
        response = engine.query("What changed in v2?")
    """
    require("llama_index.core", "llama-index")

    if method is not None:
        target = method
        if not hasattr(engine, target):
            raise AttributeError(f"{type(engine).__name__} has no method {target!r}")
    else:
        found = next((m for m in _ENTRY_POINTS if hasattr(engine, m)), None)
        if found is None:
            raise AttributeError(
                f"{type(engine).__name__} exposes none of {_ENTRY_POINTS}; "
                "pass method= to name the entry point explicitly"
            )
        target = found

    resolved_name = run_name or type(engine).__name__ or "llamaindex-run"

    def _begin(entry_point: str) -> Any:
        cap = begin_capture(
            framework="llamaindex", run_name=resolved_name, data_dir=data_dir
        )
        cap.tags["entry_point"] = entry_point
        return cap

    def _patch_sync(name: str) -> None:
        original = getattr(engine, name)

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if _in_flight.get():
                return original(*args, **kwargs)
            cap = _begin(name)
            token = _in_flight.set(True)
            deferred = False
            try:
                result = original(*args, **kwargs)
                if inspect.iscoroutine(result):
                    deferred = True
                    return _finish_after(cap, result)
                deferred = _defer_until_done(cap, result)
                return result
            except Exception as exc:
                cap.fail(exc)
                raise
            finally:
                _in_flight.reset(token)
                if not deferred:
                    cap.finish()

        setattr(engine, name, wrapped)

    def _patch_async(name: str) -> None:
        original = getattr(engine, name)

        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            if _in_flight.get():
                return await original(*args, **kwargs)
            cap = _begin(name)
            token = _in_flight.set(True)
            deferred = False
            try:
                result = await original(*args, **kwargs)
                deferred = _defer_until_done(cap, result)
                return result
            except asyncio.CancelledError:
                cap.mark_partial(CANCELLED)
                raise
            except Exception as exc:
                cap.fail(exc)
                raise
            finally:
                _in_flight.reset(token)
                if not deferred:
                    cap.finish()

        setattr(engine, name, wrapped)

    _patch_sync(target)
    twin = _ASYNC_TWINS.get(target)
    if twin is not None and inspect.iscoroutinefunction(getattr(engine, twin, None)):
        _patch_async(twin)
    for streaming in _STREAMING_TWINS.get(target, ()):
        candidate = getattr(engine, streaming, None)
        if inspect.iscoroutinefunction(candidate):
            _patch_async(streaming)
        elif callable(candidate):
            _patch_sync(streaming)
    return engine


async def _finish_after(cap: Any, coro: Any) -> Any:
    """Await a coroutine a sync entry point handed back, then close its capsule."""
    token = _in_flight.set(True)
    deferred = False
    try:
        result = await coro
        deferred = _defer_until_done(cap, result)
        return result
    except asyncio.CancelledError:
        cap.mark_partial(CANCELLED)
        raise
    except Exception as exc:
        cap.fail(exc)
        raise
    finally:
        _in_flight.reset(token)
        if not deferred:
            cap.finish()


def _defer_until_done(cap: AdapterCapture, result: Any) -> bool:
    """Hand the capsule to whatever finishes *result*, if it is still running.

    Returns ``True`` when the capsule will be closed later — by a done-callback,
    a watcher, or a guarded stream — and ``False`` when *result* is a finished
    value and the caller should close it now.
    """
    # A WorkflowHandler from llama-index-workflows < 2.x is an asyncio.Future.
    if isinstance(result, asyncio.Future):
        _close_on_done(cap, result)
        return True
    # From 2.x it is a plain Awaitable whose run is a background task. Its
    # public ``stop_event_result()`` settles when the workflow does.
    stop_event_result = getattr(result, "stop_event_result", None)
    if inspect.iscoroutinefunction(stop_event_result) and hasattr(result, "stream_events"):
        # 2.x keeps that task as ``_result_task`` (2.14 through 2.25 at least).
        # A done-callback on it runs before any awaiter of the handler resumes,
        # so the capsule is written even when the caller's coroutine returns
        # right after ``await handler`` and ``asyncio.run`` tears the loop down.
        result_task = getattr(result, "_result_task", None)
        if isinstance(result_task, asyncio.Future):
            _close_on_done(cap, result_task)
        else:
            watch_awaitable(stop_event_result(), Closer(cap))
        return True
    return _defer_stream(cap, result)


def _close_on_done(cap: AdapterCapture, fut: asyncio.Future[Any]) -> None:
    fut.add_done_callback(lambda done: _finish_from_future(cap, done))


def _defer_stream(cap: AdapterCapture, result: Any) -> bool:
    """Keep the capsule open across a streaming response, however it is read."""
    fields = getattr(result, "__dict__", None)
    if not isinstance(fields, dict):
        return False

    # Chat engines (``stream_chat`` / ``astream_chat``): LlamaIndex starts its
    # own consumer — a thread or a task — that drains the model stream into
    # chat history whether or not the caller reads ``response_gen``. The model
    # call ends when that consumer does, so that is when the capsule closes.
    writer_thread = fields.get("write_response_to_history_thread")
    if isinstance(writer_thread, threading.Thread):
        watch_thread(writer_thread, Closer(cap), lambda: getattr(result, "exception", None))
        return True
    writer_task = fields.get("awrite_response_to_history_task")
    if isinstance(writer_task, asyncio.Future):
        _close_on_done(cap, writer_task)
        return True

    # Query engines with ``streaming=True`` return a response whose
    # ``response_gen`` *is* the model stream; a chat response without a
    # background writer reads ``chat_stream`` / ``achat_stream`` directly.
    for attr in ("response_gen", "chat_stream", "achat_stream"):
        stream = fields.get(attr)
        if inspect.isgenerator(stream):
            closer = Closer(cap)
            setattr(result, attr, guard_iterator(stream, closer, _in_flight))
        elif inspect.isasyncgen(stream):
            closer = Closer(cap)
            setattr(result, attr, guard_async_iterator(stream, closer, _in_flight))
        else:
            continue
        close_when_collected(result, closer)
        return True
    return False


def _finish_from_future(cap: Any, fut: asyncio.Future[Any]) -> None:
    """Done-callback: record how the workflow ended, then write the capsule."""
    if fut.cancelled():
        cap.mark_partial(CANCELLED)
    else:
        exc = fut.exception()
        if type(exc).__name__ == "WorkflowCancelledByUser":  # handler.cancel_run()
            cap.mark_partial(CANCELLED)
        elif exc is not None:
            cap.fail(exc)
    cap.finish()
