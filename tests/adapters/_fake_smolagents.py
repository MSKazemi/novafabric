"""A minimal stand-in for ``smolagents`` 1.26.0, faithful to the parts the adapter touches.

smolagents is not a NovaFabric dependency. This reproduces the contract that was read from the
real package (see ``tests/adapters/test_smolagents.py``, which runs the same scenarios against
the real library when it is installed, so a drift between this fake and the library shows up
there): ``CallbackRegistry.callback`` semantics (one-parameter callbacks get only the step,
others also ``agent=``), a lazy ``run(stream=True)`` generator, ``interrupt_switch``,
``managed_agents`` invoked through ``__call__`` -> ``run``, and a max-steps answer that is
*returned*, not raised.
"""
from __future__ import annotations

import inspect
import sys
import time
import types
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import Any


class AgentError(Exception):
    pass


class AgentToolExecutionError(AgentError):
    pass


class AgentMaxStepsError(AgentError):
    pass


@dataclass
class Timing:
    start_time: float
    end_time: float | None = None


@dataclass
class ToolCall:
    name: str
    arguments: Any
    id: str


@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class MemoryStep:
    pass


@dataclass
class ActionStep(MemoryStep):
    step_number: int = 0
    timing: Timing = field(default_factory=lambda: Timing(time.time()))
    tool_calls: list[ToolCall] | None = None
    error: Exception | None = None
    observations: str | None = None
    token_usage: TokenUsage | None = None
    is_final_answer: bool = False
    model_output_message: Any = None


@dataclass
class PlanningStep(MemoryStep):
    plan: str = ""
    timing: Timing = field(default_factory=lambda: Timing(time.time()))
    token_usage: TokenUsage | None = None


@dataclass
class FinalAnswerStep(MemoryStep):
    output: Any = None


class CallbackRegistry:
    def __init__(self) -> None:
        self._callbacks: dict[type, list[Callable[..., Any]]] = {}

    def register(self, step_cls: type, callback: Callable[..., Any]) -> None:
        self._callbacks.setdefault(step_cls, []).append(callback)

    def callback(self, memory_step: Any, **kwargs: Any) -> None:
        for cls in memory_step.__class__.__mro__:
            for cb in self._callbacks.get(cls, []):
                cb(memory_step) if len(inspect.signature(cb).parameters) == 1 else cb(
                    memory_step, **kwargs
                )


@dataclass
class _Memory:
    steps: list[Any] = field(default_factory=list)


