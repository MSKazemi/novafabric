"""ADR-0306 slice 3 (LangChain / LangGraph): the ``wrap_tools`` seam in isolation.

No subprocess and no model: tools are invoked directly, and through LangGraph's
``ToolNode`` inside a ``StateGraph``, with a recording handler registered where
capture or replay would register one. The end-to-end proof through real
``nova capture`` / ``nova replay`` lives in
``tests/replay/test_langchain_tool_replay_e2e.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Annotated, Any

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.callbacks import BaseCallbackHandler  # noqa: E402
from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langchain_core.runnables import RunnableConfig  # noqa: E402
from langchain_core.tools import (  # noqa: E402
    BaseTool,
    InjectedToolCallId,
    StructuredTool,
    ToolException,
    tool,
)
from langgraph.graph import START, MessagesState, StateGraph  # noqa: E402
from langgraph.prebuilt import InjectedState, ToolNode  # noqa: E402

from novafabric.adapters import _langchain_tool_seam as seam  # noqa: E402
from novafabric.adapters.langchain_tools import wrap_tools  # noqa: E402
from novafabric.capture.hooks import _BUILT_IN_HOOKS  # noqa: E402

CALLS: list[Any] = []


@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    CALLS.append(("add", a, b))
    return a + b


@tool
async def aadd(a: int, b: int) -> int:
    """Add two numbers, asynchronously."""
    CALLS.append(("aadd", a, b))
    return a + b


@tool
def with_config(x: int, config: RunnableConfig) -> str:
    """Reads its RunnableConfig."""
    CALLS.append(("with_config", x, "tags" in config))
    return f"cfg-{x}"


@tool
def injected(
    x: int,
    tool_call_id: Annotated[str, InjectedToolCallId],
    state: Annotated[dict, InjectedState],
) -> str:
    """Receives injected arguments the model never sends."""
    CALLS.append(("injected", x, tool_call_id, sorted(state)))
    return f"inj-{x}"


@tool
def explode() -> str:
    """Always raises."""
    CALLS.append(("explode",))
    raise ToolException("boom")


class Echo(BaseTool):
    name: str = "echo"
    description: str = "Echo the query."

    def _run(self, q: str, run_manager: Any = None) -> str:
        CALLS.append(("echo", q, run_manager is not None))
        return "echo-" + q


class Recorder:
    """Stand-in handler: remembers what it saw, optionally serves."""

    def __init__(self, serve: Any = None, refuse: Exception | None = None) -> None:
        self.calls: list[Any] = []
        self.after_results: list[Any] = []
        self.errors: list[BaseException] = []
        self._serve, self._refuse = serve, refuse

    def before(self, call: Any) -> Any:
        self.calls.append(call)
        if self._refuse is not None:
            raise self._refuse
        return seam.Served(self._serve) if self._serve is not None else None

    def after(self, tool_context: Any, result: Any) -> None:
        self.after_results.append(result)

    def on_error(self, tool_context: Any, error: BaseException) -> None:
        self.errors.append(error)


class Events(BaseCallbackHandler):
    def __init__(self) -> None:
        self.seen: list[str] = []

    def on_tool_start(self, serialized: Any, input_str: Any, **kwargs: Any) -> None:
        self.seen.append("start")

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        self.seen.append("end")


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    CALLS.clear()
    previous = seam._set_handler(None)
    yield
    seam._set_handler(previous)


def _tool_call(name: str, call_id: str = "call-1", **args: Any) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _graph(tools: list[Any]) -> Any:
    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "tools")
    return graph.compile()


# ── copies, identity, pass-through ───────────────────────────────────────────


def test_wrap_tools_copies_and_leaves_the_original_alone() -> None:
    (wrapped,) = wrap_tools([add])
    assert wrapped is not add
    assert type(add) is StructuredTool
    assert isinstance(wrapped, StructuredTool)
    assert type(wrapped) is not StructuredTool
    assert wrapped.name == add.name and wrapped.description == add.description
    # The model sees exactly the same tool.
    assert (
        wrapped.tool_call_schema.model_json_schema() == add.tool_call_schema.model_json_schema()
    )


def test_without_a_handler_the_copy_only_forwards() -> None:
    (w_add, w_cfg, w_echo) = wrap_tools([add, with_config, Echo()])
    assert w_add.invoke({"a": 1, "b": 2}) == add.invoke({"a": 1, "b": 2}) == 3
    assert w_cfg.invoke({"x": 1}, {"tags": ["t"]}) == "cfg-1"
    assert w_echo.invoke({"q": "z"}) == "echo-z"
    assert CALLS == [("add", 1, 2), ("add", 1, 2), ("with_config", 1, True), ("echo", "z", True)]


def test_wrapping_twice_does_not_stack_wrappers() -> None:
    (once,) = wrap_tools([add])
    (twice,) = wrap_tools([once])
    assert twice is once


def test_non_tools_pass_through() -> None:
    sentinel = object()

    def plain(x: int) -> int:
        return x

    out = wrap_tools([sentinel, plain])
    assert out[0] is sentinel and out[1] is plain


def test_seam_classes_are_cached_per_tool_class_and_mutation_class() -> None:
    a1, b1 = wrap_tools([add, with_config])
    (a2,) = wrap_tools([add])
    (a3,) = wrap_tools([add], {"add": "read-only"})
    assert type(a1) is type(b1) is type(a2)
    assert type(a3) is not type(a1)


# ── served answers short-circuit the body; LangChain still formats ────────────


def test_a_served_answer_short_circuits_the_tool_body() -> None:
    handler = Recorder(serve={"served": True})
    seam._set_handler(handler)
    (wrapped,) = wrap_tools([add])
    events = Events()
    out = wrapped.invoke(_tool_call("add", a=1, b=2), {"callbacks": [events]})
    assert CALLS == []
    # LangChain built the ToolMessage from the served body result, and fired callbacks.
    assert isinstance(out, ToolMessage)
    assert out.tool_call_id == "call-1" and out.content == '{"served": true}'
    assert events.seen == ["start", "end"]
    (call,) = handler.calls
    assert call.tool_name == "add"
    assert call.arguments == {"a": 1, "b": 2}
    assert call.function_call_id == "call-1"
    assert call.mutation_class == "unknown"


def test_a_plain_dict_input_has_no_call_id() -> None:
    handler = Recorder(serve=7)
    seam._set_handler(handler)
    (wrapped,) = wrap_tools([add])
    assert wrapped.invoke({"a": 1, "b": 2}) == 7
    assert handler.calls[0].function_call_id is None


@pytest.mark.parametrize(
    "target", ["async-tool", "sync-tool-via-ainvoke", "subclass-via-ainvoke"]
)
def test_ainvoke_is_served_once_before_the_body(target: str) -> None:
    handler = Recorder(serve="S")
    seam._set_handler(handler)
    w_aadd, w_add, w_echo = wrap_tools([aadd, add, Echo()])
    tool_, args = {
        "async-tool": (w_aadd, {"a": 1, "b": 2}),
        "sync-tool-via-ainvoke": (w_add, {"a": 1, "b": 2}),
        "subclass-via-ainvoke": (w_echo, {"q": "y"}),
    }[target]
    assert asyncio.run(tool_.ainvoke(args)) == "S"
    assert CALLS == []
    assert len(handler.calls) == 1, "one call must reach the handler exactly once"


def test_a_body_that_delegates_to_another_hooked_body_asks_once() -> None:
    """``arun`` on a sync ``StructuredTool``: its ``_arun`` hands the call to
    ``BaseTool._arun``, which runs ``_run`` in an executor -- both are hooked."""
    handler = Recorder(serve="S")
    seam._set_handler(handler)
    (wrapped,) = wrap_tools([add])
    assert asyncio.run(wrapped.arun({"a": 1, "b": 2})) == "S"
    assert len(handler.calls) == 1
    seam._set_handler(Recorder())
    assert asyncio.run(wrapped.arun({"a": 1, "b": 2})) == 3
    assert CALLS == [("add", 1, 2)]


def test_a_string_input_binds_to_the_first_argument() -> None:
    handler = Recorder(serve="S")
    seam._set_handler(handler)
    (wrapped,) = wrap_tools([Echo()])
    assert wrapped.invoke("hello") == "S"
    assert handler.calls[0].arguments == {"q": "hello"}


def test_a_served_none_is_still_a_served_answer() -> None:
    handler = Recorder()
    handler.before = lambda call: seam.Served(None)  # type: ignore[method-assign]
    seam._set_handler(handler)
    (wrapped,) = wrap_tools([add])
    assert wrapped.invoke({"a": 1, "b": 2}) is None
    assert CALLS == []


def test_a_refusal_propagates_before_the_body_runs() -> None:
    seam._set_handler(Recorder(refuse=RuntimeError("refused")))
    (wrapped,) = wrap_tools([add])
    with pytest.raises(RuntimeError, match="refused"):
        wrapped.invoke(_tool_call("add", a=1, b=2))
    assert CALLS == []


def test_a_live_call_reports_its_body_result_to_the_handler() -> None:
    handler = Recorder()
    seam._set_handler(handler)
    (wrapped,) = wrap_tools([add])
    out = wrapped.invoke(_tool_call("add", a=1, b=2))
    assert isinstance(out, ToolMessage) and out.content == "3"
    assert CALLS == [("add", 1, 2)]
    assert handler.after_results == [3], "the body's own result, not the ToolMessage"


def test_a_tool_exception_is_reported_as_an_error() -> None:
    handler = Recorder()
    seam._set_handler(handler)
    (wrapped,) = wrap_tools([explode])
    wrapped.handle_tool_error = True
    out = wrapped.invoke(_tool_call("explode"))
    assert isinstance(out, ToolMessage) and out.status == "error"
    assert [type(e) for e in handler.errors] == [ToolException]
    assert handler.after_results == []


def test_mutation_classes_are_declared_in_code_and_validated() -> None:
    handler = Recorder()
    seam._set_handler(handler)
    (wrapped,) = wrap_tools([add], {"add": "read-only"})
    wrapped.invoke({"a": 1, "b": 2})
    assert handler.calls[0].mutation_class == "read-only"
    with pytest.raises(ValueError, match="mutation class"):
        wrap_tools([add], {"add": "bogus"})
    with pytest.raises(ValueError, match="mutation class"):
        wrap_tools([add], default_mutation_class="bogus")


@pytest.mark.parametrize(
    ("tool_input", "why"),
    [({"q": object()}, "not JSON-representable"), (42, "not a dict")],
    ids=["object-argument", "not-a-dict"],
)
def test_non_json_input_is_reported_not_canonical(tool_input: Any, why: str) -> None:
    (wrapped,) = wrap_tools([Echo()])
    call = seam.describe_call(seam._Current(wrapped, tool_input, None), "unknown")
    assert call.arguments is None and call.digest is None
    assert call.not_canonical is not None and why in call.not_canonical


# ── LangGraph ToolNode: injected arguments, ids, sync and async ──────────────


@pytest.mark.parametrize("mode", ["invoke", "ainvoke"])
def test_tool_node_calls_reach_the_seam_with_the_model_ids(mode: str) -> None:
    handler = Recorder(serve="S")
    seam._set_handler(handler)
    app = _graph(wrap_tools([add, aadd, injected, Echo()]))
    message = AIMessage("", tool_calls=[
        {"name": "add", "args": {"a": 1, "b": 2}, "id": "c1"},
        {"name": "aadd", "args": {"a": 3, "b": 4}, "id": "c2"},
        {"name": "injected", "args": {"x": 5}, "id": "c3"},
        {"name": "echo", "args": {"q": "r"}, "id": "c4"},
    ])
    if mode == "invoke":
        message.tool_calls = [c for c in message.tool_calls if c["name"] != "aadd"]
        out = app.invoke({"messages": [message]})
    else:
        out = asyncio.run(app.ainvoke({"messages": [message]}))
    replies = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert {m.tool_call_id: m.content for m in replies} == {
        c["id"]: "S" for c in message.tool_calls
    }
    assert CALLS == []
    by_name = {c.tool_name: c for c in handler.calls}
    assert {name: c.function_call_id for name, c in by_name.items()} == {
        c["name"]: c["id"] for c in message.tool_calls
    }
    # ToolNode injected `state` and `tool_call_id`; matching uses what the model sent.
    assert by_name["injected"].arguments == {"x": 5}
    assert by_name["injected"].ignored == ("state", "tool_call_id")


def test_tool_node_runs_live_tools_unchanged_without_a_handler() -> None:
    app = _graph(wrap_tools([add, injected]))
    message = AIMessage("", tool_calls=[
        {"name": "add", "args": {"a": 1, "b": 2}, "id": "c1"},
        {"name": "injected", "args": {"x": 5}, "id": "c3"},
    ])
    out = app.invoke({"messages": [message]})
    assert [m.content for m in out["messages"] if isinstance(m, ToolMessage)] == ["3", "inj-5"]
    assert ("injected", 5, "c3", ["messages"]) in CALLS


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_create_react_agent_reaches_the_seam() -> None:
    """LangGraph's prebuilt agent builds its own ``ToolNode`` from the list it is given."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langgraph.prebuilt import create_react_agent

    class FakeModel(GenericFakeChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            return self

    model = FakeModel(messages=iter([
        AIMessage("", tool_calls=[{"name": "add", "args": {"a": 1, "b": 2}, "id": "c9"}]),
        AIMessage("done"),
    ]))
    handler = Recorder(serve="S")
    seam._set_handler(handler)
    out = create_react_agent(model, wrap_tools([add])).invoke({"messages": [("user", "go")]})
    (reply,) = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert (reply.content, reply.tool_call_id) == ("S", "c9")
    assert CALLS == []
    assert [c.function_call_id for c in handler.calls] == ["c9"]


# ── stepping aside ───────────────────────────────────────────────────────────


def test_a_record_tool_function_is_a_boundary_of_its_own() -> None:
    from novafabric.capture import record

    @record.tool(mutation_class="none")
    def inner(a: int, b: int) -> int:
        """Declared with record.tool."""
        CALLS.append("inner")
        return a + b

    declared = tool(inner)
    (wrapped,) = wrap_tools([declared])
    assert wrapped is declared, "the seam must step aside for a record.tool function"


def test_mcp_adapter_tools_are_left_to_the_mcp_surface() -> None:
    async def call_tool(**kwargs: Any) -> tuple[str, None]:
        return "x", None

    call_tool.__module__ = "langchain_mcp_adapters.tools"
    mcp_tool = StructuredTool(
        name="remote", description="An MCP tool.", args_schema={"type": "object"},
        coroutine=call_tool, response_format="content_and_artifact",
    )
    (out,) = wrap_tools([mcp_tool])
    assert out is mcp_tool


def test_the_capture_sink_is_a_built_in_hook() -> None:
    assert (
        "langchain_core", "novafabric.adapters._langchain_tool_seam", "LangChainToolHook"
    ) in _BUILT_IN_HOOKS
