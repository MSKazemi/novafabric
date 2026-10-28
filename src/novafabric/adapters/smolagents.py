"""NovaFabric adapter for Hugging Face ``smolagents`` (HF-01; experimental, observe-only).

Wraps a ``smolagents`` agent (``CodeAgent``, ``ToolCallingAgent`` or any ``MultiStepAgent``)
so each user-visible ``run()`` writes **one** Run Capsule. Checked against smolagents 1.26.0.

What a capsule from this adapter holds
--------------------------------------
* one ``trace.jsonl`` span per agent step (action, planning, final answer): step number, type,
  timing, error *class*, token counts, tool-call count -- from the agent's own ``step_callbacks``;
* one ``tool-calls.jsonl`` record per tool call named in an action step, **at step granularity**:
  the framework reports the calls and one combined observation per step, not per-call results;
* the run's end status: ``success``, ``failure`` (the exception still propagates), or
  ``partial`` (a stream abandoned, ``interrupt()`` honoured, or the task cancelled).

Model calls are *not* written here. The HTTP/SDK layer records them (wire hooks), and writing a
second copy from the step callback would double-count every logical call.

What it does not do
-------------------
* It does not orchestrate, retry or alter an agent: ``run`` is forwarded with its arguments,
  return value (including ``RunResult``), generator laziness and exceptions unchanged.
* Prompts, model output, generated code and observations are **not stored** unless the capture
  level already allows payloads (``NOVA_CAPTURE_LEVEL=forensic``); the default keeps digests.
* **Tool calls are observed, never served.** Records carry the ``smolagents.tool`` surface
  marker, which replay does not intercept, and are marked not-servable: a mocked replay does not
  stand in for a smolagents tool. A ``CodeAgent`` step shows as one ``python_interpreter`` call;
  the tools that generated code calls are not individually visible.
* Managed (sub-)agents are recorded as spans inside the parent's capsule, not as capsules of
  their own, whichever thread runs them.

smolagents is an **optional** dependency: this module imports without it and
:func:`wrap_agent` raises :class:`ImportError` at call time.
"""
from __future__ import annotations

import contextvars
import functools
import hashlib
import inspect
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novafabric.adapters._capsule import AdapterCapture, begin_capture, require
from novafabric.adapters._streaming import (
    ABANDONED,
    CANCELLED,
    Closer,
    close_when_collected,
    guard_iterator,
)
from novafabric.capture import _tool_codec as codec
from novafabric.capture._ulid import new_span_id, new_ulid
from novafabric.capture.hooks._fail_open import TOOL_CALLS_STREAM, fail_open

FRAMEWORK = "smolagents"
#: Deliberately absent from ``replay._contract._PYTHON_TRANSPORT_SURFACES``: replay never
#: intercepts these records, so no smolagents tool is ever mocked.
TOOL_SURFACE = "smolagents.tool"
NOT_SERVABLE_REASON = (
    "smolagents tool calls are observed at step granularity; their results are not "
    "individually captured or replay-served"
)
_MARK = "_novafabric_smolagents_wrapped"
TRACE_STREAM = "trace.jsonl"

#: Set while a captured ``run`` is executing in this context; a nested ``run`` of a *non*-managed
#: agent records into the open capsule rather than opening a second one.
_in_flight: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "novafabric_smolagents_in_flight", default=False
)


