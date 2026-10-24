"""Shared capsule-writing core for framework adapters.

Every adapter does the same thing around a framework's call path: open a
capsule, claim the wire hooks, run the framework, then write the manifest and
supporting files. The four adapters that predate this module each carry their
own copy of that body: ``a2a``, ``google_adk``, ``langgraph`` and
``openai_agents``.

New adapters use this instead (``autogen``, ``bedrock_agentcore``, ``crewai``,
``dspy``, ``haystack``, ``llamaindex`` and ``pydantic_ai`` do). It is
**additive**: an existing adapter moves onto it only when its own body is broken
— ``bedrock_agentcore`` did for review eval-adapters-query-12 (no capsule for an
abandoned stream), and ``autogen``, ``crewai`` and ``dspy`` for -19 (a changed
call signature, no async twins), through ``_entry_points.wrap_entry_points`` —
because rewriting four working, tested adapters is a blast radius that buys
nothing today. ``tests/adapters/test_capsule_core_adoption.py``
keeps this list in step with the code.

The manifest literal lives here, so
``tests/adapters/test_adapter_manifests_match_the_schema.py`` still checks it:
that guard globs the whole adapters directory, and this file is in it.

ADR-0224: the hook token must be passed to ``wire_capture_state`` *before*
``uninstall_all``, and the resulting state stamped into ``metadata`` — a capsule
has to say whether its wire stream is complete.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from novafabric._capsule_version import CAPSULE_SCHEMA_VERSION
from novafabric.capture.env import capture_environment, host_info
from novafabric.capture.finalize import finalize_in_process_capsule

# Module-level so tests can patch via ``novafabric.adapters._capsule.<name>``.
from novafabric.capture.record_roles import count_logical_model_calls_in_file
from novafabric.capture.teardown import TeardownSteps, novafabric_version, open_capsule_writer


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def resolve_data_dir(data_dir: Path | None) -> Path:
    """Where capsules go when the caller does not say.

    The default is :func:`novafabric._paths.adapter_default_runs_dir` — the one
    definition the run-id resolver also searches, so ``nova replay <run-id>``
    finds what an adapter wrote.
    """
    from novafabric._paths import adapter_default_runs_dir

    if data_dir is not None:
        return Path(data_dir)
    return adapter_default_runs_dir()


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
    #: Why the run stopped before it finished, when it did — a stream the caller
    #: abandoned, or a run cancelled mid-flight. Sets ``status: partial``.
    partial_reason: str | None = None
    #: Vendor-specific manifest ``extensions``, keyed by reverse DNS (e.g. a
    #: pointer to a framework's own trace stream). Omitted when empty.
    extensions: dict[str, Any] = field(default_factory=dict)

    def fail(self, exc: BaseException) -> None:
        """Record *exc* as the run's failure. Never swallows it."""
        self.exit_code = 1
        self.error = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback_ref": None,
        }

    def mark_partial(self, reason: str) -> None:
        """Record that the run was interrupted before it finished.

        The schema's ``partial`` status exists for exactly this: a streaming
        run the caller stopped reading, or one cancelled before it completed.
        A recorded failure wins — an error is the stronger, truer statement.
        """
        if self.error is None:
            self.partial_reason = reason

    def finish(self) -> None:
        """Release the hooks and write the capsule. Never raises (eval-adapters-query-04).

        Safe to call in ``finally``: a failed teardown step is logged, counted and
        recorded in the capsule (left unsealed) — it never replaces the
        framework call's own result or exception.
        """
        from novafabric.capture.hooks import uninstall_all, wire_capture_state

        steps = TeardownSteps(f"adapter-{self.framework}")
        # The wire state is read *before* the hooks are released (ADR-0224).
        wire_state = steps.run("wire-state", lambda: wire_capture_state(self.hook_token), "unknown")
        steps.do("hooks", lambda: uninstall_all(self.hook_token))
        self._write(wire_state, steps)

    def _write(self, wire_state: str, steps: TeardownSteps) -> None:
        from novafabric.capture.replay import minimal_replay_policy

        finished_at = _now()
        duration_ms = int((time.monotonic() - self.t0) * 1000)
        if self.exit_code != 0:
            status = "failure"
        elif self.partial_reason is not None:
            status = "partial"
        else:
            status = "success"
        tags = {**self.tags, "framework": self.framework, "wire_capture": wire_state}
        if status == "partial" and self.partial_reason is not None:
            tags["partial_reason"] = self.partial_reason

        steps.do("trace", lambda: self.writer.append_trace_span({
            "span_id": self.root_span_id,
            "parent_span_id": None,
            "name": f"novafabric.adapter.{self.framework}.{self.run_name}",
            "started_at": self.created_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": "ok" if self.exit_code == 0 else "error",
            "attributes": {"run_name": self.run_name, **tags},
        }))
        steps.do("env.lock", lambda: self.writer.write_text("env.lock", yaml.dump(
            capture_environment(created_at=self.created_at, run_id=self.run_id),
            allow_unicode=True,
        )))
        steps.do("replay.yaml", lambda: self.writer.write_text(
            "replay.yaml", yaml.dump(minimal_replay_policy(), allow_unicode=True)
        ))
        model_call_count = steps.run(
            "model-calls",
            lambda: count_logical_model_calls_in_file(self.cap_dir / "model-calls.jsonl"),
            0,
        )
        tool_call_count = steps.run(
            "tool-calls", lambda: _count_lines(self.cap_dir / "tool-calls.jsonl"), 0
        )
        host: dict[str, Any] = steps.run("host", host_info, {})

        manifest: dict[str, Any] = {
            "schema_version": CAPSULE_SCHEMA_VERSION,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": status,
            "command": [f"@{self.framework}:{self.run_name}"],
            "capture_mode": "sdk-decorator",
            "novafabric_version": novafabric_version(),
            "working_directory": str(Path.cwd()).replace(str(Path.home()), "~"),
            "host": host,
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
            "exit_code": self.exit_code,
            "metadata": tags,
        }
        if self.error:
            manifest["error"] = self.error
        if self.extensions:
            manifest["extensions"] = self.extensions

        # Main scan (after replay.yaml), manifest redaction, lineage, residual pass,
        # evidence_digests, gate and opt-in seal: the path `nova capture` uses.
        # Never raises; a failure — or a failed step above — leaves the capsule
        # unsealed and says why in metadata.finalization_error.
        finalize_in_process_capsule(
            self.cap_dir, manifest, run_id=self.run_id, writer=self.writer,
            incomplete_reason=steps.incomplete_reason,
        )


