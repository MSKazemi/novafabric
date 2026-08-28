"""Public façade for emitting extended capture events (ADR-0209 P1, experimental).

``novafabric.capture.record`` is the stable, documented entry point through
which user code and framework adapters emit the extended capture events
defined by ADR-0082 into whatever run is currently being captured:

.. code-block:: python

    from novafabric.capture import record

    record.file_event(operation="write", path="out/report.md", size_bytes=1024)
    record.guardrail(guardrail_name="pii-filter", outcome="blocked")
    record.evaluator(evaluator_name="answer-relevance", score=0.91)

Contract (normative in ``the private design/spec/extended-event-wiring-v0.md``):

- Every function resolves the current :class:`~novafabric.capture.event_recorder.EventRecorder`
  singleton **per call**. With no active capture run, each call is a silent
  no-op returning ``None`` — instrumented library code runs unchanged outside
  capture.
- With an active run, the event is appended to the run's dedicated JSONL
  stream. Recording is fail-open and thread-safe (delegated to
  :class:`EventRecorder`); a bookkeeping failure never reaches caller code.
- Signatures mirror ``EventRecorder.record_*`` minus ``run_id``/``capsule_id``,
  which are owned and injected by the active run.
- The façade passes caller-supplied payload fields through at every capture
  level (the caller owns their data); everything lands in streams covered by
  the secret scanner (ADR-0209 D5). The *default-path wirings* — including
  :func:`wrap_retriever` — populate payload-bearing fields only at
  ``forensic``/``air_gapped`` capture level (ADR-0021 §4).

``network_event`` and ``human_approval`` are deliberately **not** exposed:
both streams already have owning producers (the wire-level hooks and
``nova seal-propose`` respectively), and a second public entry point would
invite double-recording.
"""
from __future__ import annotations

import functools
import time
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from novafabric.capture.event_recorder import get_current_recorder
from novafabric.policies.capture_level import CaptureLevel, CaptureLevelPolicy

__all__ = [
    "active",
    "file_event",
    "state_transition",
    "memory_operation",
    "guardrail",
    "evaluator",
    "reranker",
    "vector_retrieval",
    "wrap_retriever",
]

_R = TypeVar("_R", bound=Sequence[Any])

#: Upper bound on per-document ``content`` captured by :func:`wrap_retriever`
#: at forensic/air_gapped level — keeps per-event size bounded (ADR-0021).
_MAX_DOC_CONTENT_CHARS = 4096


def active() -> bool:
    """Return ``True`` iff a capture run is currently receiving events."""
    return get_current_recorder() is not None


def _payloads_enabled() -> bool:
    """True when the capture level permits payload-bearing fields (D5.2).

    Default-path wirings (adapters, :func:`wrap_retriever`) consult this;
    at ``minimal``/``standard`` they emit structure, digests, counts, names,
    scores, and timings only. Never raises.
    """
    try:
        level = CaptureLevelPolicy.from_env().level
    except Exception:  # pragma: no cover — defensive; from_env never raises today
        return False
    return level in (CaptureLevel.FORENSIC, CaptureLevel.AIR_GAPPED)


def file_event(
    operation: str,
    path: str,
    *,
    size_bytes: int | None = None,
    success: bool = True,
    error: str | None = None,
    agent_id: str | None = None,
) -> None:
    """Record one file I/O operation into ``file_events.jsonl``.

    Façade-only: no automatic file-I/O capture exists (no file hook is
    installed by NovaFabric — future design, ADR-0209 D4).
    """
    recorder = get_current_recorder()
    if recorder is not None:
        recorder.record_file_event(
            operation=operation,
            path=path,
            size_bytes=size_bytes,
            success=success,
            error=error,
            agent_id=agent_id,
        )


def state_transition(
    step_index: int,
    state_digest_before: str,
    state_digest_after: str,
    *,
    agent_id: str | None = None,
    state_before: dict[str, Any] | None = None,
    state_after: dict[str, Any] | None = None,
) -> None:
    """Record one agent-step state transition into ``state_transitions.jsonl``."""
    recorder = get_current_recorder()
    if recorder is not None:
        recorder.record_state_transition(
            step_index=step_index,
            state_digest_before=state_digest_before,
            state_digest_after=state_digest_after,
            agent_id=agent_id,
            state_before=state_before,
            state_after=state_after,
        )


def memory_operation(
    operation: str,
    memory_key: str,
    *,
    relevance_score: float | None = None,
    freshness_seconds: float | None = None,
    agent_id: str | None = None,
    value: object | None = None,
    origin_run_id: str | None = None,
    origin_memory_key: str | None = None,
    origin_timestamp_utc: str | None = None,
) -> None:
    """Record one memory read/write into ``memory_operations.jsonl``.

    The ``origin_*`` fields feed the ADR-0143 memory-provenance lineage
    consumer (``novafabric.lineage.memory``) — recorded as a claim, not fact.
    """
    recorder = get_current_recorder()
    if recorder is not None:
        recorder.record_memory_operation(
            operation=operation,
            memory_key=memory_key,
            relevance_score=relevance_score,
            freshness_seconds=freshness_seconds,
            agent_id=agent_id,
            value=value,
            origin_run_id=origin_run_id,
            origin_memory_key=origin_memory_key,
            origin_timestamp_utc=origin_timestamp_utc,
        )


