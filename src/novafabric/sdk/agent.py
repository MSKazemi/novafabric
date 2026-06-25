from __future__ import annotations

import functools
import json
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any, TypeVar

import yaml
from opentelemetry import trace

F = TypeVar("F", bound=Callable[..., Any])


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def agent(
    name: str,
    version: str,
    capsule_dir: Path | str | None = None,
) -> Callable[[F], F]:
    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = trace.get_tracer(__name__)
            with tracer.start_as_current_span(f"novafabric.agent.{name}") as span:
                span.set_attribute("gen_ai.agent.id", str(uuid.uuid4()))
                span.set_attribute("gen_ai.agent.name", name)
                span.set_attribute("gen_ai.agent.version", version)

                if capsule_dir is None:
                    return fn(*args, **kwargs)

                return _run_with_capture(fn, args, kwargs, name, version, Path(capsule_dir))

        return wrapper  # type: ignore[return-value]

    return decorator


def _run_with_capture(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    agent_name: str,
    agent_version: str,
    cap_dir: Path,
) -> Any:
    from novafabric.capture._ulid import new_span_id, new_ulid
    from novafabric.capture.capsule import CapsuleWriter
    from novafabric.capture.env import capture_environment
    from novafabric.capture.hooks import install_all, uninstall_all
    from novafabric.capture.replay import minimal_replay_policy
    from novafabric.capture.secrets import SecretScannerV0

    run_id = new_ulid()
    root_span_id = new_span_id()
    created_at = _now()
    t0 = time.monotonic()

    cap_dir.mkdir(parents=True, exist_ok=True)
    (cap_dir / "inputs").mkdir(exist_ok=True)
    (cap_dir / "outputs").mkdir(exist_ok=True)
    for fname in ("model-calls.jsonl", "tool-calls.jsonl", "trace.jsonl", "assets.jsonl"):
        (cap_dir / fname).touch()

    writer = CapsuleWriter(run_id=run_id, base_dir=cap_dir.parent)
    writer._dir = cap_dir

    install_all(writer=writer, parent_span_id=root_span_id)
    exit_code = 0
    error: dict[str, Any] | None = None
    result: Any = None
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        exit_code = 1
        error = {"type": type(exc).__name__, "message": str(exc), "traceback_ref": None}
        raise
    finally:
        uninstall_all()
        finished_at = _now()
        duration_ms = int((time.monotonic() - t0) * 1000)
        status = "success" if exit_code == 0 else "failure"

        writer.append_trace_span({
            "span_id": root_span_id,
            "parent_span_id": None,
            "name": f"novafabric.agent.{agent_name}",
            "started_at": created_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": "ok" if exit_code == 0 else "error",
            "attributes": {"agent_name": agent_name, "agent_version": agent_version},
        })

        env_lock = capture_environment(created_at=created_at, run_id=run_id)
        writer.write_text("env.lock", yaml.dump(env_lock, allow_unicode=True))

        scanner = SecretScannerV0(capsule_dir=cap_dir, run_id=run_id)
        proof = scanner.scan_and_redact()
        writer.write_text("redaction-proof.json", json.dumps(proof, indent=2))

        writer.write_text("replay.yaml", yaml.dump(minimal_replay_policy(), allow_unicode=True))

        model_call_count = sum(
            1
            for line in (cap_dir / "model-calls.jsonl").read_text().splitlines()
            if line.strip()
        )
        tool_call_count = sum(
            1
            for line in (cap_dir / "tool-calls.jsonl").read_text().splitlines()
            if line.strip()
        )

        import platform
        manifest: dict[str, Any] = {
            "schema_version": "0.1.0",
            "run_id": run_id,
            "created_at": created_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": status,
            "command": [f"@agent:{agent_name}@{agent_version}"],
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
            "trace_root_span_id": root_span_id,
            "model_calls_ref": "model-calls.jsonl",
            "tool_calls_ref": "tool-calls.jsonl",
            "assets_ref": "assets.jsonl",
            "inputs": [],
            "outputs": [],
            "model_call_count": model_call_count,
            "tool_call_count": tool_call_count,
            "mutating_tool_count": 0,
            "exit_code": exit_code,
        }
        if error:
            manifest["error"] = error

        writer.write_text("capsule.yaml", yaml.dump(manifest, allow_unicode=True))

    return result