@dataclass
class UncapturedRun:
    """What :func:`begin_capture` returns when the capsule could not be opened.

    The framework call then runs uncaptured (the failure was logged and counted
    by :func:`~novafabric.capture.teardown.open_capsule_writer`); every capture
    method is a no-op, so the adapters drive it exactly like an
    :class:`AdapterCapture`.
    """

    tags: dict[str, str] = field(default_factory=dict)

    def fail(self, exc: BaseException) -> None:
        """Nothing to record."""

    def mark_partial(self, reason: str) -> None:
        """Nothing to record."""

    def finish(self) -> None:
        """Nothing to write."""


def begin_capture(
    *, framework: str, run_name: str, data_dir: Path | None
) -> AdapterCapture | UncapturedRun:
    """Open a capsule and claim the wire hooks for one framework invocation.

    An unusable capsule store returns an :class:`UncapturedRun` instead of
    raising: capture never blocks the framework call.
    """
    from novafabric.capture._ulid import new_span_id, new_ulid
    from novafabric.capture.hooks import install_all_or_discard

    run_id = new_ulid()
    writer = open_capsule_writer(
        resolve_data_dir(data_dir), run_id, hook=f"adapter-{framework}"
    )
    if writer is None:
        return UncapturedRun()
    root_span_id = new_span_id()

    return AdapterCapture(
        framework=framework,
        run_name=run_name,
        cap_dir=writer.capsule_dir,
        run_id=run_id,
        root_span_id=root_span_id,
        writer=writer,
        hook_token=install_all_or_discard(writer=writer, parent_span_id=root_span_id),
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