def guardrail(
    guardrail_name: str,
    outcome: str,
    *,
    category: str | None = None,
    score: float | None = None,
    agent_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Record one guardrail evaluation into ``guardrail_events.jsonl``."""
    recorder = get_current_recorder()
    if recorder is not None:
        recorder.record_guardrail(
            guardrail_name=guardrail_name,
            outcome=outcome,
            category=category,
            score=score,
            agent_id=agent_id,
            details=details,
        )


def evaluator(
    evaluator_name: str,
    *,
    score: float | None = None,
    label: str | None = None,
    passed: bool | None = None,
    dataset_id: str | None = None,
    agent_id: str | None = None,
    rationale: str | None = None,
) -> None:
    """Record one in-trace evaluator result into ``evaluator_events.jsonl``."""
    recorder = get_current_recorder()
    if recorder is not None:
        recorder.record_evaluator(
            evaluator_name=evaluator_name,
            score=score,
            label=label,
            passed=passed,
            dataset_id=dataset_id,
            agent_id=agent_id,
            rationale=rationale,
        )


def reranker(
    reranker_model: str,
    *,
    input_count: int | None = None,
    output_count: int | None = None,
    documents: list[Any] | None = None,
    agent_id: str | None = None,
) -> None:
    """Record one reranker application into ``reranker_events.jsonl``."""
    recorder = get_current_recorder()
    if recorder is not None:
        recorder.record_reranker(
            reranker_model=reranker_model,
            input_count=input_count,
            output_count=output_count,
            documents=documents,
            agent_id=agent_id,
        )


def vector_retrieval(
    vector_store: str,
    *,
    phase: str = "completed",
    operation: str = "query",
    collection: str | None = None,
    top_k: int | None = None,
    returned_count: int | None = None,
    duration_ms: float | None = None,
    error: str | None = None,
    documents: list[Any] | None = None,
    agent_id: str | None = None,
) -> None:
    """Record one vector-store retrieval span into ``vector_retrievals.jsonl``.

    ``phase`` selects the event type: ``"started"``, ``"completed"`` (default),
    or ``"failed"``.
    """
    recorder = get_current_recorder()
    if recorder is not None:
        recorder.record_vector_retrieval(
            vector_store=vector_store,
            phase=phase,
            operation=operation,
            collection=collection,
            top_k=top_k,
            returned_count=returned_count,
            duration_ms=duration_ms,
            error=error,
            documents=documents,
            agent_id=agent_id,
        )


def _documents_from(result: Sequence[Any]) -> list[dict[str, Any]]:
    """Best-effort ``RetrievedDocument`` payloads from a retriever result.

    Used by :func:`wrap_retriever` only at forensic/air_gapped level. Reads
    common id/content shapes (dicts, LangChain ``Document.page_content``,
    DSPy ``long_text``); anything unrecognized keeps a positional id with no
    content. Never raises.
    """
    id_keys = ("document_id", "id")
    content_keys = ("page_content", "content", "text", "long_text")

    def _first_str(get: Callable[[str], Any], keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = get(key)
            if isinstance(value, str) and value:
                return value
        return None

    docs: list[dict[str, Any]] = []
    try:
        items = list(result)
    except Exception:
        return docs
    for i, item in enumerate(items):
        doc_id: str | None = None
        content: str | None = None
        if isinstance(item, str):
            content = item
        elif isinstance(item, dict):
            doc_id = _first_str(item.get, id_keys)
            content = _first_str(item.get, content_keys)
        else:
            def _attr_get(key: str, _obj: Any = item) -> Any:
                return getattr(_obj, key, None)

            doc_id = _first_str(_attr_get, id_keys)
            content = _first_str(_attr_get, content_keys)
        docs.append({
            "document_id": doc_id or f"doc-{i}",
            "content": content[:_MAX_DOC_CONTENT_CHARS] if content else None,
        })
    return docs


def wrap_retriever(
    fn: Callable[..., _R],
    *,
    vector_store: str,
    collection: str | None = None,
    operation: str = "query",
) -> Callable[..., _R]:
    """Wrap a callable retriever so each call is recorded as a retrieval span.

    Emits ``VectorRetrievalStarted`` before the call, ``…Completed`` (with
    ``returned_count`` and ``duration_ms``) on return, and ``…Failed`` (with
    ``error``) on exception — then re-raises. By default only counts and
    timings are recorded; retrieved document payloads are included only at
    ``forensic``/``air_gapped`` capture level (ADR-0209 D5.2).

    Sync callables only in v0; an async variant is future design. Outside a
    capture run the wrapper adds one no-op call per phase and nothing else.

    Usage (LangChain retriever / DSPy Retrieve)::

        search = record.wrap_retriever(
            retriever.invoke, vector_store="qdrant", collection="docs")
        docs = search("what changed in v0.63?")
    """

    @functools.wraps(fn)
    def _instrumented(*args: Any, **kwargs: Any) -> _R:
        vector_retrieval(
            vector_store, phase="started", operation=operation,
            collection=collection,
        )
        t0 = time.monotonic()
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            vector_retrieval(
                vector_store, phase="failed", operation=operation,
                collection=collection,
                duration_ms=(time.monotonic() - t0) * 1000.0,
                error=str(exc),
            )
            raise
        duration_ms = (time.monotonic() - t0) * 1000.0
        try:
            returned_count: int | None = len(result)
        except Exception:
            returned_count = None
        documents = _documents_from(result) if _payloads_enabled() else None
        vector_retrieval(
            vector_store, phase="completed", operation=operation,
            collection=collection,
            returned_count=returned_count,
            duration_ms=duration_ms,
            documents=documents,
        )
        return result

    return _instrumented
