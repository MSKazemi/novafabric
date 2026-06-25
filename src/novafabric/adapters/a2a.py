"""NovaFabric interceptor for the A2A (Agent-to-Agent) SDK.

Implements ``ClientCallInterceptor`` to capture every ``send_message`` /
``send_message_streaming`` call as a nova capsule.  The A2A task envelope
(task id, sessionId, input message parts, status) is written to
``a2a-tasks.jsonl`` inside the capsule.

Per RFC-0002 §Q4, full A2A protocol-aware capture was deferred until the
spec stabilised.  This adapter is the implementation of that deferred work
against A2A SDK 1.0.x.

``a2a-sdk`` is an **optional** dependency.  :func:`make_interceptor` raises
:class:`ImportError` at call time if not installed.
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

_CAPTURE_METHODS = frozenset({"send_message", "send_message_streaming"})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip())


class NovaA2AInterceptor:
    """A2A ClientCallInterceptor that captures send_message calls as capsules.

    Not a subclass of ClientCallInterceptor at definition time so the module
    is importable without ``a2a-sdk`` installed.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        # method_key -> (writer, run_id, span_id, created_at, t0, agent_name)
        self._pending: dict[str, tuple[Any, str, str, str, float, str]] = {}

    async def before(self, args: Any) -> None:
        if args.method not in _CAPTURE_METHODS:
            return

        from novafabric.capture._ulid import new_span_id, new_ulid
        from novafabric.capture.capsule import CapsuleWriter
        from novafabric.capture.hooks import install_all

        run_id = new_ulid()
        span_id = new_span_id()
        key = f"{args.method}:{run_id}"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        writer = CapsuleWriter(run_id=run_id, base_dir=self._data_dir)
        writer.open()
        created_at = _now()
        t0 = time.monotonic()
        agent_name = getattr(args.agent_card, "name", "unknown-agent")
        install_all(writer=writer, parent_span_id=span_id)

        # Write A2A task input
        task_log = writer.capsule_dir / "a2a-tasks.jsonl"
        with task_log.open("a") as f:
            f.write(json.dumps({"direction": "request", "method": args.method,
                                 "agent": agent_name, "input": args.input}) + "\n")

        # Store key on args so after() can match
        args._nova_key = key
        self._pending[key] = (writer, run_id, span_id, created_at, t0, agent_name)

    async def after(self, args: Any) -> None:
        if args.method not in _CAPTURE_METHODS:
            return
        key = getattr(args, "_nova_key", None)
        # Fallback: find by method (last pending for this method)
        if key is None:
            for k in list(self._pending):
                if k.startswith(f"{args.method}:"):
                    key = k
                    break
        if key is None:
            return

        entry = self._pending.pop(key, None)
        if entry is None:
            return
        writer, run_id, span_id, created_at, t0, agent_name = entry

        from novafabric.capture.hooks import uninstall_all
        from novafabric.capture.replay import minimal_replay_policy
        uninstall_all()

        # Write A2A task output
        task_log = writer.capsule_dir / "a2a-tasks.jsonl"
        with task_log.open("a") as f:
            f.write(json.dumps({"direction": "response", "method": args.method,
                                 "agent": agent_name, "result": args.result}) + "\n")

        finished_at = _now()
        duration_ms = int((time.monotonic() - t0) * 1000)
        cap_dir = writer.capsule_dir

        writer.append_trace_span({
            "span_id": span_id,
            "parent_span_id": None,
            "name": f"novafabric.adapter.a2a.{agent_name}",
            "started_at": created_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": "ok",
            "attributes": {"framework": "a2a", "agent": agent_name, "method": args.method},
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
            "command": [f"@a2a:{agent_name}"],
            "capture_mode": "adapter-a2a",
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
            "a2a_tasks_ref": "a2a-tasks.jsonl",
            "inputs": [],
            "outputs": [],
            "model_call_count": _count_jsonl(cap_dir / "model-calls.jsonl"),
            "tool_call_count": _count_jsonl(cap_dir / "tool-calls.jsonl"),
            "mutating_tool_count": 0,
            "exit_code": 0,
            "tags": {"framework": "a2a", "agent": agent_name},
        }
        writer.write_text("capsule.yaml", yaml.dump(manifest, allow_unicode=True))


def make_interceptor(data_dir: Path | None = None) -> NovaA2AInterceptor:
    """Create a NovaFabric interceptor for the A2A SDK client.

    Pass the returned interceptor to ``A2AClient(interceptors=[interceptor])``.

    Args:
        data_dir: Base directory for capsules.

    Raises:
        ImportError: If ``a2a-sdk`` is not installed.

    Usage::

        from novafabric.adapters.a2a import make_interceptor
        from a2a.client import A2AClient
        client = A2AClient(base_url="http://agent:8080",
                           interceptors=[make_interceptor()])
    """
    try:
        import a2a.client.interceptors  # noqa: F401
    except ImportError:
        raise ImportError(
            "a2a-sdk is not installed. "
            "Install it with: pip install 'novafabric[a2a]'"
        )

    import os
    resolved = data_dir or Path(
        os.environ.get("NOVAFABRIC_HOME", str(Path.cwd() / ".novafabric"))
    ) / "runs"
    return NovaA2AInterceptor(resolved)
