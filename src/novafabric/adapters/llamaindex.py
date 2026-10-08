"""NovaFabric adapter for LlamaIndex.

Wraps a query engine, chat engine, or agent so that each invocation writes a
Run Capsule. Wire-level hooks capture the model calls the framework makes.

LlamaIndex is an **optional** dependency — this module must stay importable
without it; :func:`wrap_engine` raises :class:`ImportError` at call time.

LlamaIndex has no single entry point across its object types: a query engine
exposes ``query``, a chat engine ``chat``, and an agent ``chat`` or ``run``. The
wrapper therefore patches the first method it finds from an explicit,
ordered list rather than guessing a name, and says which one it patched.

Two call shapes finish *after* the patched method returns, and both used to be
captured as an empty, successful capsule written before any model call ran:

* An agent's ``run`` (``FunctionAgent``, ``AgentWorkflow``, …) is a plain method
  that returns a ``WorkflowHandler`` — an :class:`asyncio.Future` — and the work
  happens when the caller awaits it. The capsule is now finished from the
  future's done-callback, so it covers the whole workflow.
* The async twins ``aquery`` / ``achat`` are coroutine functions. When the
  detected entry point has one, it is patched too, so async callers are not
  silently uncaptured.

A context-variable guard keeps a nested call (an ``aquery`` reached from inside
a wrapped agent run, say) recording into the capsule already open instead of
opening a second one that would steal the wire hooks from the first.
"""
from __future__ import annotations

import asyncio
import contextvars
import inspect
from pathlib import Path
from typing import Any

from novafabric.adapters._capsule import begin_capture, require

#: Checked in order. ``query`` first: on an object exposing both, the query
#: path is the one that runs a retrieval + synthesis round-trip.
_ENTRY_POINTS = ("query", "chat", "run")

#: Sync entry point -> its async twin, patched alongside it when present.
_ASYNC_TWINS = {"query": "aquery", "chat": "achat"}

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

    Patches the entry-point method in place and returns the same object, so
    existing references keep working.

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
                if isinstance(result, asyncio.Future):
                    # A WorkflowHandler: the run happens when it is awaited.
                    result.add_done_callback(lambda fut: _finish_from_future(cap, fut))
                    deferred = True
                elif inspect.iscoroutine(result):
                    deferred = True
                    return _finish_after(cap, result)
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
            try:
                return await original(*args, **kwargs)
            except Exception as exc:
                cap.fail(exc)
                raise
            finally:
                _in_flight.reset(token)
                cap.finish()

        setattr(engine, name, wrapped)

    _patch_sync(target)
    twin = _ASYNC_TWINS.get(target)
    if twin is not None and inspect.iscoroutinefunction(getattr(engine, twin, None)):
        _patch_async(twin)
    return engine


async def _finish_after(cap: Any, coro: Any) -> Any:
    """Await a coroutine a sync entry point handed back, then close its capsule."""
    token = _in_flight.set(True)
    try:
        return await coro
    except Exception as exc:
        cap.fail(exc)
        raise
    finally:
        _in_flight.reset(token)
        cap.finish()


def _finish_from_future(cap: Any, fut: asyncio.Future[Any]) -> None:
    """Done-callback: record how the workflow ended, then write the capsule."""
    if fut.cancelled():
        cap.fail(asyncio.CancelledError("the workflow was cancelled"))
    else:
        exc = fut.exception()
        if exc is not None:
            cap.fail(exc)
    cap.finish()
