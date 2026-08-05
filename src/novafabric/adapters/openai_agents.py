"""NovaFabric tracing processor for OpenAI Agents SDK.

Registers a ``TracingProcessor`` that captures every agent run as a nova
capsule.  Wire-level HTTP hooks record raw model call bytes alongside the
structured span data the SDK emits.

``openai-agents`` is an **optional** dependency — this module is importable
even when not installed.  :func:`register` raises :class:`ImportError` at
call time if the package is missing.
"""
from __future__ import annotations

import json
import platform
import time
from datetime import datetime, timezone
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

import yaml

from novafabric.capture.env import capture_environment
from novafabric.capture.secrets import SecretScannerV0

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
        from novafabric.capture.capsule import CapsuleWriter
        from novafabric.capture.hooks import install_all

        run_id = new_ulid()
        span_id = new_span_id()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        writer = CapsuleWriter(run_id=run_id, base_dir=self._data_dir)
        writer.open()
        created_at = _now()
        t0 = time.monotonic()
        hook_token = install_all(writer=writer, parent_span_id=span_id)
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
        _wire_state = wire_capture_state(hook_token)
        uninstall_all(hook_token)

        finished_at = _now()
        duration_ms = int((time.monotonic() - t0) * 1000)
        cap_dir = writer.capsule_dir
        workflow_name = getattr(trace, "workflow_name", "run")

        writer.append_trace_span({
            "span_id": span_id,
            "parent_span_id": None,
            "name": f"novafabric.adapter.openai_agents.{workflow_name}",
            "started_at": created_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": "ok",
            "attributes": {"framework": "openai-agents", "trace_id": trace.trace_id},
        })

        env_lock = capture_environment(created_at=created_at, run_id=run_id)
        writer.write_text("env.lock", yaml.dump(env_lock, allow_unicode=True))
        scanner = SecretScannerV0(capsule_dir=cap_dir, run_id=run_id)
        proof = scanner.scan_and_redact()
        writer.write_text("redaction-proof.json", json.dumps(proof, indent=2))
        writer.write_text("replay.yaml", yaml.dump(minimal_replay_policy(), allow_unicode=True))

        manifest: dict[str, Any] = {
            "schema_version": "1.0.0",
            "run_id": run_id,
            "created_at": created_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": "success",
            "command": [f"@openai-agents:{workflow_name}"],
            "capture_mode": "sdk-decorator",
            "novafabric_version": _pkg_version("novafabric"),
            "working_directory": str(Path.cwd()).replace(str(Path.home()), "~"),
            "host": {
                "os": platform.system().lower(),
                "arch": "x86_64",
                "python": platform.python_version(),
                "cpu_count": 1,
                "memory_bytes": 0,
                "gpu": [],
                "hostname_redacted": True,
            },
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
            "model_call_count": _count_jsonl(cap_dir / "model-calls.jsonl"),
            "tool_call_count": _count_jsonl(cap_dir / "tool-calls.jsonl"),
            "mutating_tool_count": 0,
            "exit_code": 0,
            "metadata": {"framework": "openai-agents", "wire_capture": _wire_state},
        }
        writer.write_text("capsule.yaml", yaml.dump(manifest, allow_unicode=True))

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

    import os
    resolved = data_dir or Path(
        os.environ.get("NOVAFABRIC_HOME", str(Path.cwd() / ".novafabric"))
    ) / "runs"
    processor = NovaCapsuleTracingProcessor(resolved)
    add_trace_processor(processor)
    return processor
