"""NovaFabric adapter for LangGraph.

Wraps a LangGraph ``StateGraph`` or compiled graph so that each
``invoke()`` / ``ainvoke()`` / ``stream()`` / ``astream()`` call creates a nova
run capsule. Wire-level hooks (requests, httpx, openai, anthropic …) capture
every model call automatically; no modification of graph internals is needed.

``stream`` and ``astream`` return before the graph runs: LangGraph's own
methods are (async) generator functions, so every node executes while the
caller iterates. The capsule therefore stays open until the stream ends and
records how it ended — ``success`` when it was read to the end, ``failure``
when it raised (the exception still propagates), ``partial`` with
``metadata.partial_reason: abandoned`` when the caller closed it early or
dropped it unread, and ``partial`` / ``cancelled`` when the run was cancelled.
A wrapped graph called while another wrapped run is producing — a wrapped
subgraph invoked from inside a node — records into the capsule that is already
open instead of opening a second one that would steal the wire hooks.

LangGraph is an **optional** dependency — this module must be importable
even when ``langgraph`` is not installed.  The :func:`wrap` function will
raise :class:`ImportError` at call time if the framework is missing.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import time
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

import yaml

from novafabric.adapters._streaming import (
    Closer,
    close_when_collected,
    guard_async_iterator,
    guard_iterator,
    record_outcome,
)

# These are imported at module level so tests can patch them via
# ``novafabric.adapters.langgraph.<name>``.
from novafabric.capture import record as _record
from novafabric.capture.env import capture_environment, host_info
from novafabric.capture.finalize import finalize_in_process_capsule
from novafabric.capture.record import _payloads_enabled
from novafabric.capture.record_roles import count_logical_model_calls_in_file

#: Set while a wrapped run is producing — in this context, and in the node
#: threads and tasks LangGraph starts from it (its executors ``copy_context()``
#: at submit time). A nested wrapped call then records into the open capsule.
_in_flight: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "novafabric_langgraph_in_flight", default=False
)


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
    partial_reason: str | None = None,
) -> None:
    """Write capsule artifacts after a graph invocation finishes.

    *partial_reason* marks a run that stopped before it finished (``status:
    partial``, stamped into ``metadata.partial_reason``); a failure wins.
    """
    from novafabric.capture.replay import minimal_replay_policy

    finished_at = _now()
    duration_ms = int((time.monotonic() - t0) * 1000)
    if exit_code != 0:
        status = "failure"
    elif partial_reason is not None:
        status = "partial"
        tags = {**tags, "partial_reason": partial_reason}
    else:
        status = "success"

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

    writer.write_text("replay.yaml", yaml.dump(minimal_replay_policy(), allow_unicode=True))

    model_call_count = count_logical_model_calls_in_file(
        cap_dir / "model-calls.jsonl"
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
        "capture_mode": "sdk-decorator",
        "novafabric_version": _pkg_version("novafabric"),
        "working_directory": str(Path.cwd()).replace(str(Path.home()), "~"),
        "host": host_info(),
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
    # uses. Never raises; a failure leaves the capsule unsealed and says why.
    finalize_in_process_capsule(cap_dir, manifest, run_id=run_id, writer=writer)


class _GraphRun:
    """One in-flight LangGraph capture.

    It has the ``fail`` / ``mark_partial`` / ``finish`` shape that
    :class:`~novafabric.adapters._streaming.Closer` drives, so the stream
    guards shared with the other adapters close it. ``finish`` writes the
    capsule through :func:`_run_capture`, whose manifest is unchanged.
    """

    def __init__(self, graph: _WrappedGraph) -> None:
        from novafabric.capture.hooks import install_all_or_discard

        self._graph = graph
        self.writer, self.run_id, self.root_span_id, self.cap_dir = graph._make_writer()
        self.hook_token = install_all_or_discard(
            writer=self.writer, parent_span_id=self.root_span_id
        )
        self.created_at = _now()
        self.t0 = time.monotonic()
        self.exit_code = 0
        self.error: dict[str, Any] | None = None
        self.partial_reason: str | None = None

    def fail(self, exc: BaseException) -> None:
        self.exit_code = 1
        self.error = {"type": type(exc).__name__, "message": str(exc), "traceback_ref": None}

    def mark_partial(self, reason: str) -> None:
        if self.error is None:
            self.partial_reason = reason

    def finish(self) -> None:
        from novafabric.capture.hooks import uninstall_all, wire_capture_state

        wire_state = wire_capture_state(self.hook_token)
        uninstall_all(self.hook_token)
        _run_capture(
            None,
            run_name=self._graph._run_name,
            data_dir=self._graph._data_dir,
            tags={**self._graph._tags, "wire_capture": wire_state},
            error=self.error,
            t0=self.t0,
            created_at=self.created_at,
            exit_code=self.exit_code,
            writer=self.writer,
            root_span_id=self.root_span_id,
            run_id=self.run_id,
            cap_dir=self.cap_dir,
            partial_reason=self.partial_reason,
        )


class _Transitions:
    """ADR-0209 D2.2 (experimental): one StateTransition per yielded chunk.

    Digest-chaining invariant: ``state_digest_after[i] ==
    state_digest_before[i+1]``, seeded with the digest of the invocation input.
    Digests always; raw state payloads only at forensic/air_gapped capture
    level (D5.2). Whatever ``stream_mode`` produces is digested as-is; a
    single-key dict chunk (the default ``"updates"`` shape) names its node.
    """

    def __init__(self, graph_input: Any) -> None:
        self._payloads_on = _payloads_enabled()
        self._prev_digest = _state_digest(graph_input)
        self._prev_payload = _state_payload(graph_input, self._payloads_on)
        self._step = 0

    def record(self, chunk: Any) -> None:
        cur_digest = _state_digest(chunk)
        agent_id: str | None = None
        if isinstance(chunk, dict) and len(chunk) == 1:
            only_key = next(iter(chunk))
            if isinstance(only_key, str):
                agent_id = only_key
        cur_payload = _state_payload(chunk, self._payloads_on)
        _record.state_transition(
            self._step, self._prev_digest, cur_digest,
            agent_id=agent_id,
            state_before=self._prev_payload,
            state_after=cur_payload,
        )
        self._prev_digest = cur_digest
        self._prev_payload = cur_payload
        self._step += 1


def _recorded(inner: Iterator[Any], transitions: _Transitions) -> Iterator[Any]:
    """Yield *inner*'s chunks, recording each; close *inner* however this ends."""
    try:
        for chunk in inner:
            transitions.record(chunk)
            yield chunk
    finally:
        close = getattr(inner, "close", None)
        if callable(close):
            close()


