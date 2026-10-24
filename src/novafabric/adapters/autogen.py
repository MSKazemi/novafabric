"""NovaFabric adapter for AutoGen (pyautogen / autogen-agentchat).

Wraps a ``ConversableAgent`` so that each ``initiate_chat()`` /
``a_initiate_chat()`` call creates a nova run capsule.  Wire-level HTTP hooks
capture every model call automatically.

AutoGen is an **optional** dependency — this module must be importable
even when ``autogen`` / ``pyautogen`` is not installed.  The
:func:`wrap_agent` function will raise :class:`ImportError` at call time
if the framework is missing.
"""
from __future__ import annotations

import contextvars
from pathlib import Path
from typing import Any

from novafabric.adapters._entry_points import wrap_entry_points

#: Set while a captured call runs, so a call it makes records into its capsule.
_in_flight: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "novafabric_autogen_in_flight", default=False
)


def wrap_agent(
    agent: Any,
    *,
    run_name: str | None = None,
    data_dir: Path | None = None,
) -> Any:
    """Wrap an AutoGen ``ConversableAgent`` for NovaFabric capture.

    Patches the agent's ``initiate_chat`` (and ``a_initiate_chat``) in-place so
    that each conversation creates a nova run capsule. The original agent object
    is returned (same identity) so existing references continue to work, and the
    wrappers forward every positional and keyword argument.

    Args:
        agent: An ``autogen.ConversableAgent`` (or any subclass).
        run_name: Human-readable name stored in the capsule manifest.
            Defaults to the agent's ``name`` attribute, or ``"autogen-run"``.
        data_dir: Base directory for capsules.  Defaults to
            ``$NOVAFABRIC_HOME/runs`` or ``.novafabric/runs`` under CWD.

    Raises:
        ImportError: If ``autogen`` / ``pyautogen`` is not installed.

    Usage::

        from novafabric.adapters.autogen import wrap_agent
        agent = wrap_agent(agent, run_name="my-agent-chat")
        agent.initiate_chat(recipient, message="hello")
    """
    try:
        import autogen  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        raise ImportError(
            "autogen / pyautogen is not installed. "
            "Install it with: pip install pyautogen"
        )

    wrap_entry_points(
        agent,
        ("initiate_chat", "a_initiate_chat"),
        framework="autogen",
        run_name=run_name or getattr(agent, "name", None) or "autogen-run",
        data_dir=data_dir,
        in_flight=_in_flight,
    )
    return agent
