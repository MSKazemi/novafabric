"""NovaFabric adapter for LlamaIndex.

Wraps a query engine, chat engine, or agent so that each invocation writes a
Run Capsule. Wire-level hooks capture the model calls the framework makes.

LlamaIndex is an **optional** dependency — this module must stay importable
without it; :func:`wrap_engine` raises :class:`ImportError` at call time.

LlamaIndex has no single entry point across its object types: a query engine
exposes ``query``, a chat engine ``chat``, and an agent ``chat`` or ``run``. The
wrapper therefore patches the first method it finds from an explicit,
ordered list rather than guessing a name, and says which one it patched.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from novafabric.adapters._capsule import begin_capture, require

#: Checked in order. ``query`` first: on an object exposing both, the query
#: path is the one that runs a retrieval + synthesis round-trip.
_ENTRY_POINTS = ("query", "chat", "run")


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
    original = getattr(engine, target)

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        cap = begin_capture(
            framework="llamaindex", run_name=resolved_name, data_dir=data_dir
        )
        cap.tags["entry_point"] = target
        try:
            return original(*args, **kwargs)
        except Exception as exc:
            cap.fail(exc)
            raise
        finally:
            cap.finish()

    setattr(engine, target, wrapped)
    return engine
