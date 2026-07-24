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

import hashlib
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
from novafabric.capture import record as _record
from novafabric.capture.env import capture_environment
from novafabric.capture.record import _payloads_enabled
from novafabric.capture.secrets import SecretScannerV0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _state_digest(obj: Any) -> str:
    """``sha256:`` digest of the canonical JSON of *obj* (ADR-0209 D2.2).

    Undigestable objects (non-JSON-serializable) fall back to a digest of
    their ``repr()`` bytes — recorded, never raised.
    """
    try:
        payload = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        payload = repr(obj).encode("utf-8", errors="replace")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _state_payload(obj: Any, payloads_on: bool) -> dict[str, Any] | None:
    """Return *obj* as a state payload only at forensic/air_gapped level."""
    return obj if payloads_on and isinstance(obj, dict) else None


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
        # ADR-0209 D2.2 (experimental): invoke() emits a start→end
        # StateTransition pair — a start marker at entry (input digest on both
        # sides: no transition observed yet) and the whole-invocation
        # transition on success. Digests always; payloads only at
        # forensic/air_gapped capture level (D5.2). Fail-open via the façade.
        payloads_on = _payloads_enabled()
        input_digest = _state_digest(input)
        _record.state_transition(
            0, input_digest, input_digest,
            state_before=_state_payload(input, payloads_on),
        )
        try:
            result = self._inner.invoke(input, config, **kwargs)
            _record.state_transition(
                1, input_digest, _state_digest(result),
                state_before=_state_payload(input, payloads_on),
                state_after=_state_payload(result, payloads_on),
            )
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
        # ADR-0209 D2.2 (experimental): one StateTransition per yielded
        # node-update chunk. Digest-chaining invariant:
        # state_digest_after[i] == state_digest_before[i+1], seeded with the
        # digest of the invocation input. Digests always; raw state payloads
        # only at forensic/air_gapped capture level (D5.2).
        payloads_on = _payloads_enabled()
        prev_digest = _state_digest(input)
        prev_payload = _state_payload(input, payloads_on)
        step_index = 0
        try:
            for chunk in self._inner.stream(input, config, **kwargs):
                cur_digest = _state_digest(chunk)
                agent_id: str | None = None
                if isinstance(chunk, dict) and len(chunk) == 1:
                    only_key = next(iter(chunk))
                    if isinstance(only_key, str):
                        agent_id = only_key
                _record.state_transition(
                    step_index, prev_digest, cur_digest,
                    agent_id=agent_id,
                    state_before=prev_payload,
                    state_after=_state_payload(chunk, payloads_on),
                )
                prev_digest = cur_digest
                prev_payload = _state_payload(chunk, payloads_on)
                step_index += 1
                yield chunk
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
