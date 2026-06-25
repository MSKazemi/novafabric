"""NovaFabric adapter for LangGraph.

Wraps a LangGraph ``StateGraph`` or compiled graph so that each
``invoke()`` / ``stream()`` call creates a nova run capsule.
Wire-level hooks (requests, httpx, openai, anthropic …) capture every
model call automatically; no modification of graph internals is needed.

LangGraph is an **optional** dependency — this module must be importable
even when ``langgraph`` is not installed.  The :func:`wrap` function will
raise :class:`ImportError` at call time if the framework is missing.
"""
from __future__ import annotations

import json
import platform
import time
from collections.abc import Generator
from datetime import datetime, timezone
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

import yaml

# These are imported at module level so tests can patch them via
# ``novafabric.adapters.langgraph.<name>``.
from novafabric.capture.env import capture_environment
from novafabric.capture.secrets import SecretScannerV0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _run_capture(
    fn_result: Any,
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
    """Write capsule artifacts after a graph invocation finishes."""
    from novafabric.capture.replay import minimal_replay_policy

    finished_at = _now()
    duration_ms = int((time.monotonic() - t0) * 1000)
    status = "success" if exit_code == 0 else "failure"

    writer.append_trace_span({
        "span_id": root_span_id,
        "parent_span_id": None,
        "name": f"novafabric.adapter.langgraph.{run_name}",
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
        "command": [f"@langgraph:{run_name}"],
        "capture_mode": "adapter-langgraph",
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
        "tags": tags,
    }
    if error:
        manifest["error"] = error

    writer.write_text("capsule.yaml", yaml.dump(manifest, allow_unicode=True))


class _WrappedGraph:
    """Thin wrapper around a LangGraph compiled graph that instruments
    every invocation with nova capture."""

    def __init__(
        self,
        inner: Any,
        run_name: str,
        data_dir: Path,
        tags: dict[str, str],
    ) -> None:
        self._inner = inner
        self._run_name = run_name
        self._data_dir = data_dir
        self._tags = tags

    def _make_writer(self) -> tuple[Any, str, str, Path]:
        """Allocate a fresh run ID, span ID, and CapsuleWriter."""
        from novafabric.capture._ulid import new_span_id, new_ulid
        from novafabric.capture.capsule import CapsuleWriter

        run_id = new_ulid()
        root_span_id = new_span_id()

        base_dir = self._data_dir
        base_dir.mkdir(parents=True, exist_ok=True)

        writer = CapsuleWriter(run_id=run_id, base_dir=base_dir)
        writer.open()
        cap_dir = writer.capsule_dir
        return writer, run_id, root_span_id, cap_dir

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        from novafabric.capture.hooks import install_all, uninstall_all

        writer, run_id, root_span_id, cap_dir = self._make_writer()
        install_all(writer=writer, parent_span_id=root_span_id)
        created_at = _now()
        t0 = time.monotonic()
        exit_code = 0
        error: dict[str, Any] | None = None
        result: Any = None
        try:
            result = self._inner.invoke(input, config, **kwargs)
        except Exception as exc:
            exit_code = 1
            error = {"type": type(exc).__name__, "message": str(exc), "traceback_ref": None}
            raise
        finally:
            uninstall_all()
            _run_capture(
                result,
                run_name=self._run_name,
                data_dir=self._data_dir,
                tags=self._tags,
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

    def stream(
        self, input: Any, config: Any = None, **kwargs: Any
    ) -> Generator[Any, None, None]:
        from novafabric.capture.hooks import install_all, uninstall_all

        writer, run_id, root_span_id, cap_dir = self._make_writer()
        install_all(writer=writer, parent_span_id=root_span_id)
        created_at = _now()
        t0 = time.monotonic()
        exit_code = 0
        error: dict[str, Any] | None = None
        try:
            yield from self._inner.stream(input, config, **kwargs)
        except Exception as exc:
            exit_code = 1
            error = {"type": type(exc).__name__, "message": str(exc), "traceback_ref": None}
            raise
        finally:
            uninstall_all()
            _run_capture(
                None,
                run_name=self._run_name,
                data_dir=self._data_dir,
                tags=self._tags,
                error=error,
                t0=t0,
                created_at=created_at,
                exit_code=exit_code,
                writer=writer,
                root_span_id=root_span_id,
                run_id=run_id,
                cap_dir=cap_dir,
            )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def wrap(
    graph: Any,
    *,
    run_name: str | None = None,
    data_dir: Path | None = None,
    capture_node_capsules: bool = True,  # noqa: ARG001 — reserved for future child capsules
) -> _WrappedGraph:
    """Wrap a LangGraph graph for NovaFabric capture.

    Returns the wrapped graph with the same ``invoke()`` / ``stream()``
    interface.  Each call creates a nova run capsule; wire-level hooks
    record every model and tool call automatically.

    Args:
        graph: A ``langgraph.graph.StateGraph`` or compiled graph object.
        run_name: Human-readable name stored in the capsule manifest.
            Defaults to ``"langgraph-run"``.
        data_dir: Base directory for capsules.  Defaults to
            ``$NOVAFABRIC_HOME/runs`` or ``.novafabric/runs`` under CWD.
        capture_node_capsules: Reserved for future per-node child capsules
            (BQ-012 parent/child hierarchy).  Currently a no-op.

    Raises:
        ImportError: If ``langgraph`` is not installed.

    Usage::

        from novafabric.adapters.langgraph import wrap
        graph = wrap(graph, run_name="my-workflow")
        result = graph.invoke({"input": "hello"})
    """
    try:
        import langgraph  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        raise ImportError(
            "langgraph is not installed. "
            "Install it with: pip install langgraph"
        )

    import os

    resolved_data_dir = data_dir or Path(
        os.environ.get("NOVAFABRIC_HOME", str(Path.cwd() / ".novafabric"))
    ) / "runs"

    return _WrappedGraph(
        inner=graph,
        run_name=run_name or "langgraph-run",
        data_dir=resolved_data_dir,
        tags={"framework": "langgraph"},
    )
