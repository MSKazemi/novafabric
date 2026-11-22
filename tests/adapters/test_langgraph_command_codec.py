"""The LangGraph ``Command`` result codec (ADR-0306 r3b): what is encoded is
rebuilt exactly, and everything else is refused with a reason naming the field.

Unit tests against the real ``langgraph.types.Command`` and
``langchain_core.messages.ToolMessage``; no capture, no replay.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langgraph.types import Command, Send  # noqa: E402

from novafabric.adapters._langgraph_command import (  # noqa: E402
    CommandNotServable,
    decode_command,
    encode_command,
    is_command,
)
from novafabric.capture import _tool_codec as codec  # noqa: E402

CALL = "call-1"


def _round_trip(command: Any, *, replayed_id: str = CALL) -> Any:
    envelope, reason = encode_command(command, CALL)
    assert reason is None, reason
    codec.json_native(envelope, "envelope")  # a JSON value, servable by the codec
    return decode_command(json.loads(json.dumps(envelope)), replayed_id)


@pytest.mark.parametrize("command", [
    Command(update={"messages": [ToolMessage("ok", tool_call_id=CALL)], "n": 3}),
    Command(update={"notes": {"a": [1, 2.5, None, True]}}, goto="audit"),
    Command(goto=("a", "b")),
    Command(goto=["a", "b"], graph=Command.PARENT),
    Command(resume={"answer": 42}),
    Command(update={"messages": (ToolMessage(
        [{"type": "text", "text": "hi"}], tool_call_id=CALL, name="t", status="error",
        artifact={"k": 1}, id="msg-1", response_metadata={"m": 1},
    ),)}),
    Command(),
])
def test_an_encodable_command_is_rebuilt_exactly(command: Command) -> None:
    rebuilt = _round_trip(command)
    assert is_command(rebuilt)
    assert rebuilt == command  # dataclass equality: every field, messages included
    assert type(rebuilt.goto) is type(command.goto)


def test_messages_answer_the_replayed_call() -> None:
    rebuilt = _round_trip(
        Command(update={"messages": [ToolMessage("ok", tool_call_id=CALL)]}),
        replayed_id="call-replayed",
    )
    assert rebuilt.update["messages"][0].tool_call_id == "call-replayed"


@pytest.mark.parametrize(("command", "why"), [
    (Command(goto=Send("n", {"x": 1})), "Command.goto holds a Send"),
    (Command(goto=["a", Send("n", {})]), "Command.goto holds a Send"),
    (Command(update=[ToolMessage("ok", tool_call_id=CALL)]), "Command.update is a list"),
    (Command(update={"notes": {1, 2}}), "Command.update['notes']"),
    (Command(update={"messages": [ToolMessage("ok", tool_call_id="other")]}),
     "answers another tool call"),
    (Command(update={"messages": [AIMessage("hi")]}), "not ToolMessages"),
    (Command(update={"messages": [ToolMessage("ok", tool_call_id=CALL, artifact=object())]}),
     "artifact"),
    (Command(resume=object()), "Command.resume"),
    (Command(graph=3), "Command.graph"),  # type: ignore[arg-type]
])
def test_a_command_that_cannot_be_rebuilt_is_refused_with_a_reason(
    command: Command, why: str,
) -> None:
    envelope, reason = encode_command(command, CALL)
    assert envelope == {}
    assert reason is not None and why in reason


def test_a_call_without_an_id_cannot_encode_a_message() -> None:
    _envelope, reason = encode_command(
        Command(update={"messages": [ToolMessage("ok", tool_call_id=CALL)]}), None,
    )
    assert reason is not None and "answers another tool call" in reason


@pytest.mark.parametrize("tamper", [
    lambda e: e.pop("goto"),
    lambda e: e.__setitem__("goto", [1]),
    lambda e: e.__setitem__("graph", 1),
    lambda e: e.__setitem__("update", {"entries": [["k"]]}),
    lambda e: e.__setitem__("update", {"entries": [["k", {"json": 1, "x": 2}]]}),
    lambda e: e["update"]["entries"][0][1]["messages"][0].__setitem__("answers", "x"),
])
def test_a_malformed_envelope_is_refused_never_guessed(tamper: Any) -> None:
    envelope, _ = encode_command(
        Command(update={"messages": [ToolMessage("ok", tool_call_id=CALL)]}), CALL,
    )
    tamper(envelope)
    with pytest.raises(CommandNotServable):
        decode_command(envelope, CALL)


def test_a_replayed_call_without_an_id_is_refused() -> None:
    envelope, _ = encode_command(
        Command(update={"messages": [ToolMessage("ok", tool_call_id=CALL)]}), CALL,
    )
    with pytest.raises(CommandNotServable):
        decode_command(envelope, None)


def test_only_a_langgraph_command_is_recognised() -> None:
    class Command:  # same name, another module
        pass

    assert not is_command(Command())
    assert not is_command({"goto": "x"})
