"""NovaFabric plugin for Google ADK.

Implements the ADK ``BasePlugin`` interface to capture every Runner
invocation as a nova capsule (:func:`make_plugin`), and -- experimental,
ADR-0306 slice 3 -- a tool plugin (:func:`make_tool_plugin`) that records each
ADK tool call under ``nova capture`` and serves it from the capsule under a
mocked ``nova replay``, before the tool body runs.

``google-adk`` is an **optional** dependency.  :func:`make_plugin` and
:func:`make_tool_plugin` raise :class:`ImportError` at call time if the package
is missing.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

import yaml

from novafabric.capture.env import capture_environment, host_info
from novafabric.capture.finalize import finalize_in_process_capsule
from novafabric.capture.hooks import ConcurrentCaptureRefused
from novafabric.capture.record_roles import count_logical_model_calls_in_file

_log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip())


#: Upper bound on invocations in flight through one plugin. A Runner serves
#: every invocation through a single plugin instance; a caller whose runs never
#: finish (or never reach a callback) must not grow this without bound. Past the
#: cap an invocation is simply not captured — fail-open, the run is unaffected.
_MAX_LIVE_INVOCATIONS = 64


class _Run:
    """State of ONE in-flight invocation (never shared between invocations)."""

    __slots__ = ("writer", "run_id", "span_id", "created_at", "t0", "hook_token")

    def __init__(
        self, writer: Any, run_id: str, span_id: str, created_at: str, t0: float, hook_token: str
    ) -> None:
        self.writer = writer
        self.run_id = run_id
        self.span_id = span_id
        self.created_at = created_at
        self.t0 = t0
        self.hook_token = hook_token


def _pick(ctx: Any, invocation_context: Any) -> Any:
    return invocation_context if invocation_context is not None else ctx


def _invocation_key(ctx: Any) -> str:
    inv = getattr(ctx, "invocation_id", None)
    return str(inv) if inv is not None else f"ctx-{id(ctx)}"


class NovaAdkPlugin:
    """ADK plugin that captures each runner invocation as a nova capsule.

    One plugin instance serves every invocation of its ``Runner``, concurrently
    when the caller gathers several ``run_async`` calls, so per-run state is
    keyed by ``invocation_id`` and never held on ``self`` as a single slot
    (ADR-0224 D3). Each invocation's hooks scope is bound in the task that runs
    ``before_run_callback``, which is the task that drives the agent.

    ADK calls plugin callbacks by keyword (``invocation_context=``) and
    registers plugins by ``.name``; both are honoured. A positional ``ctx`` is
    still accepted. Not a subclass of BasePlugin at definition time so the
    module is importable without ``google-adk`` installed.
    """

    name = "novafabric"

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()

    async def before_run_callback(
        self, ctx: Any = None, *, invocation_context: Any = None, **kwargs: Any
    ) -> None:
        try:
            self._begin(_invocation_key(_pick(ctx, invocation_context)))
        except ConcurrentCaptureRefused:
            raise  # strict mode is an explicit operator request to refuse
        except Exception:
            _log.debug("google_adk: could not begin a capture", exc_info=True)

    def _begin(self, key: str) -> None:
        from novafabric.capture._ulid import new_span_id, new_ulid
        from novafabric.capture.capsule import CapsuleWriter
        from novafabric.capture.hooks import install_all_or_discard

        with self._lock:
            if key in self._runs or len(self._runs) >= _MAX_LIVE_INVOCATIONS:
                return
            # Reserve the slot so a racing duplicate or the cap check sees it.
            self._runs[key] = None  # type: ignore[assignment]
        try:
            run_id, span_id = new_ulid(), new_span_id()
            self._data_dir.mkdir(parents=True, exist_ok=True)
            writer = CapsuleWriter(run_id=run_id, base_dir=self._data_dir)
            writer.open()
            created_at, t0 = _now(), time.monotonic()
            token = install_all_or_discard(writer=writer, parent_span_id=span_id)
            run = _Run(writer, run_id, span_id, created_at, t0, token)
        except BaseException:
            with self._lock:
                self._runs.pop(key, None)
            raise
        with self._lock:
            self._runs[key] = run

    async def after_run_callback(
        self, ctx: Any = None, *, invocation_context: Any = None, **kwargs: Any
    ) -> None:
        self._end(_pick(ctx, invocation_context), None)

    async def on_run_error_callback(
        self,
        ctx: Any = None,
        *,
        invocation_context: Any = None,
        error: BaseException | None = None,
        **kwargs: Any,
    ) -> None:
        """ADK runs ``after_run`` only on success; failures arrive here."""
        self._end(_pick(ctx, invocation_context), error or RuntimeError("run failed"))

    def _end(self, ctx: Any, error: BaseException | None) -> None:
        with self._lock:
            run = self._runs.pop(_invocation_key(ctx), None)
        if run is None:
            return
        try:
            self._finish(run, _invocation_key(ctx), error)
        except Exception:
            _log.debug("google_adk: could not finalise a capture", exc_info=True)

    def _finish(self, run: _Run, inv_key: str, error: BaseException | None) -> None:
        from novafabric.capture.hooks import uninstall_all, wire_capture_state
        from novafabric.capture.replay import minimal_replay_policy

        writer = run.writer
        # BEFORE teardown: uninstall_all forgets the contention record.
        wire_state = wire_capture_state(run.hook_token)
        try:
            uninstall_all(run.hook_token)
        except Exception:
            _log.debug("google_adk: hook teardown failed", exc_info=True)

        finished_at = _now()
        duration_ms = int((time.monotonic() - run.t0) * 1000)
        cap_dir = writer.capsule_dir
        failed = error is not None

        writer.append_trace_span({
            "span_id": run.span_id,
            "parent_span_id": None,
            "name": "novafabric.adapter.google_adk.run",
            "started_at": run.created_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": "error" if failed else "ok",
            "attributes": {"framework": "google-adk"},
        })

        run_id, created_at = run.run_id, run.created_at
        env_lock = capture_environment(created_at=created_at, run_id=run_id)
        writer.write_text("env.lock", yaml.dump(env_lock, allow_unicode=True))
        writer.write_text(
            "replay.yaml", yaml.dump(minimal_replay_policy(), allow_unicode=True)
        )

        manifest: dict[str, Any] = {
            "schema_version": "1.0.0",
            "run_id": run_id,
            "created_at": created_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "status": "failure" if failed else "success",
            "command": ["@google-adk:run"],
            "capture_mode": "sdk-decorator",
            "novafabric_version": _pkg_version("novafabric"),
            "working_directory": str(Path.cwd()).replace(str(Path.home()), "~"),
            "host": host_info(),
            "environment_ref": "env.lock",
            "replay_policy_ref": "replay.yaml",
            "redaction_proof_ref": "redaction-proof.json",
            "trace_ref": "trace.jsonl",
            "trace_root_span_id": run.span_id,
            "model_calls_ref": "model-calls.jsonl",
            "tool_calls_ref": "tool-calls.jsonl",
            "assets_ref": "assets.jsonl",
            "inputs": [],
            "outputs": [],
            "model_call_count": count_logical_model_calls_in_file(cap_dir / "model-calls.jsonl"),
            "tool_call_count": _count_jsonl(cap_dir / "tool-calls.jsonl"),
            "mutating_tool_count": 0,
            "exit_code": 1 if failed else 0,
            "metadata": {
                "framework": "google-adk",
                "wire_capture": wire_state,
                "adk_invocation_id": inv_key,
            },
        }
        if error is not None:
            manifest["error"] = {
                "type": type(error).__name__,
                "message": str(error)[:500],
                "traceback_ref": None,
            }
        # Main scan (after replay.yaml), manifest redaction, lineage, residual
        # pass, evidence_digests, gate and opt-in seal: the path `nova capture`
        # uses. Never raises; a failure leaves the capsule unsealed and says why.
        finalize_in_process_capsule(cap_dir, manifest, run_id=run_id, writer=writer)

    async def on_event_callback(self, ctx: Any = None, event: Any = None, **kwargs: Any) -> None:
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
        from google.adk.plugins.base_plugin import BasePlugin
    except ImportError:
        raise ImportError(
            "google-adk is not installed. "
            "Install it with: pip install 'novafabric[google-adk]'"
        )

    from novafabric._paths import adapter_default_runs_dir

    resolved = data_dir or adapter_default_runs_dir()

    class _AdkPlugin(NovaAdkPlugin, BasePlugin):
        """NovaAdkPlugin on ADK's BasePlugin, so every callback ADK looks up by
        name exists (the base supplies the no-op defaults we do not override)."""

        def __init__(self, data_dir: Path) -> None:
            BasePlugin.__init__(self, name=NovaAdkPlugin.name)
            NovaAdkPlugin.__init__(self, data_dir)

    return _AdkPlugin(resolved)


def make_tool_plugin(
    mutation_classes: Mapping[str, str] | None = None,
    *,
    default_mutation_class: str = "unknown",
) -> Any:
    """Create the NovaFabric ADK **tool** plugin (ADR-0306 slice 3, experimental).

    Pass it **first** in ``Runner(plugins=[...])`` -- ADK stops at the first
    plugin that answers a callback. It does nothing on its own: under
    ``nova capture python …`` each ADK tool call becomes one
    ``tool-calls.jsonl`` record; under a mocked ``nova replay`` of that capsule
    the recorded result is returned through ADK's documented
    ``before_tool_callback`` short-circuit and the tool body never runs (an
    unmatched or unservable call is refused, fail closed). Outside capture and
    replay every callback returns ``None``.

    Only capsules with a re-runnable command (``nova capture python …``) can be
    replayed: capsules written by :func:`make_plugin` carry a pseudo-command and
    are refused by ``nova replay`` (ADR-0306 open question 7).

    Args:
        mutation_classes: ADR-0012 mutation class per tool name. It gates
            ``--permissive`` and ``replay.yaml`` ``allow: true`` at replay, and
            is declared here, in the workload's code -- never read from a capsule.
        default_mutation_class: Class of every tool not named above.

    Raises:
        ImportError: If ``google-adk`` is not installed.
        ValueError: If a mutation class is not an ADR-0012 class.

    Usage::

        from novafabric.adapters.google_adk import make_tool_plugin
        runner = Runner(agent=agent, session_service=svc, plugins=[
            make_tool_plugin({"lookup_order": "read-only"}),
        ])
    """
    try:
        from google.adk.plugins.base_plugin import BasePlugin
    except ImportError:
        raise ImportError(
            "google-adk is not installed. "
            "Install it with: pip install 'novafabric[google-adk]'"
        )

    from novafabric.adapters._adk_tool_seam import AdkToolSeamPlugin

    class _AdkToolPlugin(AdkToolSeamPlugin, BasePlugin):
        """The tool seam on ADK's BasePlugin (its no-op defaults fill the rest)."""

        def __init__(
            self, classes: Mapping[str, str] | None, default_class: str
        ) -> None:
            BasePlugin.__init__(self, name=AdkToolSeamPlugin.name)
            AdkToolSeamPlugin.__init__(self, classes, default_class)

    return _AdkToolPlugin(mutation_classes, default_mutation_class)
