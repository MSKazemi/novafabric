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

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from novafabric._capsule_version import CAPSULE_SCHEMA_VERSION
from novafabric.capture.env import capture_environment, host_info
from novafabric.capture.finalize import finalize_in_process_capsule

# Module-level imports so tests can patch via ``novafabric.adapters.autogen.<name>``.
from novafabric.capture.record_roles import count_logical_model_calls_in_file
from novafabric.capture.teardown import TeardownSteps, novafabric_version, open_capsule_writer


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
    steps: TeardownSteps,
) -> None:
    """Write the capsule. Never raises: a failed step is recorded, not propagated."""
    from novafabric.capture.replay import minimal_replay_policy

    finished_at = _now()
    duration_ms = int((time.monotonic() - t0) * 1000)
    status = "success" if exit_code == 0 else "failure"

    steps.do("trace", lambda: writer.append_trace_span({
        "span_id": root_span_id,
        "parent_span_id": None,
        "name": f"novafabric.adapter.autogen.{run_name}",
        "started_at": created_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "status": "ok" if exit_code == 0 else "error",
        "attributes": {"run_name": run_name, **tags},
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
    tool_call_count = steps.run("tool-calls", lambda: sum(
        1
        for line in (cap_dir / "tool-calls.jsonl").read_text().splitlines()
        if line.strip()
    ), 0)
    host: dict[str, Any] = steps.run("host", host_info, {})

    manifest: dict[str, Any] = {
        "schema_version": CAPSULE_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": created_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "status": status,
        "command": [f"@autogen:{run_name}"],
        "capture_mode": "sdk-decorator",
        "novafabric_version": novafabric_version(),
        "working_directory": str(Path.cwd()).replace(str(Path.home()), "~"),
        "host": host,
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

    # Main scan (after replay.yaml), manifest redaction, lineage, residual
    # pass, evidence_digests, gate and opt-in seal: the path `nova capture`
    # uses. Never raises; a failure — or a failed step above — leaves the capsule
    # unsealed and says why in metadata.finalization_error.
    finalize_in_process_capsule(
        cap_dir, manifest, run_id=run_id, writer=writer,
        incomplete_reason=steps.incomplete_reason,
    )


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

    from novafabric._paths import adapter_default_runs_dir

    resolved_data_dir = data_dir or adapter_default_runs_dir()

    resolved_name: str = run_name or getattr(agent, "name", None) or "autogen-run"
    tags: dict[str, str] = {"framework": "autogen"}

    original_initiate = agent.initiate_chat

    def wrapped_initiate(recipient: Any, **kwargs: Any) -> Any:
        from novafabric.capture._ulid import new_span_id, new_ulid
        from novafabric.capture.hooks import (
            install_all_or_discard,
            uninstall_all,
            wire_capture_state,
        )

        run_id = new_ulid()
        root_span_id = new_span_id()

        writer = open_capsule_writer(resolved_data_dir, run_id, hook="adapter-autogen")
        if writer is None:  # an unusable capsule store: run uncaptured
            return original_initiate(recipient, **kwargs)
        cap_dir = writer.capsule_dir

        _hook_token = install_all_or_discard(writer=writer, parent_span_id=root_span_id)
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
            # eval-adapters-query-04: teardown never replaces the call's outcome.
            steps = TeardownSteps("adapter-autogen")
            _wire_state = steps.run(
                "wire-state", lambda: wire_capture_state(_hook_token), "unknown"
            )
            steps.do("hooks", lambda: uninstall_all(_hook_token))
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
                steps=steps,
            )
        return result

    agent.initiate_chat = wrapped_initiate
    return agent
