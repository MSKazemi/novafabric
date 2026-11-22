"""A JSON codec for LangGraph ``Command`` tool results (ADR-0306 r3b).

A LangChain tool run by LangGraph's ``ToolNode`` may return
``langgraph.types.Command`` -- a state update (``update``), a route (``goto``),
a target graph (``graph``) and a resume value (``resume``) -- instead of a value.
LangChain passes it through unchanged (``Command`` is a ``ToolOutputMixin``), and
``ToolNode`` validates it and hands it to the graph. The ``record.tool`` codec
keeps JSON values only, so such a result used to be recorded as not servable.
This module turns one into a JSON *envelope* at capture and rebuilds a real
``Command`` from it at replay.

**What is encoded, and only that.**

* ``graph``: ``None`` or a string (``Command.PARENT`` is ``"__parent__"``).
* ``goto``: a node name or a sequence of node names (a tuple and a list are told
  apart). A ``Send`` carries an arbitrary argument for the target node and is
  not encoded: the record is not servable.
* ``resume``: a JSON value.
* ``update``: ``None``, or a ``dict`` with string keys whose every value is
  either a JSON value or a list (or tuple) of ``ToolMessage``s, each answering
  *this* call (its ``tool_call_id`` is the call's own). Any other update -- a
  list of messages (``ToolNode`` list input), a Pydantic state object, another
  message type, a message answering another call, a non-JSON value -- makes the
  record not servable, with a reason naming the field.

A message is the ``_langchain_tool_message`` envelope (``content``, ``status``,
``name``, ``artifact``, non-empty ``additional_kwargs`` / ``response_metadata``),
plus its ``id`` when the tool set one and ``"answers": "this-call"``; its
``tool_call_id`` is not kept. Replay rebuilds each message with the **replayed
call's** ``tool_call_id``, exactly as the ``ToolMessage`` codec does, so
``ToolNode``'s "a ToolMessage must answer the call" check sees the same thing it
saw at capture. Every rebuilt field equals the recorded one; an envelope that is
malformed refuses the call (never a guess).

Nothing here imports ``langgraph`` or ``langchain_core`` at module level.
"""

from __future__ import annotations

import importlib
from typing import Any

from novafabric.adapters import _langchain_tool_message as _tool_message
from novafabric.capture import _tool_codec as codec

__all__ = [
    "KIND_COMMAND",
    "CommandNotServable",
    "decode_command",
    "encode_command",
    "is_command",
]

#: ``io.novafabric.langchain_result_kind`` of a record whose value is a Command envelope.
KIND_COMMAND = "langgraph_command"
#: The marker a message envelope carries: it answers the call that returned it.
_ANSWERS_THIS_CALL = "this-call"
_SERVED_ONLY = "a LangGraph Command result is served only when it can be rebuilt exactly"


class CommandNotServable(Exception):
    """A Command cannot be encoded faithfully, or a recorded envelope rebuilt."""


def _is_class(value: Any, name: str, module_prefix: str) -> bool:
    return any(
        klass.__name__ == name and klass.__module__.startswith(module_prefix)
        for klass in type(value).__mro__
    )


def is_command(value: Any) -> bool:
    """Whether *value* is a ``langgraph.types.Command`` (checked by class name and
    module, so LangGraph is never imported here)."""
    return _is_class(value, "Command", "langgraph.types")


def _json(value: Any, where: str) -> Any:
    try:
        return codec.json_native(value, where)
    except codec.NotJSONRepresentable as exc:
        raise CommandNotServable(f"{exc.where} {exc.why}; {_SERVED_ONLY}") from exc


def _encode_goto(goto: Any) -> tuple[Any, bool]:
    if isinstance(goto, str):
        return goto, False
    if _is_class(goto, "Send", "langgraph.types"):
        raise CommandNotServable(
            f"Command.goto holds a Send (an arbitrary node argument); {_SERVED_ONLY}"
        )
    if type(goto) in (list, tuple):
        for i, item in enumerate(goto):
            if _is_class(item, "Send", "langgraph.types"):
                raise CommandNotServable(
                    f"Command.goto holds a Send (an arbitrary node argument); {_SERVED_ONLY}"
                )
            if type(item) is not str:
                raise CommandNotServable(
                    f"Command.goto[{i}] is a {type(item).__name__}, not a node name; "
                    f"{_SERVED_ONLY}"
                )
        return list(goto), type(goto) is tuple
    raise CommandNotServable(
        f"Command.goto is a {type(goto).__name__}, not a node name; {_SERVED_ONLY}"
    )


def _encode_message(message: Any, where: str, call_id: Any) -> dict[str, Any]:
    if not _tool_message.is_tool_message(message):
        raise CommandNotServable(
            f"{where} is a {type(message).__name__}, not a ToolMessage; {_SERVED_ONLY}"
        )
    if not isinstance(call_id, str) or not call_id or message.tool_call_id != call_id:
        raise CommandNotServable(
            f"{where} answers another tool call, not this one; {_SERVED_ONLY}"
        )
    envelope, reason = _tool_message.encode_tool_message(message)
    if reason is not None:
        raise CommandNotServable(f"{where}: {reason}")
    message_id = getattr(message, "id", None)
    if message_id is not None:
        envelope["id"] = _json(message_id, f"{where}.id")
    envelope["answers"] = _ANSWERS_THIS_CALL
    return envelope


