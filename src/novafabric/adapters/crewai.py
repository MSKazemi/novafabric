"""NovaFabric adapter for CrewAI.

Wraps a ``crewai.Crew`` object so that each ``kickoff()`` / ``kickoff_async()`` call
creates a nova run capsule.  Wire-level HTTP hooks capture every model call made
by the crew's agents automatically.

CrewAI is an **optional** dependency — this module must be importable
even when ``crewai`` is not installed.  The :func:`wrap_crew` function
will raise :class:`ImportError` at call time if the framework is missing.
"""
from __future__ import annotations

import contextvars
from pathlib import Path
from typing import Any

from novafabric.adapters._entry_points import wrap_entry_points

#: Set while a captured call runs, so a call it makes records into its capsule.
_in_flight: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "novafabric_crewai_in_flight", default=False
)


def wrap_crew(
    crew: Any,
    *,
    run_name: str | None = None,
    data_dir: Path | None = None,
) -> Any:
    """Wrap a CrewAI ``Crew`` for NovaFabric capture.

    Patches the crew's ``kickoff`` (and ``kickoff_async``) in-place so that each
    run creates a nova capsule; the original crew object is returned (same
    identity). The wrappers forward every positional and keyword argument, and
    are set on the pydantic model without it treating them as fields
    (:mod:`novafabric.adapters._entry_points`).

    Args:
        crew: A ``crewai.Crew`` instance.
        run_name: Human-readable name stored in the capsule manifest.
            Defaults to ``"crewai-run"``.
        data_dir: Base directory for capsules.  Defaults to
            ``$NOVAFABRIC_HOME/runs`` or ``.novafabric/runs`` under CWD.

    Raises:
        ImportError: If ``crewai`` is not installed.

    Usage::

        from novafabric.adapters.crewai import wrap_crew
        crew = wrap_crew(crew, run_name="my-crew")
        result = crew.kickoff({"topic": "AI"})
    """
    try:
        import crewai  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        raise ImportError(
            "crewai is not installed. "
            "Install it with: pip install crewai"
        )

    wrap_entry_points(
        crew,
        ("kickoff", "kickoff_async"),
        framework="crewai",
        run_name=run_name or "crewai-run",
        data_dir=data_dir,
        in_flight=_in_flight,
    )
    return crew