class MultiStepAgent:
    """Runs a *script*: a list of steps, each a dict.

    ``{"call": (tool, args)}`` -- a tool-calling step; a tool that raises becomes the step's
    ``error`` and the loop continues (as the real agent does for AgentError subclasses).
    ``{"plan": text}`` -- a planning step. ``{"final": value}`` -- the final answer.
    ``{"raise": exc}`` -- an exception that escapes the run (a model/implementation error).
    ``{"on_step": fn}`` may be added to any step to run code at the start of that step.
    """

    def __init__(
        self,
        script: list[dict[str, Any]],
        tools: dict[str, Callable[..., Any]] | None = None,
        managed_agents: list[Any] | None = None,
        step_callbacks: list[Callable[..., Any]] | None = None,
        name: str | None = None,
        max_steps: int = 20,
    ) -> None:
        self.script, self.tools = list(script), dict(tools or {})
        self.managed_agents = {a.name: a for a in managed_agents or []}
        self.name = name
        self.max_steps = max_steps
        self.memory = _Memory()
        self.interrupt_switch = False
        self.step_number = 0
        self.step_callbacks = CallbackRegistry()
        for cb in step_callbacks or []:
            self.step_callbacks.register(ActionStep, cb)

    def interrupt(self) -> None:
        self.interrupt_switch = True

    def __call__(self, task: str, **kwargs: Any) -> str:
        return f"[{self.name}] {self.run(task, **kwargs)}"

    def run(
        self,
        task: str,
        stream: bool = False,
        reset: bool = True,
        images: Any = None,
        additional_args: dict[str, Any] | None = None,
        max_steps: int | None = None,
        return_full_result: bool | None = None,
    ) -> Any:
        self.interrupt_switch = False
        if additional_args:
            {}.update(additional_args)  # the real run() does self.state.update(...): a non-mapping raises here
        if reset:
            self.memory.steps.clear()
        if stream:
            return self._run_stream(task)
        steps = list(self._run_stream(task))
        assert isinstance(steps[-1], FinalAnswerStep)
        return steps[-1].output

    def _finalize(self, step: Any) -> None:
        self.step_callbacks.callback(step, agent=self)

    def _run_stream(self, task: str) -> Generator[Any, None, None]:
        script = iter(self.script)
        self.step_number = 1
        final: Any = None
        while self.step_number <= self.max_steps:
            if self.interrupt_switch:
                raise AgentError("Agent interrupted.")
            entry = next(script, None)
            if entry is None:
                break
            if "on_step" in entry:
                entry["on_step"](self)
            if "plan" in entry:
                plan = PlanningStep(plan=entry["plan"], token_usage=TokenUsage(3, 4))
                plan.timing.end_time = time.time()
                self._finalize(plan)
                self.memory.steps.append(plan)
                yield plan
                continue
            if "raise" in entry:
                raise entry["raise"]
            step = ActionStep(step_number=self.step_number, token_usage=TokenUsage(10, 5))
            try:
                if "final" in entry:
                    step.model_output_message = _message("final_answer", {"answer": entry["final"]})
                    step.tool_calls = [ToolCall("final_answer", {"answer": entry["final"]}, "c0")]
                    step.is_final_answer, final = True, entry["final"]
                else:
                    name, args = entry["call"]
                    step.model_output_message = _message(name, args)
                    try:
                        impl = self.managed_agents.get(name) or self.tools[name]
                        step.observations = str(impl(**args) if isinstance(args, dict) else impl(args))
                        # Like the real agent, tool_calls is filled only once the call has run.
                        step.tool_calls = [ToolCall(name, args, f"c{self.step_number}")]
                    except Exception as exc:  # the real agent turns tool failures into a step error
                        raise AgentToolExecutionError(f"{name} failed: {exc}") from exc
            except AgentError as exc:
                step.error = exc
            finally:
                step.timing.end_time = time.time()
                self._finalize(step)
                self.memory.steps.append(step)
                yield step
                self.step_number += 1
            if step.is_final_answer:
                break
        else:
            final = "max steps reached"
            err = ActionStep(step_number=self.step_number, error=AgentMaxStepsError("max steps"))
            self.memory.steps.append(err)
        done = FinalAnswerStep(output=final)
        self._finalize(done)
        yield done


def _message(name: str, arguments: Any) -> Any:
    function = types.SimpleNamespace(name=name, arguments=arguments)
    return types.SimpleNamespace(tool_calls=[types.SimpleNamespace(id="m", function=function)])


class CodeAgent(MultiStepAgent):
    pass


class ToolCallingAgent(MultiStepAgent):
    pass


def install(monkeypatch: Any) -> types.ModuleType:
    """Put the fake in ``sys.modules`` as ``smolagents`` and ``smolagents.memory``."""
    pkg = types.ModuleType("smolagents")
    memory = types.ModuleType("smolagents.memory")
    for cls in (ActionStep, PlanningStep, FinalAnswerStep, ToolCall, CallbackRegistry, MemoryStep):
        setattr(memory, cls.__name__, cls)
    for cls in (MultiStepAgent, CodeAgent, ToolCallingAgent):
        setattr(pkg, cls.__name__, cls)
    pkg.memory = memory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "smolagents", pkg)
    monkeypatch.setitem(sys.modules, "smolagents.memory", memory)
    return pkg
