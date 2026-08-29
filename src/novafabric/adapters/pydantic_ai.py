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
"""
from __future__ import annotations

import contextvars
from pathlib import Path
from typing import Any

from novafabric.adapters._capsule import begin_capture, require

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

    Patches ``run`` and ``run_sync`` in place and returns the same object.

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
            except Exception as exc:
                cap.fail(exc)
                raise
            finally:
                _in_flight.reset(token)
                cap.finish()

        agent.run = wrapped_run

    return agent
