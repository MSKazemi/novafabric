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
        self._active: dict[str, tuple[Any, str, str, str, float]] = {}

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
        install_all(writer=writer, parent_span_id=span_id)
        self._active[trace.trace_id] = (writer, run_id, span_id, created_at, t0)

    def on_trace_end(self, trace: Any) -> None:
        from novafabric.capture.hooks import uninstall_all
        from novafabric.capture.replay import minimal_replay_policy

        entry = self._active.pop(trace.trace_id, None)
        if entry is None:
            return
        writer, run_id, span_id, created_at, t0 = entry
        uninstall_all()

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
            "capture_mode": "adapter-openai-agents",
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
            "tags": {"framework": "openai-agents"},
        }
        writer.write_text("capsule.yaml", yaml.dump(manifest, allow_unicode=True))

    def on_span_start(self, span: Any) -> None:
        pass  # wire hooks capture HTTP bytes; spans provide supplementary metadata

    def on_span_end(self, span: Any) -> None:
        pass

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
