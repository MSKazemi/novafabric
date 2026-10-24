"""Capture a framework object's entry points without changing how they are called.

An adapter that patches methods on the user's object (``crew.kickoff``,
``agent.initiate_chat``, ``program.forward``) must keep three things the framework
promises (review eval-adapters-query-19):

* **the signature** — every wrapper is ``(*args, **kwargs)`` and forwards both, so a
  positional argument the framework accepts is still accepted;
* **the object** — a pydantic ``BaseModel`` (``crewai.Crew``) refuses ``obj.name = fn``
  for a non-field, so the wrapper is set with ``object.__setattr__`` when plain
  assignment is refused, and the same instance is returned;
* **sync and async alike** — a coroutine function gets an ``async`` wrapper, so
  ``kickoff_async`` / ``a_initiate_chat`` / ``aforward`` are captured as their sync
  twins are.

Each call becomes one capsule on the shared :class:`~novafabric.adapters._capsule.AdapterCapture`
core, ``metadata.entry_point`` naming the method. A call made while another captured
call of the same adapter is in flight (``kickoff_async`` running ``kickoff`` in a
thread, ``__call__`` delegating to ``forward``) records into the outer capsule.
"""
from __future__ import annotations

import contextvars
import functools
import inspect
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from novafabric.adapters._capsule import begin_capture
from novafabric.adapters._streaming import record_outcome


def set_method(target: Any, name: str, fn: Callable[..., Any]) -> None:
    """``target.name = fn``, also on an object that refuses non-field attributes."""
    try:
        setattr(target, name, fn)
    except (AttributeError, TypeError, ValueError):  # pydantic: no such field
        object.__setattr__(target, name, fn)


def wrap_entry_points(
    target: Any,
    names: Iterable[str],
    *,
    framework: str,
    run_name: str,
    data_dir: Path | None,
    in_flight: contextvars.ContextVar[bool],
) -> list[str]:
    """Wrap each method in *names* that *target* has; return the names wrapped."""
    wrapped: list[str] = []
    for name in names:
        original = getattr(target, name, None)
        if not callable(original):
            continue
        set_method(
            target,
            name,
            _wrapper(original, name, framework, run_name, data_dir, in_flight),
        )
        wrapped.append(name)
    return wrapped


def _wrapper(
    original: Callable[..., Any],
    entry_point: str,
    framework: str,
    run_name: str,
    data_dir: Path | None,
    in_flight: contextvars.ContextVar[bool],
) -> Callable[..., Any]:
    def _begin() -> Any:
        cap = begin_capture(framework=framework, run_name=run_name, data_dir=data_dir)
        cap.tags["entry_point"] = entry_point
        return cap

    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            if in_flight.get():
                return await original(*args, **kwargs)
            cap = _begin()
            token = in_flight.set(True)
            try:
                return await original(*args, **kwargs)
            except BaseException as exc:
                record_outcome(cap, exc)
                raise
            finally:
                in_flight.reset(token)
                cap.finish()

        return async_wrapper

    @functools.wraps(original)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        if in_flight.get():
            return original(*args, **kwargs)
        cap = _begin()
        token = in_flight.set(True)
        try:
            return original(*args, **kwargs)
        except BaseException as exc:
            record_outcome(cap, exc)
            raise
        finally:
            in_flight.reset(token)
            cap.finish()

    return sync_wrapper
