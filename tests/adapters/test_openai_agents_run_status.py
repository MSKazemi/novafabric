"""The OpenAI Agents capsule says how the run ended (review eval-adapters-query-14).

The processor stamped every capsule ``status: success`` / ``exit_code: 0`` and never
read a span's error, so a run that ended in ``MaxTurnsExceeded``, a tripped guardrail
or an exception escaping the agent looked successful to ``nova query --where status =
failure`` and every dashboard. The SDK records those three on the **agent** span
(``run.py``/``run_loop.py`` "Max turns exceeded", ``run_internal/guardrails.py``
"Guardrail tripwire triggered" on the parent agent span, ``run_internal/error_handlers.py``
for an escaping exception). A tool error is fed back to the model and the run goes on,
so it is counted, not a failure.

Real SDK ``TraceImpl``/``SpanImpl`` objects bound to the processor alone — no global
tracing provider, no model.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

pytest.importorskip("agents")

from agents.tracing.span_data import AgentSpanData, FunctionSpanData  # noqa: E402
from agents.tracing.spans import SpanError, SpanImpl  # noqa: E402
from agents.tracing.traces import TraceImpl  # noqa: E402

from novafabric.adapters.openai_agents import NovaCapsuleTracingProcessor  # noqa: E402


def _run(
    tmp_path: Path, spans: list[tuple[Any, SpanError | None]]
) -> dict[str, Any]:
    processor = NovaCapsuleTracingProcessor(tmp_path)
    with (
        patch("novafabric.adapters.openai_agents.capture_environment", return_value={}),
        patch("novafabric.capture.hooks.install_all"),
        patch("novafabric.capture.hooks.uninstall_all"),
    ):
        trace = TraceImpl("wf", None, None, None, processor)
        trace.start()
        for data, error in spans:
            span = SpanImpl(trace.trace_id, None, None, processor, data, None)
            span.start()
            if error is not None:
                span.set_error(error)
            span.finish()
        trace.finish()
    (manifest_path,) = tmp_path.rglob("capsule.yaml")
    manifest: dict[str, Any] = yaml.safe_load(manifest_path.read_text())
    return manifest


def _agent() -> AgentSpanData:
    return AgentSpanData(name="triage")


def _tool() -> FunctionSpanData:
    return FunctionSpanData(name="lookup", input="{}", output=None)


def test_a_clean_run_is_a_success(tmp_path: Path) -> None:
    manifest = _run(tmp_path, [(_tool(), None), (_agent(), None)])
    assert (manifest["status"], manifest["exit_code"]) == ("success", 0)
    assert "error" not in manifest
    assert manifest["command"] == ["@openai-agents:wf"]  # the SDK trace's name


@pytest.mark.parametrize(
    "message",
    ["Max turns exceeded", "Guardrail tripwire triggered", "Error in agent run"],
)
def test_an_error_on_the_agent_span_fails_the_run(tmp_path: Path, message: str) -> None:
    manifest = _run(tmp_path, [(_agent(), SpanError(message=message, data={"x": 1}))])
    assert (manifest["status"], manifest["exit_code"]) == ("failure", 1)
    assert manifest["error"]["type"] == "AgentRunError"
    assert manifest["error"]["message"] == message
    assert manifest["metadata"]["span_errors"] == {"agent": 1}


def test_a_tool_error_the_run_survived_is_counted_not_a_failure(tmp_path: Path) -> None:
    manifest = _run(
        tmp_path,
        [(_tool(), SpanError(message="Error running tool", data=None)), (_agent(), None)],
    )
    assert (manifest["status"], manifest["exit_code"]) == ("success", 0)
    assert manifest["metadata"]["span_errors"] == {"function": 1}


def test_concurrent_traces_keep_their_errors_apart(tmp_path: Path) -> None:
    processor = NovaCapsuleTracingProcessor(tmp_path)
    with (
        patch("novafabric.adapters.openai_agents.capture_environment", return_value={}),
        patch("novafabric.capture.hooks.install_all"),
        patch("novafabric.capture.hooks.uninstall_all"),
    ):
        failing = TraceImpl("bad", None, None, None, processor)
        clean = TraceImpl("good", None, None, None, processor)
        failing.start()
        clean.start()
        span = SpanImpl(failing.trace_id, None, None, processor, _agent(), None)
        span.start()
        span.set_error(SpanError(message="Max turns exceeded", data=None))
        span.finish()
        clean.finish()
        failing.finish()
    by_command = {
        m["command"][0]: m
        for m in (yaml.safe_load(p.read_text()) for p in tmp_path.rglob("capsule.yaml"))
    }
    assert by_command["@openai-agents:bad"]["status"] == "failure"
    assert by_command["@openai-agents:good"]["status"] == "success"


def test_a_span_of_an_uncaptured_trace_leaves_no_state(tmp_path: Path) -> None:
    """A trace that never started capturing (e.g. an unusable capsule store) must not
    accumulate per-trace error state the processor would never release."""
    processor = NovaCapsuleTracingProcessor(tmp_path)
    span = SpanImpl("trace_never_started", None, None, processor, _agent(), None)
    span.start()
    span.set_error(SpanError(message="Max turns exceeded", data=None))
    span.finish()
    assert processor._span_errors == {} and processor._run_error == {}
