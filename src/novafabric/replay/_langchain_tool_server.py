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
through the seam's observer (``call.wrapped`` is ``False``, mutation class
``unknown``, set by the seam -- never read from the capsule). It is served and
refused exactly like a wrapped tool declared ``unknown`` (ADR-0306 r3
amendment, owner decision 2026-11-21): a matched servable record is served
before the body runs; an unmatched or unservable call is refused, and under
``--permissive`` runs live only with ``--allow-unknown-mutation``.

A tool whose body made model or tool calls (a sub-agent tool such as
deepagents' ``task``) is served like a nested ``record.tool`` boundary
(ADR-0306 r3b, the slice-4 rule): its body never runs, and the dispatcher
consumes the records written inside it as *covered* (``on_served``).
"""

from __future__ import annotations

from typing import Any

from novafabric.adapters import _langchain_tool_message as _tool_message
from novafabric.adapters import _langgraph_command as _command
from novafabric.adapters._adk_tool_seam import AdkToolCall
from novafabric.adapters._langchain_tool_seam import CALL_ID_EXT, Served
from novafabric.capture import _tool_codec
from novafabric.replay._adk_tool_server import AdkToolServer, RecordNotDecodable
from novafabric.replay._contract import TOOL_SURFACE_LANGCHAIN, upgrade_legacy_langchain_record


class LangChainToolServer(AdkToolServer):
    """The LangChain replay server: one matcher, LangChain records only."""

    SURFACE = TOOL_SURFACE_LANGCHAIN
    CALL_ID_EXT = CALL_ID_EXT
    SUBJECT = "the LangChain tool"
    FAILURE_SERVED_REASON = (
        "the record is a failure, and LangChain tool failures are not served"
    )

    def __init__(self, records: list[dict[str, Any]], **kwargs: Any) -> None:
        # A round-2 observed record, or one an earlier capture marked not
        # servable for nesting, kept its value but was marked by policy only;
        # serve it as current capture would have marked it.
        super().__init__([upgrade_legacy_langchain_record(r) for r in records], **kwargs)

    def _decode(self, record: dict[str, Any], call: AdkToolCall) -> Any:
        """A ToolMessage envelope is rebuilt into a real ``ToolMessage``, and a
        LangGraph Command envelope into a real ``Command`` (ADR-0306 r3b), each
        message answering the replayed call's own tool-call id."""
        _ok, value = _tool_codec.decode_result(record)
        ext = record.get("extensions")
        kind = ext.get(_tool_message.RESULT_KIND_EXT) if isinstance(ext, dict) else None
        if kind == _tool_message.KIND_TOOL_MESSAGE:
            try:
                return _tool_message.decode_tool_message(value, call.function_call_id)
            except _tool_message.ToolMessageNotServable as exc:
                raise RecordNotDecodable(str(exc)) from exc
        if kind == _command.KIND_COMMAND:
            try:
                return _command.decode_command(value, call.function_call_id)
            except _command.CommandNotServable as exc:
                raise RecordNotDecodable(f"the recorded LangGraph Command: {exc}") from exc
        if kind is not None:
            # A result kind this version does not know: never serve it as JSON.
            raise RecordNotDecodable(f"the recorded LangChain result kind {kind!r} is unknown")
        return value

    def _answer(self, value: Any) -> Any:
        return Served(value)
