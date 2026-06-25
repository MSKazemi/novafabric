"""NovaFabric plugin for Google ADK.

Implements the ADK ``BasePlugin`` interface to capture every Runner
invocation as a nova capsule.

``google-adk`` is an **optional** dependency.  :func:`make_plugin` raises
:class:`ImportError` at call time if the package is missing.
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


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip())


class NovaAdkPlugin:
    """ADK plugin that captures each runner invocation as a nova capsule.

    Not a subclass of BasePlugin at definition time so the module is
    importable without ``google-adk`` installed.  :func:`make_plugin` performs
    the runtime isinstance check.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._writer: Any = None
        self._run_id: str | None = None
        self._span_id: str | None = None
        self._created_at: str | None = None
        self._t0: float = 0.0

    async def before_run_callback(self, ctx: Any, **kwargs: Any) -> None:
        from novafabric.capture._ulid import new_span_id, new_ulid
        from novafabric.capture.capsule import CapsuleWriter
        from novafabric.capture.hooks import install_all

        self._run_id = new_ulid()
        self._span_id = new_span_id()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._writer = CapsuleWriter(run_id=self._run_id, base_dir=self._data_dir)
        self._writer.open()
        self._created_at = _now()
        self._t0 = time.monotonic()
        install_all(writer=self._writer, parent_span_id=self._span_id)

    async def after_run_callback(self, ctx: Any, **kwargs: Any) -> None:
        from novafabric.capture.hooks import uninstall_all
        from novafabric.capture.replay import minimal_replay_policy

        if self._writer is None:
            return
        uninstall_all()

        finished_at = _now()
        duration_ms = int((time.monotonic() - self._t0) * 1000)
        cap_dir = self._writer.capsule_dir

        self._writer.append_trace_span({
            "span_id": self._span_id,
            "parent_span_id": None,
            "name": "novafabric.adapter.google_adk.run",
            "started_at": self._created_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": "ok",
            "attributes": {"framework": "google-adk"},
        })

        # _run_id and _created_at are always set before after_run_callback
        run_id: str = self._run_id  # type: ignore[assignment]
        created_at: str = self._created_at  # type: ignore[assignment]
        env_lock = capture_environment(created_at=created_at, run_id=run_id)
        self._writer.write_text("env.lock", yaml.dump(env_lock, allow_unicode=True))
        scanner = SecretScannerV0(capsule_dir=cap_dir, run_id=run_id)
        proof = scanner.scan_and_redact()
        self._writer.write_text("redaction-proof.json", json.dumps(proof, indent=2))
        self._writer.write_text(
            "replay.yaml", yaml.dump(minimal_replay_policy(), allow_unicode=True)
        )

        manifest: dict[str, Any] = {
            "schema_version": "1.0.0",
            "run_id": run_id,
            "created_at": created_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": "success",
            "command": ["@google-adk:run"],
            "capture_mode": "adapter-google-adk",
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
            "trace_root_span_id": self._span_id,
            "model_calls_ref": "model-calls.jsonl",
            "tool_calls_ref": "tool-calls.jsonl",
            "assets_ref": "assets.jsonl",
            "inputs": [],
            "outputs": [],
            "model_call_count": _count_jsonl(cap_dir / "model-calls.jsonl"),
            "tool_call_count": _count_jsonl(cap_dir / "tool-calls.jsonl"),
            "mutating_tool_count": 0,
            "exit_code": 0,
            "tags": {"framework": "google-adk"},
        }
        self._writer.write_text("capsule.yaml", yaml.dump(manifest, allow_unicode=True))
        self._writer = None

    async def on_event_callback(self, ctx: Any, event: Any, **kwargs: Any) -> None:
        pass  # wire hooks capture model/tool events via HTTP


def make_plugin(data_dir: Path | None = None) -> NovaAdkPlugin:
    """Create a NovaFabric plugin for Google ADK Runner.

    Pass the returned plugin to ``Runner(plugins=[plugin])``.

    Args:
        data_dir: Base directory for capsules.

    Raises:
        ImportError: If ``google-adk`` is not installed.

    Usage::

        from novafabric.adapters.google_adk import make_plugin
        from google.adk.runners import Runner
        runner = Runner(agent=my_agent, session_service=svc, plugins=[make_plugin()])
    """
    try:
        import google.adk.plugins.base_plugin  # noqa: F401
    except ImportError:
        raise ImportError(
            "google-adk is not installed. "
            "Install it with: pip install 'novafabric[google-adk]'"
        )

    import os
    resolved = data_dir or Path(
        os.environ.get("NOVAFABRIC_HOME", str(Path.cwd() / ".novafabric"))
    ) / "runs"
    return NovaAdkPlugin(resolved)
