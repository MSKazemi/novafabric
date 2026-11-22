"""A JSON codec for LangChain ``ToolMessage`` tool results (ADR-0306 r3 amendment).

A LangChain tool may return a ``ToolMessage`` itself instead of a value -- the
file tools of ``deepagents`` do, building it from the injected tool-call id.
LangChain passes such a result through unchanged (``_format_output``), so the
``record.tool`` codec, which keeps JSON values only, recorded it as not
servable. This module turns one into a JSON *envelope* at capture and rebuilds
a real ``ToolMessage`` from it at replay.

The envelope holds exactly the fields the rest of the run can see: ``content``,
``status``, ``name``, ``artifact`` (``None`` when absent) and, only when non-empty,
``additional_kwargs`` / ``response_metadata``. Every one must be JSON-native, or
the record is not servable with a named reason. ``tool_call_id`` is *not* part
of the envelope: replay rebuilds the message with the id of the call being
served (the model's ``ToolCall["id"]`` in the replayed run), so the message
pairs with the request that asked for it. The message ``id`` is not kept
(LangGraph assigns one when it adds the message to the state).

Nothing here imports ``langchain_core`` at module level.
"""

from __future__ import annotations

import importlib
from typing import Any

from novafabric.capture import _tool_codec as codec

__all__ = [
    "KIND_TOOL_MESSAGE",
    "RESULT_KIND_EXT",
    "ToolMessageNotServable",
    "decode_tool_message",
    "encode_tool_message",
    "is_tool_message",
]

#: Extension naming how a LangChain record's ``result.value`` must be decoded.
RESULT_KIND_EXT = "io.novafabric.langchain_result_kind"
#: ``result.value`` is a ToolMessage envelope (see the module docstring).
KIND_TOOL_MESSAGE = "tool_message"

_OPTIONAL_DICTS = ("additional_kwargs", "response_metadata")


class ToolMessageNotServable(Exception):
    """A recorded ToolMessage envelope could not be rebuilt at replay."""


def is_tool_message(value: Any) -> bool:
    """Whether *value* is a ``langchain_core`` ``ToolMessage`` (or a subclass).

    Checked by class name and module so that LangChain is never imported here.
    """
    for klass in type(value).__mro__:
        if klass.__name__ == "ToolMessage" and klass.__module__.startswith(
            "langchain_core.messages"
        ):
            return True
    return False


def encode_tool_message(message: Any) -> tuple[dict[str, Any], str | None]:
    """``(envelope, None)``, or ``(partial envelope, reason)`` when a field is not JSON."""
    envelope: dict[str, Any] = {}
    fields: list[tuple[str, Any]] = [
        ("content", getattr(message, "content", None)),
        ("status", getattr(message, "status", "success")),
        ("name", getattr(message, "name", None)),
        ("artifact", getattr(message, "artifact", None)),
    ]
    for key in _OPTIONAL_DICTS:
        value = getattr(message, key, None)
        if value:
            fields.append((key, value))
    for key, value in fields:
        try:
            envelope[key] = codec.json_native(value, f"the ToolMessage {key}")
        except codec.NotJSONRepresentable as exc:
            return envelope, (
                f"{exc.where} {exc.why}; a ToolMessage result is served only when "
                "every field is a JSON value"
            )
    return envelope, None


def decode_tool_message(envelope: Any, tool_call_id: Any) -> Any:
    """A real ``ToolMessage`` from a recorded *envelope*, answering *tool_call_id*.

    Raises :class:`ToolMessageNotServable` when the envelope is malformed, the
    call has no tool-call id, or LangChain rejects the fields.
    """
    if not isinstance(envelope, dict) or "content" not in envelope:
        raise ToolMessageNotServable("the recorded ToolMessage envelope is malformed")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise ToolMessageNotServable(
            "the replayed call carries no tool_call_id to answer with a ToolMessage"
        )
    try:
        tool_message = importlib.import_module("langchain_core.messages").ToolMessage
        kwargs = {
            k: envelope[k]
            for k in ("status", "name", "artifact", *_OPTIONAL_DICTS)
            if k in envelope and envelope[k] is not None
        }
        return tool_message(content=envelope["content"], tool_call_id=tool_call_id, **kwargs)
    except Exception as exc:  # noqa: BLE001 -- a refusal, never a crash in the workload
        raise ToolMessageNotServable(
            f"LangChain could not rebuild the recorded ToolMessage: {type(exc).__name__}"
        ) from exc
