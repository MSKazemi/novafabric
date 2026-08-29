"""Shared capsule-writing core for framework adapters.

Every adapter does the same thing around a framework's call path: open a
capsule, claim the wire hooks, run the framework, then write the manifest and
supporting files. The eleven adapters that predate this module each carry their
own copy of that body.

New adapters use this instead. It is deliberately **additive** — no existing
adapter was changed to adopt it, because rewriting eleven working, tested
adapters is a blast radius that buys nothing today.

The manifest literal lives here, so
``tests/adapters/test_adapter_manifests_match_the_schema.py`` still checks it:
that guard globs the whole adapters directory, and this file is in it.

ADR-0224: the hook token must be passed to ``wire_capture_state`` *before*
``uninstall_all``, and the resulting state stamped into ``metadata`` — a capsule
has to say whether its wire stream is complete.
"""
from __future__ import annotations

import json
import os
import platform
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

import yaml

# Module-level so tests can patch via ``novafabric.adapters._capsule.<name>``.
from novafabric.capture.env import capture_environment
from novafabric.capture.secrets import SecretScannerV0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def resolve_data_dir(data_dir: Path | None) -> Path:
    """Where capsules go when the caller does not say."""
    if data_dir is not None:
        return Path(data_dir)
    return Path(
        os.environ.get("NOVAFABRIC_HOME", str(Path.cwd() / ".novafabric"))
    ) / "runs"


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip())


@dataclass
class AdapterCapture:
    """One in-flight adapter capture.

    Split into ``begin`` / ``finish`` rather than a context manager because the
    async adapters need to await the framework call between the two halves.
    """

    framework: str
    run_name: str
    cap_dir: Path
    run_id: str
    root_span_id: str
    writer: Any
    hook_token: str
    created_at: str
    t0: float
    tags: dict[str, str] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    exit_code: int = 0

    def fail(self, exc: BaseException) -> None:
        """Record *exc* as the run's failure. Never swallows it."""
        self.exit_code = 1
        self.error = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback_ref": None,
        }

    def finish(self) -> None:
        """Release the hooks and write the capsule. Safe to call in ``finally``."""
        from novafabric.capture.hooks import uninstall_all, wire_capture_state

        wire_state = wire_capture_state(self.hook_token)
        uninstall_all(self.hook_token)
        self._write(wire_state)

    def _write(self, wire_state: str) -> None:
        from novafabric.capture.replay import minimal_replay_policy

        finished_at = _now()
        duration_ms = int((time.monotonic() - self.t0) * 1000)
        status = "success" if self.exit_code == 0 else "failure"
        tags = {**self.tags, "framework": self.framework, "wire_capture": wire_state}

        self.writer.append_trace_span({
            "span_id": self.root_span_id,
            "parent_span_id": None,
            "name": f"novafabric.adapter.{self.framework}.{self.run_name}",
            "started_at": self.created_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": "ok" if self.exit_code == 0 else "error",
            "attributes": {"run_name": self.run_name, **tags},
        })

        env_lock = capture_environment(created_at=self.created_at, run_id=self.run_id)
        self.writer.write_text("env.lock", yaml.dump(env_lock, allow_unicode=True))

        scanner = SecretScannerV0(capsule_dir=self.cap_dir, run_id=self.run_id)
        self.writer.write_text(
            "redaction-proof.json", json.dumps(scanner.scan_and_redact(), indent=2)
        )
        self.writer.write_text(
            "replay.yaml", yaml.dump(minimal_replay_policy(), allow_unicode=True)
        )

        manifest: dict[str, Any] = {
            "schema_version": "0.1.0",
            "run_id": self.run_id,
            "created_at": self.created_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": status,
            "command": [f"@{self.framework}:{self.run_name}"],
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
            "trace_root_span_id": self.root_span_id,
            "model_calls_ref": "model-calls.jsonl",
            "tool_calls_ref": "tool-calls.jsonl",
            "assets_ref": "assets.jsonl",
            "inputs": [],
            "outputs": [],
            "model_call_count": _count_lines(self.cap_dir / "model-calls.jsonl"),
            "tool_call_count": _count_lines(self.cap_dir / "tool-calls.jsonl"),
            "mutating_tool_count": 0,
            "exit_code": self.exit_code,
            "metadata": tags,
        }
        if self.error:
            manifest["error"] = self.error

        self.writer.write_text("capsule.yaml", yaml.dump(manifest, allow_unicode=True))


def begin_capture(
    *, framework: str, run_name: str, data_dir: Path | None
) -> AdapterCapture:
    """Open a capsule and claim the wire hooks for one framework invocation."""
    from novafabric.capture._ulid import new_span_id, new_ulid
    from novafabric.capture.capsule import CapsuleWriter
    from novafabric.capture.hooks import install_all

    resolved = resolve_data_dir(data_dir)
    resolved.mkdir(parents=True, exist_ok=True)

    run_id = new_ulid()
    root_span_id = new_span_id()
    writer = CapsuleWriter(run_id=run_id, base_dir=resolved)
    writer.open()

    return AdapterCapture(
        framework=framework,
        run_name=run_name,
        cap_dir=writer.capsule_dir,
        run_id=run_id,
        root_span_id=root_span_id,
        writer=writer,
        hook_token=install_all(writer=writer, parent_span_id=root_span_id),
        created_at=_now(),
        t0=time.monotonic(),
    )


def require(module: str, install_hint: str) -> None:
    """Raise a useful ImportError when the target framework is absent."""
    import importlib

    try:
        importlib.import_module(module)
    except ImportError:
        raise ImportError(
            f"{module} is not installed. Install it with: pip install {install_hint}"
        ) from None
