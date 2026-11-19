"""Serves LangChain / LangGraph tool calls in a mocked replay (ADR-0306 slice 3).

The same server as the Google ADK and OpenAI Agents ones (``_adk_tool_server``):
one-to-one matching by the model's ``tool_call_id`` when it recurs with the
same name and arguments, else by name + after-redaction argument digest; an
unmatched or unservable call is refused *before the tool body runs*;
``--permissive`` and ``replay.yaml`` ``tool_overrides`` go through
``_policy.decide_intercepted``.

Registered by ``MockToolDispatcher.install`` as the handler of
``adapters._langchain_tool_seam``. A served value is returned as the tool
body's result, so LangChain still builds the ``ToolMessage``. A refusal is an
exception raised from the body: ``BaseTool.run`` re-raises it, and LangGraph's
``ToolNode`` either re-raises it (the default) or turns it into an error
message (``handle_tool_errors``); the divergence is in the event log first, so
a workload that swallows it still fails the replay.
"""

from __future__ import annotations

from typing import Any

from novafabric.adapters._langchain_tool_seam import CALL_ID_EXT, Served
from novafabric.replay._adk_tool_server import AdkToolServer
from novafabric.replay._contract import TOOL_SURFACE_LANGCHAIN


class LangChainToolServer(AdkToolServer):
    """The LangChain replay server: one matcher, LangChain records only."""

    SURFACE = TOOL_SURFACE_LANGCHAIN
    CALL_ID_EXT = CALL_ID_EXT
    SUBJECT = "the LangChain tool"
    FAILURE_SERVED_REASON = (
        "the record is a failure, and LangChain tool failures are not served"
    )

    def _answer(self, value: Any) -> Any:
        return Served(value)
