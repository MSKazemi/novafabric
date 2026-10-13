"""Serves OpenAI Agents SDK function-tool calls in a mocked replay (ADR-0306 slice 3).

The same server as the Google ADK one (``_adk_tool_server``): one-to-one
matching by the model's ``call_id`` when it recurs with the same name and
arguments, else by name + after-redaction argument digest; an unmatched or
unservable call is refused *before the tool body runs*; ``--permissive`` and
``replay.yaml`` ``tool_overrides`` go through ``_policy.decide_intercepted``.

Registered by ``MockToolDispatcher.install`` as the handler of
``adapters._oai_tool_seam``. A refusal is an exception raised from the tool's
invoker: the Agents SDK propagates it and fails the run (the divergence is in
the event log first, so a workload that swallows it still fails the replay).
"""

from __future__ import annotations

from typing import Any

from novafabric.adapters._oai_tool_seam import CALL_ID_EXT, Served
from novafabric.replay._adk_tool_server import AdkToolServer
from novafabric.replay._contract import TOOL_SURFACE_OPENAI_AGENTS


class OaiToolServer(AdkToolServer):
    """The Agents SDK replay server: one matcher, Agents SDK records only."""

    SURFACE = TOOL_SURFACE_OPENAI_AGENTS
    CALL_ID_EXT = CALL_ID_EXT
    SUBJECT = "the Agents SDK tool"
    FAILURE_SERVED_REASON = (
        "the record is a failure, and Agents SDK tool failures are not served"
    )

    def _answer(self, value: Any) -> Any:
        return Served(value)
