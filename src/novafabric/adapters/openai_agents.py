"""NovaFabric tracing processor for OpenAI Agents SDK.

Registers a ``TracingProcessor`` that captures every agent run as a nova
capsule.  Wire-level HTTP hooks record raw model call bytes alongside the
structured span data the SDK emits.

``openai-agents`` is an **optional** dependency — this module is importable
even when not installed.  :func:`register` raises :class:`ImportError` at
call time if the package is missing.
"""
from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from novafabric.capture.env import capture_environment, host_info
from novafabric.capture.finalize import finalize_in_process_capsule
from novafabric.capture.record_roles import count_logical_model_calls_in_file
from novafabric.capture.teardown import TeardownSteps, novafabric_version, open_capsule_writer

try:
    from agents.tracing import TracingProcessor as _TracingProcessor
except ImportError:
    _TracingProcessor = object  # type: ignore[assignment, misc]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip())


class NovaCapsuleTracingProcessor(_TracingProcessor):
    """OpenAI Agents SDK TracingProcessor that writes a Nova capsule per trace."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        # trace_id -> (writer, run_id, span_id, created_at, t0)
        self._active: dict[str, tuple[Any, str, str, str, float, str]] = {}

    def on_trace_start(self, trace: Any) -> None:
        from novafabric.capture._ulid import new_span_id, new_ulid
        from novafabric.capture.hooks import install_all_or_discard

        run_id = new_ulid()
        span_id = new_span_id()
        writer = open_capsule_writer(self._data_dir, run_id, hook="adapter-openai_agents")
        if writer is None:  # an unusable capsule store: this trace runs uncaptured
            return
        created_at = _now()
        t0 = time.monotonic()
        hook_token = install_all_or_discard(writer=writer, parent_span_id=span_id)
        # Keyed per trace: this processor handles concurrent traces, so the
        # ownership token must travel with the trace, not with the processor.
        self._active[trace.trace_id] = (writer, run_id, span_id, created_at, t0, hook_token)

    def on_trace_end(self, trace: Any) -> None:
        from novafabric.capture.hooks import uninstall_all, wire_capture_state
        from novafabric.capture.replay import minimal_replay_policy

        entry = self._active.pop(trace.trace_id, None)
        if entry is None:
            return
        writer, run_id, span_id, created_at, t0, hook_token = entry
        # eval-adapters-query-04: teardown never replaces the call's outcome.
        steps = TeardownSteps("adapter-openai_agents")
        _wire_state = steps.run(
            "wire-state", lambda: wire_capture_state(hook_token), "unknown"
        )
        steps.do("hooks", lambda: uninstall_all(hook_token))

        finished_at = _now()
        duration_ms = int((time.monotonic() - t0) * 1000)
        cap_dir = writer.capsule_dir
        workflow_name = getattr(trace, "workflow_name", "run")

        steps.do("trace", lambda: writer.append_trace_span({
            "span_id": span_id,
            "parent_span_id": None,
            "name": f"novafabric.adapter.openai_agents.{workflow_name}",
            "started_at": created_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": "ok",
            "attributes": {"framework": "openai-agents", "trace_id": trace.trace_id},
        }))

        steps.do("env.lock", lambda: writer.write_text("env.lock", yaml.dump(
            capture_environment(created_at=created_at, run_id=run_id), allow_unicode=True
        )))
        steps.do("replay.yaml", lambda: writer.write_text(
            "replay.yaml", yaml.dump(minimal_replay_policy(), allow_unicode=True)
        ))
        model_call_count = steps.run(
            "model-calls",
            lambda: count_logical_model_calls_in_file(cap_dir / "model-calls.jsonl"),
            0,
        )
        tool_call_count = steps.run(
            "tool-calls", lambda: _count_jsonl(cap_dir / "tool-calls.jsonl"), 0
        )
        host: dict[str, Any] = steps.run("host", host_info, {})

        manifest: dict[str, Any] = {
            "schema_version": "1.0.0",
            "run_id": run_id,
            "created_at": created_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": "success",
            "command": [f"@openai-agents:{workflow_name}"],
            "capture_mode": "sdk-decorator",
            "novafabric_version": novafabric_version(),
            "working_directory": str(Path.cwd()).replace(str(Path.home()), "~"),
            "host": host,
            "environment_ref": "env.lock",
            "replay_policy_ref": "replay.yaml",
            "redaction_proof_ref": "redaction-proof.json",
            "trace_ref": "trace.jsonl",
            "trace_root_span_id": span_id,
            "model_calls_ref": "model-calls.jsonl",
            "tool_calls_ref": "tool-calls.jsonl",
            "assets_ref": "assets.jsonl",
            "inputs": [],
            "outputs": [],
            "model_call_count": model_call_count,
            "tool_call_count": tool_call_count,
            "mutating_tool_count": 0,
            "exit_code": 0,
            "metadata": {"framework": "openai-agents", "wire_capture": _wire_state},
        }
        # Main scan (after replay.yaml), manifest redaction, lineage, residual
        # pass, evidence_digests, gate and opt-in seal: the path `nova capture`
        # uses. Never raises; a failure — or a failed step above — leaves the capsule
        # unsealed and says why in metadata.finalization_error.
        finalize_in_process_capsule(
            cap_dir, manifest, run_id=run_id, writer=writer,
            incomplete_reason=steps.incomplete_reason,
        )

    def on_span_start(self, span: Any) -> None:
        pass  # wire hooks capture HTTP bytes; spans provide supplementary metadata

    def on_span_end(self, span: Any) -> None:
        # ADR-0209 D2.1 (experimental): the SDK emits typed guardrail span
        # data natively — map it onto the capsule's guardrail_events.jsonl
        # stream via the record façade. Duck-typed and fail-open: any
        # attribute error means no event, never an exception into the SDK.
        # No other span types are consumed in v0.
        try:
            span_data = getattr(span, "span_data", None)
            if span_data is None or getattr(span_data, "type", None) != "guardrail":
                return
            from novafabric.capture import record

            name = getattr(span_data, "name", None)
            triggered = bool(getattr(span_data, "triggered", False))
            record.guardrail(
                guardrail_name=name if isinstance(name, str) and name else "guardrail",
                outcome="blocked" if triggered else "passed",
            )
        except Exception:
            pass  # fail-open (ADR-0021: never block the workload)

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass


def register(data_dir: Path | None = None) -> NovaCapsuleTracingProcessor:
    """Register a NovaFabric tracing processor with the OpenAI Agents SDK.

    Call once at application startup before any agent runs.

    Args:
        data_dir: Base directory for capsules.  Defaults to
            ``$NOVAFABRIC_HOME/runs`` or ``.novafabric/runs`` under CWD.

    Returns:
        The registered processor (keep reference to deregister if needed).

    Raises:
        ImportError: If ``openai-agents`` is not installed.

    Usage::

        from novafabric.adapters.openai_agents import register
        register()
        result = await Runner.run(agent, "hello")
    """
    try:
        from agents.tracing import add_trace_processor
    except ImportError:
        raise ImportError(
            "openai-agents is not installed. "
            "Install it with: pip install 'novafabric[openai-agents]'"
        )

    from novafabric._paths import adapter_default_runs_dir

    resolved = data_dir or adapter_default_runs_dir()
    processor = NovaCapsuleTracingProcessor(resolved)
    add_trace_processor(processor)
    return processor


def wrap_tools(
    tools: Iterable[Any],
    mutation_classes: Mapping[str, str] | None = None,
    default_mutation_class: str = "unknown",
) -> list[Any]:
    """Route OpenAI Agents SDK function tools through NovaFabric's tool seam.

    **Experimental** (ADR-0306 slice 3). Returns *tools* with every
    ``FunctionTool`` replaced by a copy whose ``on_invoke_tool`` consults the
    NovaFabric handler first. Under ``nova capture python …`` each call becomes
    one ``tool-calls.jsonl`` record; under a mocked ``nova replay`` of that
    capsule the recorded result is returned and the tool body never runs (an
    unmatched or unservable call is refused, fail closed). Outside capture and
    replay the copies behave exactly like the originals.

    The seam replaces ``FunctionTool.on_invoke_tool``, which the SDK does not
    document as an extension point; it was verified against ``openai-agents``
    0.17.x for ``Runner.run`` and ``run_streamed``, parallel calls,
    ``Agent.clone`` and ``Agent.as_tool``, and a different SDK release may
    break it. Hosted tools and MCP servers pass through unchanged.

    Only capsules with a re-runnable command (``nova capture python …``) can be
    replayed: capsules written by :func:`register` carry a pseudo-command and
    are refused by ``nova replay`` (ADR-0306 open question 7).

    Args:
        tools: The agent's tools.
        mutation_classes: ADR-0012 mutation class per tool name. It gates
            ``--permissive`` and ``replay.yaml`` ``allow: true`` at replay, and
            is declared here, in the workload's code -- never read from a capsule.
        default_mutation_class: Class of every tool not named above.

    Raises:
        ValueError: If a mutation class is not an ADR-0012 class.

    Usage::

        from novafabric.adapters.openai_agents import wrap_tools
        agent = Agent(name="a", tools=wrap_tools([lookup_order], {"lookup_order": "read-only"}))
    """
    from novafabric.adapters._oai_tool_seam import wrap_tools as _wrap_tools

    return _wrap_tools(tools, mutation_classes, default_mutation_class)