def _iso(epoch: float | None) -> str:
    stamp = datetime.fromtimestamp(epoch if epoch is not None else time.time(), timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()


def _payloads_enabled() -> bool:
    from novafabric.capture.record import _payloads_enabled as enabled

    return enabled()


class _RunState:
    """What the step callbacks of one wrapped agent tree write into."""

    def __init__(self) -> None:
        self.cap: AdapterCapture | None = None
        self.steps = 0
        self.lock = threading.Lock()

    def reset(self, cap: Any) -> None:
        with self.lock:
            self.cap = cap if isinstance(cap, AdapterCapture) else None
            self.steps = 0


class _Handle:
    """Adapts one in-flight run to the ``fail / mark_partial / finish`` contract of
    :class:`~novafabric.adapters._streaming.Closer`, and adds what only smolagents knows."""

    def __init__(self, cap: Any, agent: Any, state: _RunState, managed: list[str]) -> None:
        self._cap, self._agent, self._state, self._managed = cap, agent, state, managed

    def fail(self, exc: BaseException) -> None:
        # ``interrupt()`` makes the next step raise AgentError: the caller asked to stop, so
        # that is a cancelled run, not a failed one. The exception still propagates.
        if getattr(self._agent, "interrupt_switch", False) is True:
            self._cap.mark_partial(CANCELLED)
        else:
            self._cap.fail(exc)

    def mark_partial(self, reason: str) -> None:
        self._cap.mark_partial(reason)

    def finish(self) -> None:
        try:
            self._tag()
        finally:
            self._state.reset(None)
            self._cap.finish()

    def _tag(self) -> None:
        tags = self._cap.tags
        tags["agent_class"] = type(self._agent).__name__
        tags["step_count"] = str(self._state.steps)
        if self._managed:
            tags["managed_agents"] = ",".join(self._managed)
        # A smolagents run that hit max_steps *returns* a final answer; saying so keeps the
        # capsule's `success` from reading as "the task was solved".
        steps = getattr(getattr(self._agent, "memory", None), "steps", None) or []
        last_error = getattr(steps[-1], "error", None) if steps else None
        if last_error is not None and type(last_error).__name__ == "AgentMaxStepsError":
            tags["agent_state"] = "max_steps_error"


def _wants_stream(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    # run(task, stream=False, reset=True, ...): `stream` is the second positional parameter.
    if "stream" in kwargs:
        return bool(kwargs["stream"])
    return bool(args[1]) if len(args) > 1 else False


def wrap_agent(
    agent: Any,
    *,
    run_name: str | None = None,
    data_dir: Path | None = None,
) -> Any:
    """Wrap a smolagents agent so every ``run`` writes one Run Capsule; returns the same object.

    Args:
        agent: A ``smolagents.CodeAgent`` / ``ToolCallingAgent`` / ``MultiStepAgent``.
        run_name: Name recorded in the manifest. Defaults to the agent's ``name``, then its class.
        data_dir: Base directory for capsules (default: the NovaFabric runs directory).

    Raises:
        ImportError: If ``smolagents`` is not installed.

    Usage::

        from novafabric.adapters.smolagents import wrap_agent
        agent = wrap_agent(agent, run_name="support-bot")
        answer = agent.run("Is SKU-1042 in stock?")

    Wrapping an already-wrapped agent is a no-op. Managed agents in ``agent.managed_agents`` are
    wrapped too (recursively) and record as spans inside this agent's capsule.
    """
    require("smolagents", "smolagents")
    if getattr(agent, _MARK, False):
        return agent
    from smolagents.memory import (  # type: ignore[import-not-found,unused-ignore]
        ActionStep,
        FinalAnswerStep,
        PlanningStep,
    )

    state = _RunState()
    name = run_name or getattr(agent, "name", None) or type(agent).__name__ or "smolagents-run"
    managed = _walk_managed(agent)
    for sub in managed.values():
        _instrument_steps(sub, state, (ActionStep, PlanningStep, FinalAnswerStep), nested=True)
        _wrap_managed_run(sub, state)
    _instrument_steps(agent, state, (ActionStep, PlanningStep, FinalAnswerStep), nested=False)
    _wrap_root_run(agent, state, name, data_dir, sorted(managed))
    setattr(agent, _MARK, True)
    return agent


def _walk_managed(root: Any) -> dict[str, Any]:
    """Every managed agent below *root*, by name; each object once (a cycle cannot loop)."""
    found: dict[str, Any] = {}
    seen: set[int] = {id(root)}
    queue = [root]
    while queue:
        parent = queue.pop()
        children = getattr(parent, "managed_agents", None) or {}
        if not isinstance(children, dict):
            continue
        for key, sub in children.items():
            if id(sub) in seen:
                continue
            seen.add(id(sub))
            found[str(getattr(sub, "name", None) or key)] = sub
            queue.append(sub)
    return found


def _instrument_steps(
    agent: Any, state: _RunState, step_types: tuple[type, ...], *, nested: bool
) -> None:
    registry = getattr(agent, "step_callbacks", None)
    register = getattr(registry, "register", None)
    if not callable(register):
        return  # no CallbackRegistry (older smolagents): no step spans; run capture still works
    label = str(getattr(agent, "name", None) or type(agent).__name__)

    def on_step(step: Any, agent: Any = None) -> None:
        cap = state.cap
        if cap is None:
            return
        with fail_open(FRAMEWORK, TRACE_STREAM):
            _record_step(cap, state, step, agent_label=label, managed=nested, owner=agent)

    for step_type in step_types:
        register(step_type, on_step)


def _record_step(
    cap: AdapterCapture, state: _RunState, step: Any, *, agent_label: str, managed: bool, owner: Any
) -> None:
    kinds = {"ActionStep": "action", "PlanningStep": "planning", "FinalAnswerStep": "final_answer"}
    kind = kinds.get(type(step).__name__, type(step).__name__)
    timing = getattr(step, "timing", None)
    start = getattr(timing, "start_time", None)
    end = getattr(timing, "end_time", None) or time.time()
    error = getattr(step, "error", None)
    attrs: dict[str, Any] = {"agent": agent_label, "step_type": kind, "managed_agent": managed}
    number = getattr(step, "step_number", None)
    if isinstance(number, int):
        attrs["step_number"] = number
    tool_calls, requested_only = _calls_of(step)
    if kind == "action":
        attrs["tool_call_count"] = len(tool_calls)
        attrs["is_final_answer"] = bool(getattr(step, "is_final_answer", False))
        observations = getattr(step, "observations", None)
        if observations:
            attrs["observations_digest"] = _digest(observations)
    # smolagents finalises a step even when generation raised, and then leaves `error` unset
    # (it re-raises AgentGenerationError). An action step with no model output, no tool call
    # and no error never completed: say so rather than report it ok.
    incomplete = (
        kind == "action"
        and error is None
        and not tool_calls
        and not getattr(step, "is_final_answer", False)
        and getattr(step, "model_output_message", None) is None
    )
    if incomplete:
        attrs["incomplete"] = True
    if error is not None:
        # The class only: a message may quote generated code or data.
        attrs["error_type"] = type(error).__name__
    usage = getattr(step, "token_usage", None)
    for key in ("input_tokens", "output_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            attrs[key] = value
    if kind == "final_answer":
        output = getattr(step, "output", None)
        attrs["output_type"] = type(output).__name__
        attrs["output_digest"] = _digest(output)

    span_id = new_span_id()
    started = _iso(start if isinstance(start, (int, float)) else end)
    cap.writer.append_trace_span({
        "span_id": span_id,
        "parent_span_id": cap.root_span_id,
        "name": f"smolagents.step.{kind}",
        "started_at": started,
        "finished_at": _iso(end),
        "duration_ms": max(0, int(_span_seconds(timing) * 1000)),
        "status": "error" if error is not None or incomplete else "ok",
        "attributes": attrs,
    })
    if kind != "final_answer":  # the closing marker is not a step the agent took
        with state.lock:
            state.steps += 1
    managed_names = set(getattr(owner, "managed_agents", None) or {})
    for call in tool_calls:
        _record_tool_call(cap, span_id, call, step, error, managed_names, requested_only)


def _calls_of(step: Any) -> tuple[list[Any], bool]:
    """The tool calls of an action step, and whether they are only what the model *asked for*.

    smolagents fills ``step.tool_calls`` once a call has been parsed and run; a tool that raises
    leaves it ``None`` (verified against 1.26.0), so the failing call would otherwise vanish.
    The model's own message still names what it requested.
    """
    calls = list(getattr(step, "tool_calls", None) or [])
    if calls:
        return calls, False
    message = getattr(step, "model_output_message", None)
    requested = []
    for item in getattr(message, "tool_calls", None) or []:
        function = getattr(item, "function", None)
        requested.append(
            _Requested(
                name=getattr(function, "name", None),
                arguments=getattr(function, "arguments", None),
            )
        )
    return requested, bool(requested)


class _Requested:
    """A tool call read from the model's message rather than from ``step.tool_calls``."""

    def __init__(self, name: Any, arguments: Any) -> None:
        self.name, self.arguments = name, arguments


def _record_tool_call(
    cap: AdapterCapture,
    parent_span: str,
    call: Any,
    step: Any,
    error: Any,
    managed_names: set[str],
    requested_only: bool,
) -> None:
    with fail_open(FRAMEWORK, TOOL_CALLS_STREAM):
        payloads = _payloads_enabled()
        name = str(getattr(call, "name", None) or "unknown")
        arguments = getattr(call, "arguments", None)
        timing = getattr(step, "timing", None)
        started = _iso(getattr(timing, "start_time", None))
        finished = _iso(getattr(timing, "end_time", None))
        ext: dict[str, Any] = {
            codec.TOOL_SURFACE_EXT: TOOL_SURFACE,
            codec.RESULT_CODEC_EXT: codec.CODEC_NOT_SERVABLE,
            codec.NOT_SERVABLE_REASON_EXT: NOT_SERVABLE_REASON,
            "io.novafabric.smolagents.outcome_scope": "step",
        }
        if requested_only:
            # The call never completed (the tool raised): this is what the model asked for.
            ext["io.novafabric.smolagents.requested_only"] = True
        if name in managed_names:
            ext["io.novafabric.smolagents.managed_agent"] = True
        if name == "python_interpreter":
            ext["io.novafabric.smolagents.code_execution"] = True
        record_arguments: dict[str, Any] = {}
        if payloads and isinstance(arguments, dict):
            record_arguments = codec.json_native(arguments, "arguments") if arguments else {}
        else:
            ext[codec.ARGUMENTS_DIGEST_EXT] = codec.redacted_arguments_digest(arguments)
        cap.writer.append_tool_call({
            "schema_version": "0.1.0",
            "tool_call_id": new_ulid(),
            "parent_span_id": parent_span,
            "started_at": started,
            "finished_at": finished,
            "duration_ms": max(0, int(_span_seconds(timing) * 1000)),
            "tool_name": name,
            "tool_version": "unknown",
            "tool_provider": "python://smolagents",
            "transport": "python",
            "mutates": True,
            "mutation_class": "unknown",
            "arguments": record_arguments,
            "arguments_schema_ref": None,
            "result": None,
            "result_schema_ref": None,
            # The framework reports one outcome per step, so an error is attributed to every call
            # in it; `outcome_scope: step` says so.
            "status": "error" if error is not None else "success",
            "agent_call_id": None,
            "extensions": ext,
        })


def _span_seconds(timing: Any) -> float:
    start, end = getattr(timing, "start_time", None), getattr(timing, "end_time", None)
    return end - start if isinstance(start, (int, float)) and isinstance(end, (int, float)) else 0.0


def _wrap_managed_run(sub: Any, state: _RunState) -> None:
    original = sub.run

    @functools.wraps(original)
    def run(*args: Any, **kwargs: Any) -> Any:
        cap = state.cap
        if cap is None:
            return original(*args, **kwargs)  # called outside a captured parent run: not ours
        started, t0 = _iso(None), time.monotonic()
        status, error_type = "ok", None
        try:
            return original(*args, **kwargs)
        except BaseException as exc:
            status, error_type = "error", type(exc).__name__
            raise
        finally:
            with fail_open(FRAMEWORK, TRACE_STREAM):
                attrs: dict[str, Any] = {
                    "agent": str(getattr(sub, "name", None) or type(sub).__name__),
                    "managed_agent": True,
                }
                if error_type:
                    attrs["error_type"] = error_type
                cap.writer.append_trace_span({
                    "span_id": new_span_id(),
                    "parent_span_id": cap.root_span_id,
                    "name": "smolagents.managed_agent.run",
                    "started_at": started,
                    "finished_at": _iso(None),
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                    "status": status,
                    "attributes": attrs,
                })

    sub.run = run


def _wrap_root_run(
    agent: Any, state: _RunState, name: str, data_dir: Path | None, managed: list[str]
) -> None:
    original = agent.run

    @functools.wraps(original)
    def run(*args: Any, **kwargs: Any) -> Any:
        if _in_flight.get():
            return original(*args, **kwargs)
        cap = begin_capture(framework=FRAMEWORK, run_name=name, data_dir=data_dir)
        cap.tags["entry_point"] = "run"
        cap.tags["stream"] = str(_wants_stream(args, kwargs)).lower()
        _tag_version(cap)
        state.reset(cap)
        handle = _Handle(cap, agent, state, managed)
        streaming = _wants_stream(args, kwargs)
        token = _in_flight.set(True)
        try:
            result = original(*args, **kwargs)
        except BaseException as exc:
            _record_outcome(handle, exc)
            handle.finish()
            raise
        finally:
            _in_flight.reset(token)
        if streaming and inspect.isgenerator(result):
            closer = Closer(handle)
            guarded: Iterator[Any] = guard_iterator(result, closer, _in_flight)
            close_when_collected(guarded, closer)
            return guarded
        handle.finish()
        return result

    agent.run = run


def _record_outcome(handle: _Handle, exc: BaseException) -> None:
    if isinstance(exc, Exception):
        handle.fail(exc)
    else:  # KeyboardInterrupt / GeneratorExit / CancelledError: stopped, not failed
        handle.mark_partial(CANCELLED if type(exc).__name__ == "CancelledError" else ABANDONED)


def _tag_version(cap: Any) -> None:
    try:
        from importlib.metadata import version

        cap.tags["smolagents_version"] = version("smolagents")
    except Exception:  # noqa: BLE001 - a missing version string never matters to the run
        pass
