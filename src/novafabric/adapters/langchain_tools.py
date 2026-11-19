"""NovaFabric tool seam for LangChain / LangGraph tools (ADR-0306 slice 3, experimental).

LangChain and LangGraph are **optional** dependencies: this module imports
without them, and :func:`wrap_tools` returns its input unchanged when
``langchain_core`` is not installed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__all__ = ["wrap_tools"]


def wrap_tools(
    tools: Iterable[Any],
    mutation_classes: Mapping[str, str] | None = None,
    default_mutation_class: str = "unknown",
) -> list[Any]:
    """Route LangChain tools through NovaFabric's tool seam.

    **Experimental** (ADR-0306 slice 3). Returns *tools* with every LangChain
    ``BaseTool`` (``@tool`` functions, ``StructuredTool``, ``BaseTool``
    subclasses) replaced by a copy that consults the NovaFabric handler before
    the tool body runs. Pass the result wherever the tools go -- ``bind_tools``,
    LangGraph's ``ToolNode``, ``create_react_agent``. Under
    ``nova capture python …`` each call becomes one ``tool-calls.jsonl``
    record; under a mocked ``nova replay`` of that capsule the recorded result
    is returned as the body's result and the body never runs (an unmatched or
    unservable call is refused, fail closed). Outside capture and replay the
    copies behave exactly like the originals.

    The copy's class is a generated subclass of the tool's own class that
    overrides the public ``run`` / ``arun`` and the documented body hooks
    ``_run`` / ``_arun``; LangChain still validates input, fires callbacks and
    builds the ``ToolMessage``. That construction is NovaFabric's, not a
    LangChain extension point: it was checked against ``langchain-core`` 1.8.3
    and ``langgraph`` 1.2.23, and a different release may bypass it. MCP tools
    from ``langchain_mcp_adapters`` (already served on the MCP surface) and
    functions declared with ``record.tool`` pass through unchanged, as does
    anything that is not a ``BaseTool``.

    Only capsules with a re-runnable command (``nova capture python …``) can be
    replayed: capsules written by the LangGraph adapter carry a pseudo-command
    and are refused by ``nova replay`` (ADR-0306 open question 7).

    Args:
        tools: The agent's tools.
        mutation_classes: ADR-0012 mutation class per tool name. It gates
            ``--permissive`` and ``replay.yaml`` ``allow: true`` at replay, and
            is declared here, in the workload's code -- never read from a capsule.
        default_mutation_class: Class of every tool not named above.

    Raises:
        ValueError: If a mutation class is not an ADR-0012 class.

    Usage::

        from langchain_core.tools import tool
        from langgraph.prebuilt import ToolNode
        from novafabric.adapters.langchain_tools import wrap_tools

        @tool
        def lookup_order(order_id: str) -> dict:
            "Look up an order."
            ...

        tools = wrap_tools([lookup_order], {"lookup_order": "read-only"})
        llm = llm.bind_tools(tools)
        node = ToolNode(tools)
    """
    from novafabric.adapters._langchain_tool_seam import wrap_tools as _wrap_tools

    return _wrap_tools(tools, mutation_classes, default_mutation_class)
