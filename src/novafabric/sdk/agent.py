from __future__ import annotations

import functools
import json
import time
import uuid
from collections.abc import Callable, Mapping
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
    deployment_environment: str | None = None,
    variant: Mapping[str, Any] | None = None,
    session_id: str | None = None,
    session_sequence: int | None = None,
) -> Callable[[F], F]:
    """Capture decorator. ``deployment_environment`` (ADR-0126) tags the capsule
    with its delivery-lifecycle context (e.g. ``production``); the CLI flag and
    the ``NOVAFABRIC_ENVIRONMENT`` env var take precedence over this argument.

    ``variant`` (ADR-0116, record-only) records which A/B experiment/variant an
    *external* allocator had active — a mapping with ``experiment_id``,
    ``variant_id``, and ``assignment_source`` (plus optional ``variant_label``,
    ``assigned_at``, ``extensions``), copied verbatim into the capsule's
    optional ``variant`` block. NovaFabric never assigns variants. The
    ``NOVAFABRIC_VARIANT*`` env vars take precedence over this argument.

    ``session_id``/``session_sequence`` (ADR-0122, record-only) tag the capsule
    as one ordered turn of a multi-turn session; ``session_id`` must be a ULID
    (create one with ``nova session new``). The ``NOVAFABRIC_SESSION_ID`` /
    ``NOVAFABRIC_SESSION_SEQUENCE`` env vars take precedence over these
    arguments. Absent = a standalone run (today's behavior).
    """
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

                return _run_with_capture(
                    fn, args, kwargs, name, version, Path(capsule_dir),
                    deployment_environment=deployment_environment,
                    variant=variant,
                    session_id=session_id,
                    session_sequence=session_sequence,
                )

        return wrapper  # type: ignore[return-value]

    return decorator


def _run_with_capture(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    agent_name: str,
    agent_version: str,
    cap_dir: Path,
    deployment_environment: str | None = None,
    variant: Mapping[str, Any] | None = None,
    session_id: str | None = None,
    session_sequence: int | None = None,
) -> Any:
    from novafabric.capture._ulid import new_span_id, new_ulid
    from novafabric.capture.capsule import CapsuleWriter
    from novafabric.capture.deployment_env import resolve_deployment_environment
    from novafabric.capture.env import capture_environment
    from novafabric.capture.hooks import install_all, uninstall_all
    from novafabric.capture.replay import minimal_replay_policy
    from novafabric.capture.secrets import SecretScannerV0
    from novafabric.capture.variant import resolve_variant_attribution

    # ADR-0126: resolve pre-flight (env var beats the SDK argument); an invalid
    # explicit sdk-arg raises here, before the workload runs.
    deployment_env = resolve_deployment_environment(sdk_value=deployment_environment)
    # ADR-0116: resolve pre-flight (NOVAFABRIC_VARIANT* env vars beat the SDK
    # argument); an incomplete/invalid explicit block raises here, before the
    # workload runs. Record-only — values are copied verbatim, never derived.
    variant_attr = resolve_variant_attribution(sdk_values=variant)
    # ADR-0122: resolve pre-flight (NOVAFABRIC_SESSION_* env vars beat the SDK
    # arguments); an invalid explicit membership raises here, before the
    # workload runs. Record-only back-reference; session.json is authoritative.
    from novafabric.capture.session import resolve_session_membership

    session_membership = resolve_session_membership(
        sdk_session_id=session_id, sdk_sequence=session_sequence
    )

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
        # ADR-0126: additive optional deployment-context tag (absent = unchanged manifest).
        if deployment_env is not None:
            manifest["deployment_environment"] = deployment_env.value
            manifest["environment_source"] = deployment_env.source
        # ADR-0116: additive optional variant-attribution block (absent = unchanged manifest).
        if variant_attr is not None:
            manifest["variant"] = variant_attr.to_manifest_block()
        # ADR-0122: additive optional session back-reference (absent = unchanged manifest).
        if session_membership is not None:
            manifest["session_id"] = session_membership.session_id
            if session_membership.sequence is not None:
                manifest["sequence"] = session_membership.sequence
        if error:
            manifest["error"] = error

        writer.write_text("capsule.yaml", yaml.dump(manifest, allow_unicode=True))

    return result
