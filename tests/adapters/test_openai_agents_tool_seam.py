"""ADR-0306 slice 3 (OpenAI Agents SDK): the ``wrap_tools`` seam in isolation.

No subprocess and no model: the invoker is driven directly, with a recording
handler registered where capture or replay would register one. The end-to-end
proof through ``Runner`` lives in ``tests/replay/test_openai_agents_tool_replay_e2e.py``.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("agents")

from agents import Agent, FunctionTool, function_tool  # noqa: E402
from agents.tool_context import ToolContext  # noqa: E402

from novafabric.adapters import _oai_tool_seam as seam  # noqa: E402
from novafabric.adapters.openai_agents import wrap_tools  # noqa: E402

CALLS: list[str] = []


@function_tool
def add(a: int, b: int) -> int:
    CALLS.append("add")
    return a + b


@function_tool
def explode() -> str:
    CALLS.append("explode")
    raise ValueError("boom")


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


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    CALLS.clear()
    previous = seam._set_handler(None)
    yield
    seam._set_handler(previous)


def _invoke(tool: FunctionTool, args: str = '{"a": 1, "b": 2}') -> Any:
    ctx = ToolContext(
        context=None, tool_name=tool.name, tool_call_id="call-1", tool_arguments=args
    )
    return asyncio.run(tool.on_invoke_tool(ctx, args))


def test_wrap_tools_copies_and_leaves_the_original_alone() -> None:
    (wrapped,) = wrap_tools([add])
    assert wrapped is not add
    assert not hasattr(add.on_invoke_tool, seam._MARKER)
    assert getattr(wrapped.on_invoke_tool, seam._MARKER) is True
    assert wrapped.name == add.name


def test_without_a_handler_the_wrapper_only_forwards() -> None:
    (wrapped,) = wrap_tools([add])
    assert _invoke(wrapped) == _invoke(add) == 3
    assert CALLS == ["add", "add"]


def test_wrapping_twice_does_not_stack_wrappers() -> None:
    (once,) = wrap_tools([add])
    (twice,) = wrap_tools([once])
    assert twice is once


def test_a_served_answer_short_circuits_the_tool_body() -> None:
    handler = Recorder(serve="SERVED")
    seam._set_handler(handler)
    (wrapped,) = wrap_tools([add])
    assert _invoke(wrapped) == "SERVED"
    assert CALLS == []
    (call,) = handler.calls
    assert call.tool_name == "add"
    assert call.arguments == {"a": 1, "b": 2}
    assert call.function_call_id == "call-1"
    assert call.mutation_class == "unknown"


def test_a_served_none_is_still_a_served_answer() -> None:
    seam._set_handler(Recorder(serve=None))  # `serve=None` means "run the tool"...
    handler = Recorder()
    handler.before = lambda call: seam.Served(None)  # type: ignore[method-assign]
    seam._set_handler(handler)
    (wrapped,) = wrap_tools([add])
    assert _invoke(wrapped) is None
    assert CALLS == []


def test_a_refusal_propagates_before_the_body_runs() -> None:
    seam._set_handler(Recorder(refuse=RuntimeError("refused")))
    (wrapped,) = wrap_tools([add])
    with pytest.raises(RuntimeError, match="refused"):
        _invoke(wrapped)
    assert CALLS == []


def test_a_live_call_reports_its_result_to_the_handler() -> None:
    handler = Recorder()
    seam._set_handler(handler)
    (wrapped,) = wrap_tools([add])
    assert _invoke(wrapped) == 3
    assert CALLS == ["add"]
    assert handler.after_results == [3]


def test_the_sdk_failure_handler_still_runs_inside_the_seam() -> None:
    handler = Recorder()
    seam._set_handler(handler)
    (wrapped,) = wrap_tools([explode])
    out = _invoke(wrapped, "{}")
    assert "error occurred" in out  # the SDK turned the raise into a model-visible string
    assert handler.after_results == [out]
    assert handler.errors == []


def test_an_exception_that_escapes_the_sdk_is_reported_and_reraised() -> None:
    @function_tool(failure_error_function=None)
    def strict() -> str:
        raise ValueError("escapes")

    handler = Recorder()
    seam._set_handler(handler)
    (wrapped,) = wrap_tools([strict])
    with pytest.raises(ValueError, match="escapes"):
        _invoke(wrapped, "{}")
    assert [type(e) for e in handler.errors] == [ValueError]
    assert handler.after_results == []


def test_malformed_arguments_are_reported_not_canonical() -> None:
    handler = Recorder()
    seam._set_handler(handler)
    (wrapped,) = wrap_tools([add])
    _invoke(wrapped, "[1, 2]")  # valid JSON, not an object: the SDK rejects it itself
    (call,) = handler.calls
    assert call.arguments is None
    assert call.not_canonical is not None and "not an object" in call.not_canonical
    assert call.digest is None


def test_mutation_classes_are_declared_in_code_and_validated() -> None:
    handler = Recorder()
    seam._set_handler(handler)
    (wrapped,) = wrap_tools([add], {"add": "read-only"})
    _invoke(wrapped)
    assert handler.calls[0].mutation_class == "read-only"
    with pytest.raises(ValueError, match="mutation class"):
        wrap_tools([add], {"add": "bogus"})
    with pytest.raises(ValueError, match="mutation class"):
        wrap_tools([add], default_mutation_class="bogus")


def test_non_function_tools_and_uncallable_invokers_pass_through() -> None:
    sentinel = object()
    broken = copy.copy(add)
    broken.on_invoke_tool = None  # type: ignore[assignment]
    out = wrap_tools([sentinel, broken])
    assert out[0] is sentinel
    assert out[1] is broken


def test_the_seam_survives_the_sdk_copying_the_tool() -> None:
    handler = Recorder(serve="SERVED")
    seam._set_handler(handler)
    (wrapped,) = wrap_tools([add])
    clone = copy.copy(wrapped)  # the SDK rebinds the invoker to the copy
    assert clone.on_invoke_tool is not wrapped.on_invoke_tool
    assert getattr(clone.on_invoke_tool, seam._MARKER) is True
    assert _invoke(clone) == "SERVED"
    agent = Agent(name="a", tools=[wrapped]).clone(name="b")
    assert _invoke(agent.tools[0]) == "SERVED"  # type: ignore[arg-type]
    assert CALLS == []


def test_sdk_invoker_attributes_remain_reachable_through_the_wrapper() -> None:
    (wrapped,) = wrap_tools([add])
    # approval preparation reads `prepare_arguments` off the invoker
    assert callable(wrapped.on_invoke_tool.prepare_arguments)  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        wrapped.on_invoke_tool.__not_an_sdk_name__  # type: ignore[attr-defined]  # noqa: B018


def test_a_record_tool_function_is_a_boundary_of_its_own() -> None:
    from novafabric.capture import record

    @record.tool(mutation_class="none")
    def inner(a: int, b: int) -> int:
        CALLS.append("inner")
        return a + b

    declared = function_tool(inner)
    handler = Recorder(serve="SERVED")
    seam._set_handler(handler)
    (wrapped,) = wrap_tools([declared])
    assert _invoke(wrapped) == 3
    assert handler.calls == [], "the seam must step aside for a record.tool function"
