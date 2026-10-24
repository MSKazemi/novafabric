from __future__ import annotations

import contextlib
import functools
import inspect
import logging
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

import yaml
from opentelemetry import trace

from novafabric._capsule_fs import read_capsule_member_or_none
from novafabric._capsule_version import CAPSULE_SCHEMA_VERSION
from novafabric.capture.hooks._fail_open import note_capture_failure
from novafabric.capture.record_roles import count_logical_model_calls_in_file
from novafabric.capture.teardown import TeardownSteps, novafabric_version

F = TypeVar("F", bound=Callable[..., Any])

logger = logging.getLogger(__name__)


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
    """Capture decorator.

    ``capsule_dir`` is a capsule *store*, like ``nova capture --output-dir``:
    every call to the decorated function writes its own capsule to
    ``<capsule_dir>/<run_id>/`` (the run id is a fresh ULID per call, also
    logged as ``sdk_capsule_started``). ``None`` = OTel spans only, no capsule.

    ``deployment_environment`` (ADR-0126) tags the capsule
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
        store = None if capsule_dir is None else Path(capsule_dir)
        tags = _Tags(deployment_environment, variant, session_id, session_sequence)

        if inspect.iscoroutinefunction(fn):
            # eval-adapters-query-03: an async agent must be awaited *inside* the
            # capture; calling it returned an un-awaited coroutine, so the capsule
            # was finalised (and the hooks removed) before any agent code ran.
            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with _agent_span(name, version):
                    capture = _SdkCapture.start(name, version, store, tags)
                    if capture is None:
                        return await fn(*args, **kwargs)
                    try:
                        result = await fn(*args, **kwargs)
                    except BaseException as exc:
                        capture.finish(exc)
                        raise
                    capture.finish(None)
                    return result

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with _agent_span(name, version):
                capture = _SdkCapture.start(name, version, store, tags)
                if capture is None:
                    return fn(*args, **kwargs)
                try:
                    result = fn(*args, **kwargs)
                except BaseException as exc:
                    capture.finish(exc)
                    raise
                capture.finish(None)
                return result

        return wrapper  # type: ignore[return-value]

    return decorator


@contextlib.contextmanager
def _agent_span(agent_name: str, agent_version: str) -> Iterator[None]:
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span(f"novafabric.agent.{agent_name}") as span:
        span.set_attribute("gen_ai.agent.id", str(uuid.uuid4()))
        span.set_attribute("gen_ai.agent.name", agent_name)
        span.set_attribute("gen_ai.agent.version", agent_version)
        yield


@dataclass(frozen=True)
class _Tags:
    """The optional record-only tagging arguments of :func:`agent`."""

    deployment_environment: str | None
    variant: Mapping[str, Any] | None
    session_id: str | None
    session_sequence: int | None


def _outcome(exc: BaseException | None) -> tuple[int, dict[str, Any] | None]:
    """``(exit_code, error)`` for how the wrapped call ended.

    ``sys.exit(n)`` and Ctrl-C are outcomes too (eval-adapters-query-03): they
    escaped ``except Exception`` and the capsule said ``success, exit_code 0``.
    The codes follow what ``nova capture`` would record for the same process:
    ``SystemExit`` carries its own code (``None``/``0`` = success, a non-int
    message = 1) and an interrupt is 130 (128 + SIGINT).
    """
    if exc is None:
        return 0, None
    if isinstance(exc, SystemExit):
        code = exc.code
        if code is None or code == 0:
            return 0, None
        exit_code = int(code) if isinstance(code, int) else 1
    elif isinstance(exc, KeyboardInterrupt):
        exit_code = 130
    else:
        exit_code = 1
    return exit_code, {"type": type(exc).__name__, "message": str(exc), "traceback_ref": None}


@dataclass
class _SdkCapture:
    """One ``@agent`` call's capsule: opened by :meth:`start`, closed by :meth:`finish`.

    Fail-open by construction (eval-adapters-query-04): capture I/O never
    replaces the workload's own outcome. An unwritable store means the call runs
    uncaptured; a teardown step that fails is logged, counted, and recorded in
    the capsule, which is then left unsealed. Only an invalid *explicit* tagging
    argument raises — before the workload runs, as documented — because that is
    the caller's error, not capture's.
    """

    agent_name: str
    agent_version: str
    writer: Any
    hook_token: str
    run_id: str
    root_span_id: str
    created_at: str
    t0: float
    manifest_tags: dict[str, Any]

    @classmethod
    def start(
        cls, agent_name: str, agent_version: str, store: Path | None, tags: _Tags
    ) -> _SdkCapture | None:
        if store is None:
            return None
        from novafabric.capture._ulid import new_span_id, new_ulid
        from novafabric.capture.capsule import CapsuleWriter
        from novafabric.capture.hooks import install_all

        # Pre-flight: an invalid explicit argument raises here, before the
        # workload runs; an invalid ambient env var warns and is ignored.
        manifest_tags = _resolve_tags(tags)

        run_id = new_ulid()
        # M13: ``store`` is a capsule *store*, exactly like ``nova capture
        # --output-dir``: every call gets its own fresh ``<store>/<run_id>/``.
        # Writing into ``store`` itself made a second call overwrite capsule.yaml
        # while the JSONL streams appended, so one capsule held several runs.
        writer = CapsuleWriter(run_id=run_id, base_dir=store)
        try:
            writer.open()
        except Exception as exc:  # an unwritable store must not block the call
            note_capture_failure("sdk-agent", "capsule", exc)
            logger.warning(
                "sdk_capsule_not_started",
                extra={"agent_name": agent_name, "error_type": type(exc).__name__},
            )
            return None
        logger.info(
            "sdk_capsule_started",
            extra={
                "run_id": run_id,
                "capsule_dir": str(writer.capsule_dir),
                "agent_name": agent_name,
            },
        )
        root_span_id = new_span_id()
        return cls(
            agent_name=agent_name,
            agent_version=agent_version,
            writer=writer,
            hook_token=install_all(writer=writer, parent_span_id=root_span_id),
            run_id=run_id,
            root_span_id=root_span_id,
            created_at=_now(),
            t0=time.monotonic(),
            manifest_tags=manifest_tags,
        )

    def finish(self, exc: BaseException | None) -> None:
        """Write the root span, env.lock, replay.yaml and the manifest. Never raises."""
        from novafabric.capture.finalize import finalize_in_process_capsule
        from novafabric.capture.hooks import uninstall_all

        try:
            uninstall_all(self.hook_token)
        except Exception as err:
            note_capture_failure("sdk-agent", "hooks", err)
        exit_code, error = _outcome(exc)
        finished_at = _now()
        duration_ms = int((time.monotonic() - self.t0) * 1000)
        steps = TeardownSteps("sdk-agent")
        steps.do("trace", lambda: self.writer.append_trace_span(
            self._root_span(finished_at, duration_ms, exit_code)
        ))
        steps.do("env.lock", self._write_env_lock)
        steps.do("replay.yaml", lambda: self.writer.write_text(
            "replay.yaml", yaml.dump(_minimal_replay_policy(), allow_unicode=True)
        ))
        cap_dir: Path = self.writer.capsule_dir
        model_call_count = steps.run(
            "model-calls",
            lambda: count_logical_model_calls_in_file(cap_dir / "model-calls.jsonl"),
            0,
        )
        tool_call_count = steps.run("tool-calls", lambda: _count_lines(cap_dir), 0)

        manifest: dict[str, Any] = {
            "schema_version": CAPSULE_SCHEMA_VERSION,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": "success" if exit_code == 0 else "failure",
            "command": [f"@agent:{self.agent_name}@{self.agent_version}"],
            "capture_mode": "sdk-decorator",
            "novafabric_version": novafabric_version(),
            "working_directory": str(Path.cwd()).replace(str(Path.home()), "~"),
            "host": steps.run("host", _host_info, _NO_HOST),
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
            "model_call_count": model_call_count,
            "tool_call_count": tool_call_count,
            "mutating_tool_count": 0,
            "exit_code": exit_code,
            **self.manifest_tags,
        }
        if error:
            manifest["error"] = error

        # Main scan (after replay.yaml), manifest redaction, lineage, residual pass,
        # evidence_digests, gate and opt-in seal: the path `nova capture` uses.
        # Never raises; a failed step above leaves the capsule unsealed with the
        # reason in metadata.finalization_error, so its counts are not vouched for.
        finalize_in_process_capsule(
            cap_dir, manifest, run_id=self.run_id, writer=self.writer,
            incomplete_reason=steps.incomplete_reason,
        )

    def _root_span(self, finished_at: str, duration_ms: int, exit_code: int) -> dict[str, Any]:
        return {
            "span_id": self.root_span_id,
            "parent_span_id": None,
            "name": f"novafabric.agent.{self.agent_name}",
            "started_at": self.created_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": "ok" if exit_code == 0 else "error",
            "attributes": {"agent_name": self.agent_name, "agent_version": self.agent_version},
        }

    def _write_env_lock(self) -> None:
        from novafabric.capture import env

        env_lock = env.capture_environment(created_at=self.created_at, run_id=self.run_id)
        self.writer.write_text("env.lock", yaml.dump(env_lock, allow_unicode=True))


def _resolve_tags(tags: _Tags) -> dict[str, Any]:
    """The additive optional manifest fields (absent = unchanged manifest)."""
    from novafabric.capture.deployment_env import resolve_deployment_environment
    from novafabric.capture.session import resolve_session_membership
    from novafabric.capture.variant import resolve_variant_attribution

    out: dict[str, Any] = {}
    # ADR-0126: deployment-context tag; the env var beats the SDK argument.
    deployment_env = resolve_deployment_environment(sdk_value=tags.deployment_environment)
    if deployment_env is not None:
        out["deployment_environment"] = deployment_env.value
        out["environment_source"] = deployment_env.source
    # ADR-0116: variant attribution, record-only — copied verbatim, never derived.
    variant_attr = resolve_variant_attribution(sdk_values=tags.variant)
    if variant_attr is not None:
        out["variant"] = variant_attr.to_manifest_block()
    # ADR-0122: session back-reference; session.json stays authoritative.
    membership = resolve_session_membership(
        sdk_session_id=tags.session_id, sdk_sequence=tags.session_sequence
    )
    if membership is not None:
        out["session_id"] = membership.session_id
        if membership.sequence is not None:
            out["sequence"] = membership.sequence
    return out


def _minimal_replay_policy() -> dict[str, Any]:
    from novafabric.capture.replay import minimal_replay_policy

    return minimal_replay_policy()


_NO_HOST: dict[str, Any] = {}


def _host_info() -> dict[str, Any]:
    from novafabric.capture.env import host_info

    return host_info()


def _count_lines(cap_dir: Path) -> int:
    text = read_capsule_member_or_none(cap_dir / "tool-calls.jsonl", operation="the SDK")
    return sum(1 for line in (text or "").splitlines() if line.strip())
