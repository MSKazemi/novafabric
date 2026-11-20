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

A tool the workload did **not** pass through ``wrap_tools`` reaches this server
through the seam's observer (``call.wrapped`` is ``False``). It is never served
(ADR-0306 D1 serves declared tools only; the default is unchanged): it runs live
and is counted as a ``tool_live`` event, consuming the record it matches, if
any. A ``replay.yaml`` ``allow: false`` override naming it refuses it before the
body runs, under ``--permissive`` too (ADR-0306 D8).
"""

from __future__ import annotations

from typing import Any

from novafabric.adapters._adk_tool_seam import AdkToolCall
from novafabric.adapters._langchain_tool_seam import (
    CALL_ID_EXT,
    NOT_WRAPPED_REASON,
    SEAM_OBSERVED,
    Served,
)
from novafabric.replay._adk_tool_server import AdkToolServer
from novafabric.replay._contract import TOOL_SURFACE_LANGCHAIN
from novafabric.replay._errors import ReplayToolRecordNotServableError


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

    def before(self, call: AdkToolCall) -> Any:
        if getattr(call, "wrapped", True):
            return super().before(call)
        return self._observe(call)

    def _observe(self, call: AdkToolCall) -> None:
        """A tool that was not wrapped: refused under ``allow: false``, else live."""
        consumed = call.not_canonical is None and self._match(call).record is not None
        if self._overrides.get(call.tool_name) is False:
            # Raises (under `fail` from `_report_divergence`, else after `tool_refused`).
            self._diverge(
                call, ReplayToolRecordNotServableError, call.digest,
                "is not served", NOT_WRAPPED_REASON, consumed=consumed,
            )
        self._events.emit(
            "tool_live", tool_name=call.tool_name, surface=self.SURFACE,
            mutation_class=call.mutation_class, consumed=consumed, seam=SEAM_OBSERVED,
        )
        return None
