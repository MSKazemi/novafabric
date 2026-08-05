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

import contextvars
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

#: Correlates :meth:`NovaA2AInterceptor.before` with its matching ``after``.
#:
#: The SDK does *not* hand the same object to both hooks: ``BaseClient.
#: _execute_with_interceptors`` builds a ``BeforeArgs``, and then builds a
#: **separate** ``AfterArgs`` from the transport result. Anything stashed on
#: the ``before`` args is therefore invisible to ``after``.
#:
#: A ContextVar is the correct carrier because both hooks are awaited inline
#: within the *same* asyncio task, while two concurrent calls run in two tasks
#: with independent contexts — so the pairing survives interleaving, which a
#: single module-level slot would not.
_CALL_KEY: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "novafabric_a2a_call_key", default=None
)

#: Hook ownership now lives in `capture.hooks` itself (ADR-0224): install_all()
#: returns a token, and uninstall_all(token) only tears down for the owner. This
#: adapter used to carry its own copy of that guard; keeping a second, parallel
#: ownership lock would mean two answers to one question.


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
        # method_key -> (writer, run_id, span_id, created_at, t0, agent_name,
        #                hook_token)
        self._pending: dict[
            str, tuple[Any, str, str, str, float, str, str]
        ] = {}

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
        # Ownership now lives in capture.hooks itself (ADR-0224), so every
        # in-process caller gets the same guarantee rather than each adapter
        # reimplementing it. An empty token means another capture owns the
        # hooks; it is safe to hand straight back to uninstall_all().
        hook_token = install_all(writer=writer, parent_span_id=span_id)

        # Write A2A task input
        task_log = writer.capsule_dir / "a2a-tasks.jsonl"
        with task_log.open("a") as f:
            f.write(json.dumps({"direction": "request", "method": args.method,
                                 "agent": agent_name, "input": args.input}) + "\n")

        self._pending[key] = (
            writer, run_id, span_id, created_at, t0, agent_name, hook_token,
        )
        # after() receives a *different* args object, so the key travels in the
        # task-local ContextVar, not on args. Also stamped on args for the
        # benefit of callers that do reuse one object across both hooks.
        _CALL_KEY.set(key)
        args._nova_key = key

    async def after(self, args: Any) -> None:
        if args.method not in _CAPTURE_METHODS:
            return
        key = getattr(args, "_nova_key", None) or _CALL_KEY.get()
        # Last resort: an interceptor invoked without a matching before() in
        # this task. Pairing is then a guess, so only take it when exactly one
        # capture for this method is outstanding — guessing between several
        # would silently attach a response to the wrong capsule.
        if key is None:
            outstanding = [k for k in self._pending if k.startswith(f"{args.method}:")]
            if len(outstanding) != 1:
                return
            key = outstanding[0]

        entry = self._pending.pop(key, None)
        if entry is None:
            return
        _CALL_KEY.set(None)
        writer, run_id, span_id, created_at, t0, agent_name, hook_token = entry

        # Only the capture that installed the global hooks may remove them;
        # otherwise the first call to finish would end wire capture for every
        # other call still in flight. uninstall_all() enforces that from the
        # token, so this is safe even for the call that lost the race.
        from novafabric.capture.hooks import uninstall_all, wire_capture_state
        from novafabric.capture.replay import minimal_replay_policy
        # Read before teardown: uninstall_all forgets the contention record.
        _wire_state = wire_capture_state(hook_token)
        uninstall_all(hook_token)

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
            "trace_root_span_id": span_id,
            "model_calls_ref": "model-calls.jsonl",
            "tool_calls_ref": "tool-calls.jsonl",
            "assets_ref": "assets.jsonl",
            # A bare `a2a_tasks_ref` is not a run-capsule property, and the
            # schema is additionalProperties:false — it made every capsule this
            # adapter wrote fail `nova validate`. The A2A task stream is
            # third-party protocol data, so it belongs under `extensions`,
            # keyed by reverse DNS.
            "extensions": {"io.a2aproject": {"tasks_ref": "a2a-tasks.jsonl"}},
            "inputs": [],
            "outputs": [],
            "model_call_count": _count_jsonl(cap_dir / "model-calls.jsonl"),
            "tool_call_count": _count_jsonl(cap_dir / "tool-calls.jsonl"),
            "mutating_tool_count": 0,
            "exit_code": 0,
            "metadata": {
                "framework": "a2a",
                "agent": agent_name,
                # Say so in the capsule when wire-level hooks were not installed
                # for this call, so a short model-calls/tool-calls stream reads
                # as "not captured", never as "did not happen".
                "wire_capture": _wire_state,
            },
        }
        writer.write_text("capsule.yaml", yaml.dump(manifest, allow_unicode=True))


def make_interceptor(data_dir: Path | None = None) -> NovaA2AInterceptor:
    """Create a NovaFabric interceptor for the A2A SDK client.

    Pass the returned interceptor to the ``interceptors=`` argument of
    ``ClientFactory.create`` / ``create_from_url``.

    Args:
        data_dir: Base directory for capsules.

    Raises:
        ImportError: If ``a2a-sdk`` is not installed.

    Usage (a2a-sdk 1.0.x)::

        from a2a.client import ClientConfig, ClientFactory

        from novafabric.adapters.a2a import make_interceptor

        factory = ClientFactory(ClientConfig(httpx_client=httpx_client))
        client = await factory.create_from_url(
            "http://agent:8080", interceptors=[make_interceptor()]
        )
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
