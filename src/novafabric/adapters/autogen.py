"""NovaFabric adapter for AutoGen (pyautogen / autogen-agentchat).

Wraps a ``ConversableAgent`` so that each ``initiate_chat()`` call
creates a nova run capsule.  Wire-level HTTP hooks capture every model
call automatically.

AutoGen is an **optional** dependency — this module must be importable
even when ``autogen`` / ``pyautogen`` is not installed.  The
:func:`wrap_agent` function will raise :class:`ImportError` at call time
if the framework is missing.
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

# Module-level imports so tests can patch via ``novafabric.adapters.autogen.<name>``.
from novafabric.capture.env import capture_environment
from novafabric.capture.secrets import SecretScannerV0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _write_capsule(
    *,
    run_name: str,
    data_dir: Path,
    tags: dict[str, str],
    error: dict[str, Any] | None,
    t0: float,
    created_at: str,
    exit_code: int,
    writer: Any,
    root_span_id: str,
    run_id: str,
    cap_dir: Path,
) -> None:
    from novafabric.capture.replay import minimal_replay_policy

    finished_at = _now()
    duration_ms = int((time.monotonic() - t0) * 1000)
    status = "success" if exit_code == 0 else "failure"

    writer.append_trace_span({
        "span_id": root_span_id,
        "parent_span_id": None,
        "name": f"novafabric.adapter.autogen.{run_name}",
        "started_at": created_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "status": "ok" if exit_code == 0 else "error",
        "attributes": {"run_name": run_name, **tags},
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

    manifest: dict[str, Any] = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "created_at": created_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "status": status,
        "command": [f"@autogen:{run_name}"],
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
        "metadata": tags,
    }
    if error:
        manifest["error"] = error

    writer.write_text("capsule.yaml", yaml.dump(manifest, allow_unicode=True))


def wrap_agent(
    agent: Any,
    *,
    run_name: str | None = None,
    data_dir: Path | None = None,
) -> Any:
    """Wrap an AutoGen ``ConversableAgent`` for NovaFabric capture.

    Patches the agent's ``initiate_chat`` method in-place so that each
    conversation creates a nova run capsule.  The original agent object is
    returned (same identity) so existing references continue to work.

    Args:
        agent: An ``autogen.ConversableAgent`` (or any subclass).
        run_name: Human-readable name stored in the capsule manifest.
            Defaults to the agent's ``name`` attribute, or ``"autogen-run"``.
        data_dir: Base directory for capsules.  Defaults to
            ``$NOVAFABRIC_HOME/runs`` or ``.novafabric/runs`` under CWD.

    Raises:
        ImportError: If ``autogen`` / ``pyautogen`` is not installed.

    Usage::

        from novafabric.adapters.autogen import wrap_agent
        agent = wrap_agent(agent, run_name="my-agent-chat")
        agent.initiate_chat(recipient, message="hello")
    """
    try:
        import autogen  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        raise ImportError(
            "autogen / pyautogen is not installed. "
            "Install it with: pip install pyautogen"
        )

    import os

    resolved_data_dir = data_dir or Path(
        os.environ.get("NOVAFABRIC_HOME", str(Path.cwd() / ".novafabric"))
    ) / "runs"

    resolved_name: str = run_name or getattr(agent, "name", None) or "autogen-run"
    tags: dict[str, str] = {"framework": "autogen"}

    original_initiate = agent.initiate_chat

    def wrapped_initiate(recipient: Any, **kwargs: Any) -> Any:
        from novafabric.capture._ulid import new_span_id, new_ulid
        from novafabric.capture.capsule import CapsuleWriter
        from novafabric.capture.hooks import install_all, uninstall_all, wire_capture_state

        run_id = new_ulid()
        root_span_id = new_span_id()

        resolved_data_dir.mkdir(parents=True, exist_ok=True)
        writer = CapsuleWriter(run_id=run_id, base_dir=resolved_data_dir)
        writer.open()
        cap_dir = writer.capsule_dir

        _hook_token = install_all(writer=writer, parent_span_id=root_span_id)
        created_at = _now()
        t0 = time.monotonic()
        exit_code = 0
        error: dict[str, Any] | None = None
        result: Any = None
        try:
            result = original_initiate(recipient, **kwargs)
        except Exception as exc:
            exit_code = 1
            error = {"type": type(exc).__name__, "message": str(exc), "traceback_ref": None}
            raise
        finally:
            _wire_state = wire_capture_state(_hook_token)
            uninstall_all(_hook_token)
            _write_capsule(
                run_name=resolved_name,
                data_dir=resolved_data_dir,
                tags={**tags, "wire_capture": _wire_state},
                error=error,
                t0=t0,
                created_at=created_at,
                exit_code=exit_code,
                writer=writer,
                root_span_id=root_span_id,
                run_id=run_id,
                cap_dir=cap_dir,
            )
        return result

    agent.initiate_chat = wrapped_initiate
    return agent
