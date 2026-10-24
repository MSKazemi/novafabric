"""NovaFabric adapter for DSPy.

Wraps a ``dspy.Module`` (or any ``dspy.Program``) so that each
``forward()`` / ``__call__`` invocation, and each ``aforward()`` / ``acall``,
creates a nova run capsule. Wire-level HTTP hooks capture every model call
automatically.

DSPy is an **optional** dependency — this module must be importable
even when ``dspy`` is not installed.  The :func:`wrap_program` function
will raise :class:`ImportError` at call time if the framework is missing.
"""
from __future__ import annotations

import contextvars
from pathlib import Path
from typing import Any

from novafabric.adapters._entry_points import wrap_entry_points

#: Set while a captured call runs, so a call it makes records into its capsule.
_in_flight: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "novafabric_dspy_in_flight", default=False
)


def wrap_program(
    program: Any,
    *,
    run_name: str | None = None,
    data_dir: Path | None = None,
) -> Any:
    """Wrap a DSPy ``Module`` / ``Program`` for NovaFabric capture.

    Patches the program's ``forward`` (and ``aforward``) in-place so that each
    invocation creates a nova capsule.  Because DSPy's ``__call__`` delegates to
    ``forward`` and ``acall`` to ``aforward``, every call style is captured.

    The original program object is returned (same identity) so existing
    references continue to work.

    Args:
        program: A ``dspy.Module`` or ``dspy.Program`` instance.
        run_name: Human-readable name stored in the capsule manifest.
            Defaults to the program's class name, or ``"dspy-run"``.
        data_dir: Base directory for capsules.  Defaults to
            ``$NOVAFABRIC_HOME/runs`` or ``.novafabric/runs`` under CWD.

    Raises:
        ImportError: If ``dspy`` / ``dspy-ai`` is not installed.

    Usage::

        from novafabric.adapters.dspy import wrap_program
        program = wrap_program(program, run_name="my-dspy-chain")
        result = program(question="What is 2+2?")
    """
    try:
        import dspy  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        raise ImportError(
            "dspy-ai is not installed. "
            "Install it with: pip install dspy-ai"
        )

    wrap_entry_points(
        program,
        ("forward", "aforward"),
        framework="dspy",
        run_name=run_name or type(program).__name__ or "dspy-run",
        data_dir=data_dir,
        in_flight=_in_flight,
    )
    return program