async def _arecorded(
    inner: AsyncIterator[Any], transitions: _Transitions
) -> AsyncIterator[Any]:
    """Async twin of :func:`_recorded`."""
    try:
        async for chunk in inner:
            transitions.record(chunk)
            yield chunk
    finally:
        aclose = getattr(inner, "aclose", None)
        if callable(aclose):
            await aclose()


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

    def _start_invoke(self, input: Any) -> tuple[_GraphRun, str, dict[str, Any] | None]:
        # ADR-0209 D2.2 (experimental): invoke() emits a start→end
        # StateTransition pair — a start marker at entry (input digest on both
        # sides: no transition observed yet) and the whole-invocation
        # transition on success. Digests always; payloads only at
        # forensic/air_gapped capture level (D5.2). Fail-open via the façade.
        run = _GraphRun(self)
        input_digest = _state_digest(input)
        input_payload = _state_payload(input, _payloads_enabled())
        _record.state_transition(0, input_digest, input_digest, state_before=input_payload)
        return run, input_digest, input_payload

    @staticmethod
    def _end_invoke(input_digest: str, input_payload: Any, result: Any) -> None:
        _record.state_transition(
            1, input_digest, _state_digest(result),
            state_before=input_payload,
            state_after=_state_payload(result, _payloads_enabled()),
        )

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        if _in_flight.get():
            return self._inner.invoke(input, config, **kwargs)
        run, input_digest, input_payload = self._start_invoke(input)
        token = _in_flight.set(True)
        try:
            result = self._inner.invoke(input, config, **kwargs)
            self._end_invoke(input_digest, input_payload, result)
            return result
        except BaseException as exc:
            record_outcome(run, exc)
            raise
        finally:
            _in_flight.reset(token)
            run.finish()

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        if _in_flight.get():
            return await self._inner.ainvoke(input, config, **kwargs)
        run, input_digest, input_payload = self._start_invoke(input)
        token = _in_flight.set(True)
        try:
            result = await self._inner.ainvoke(input, config, **kwargs)
            self._end_invoke(input_digest, input_payload, result)
            return result
        except BaseException as exc:
            record_outcome(run, exc)
            raise
        finally:
            _in_flight.reset(token)
            run.finish()

    def stream(self, input: Any, config: Any = None, **kwargs: Any) -> Iterator[Any]:
        """Wrap ``stream``; every ``stream_mode`` / ``subgraphs`` / ``version``
        keyword passes through untouched.

        The capsule opens here and closes when the returned iterator ends:
        read to the end, raised, closed early, or garbage-collected unread.
        """
        if _in_flight.get():
            return self._inner.stream(input, config, **kwargs)  # type: ignore[no-any-return]
        closer = Closer(_GraphRun(self))
        try:
            inner = self._inner.stream(input, config, **kwargs)
        except Exception as exc:
            closer.close(error=exc)
            raise
        guarded = guard_iterator(_recorded(inner, _Transitions(input)), closer, _in_flight)
        close_when_collected(guarded, closer)
        return guarded

    def astream(self, input: Any, config: Any = None, **kwargs: Any) -> AsyncIterator[Any]:
        """Async twin of :meth:`stream`; a run cancelled mid-stream is
        ``partial`` / ``cancelled``."""
        if _in_flight.get():
            return self._inner.astream(input, config, **kwargs)  # type: ignore[no-any-return]
        closer = Closer(_GraphRun(self))
        try:
            inner = self._inner.astream(input, config, **kwargs)
        except Exception as exc:
            closer.close(error=exc)
            raise
        guarded = guard_async_iterator(
            _arecorded(inner, _Transitions(input)), closer, _in_flight
        )
        close_when_collected(guarded, closer)
        return guarded

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

    Returns a wrapper with the same ``invoke()`` / ``ainvoke()`` /
    ``stream()`` / ``astream()`` interface.  Each call creates a nova run
    capsule (a nested call inside a wrapped run reuses the open one);
    wire-level hooks record every model and tool call automatically. A stream
    closed early or dropped unread is recorded as ``status: partial``.

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

    from novafabric._paths import adapter_default_runs_dir

    resolved_data_dir = data_dir or adapter_default_runs_dir()

    return _WrappedGraph(
        inner=graph,
        run_name=run_name or "langgraph-run",
        data_dir=resolved_data_dir,
        tags={"framework": "langgraph"},
    )
