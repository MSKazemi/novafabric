"""``Agent.as_tool`` calls are served only when the returned value is all the
sub-run did that the workload can see (ADR-0306 r3c).

Serving an agent tool skips its sub-run. The options ``on_stream``, ``hooks``,
``session``, ``previous_response_id`` and ``conversation_id`` give that sub-run
an effect beyond the value -- a callback fed its events, a session it writes, a
server-side conversation it continues -- and a sub-run that paused for approval
leaves state the parent run reads. Capture marks such a call not servable, with
a reason naming the option. The options are read from the SDK's private
``_run_agent_impl`` closure (openai-agents 0.24.0); when they cannot be read,
the call is not servable (fail closed).

Real ``agents`` objects; no run, no network.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("agents")

from agents import Agent, RunHooks, function_tool  # noqa: E402

from novafabric.adapters import _oai_tool_seam as seam  # noqa: E402


def _helper() -> Any:
    return Agent(name="helper")


async def _on_stream(_event: Any) -> None:
    return None


def test_a_plain_agent_tool_has_no_obstacle() -> None:
    tool = _helper().as_tool("ask", "Ask.")
    assert seam.is_agent_tool(tool)
    assert seam.agent_tool_option_obstacles(tool) == ()


@pytest.mark.parametrize("option, value", [
    ("on_stream", _on_stream),
    ("hooks", RunHooks()),
    ("previous_response_id", "resp_1"),
    ("conversation_id", "conv_1"),
])
def test_an_option_with_an_effect_beyond_the_value_is_an_obstacle(option: str, value: Any) -> None:
    tool = _helper().as_tool("ask", "Ask.", **{option: value})
    (reason,) = seam.agent_tool_option_obstacles(tool)
    assert f"sets {option}" in reason


def test_a_session_is_an_obstacle() -> None:
    from agents import SQLiteSession

    tool = _helper().as_tool("ask", "Ask.", session=SQLiteSession("s"))
    (reason,) = seam.agent_tool_option_obstacles(tool)
    assert "sets session" in reason


def test_a_seam_copy_is_read_through_the_seam_invoker() -> None:
    (tool,) = seam.wrap_tools([_helper().as_tool("ask", "Ask.", on_stream=_on_stream)])
    assert isinstance(tool.on_invoke_tool, seam._SeamInvoker)
    (reason,) = seam.agent_tool_option_obstacles(tool)
    assert "sets on_stream" in reason


def test_options_that_cannot_be_read_are_an_obstacle() -> None:
    """Fail closed: an SDK whose agent tool hides the closure."""
    tool = _helper().as_tool("ask", "Ask.")
    tool.on_invoke_tool = lambda *_a: None  # no _run_agent_impl behind it
    assert seam.agent_tool_option_obstacles(tool) == (seam.AGENT_TOOL_OPTIONS_UNKNOWN,)


def test_a_function_tool_is_never_an_agent_tool() -> None:
    @function_tool
    def ping() -> str:
        return "pong"

    assert not seam.is_agent_tool(ping)
    assert seam.agent_tool_option_obstacles(ping) == ()


class _Ctx:
    def __init__(self, tool_call: Any) -> None:
        self.tool_call = tool_call


def test_a_paused_sub_run_is_detected_from_the_sdk_store() -> None:
    from agents.agent_tool_state import drop_agent_tool_run_result, record_agent_tool_run_result
    from openai.types.responses import ResponseFunctionToolCall

    call = ResponseFunctionToolCall(
        type="function_call", id="fc1", call_id="c1", name="ask", arguments="{}",
    )
    ctx = _Ctx(call)
    assert seam.agent_tool_run_pending(ctx) is False
    record_agent_tool_run_result(call, object())
    try:
        assert seam.agent_tool_run_pending(ctx) is True
    finally:
        drop_agent_tool_run_result(call)


def test_the_capture_record_names_the_obstacle_and_is_not_servable() -> None:
    from novafabric.adapters._adk_tool_seam import _Pending
    from novafabric.capture import _tool_codec as codec

    tool = _helper().as_tool("ask", "Ask.", on_stream=_on_stream)
    call = seam.describe_call(tool, None, _Ctx(None), '{"input": "x"}', "unknown",
                              wrapped=False)
    assert call.agent_tool is True
    hook = seam.OaiToolHook.__new__(seam.OaiToolHook)
    hook._parent_span_id = "span"
    pending = _Pending(call, seam._pending)
    nested = pending.close()
    record = hook.build_record(pending, nested, value="answer")
    ext = record["extensions"]
    assert ext[codec.RESULT_CODEC_EXT] == codec.CODEC_NOT_SERVABLE
    assert "sets on_stream" in ext[codec.NOT_SERVABLE_REASON_EXT]
