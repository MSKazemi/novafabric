"""NovaFabric adapter for Pydantic AI.

Wraps a ``pydantic_ai.Agent`` so that each run writes a Run Capsule. Wire-level
hooks capture the model calls the agent makes.

Pydantic AI is an **optional** dependency — this module must stay importable
without it; :func:`wrap_agent` raises :class:`ImportError` at call time.

Pydantic AI's primary API is **async** (``Agent.run``), with ``run_sync`` as the
blocking convenience wrapper. Both are patched: wrapping only ``run_sync`` would
silently capture nothing for the async callers the framework steers people
towards, and wrapping only ``run`` would double-count, because ``run_sync``
drives ``run`` internally.

That last point is the reason for the re-entrancy guard below. Without it a
single ``run_sync`` call produces **two** capsules — an outer one from the sync
wrapper and an inner one from the async method it delegates to — and the inner
capsule steals the wire hooks from the outer.

The streaming entry points ``run_stream`` and ``iter`` are ``async with``
context managers: the model calls happen inside the caller's ``async with``
body, after the method has returned. Both are wrapped so the capsule opens on
entry and closes on exit, and records how the run ended — ``success`` when it
finished, ``failure`` when the body or the run raised, ``partial`` when the
caller left the block before the run finished (a ``run_stream`` whose output was
never fully read, an ``iter`` abandoned before its ``End`` node) or it was
cancelled. ``run`` and ``run_stream`` drive ``iter`` internally, and
``run_stream_sync`` drives ``run_stream``; the guard makes each of those one
capsule, owned by the outermost call.
"""
from __future__ import annotations

import asyncio
import contextvars
from pathlib import Path
from typing import Any

from novafabric.adapters._capsule import begin_capture, require
from novafabric.adapters._streaming import (
    ABANDONED,
    CANCELLED,
    GuardedAsyncContext,
)

#: Set while a capture is already in flight on this task. A nested call records
#: into the capsule that is already open rather than opening a second one.
_in_flight: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "novafabric_pydantic_ai_in_flight", default=False
)


def wrap_agent(
    agent: Any,
    *,
    run_name: str | None = None,
    data_dir: Path | None = None,
) -> Any:
    """Wrap a Pydantic AI ``Agent`` for NovaFabric capture.

    Patches ``run``, ``run_sync``, ``run_stream`` and ``iter`` in place and
    returns the same object.

    Args:
        agent: A ``pydantic_ai.Agent`` instance.
        run_name: Name recorded in the manifest. Defaults to the agent's
            ``name``, then its class name.
        data_dir: Base directory for capsules.

    Raises:
        ImportError: If ``pydantic-ai`` is not installed.

    Usage::

        from novafabric.adapters.pydantic_ai import wrap_agent
        agent = wrap_agent(agent, run_name="support-bot")
        result = agent.run_sync("Where is my order?")
    """
    require("pydantic_ai", "pydantic-ai")

    resolved_name = (
        run_name
        or getattr(agent, "name", None)
        or type(agent).__name__
        or "pydantic-ai-run"
    )

    def _capture(entry_point: str) -> Any:
        cap = begin_capture(
            framework="pydantic-ai", run_name=resolved_name, data_dir=data_dir
        )
        cap.tags["entry_point"] = entry_point
        return cap

    if hasattr(agent, "run_sync"):
        original_run_sync = agent.run_sync

        def wrapped_run_sync(*args: Any, **kwargs: Any) -> Any:
            if _in_flight.get():
                return original_run_sync(*args, **kwargs)
            cap = _capture("run_sync")
            token = _in_flight.set(True)
            try:
                return original_run_sync(*args, **kwargs)
            except Exception as exc:
                cap.fail(exc)
                raise
            finally:
                _in_flight.reset(token)
                cap.finish()

        agent.run_sync = wrapped_run_sync

    if hasattr(agent, "run"):
        original_run = agent.run

        async def wrapped_run(*args: Any, **kwargs: Any) -> Any:
            if _in_flight.get():
                return await original_run(*args, **kwargs)
            cap = _capture("run")
            token = _in_flight.set(True)
            try:
                return await original_run(*args, **kwargs)
            except asyncio.CancelledError:
                # e.g. ``run_stream_events`` abandoned mid-run: not a success.
                cap.mark_partial(CANCELLED)
                raise
            except Exception as exc:
                cap.fail(exc)
                raise
            finally:
                _in_flight.reset(token)
                cap.finish()

        agent.run = wrapped_run

    for name, settle in (("run_stream", _stream_unfinished), ("iter", _iter_unfinished)):
        if callable(getattr(agent, name, None)):
            _patch_context(agent, name, settle, _capture)

    return agent


def _patch_context(agent: Any, name: str, settle: Any, capture: Any) -> None:
    original = getattr(agent, name)

    def wrapped(*args: Any, **kwargs: Any) -> GuardedAsyncContext:
        return GuardedAsyncContext(
            original(*args, **kwargs),
            begin=lambda: capture(name),
            in_flight=_in_flight,
            settle=settle,
        )

    setattr(agent, name, wrapped)


def _stream_unfinished(streamed: Any) -> str | None:
    """Why a ``run_stream`` result is not a finished run, or ``None`` if it is.

    ``StreamedRunResult.is_complete`` turns true once the output has been read
    to the end (``get_output``, ``stream_output``, ``stream_text``, …). A
    result built from an already-settled run — Pydantic AI's ``wrap_run``
    short-circuit, or a deferred-tool ending — carries no stream at all and is
    finished from the start.
    """
    if getattr(streamed, "cancelled", False) is True:
        return CANCELLED
    complete = getattr(streamed, "is_complete", None)
    if complete is True or not isinstance(complete, bool):
        return None
    if getattr(streamed, "_stream_response", ...) is None:
        return None
    return ABANDONED


def _iter_unfinished(agent_run: Any) -> str | None:
    """An ``AgentRun`` has a ``result`` only once the graph reached ``End``."""
    if not hasattr(agent_run, "result"):
        return None
    return ABANDONED if agent_run.result is None else None