def _encode_update(update: Any, call_id: Any) -> dict[str, Any] | None:
    if update is None:
        return None
    if type(update) is not dict:
        raise CommandNotServable(
            f"Command.update is a {type(update).__name__}, not a dict; {_SERVED_ONLY}"
        )
    entries: list[list[Any]] = []
    for key, value in update.items():
        where = f"Command.update[{key!r}]"
        if type(key) is not str:
            raise CommandNotServable(f"{where} has a non-string key; {_SERVED_ONLY}")
        if type(value) in (list, tuple) and value and all(
            _tool_message.is_tool_message(v) for v in value
        ):
            messages = [_encode_message(m, f"{where}[{i}]", call_id) for i, m in enumerate(value)]
            entries.append([key, {"messages": messages, "tuple": type(value) is tuple}])
            continue
        if type(value) in (list, tuple) and any(_is_class(v, "BaseMessage", "langchain_core")
                                                for v in value):
            raise CommandNotServable(
                f"{where} mixes messages that are not ToolMessages (or other values); "
                f"{_SERVED_ONLY}"
            )
        try:
            entries.append([key, {"json": codec.json_native(value, where)}])
        except codec.NotJSONRepresentable as exc:
            raise CommandNotServable(
                f"{exc.where} {exc.why}, not a JSON value or a list of ToolMessages; "
                f"{_SERVED_ONLY}"
            ) from exc
    return {"entries": entries}


def encode_command(command: Any, call_id: Any) -> tuple[dict[str, Any], str | None]:
    """``(envelope, None)``, or ``({}, reason)`` when *command* cannot be encoded
    faithfully. *call_id* is the call's own ``tool_call_id``."""
    try:
        goto, goto_tuple = _encode_goto(getattr(command, "goto", ()))
        graph = getattr(command, "graph", None)
        if graph is not None and type(graph) is not str:
            raise CommandNotServable(
                f"Command.graph is a {type(graph).__name__}, not a graph name; {_SERVED_ONLY}"
            )
        envelope: dict[str, Any] = {
            "graph": graph,
            "goto": goto,
            "resume": _json(getattr(command, "resume", None), "Command.resume"),
            "update": _encode_update(getattr(command, "update", None), call_id),
        }
        if goto_tuple:
            envelope["goto_tuple"] = True
    except CommandNotServable as exc:
        return {}, str(exc)
    except Exception as exc:  # noqa: BLE001 -- an odd Command is a refusal, never a crash
        return {}, f"the Command could not be encoded: {type(exc).__name__}; {_SERVED_ONLY}"
    return envelope, None


def _decode_update(update: Any, call_id: Any) -> dict[str, Any] | None:
    if update is None:
        return None
    entries = update.get("entries") if isinstance(update, dict) else None
    if not isinstance(entries, list):
        raise CommandNotServable("the recorded Command update is malformed")
    out: dict[str, Any] = {}
    for entry in entries:
        if not (isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], str)):
            raise CommandNotServable("a recorded Command update entry is malformed")
        key, body = entry
        if isinstance(body, dict) and set(body) == {"json"}:
            out[key] = body["json"]
        elif isinstance(body, dict) and isinstance(body.get("messages"), list):
            messages = []
            for envelope in body["messages"]:
                if not isinstance(envelope, dict) or envelope.get("answers") != _ANSWERS_THIS_CALL:
                    raise CommandNotServable(
                        "a recorded Command message does not answer this call"
                    )
                try:
                    message = _tool_message.decode_tool_message(envelope, call_id)
                except _tool_message.ToolMessageNotServable as exc:
                    raise CommandNotServable(f"a recorded Command message: {exc}") from exc
                if envelope.get("id") is not None:
                    message.id = envelope["id"]
                messages.append(message)
            out[key] = tuple(messages) if body.get("tuple") is True else messages
        else:
            raise CommandNotServable("a recorded Command update entry is malformed")
    return out


def decode_command(envelope: Any, call_id: Any) -> Any:
    """A real ``langgraph.types.Command`` from a recorded *envelope*, its messages
    answering *call_id* (the replayed call's ``tool_call_id``).

    Raises :class:`CommandNotServable` when the envelope is malformed or LangGraph
    rejects it."""
    if not isinstance(envelope, dict) or not {"graph", "goto", "resume", "update"} <= set(
        envelope
    ):
        raise CommandNotServable("the recorded Command envelope is malformed")
    goto = envelope["goto"]
    if isinstance(goto, list):
        if not all(isinstance(g, str) for g in goto):
            raise CommandNotServable("the recorded Command goto is malformed")
        goto = tuple(goto) if envelope.get("goto_tuple") is True else goto
    elif not isinstance(goto, str):
        raise CommandNotServable("the recorded Command goto is malformed")
    graph = envelope["graph"]
    if graph is not None and not isinstance(graph, str):
        raise CommandNotServable("the recorded Command graph is malformed")
    update = _decode_update(envelope["update"], call_id)
    try:
        command = importlib.import_module("langgraph.types").Command
        return command(graph=graph, update=update, resume=envelope["resume"], goto=goto)
    except Exception as exc:  # noqa: BLE001 -- a refusal, never a crash in the workload
        raise CommandNotServable(
            f"LangGraph could not rebuild the recorded Command: {type(exc).__name__}"
        ) from exc
