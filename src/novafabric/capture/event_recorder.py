"""Thread-safe recorder for the extended capsule event JSONL streams.

Writes to:
- ``file_events.jsonl``        (FileEvent)
- ``network_events.jsonl``     (NetworkEvent)
- ``human_approvals.jsonl``    (HumanApprovalEvent)
- ``state_transitions.jsonl``  (StateTransition)
- ``memory_operations.jsonl``  (MemoryOperation)
- ``guardrail_events.jsonl``   (GuardrailEvaluated)
- ``evaluator_events.jsonl``   (EvaluatorScored)
- ``reranker_events.jsonl``    (RerankerApplied)
- ``vector_retrievals.jsonl``  (VectorRetrieval{Started,Completed,Failed})

Design principle: every public method is fail-open. A bookkeeping failure
must never surface to the agent workflow. All exceptions are swallowed inside
``_append()``. This mirrors the pattern used by ``CapsuleWriter._append``.

Module-level singleton (``_current_recorder``) is set by the orchestrator at
run start / run end so that wire-level hooks (which do not receive the writer
as a constructor argument) can record network events without coupling to the
orchestrator or the hook installation sequence.

Since ADR-0224 D3 the singleton is a **fallback**, not the only answer: a
capture may bind a recorder to its own task with :func:`bind_recorder`, and
:func:`get_current_recorder` prefers that. Because the hooks resolve the
recorder when an event *fires* rather than when they are installed, one
installed set of hooks can serve several concurrent in-process captures, each
filing into its own capsule — and no hook's signature changes to allow it.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novafabric.capture.events import (
    EvaluatorEvent,
    FileEvent,
    GuardrailEvent,
    HumanApprovalEvent,
    MemoryOperationEvent,
    NetworkEvent,
    RerankerEvent,
    StateTransitionEvent,
    VectorRetrievalEvent,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known AI API host fragments — used by _is_ai_api()
# ---------------------------------------------------------------------------
_AI_API_HOSTS: tuple[str, ...] = (
    "api.openai.com",
    "api.anthropic.com",
    "api.cohere.ai",
    "api.together.xyz",
    "api.mistral.ai",
    "api.replicate.com",
    "bedrock-runtime.",
    "bedrock.",
    "generativelanguage.googleapis.com",
    "api.deepmind.com",
    "api.groq.com",
    "openrouter.ai",
    "api.perplexity.ai",
    "inference.azure.com",
    "services.ai.azure.com",
    "localhost:11434",
    "127.0.0.1:11434",
)


def _is_ai_api(url: str) -> bool:
    """Return True if *url* matches a known AI API endpoint.

    Uses the same substring-match strategy as the URL registry to avoid
    importing the registry here (which would create a circular dependency).
    """
    url_lower = url.lower()
    return any(fragment in url_lower for fragment in _AI_API_HOSTS)


# ---------------------------------------------------------------------------
# Module-level singleton — set by CaptureOrchestrator
# ---------------------------------------------------------------------------

_current_recorder: EventRecorder | None = None
_singleton_lock = threading.Lock()

#: Per-task recorder (ADR-0224 D3). Overrides the process-wide singleton for
#: the task that bound it, so two concurrent in-process captures each record
#: into their own capsule through **one** set of installed hooks — the hooks
#: resolve the recorder when an event fires, not when they are installed, so
#: nothing about their signatures changes.
_recorder_var: ContextVar[EventRecorder | None] = ContextVar(
    "novafabric_current_recorder", default=None
)

#: Per-task capsule writer (ADR-0224 D3, phase 2 — 2026-08-29).
#:
#: The recorder ContextVar above was landed on 2026-08-06 on the conclusion that
#: it alone gave concurrent captures their own capsules. Measuring that on
#: 2026-08-29 showed it covers ``NetworkEvent`` and nothing else: every wire hook
#: writes its **model call** — the richest record it produces — through the
#: ``self._writer`` it was constructed with, which belongs to whichever capture
#: won the hook race. So a second capture's model calls were still filed into the
#: first capture's capsule.
#:
#: Resolving the writer per task too closes that, and (like the recorder) needs no
#: hook signature to change, because the hooks resolve it when an event *fires*.
_writer_var: ContextVar[Any | None] = ContextVar(
    "novafabric_current_writer", default=None
)

#: Live bindings by handle. Exists so a binding can be released from a task
#: *other* than the one that created it — see :func:`unbind_capture`. Bounded
#: by the number of concurrent captures in the process (single digits), and
#: every entry is removed by the ``finally`` that pairs with its bind.
#:
#: Each entry holds the recorder **and** the writer, so the two can never drift
#: apart: one call binds a whole capture, one call releases it.
_bindings: dict[str, tuple[EventRecorder | None, Any | None]] = {}
_binding_lock = threading.Lock()


def get_current_recorder() -> EventRecorder | None:
    """Return the recorder active for the current capture run, or None.

    A recorder bound to this task wins; otherwise the process-wide singleton
    answers. That fallback is what keeps single-capture processes — the
    subprocess ``sitecustomize`` loader and the orchestrator — working exactly
    as before, and what makes a capture started on a bare thread still record:
    **threads do not inherit context** (ADR-0224 D3), so a thread that binds
    nothing sees the singleton rather than nothing at all.
    """
    bound = _recorder_var.get()
    return bound if bound is not None else _current_recorder


def set_current_recorder(recorder: EventRecorder | None) -> None:
    """Set (or clear) the module-level recorder singleton.

    Called by ``CaptureOrchestrator.run()`` at run start and run end so that
    wire-level hooks can call ``get_current_recorder()`` without holding a
    reference to the orchestrator.

    Process-wide. For one capture among several in a process, bind a task-local
    recorder with :func:`bind_recorder` instead.
    """
    global _current_recorder
    with _singleton_lock:
        _current_recorder = recorder


def clear_current_recorder(recorder: EventRecorder) -> bool:
    """Clear the singleton, but only if *recorder* is still the one installed.

    Returns True if the slot was cleared. This is the teardown half of
    :func:`set_current_recorder` and exists because an unconditional
    ``set_current_recorder(None)`` is not composable: with two captures
    overlapping in one process, whichever finishes first blanks the slot the
    other is still recording through, and every ``record_*`` path is fail-open,
    so the loser's events vanish without an error, a log line, or a gap marker.

    No shipped path runs two orchestrators in one process — the daemon forks per
    worker, ``run_experiment`` iterates its dataset sequentially, and the CLI
    runs one command. The guard is therefore cheap insurance on a hazard rather
    than a fix for a live outage, and it belongs here because this module's own
    docstring invites callers to run concurrent in-process captures.

    Idempotent and never raises: teardown runs in a ``finally`` beside the
    workload being captured.
    """
    global _current_recorder
    with _singleton_lock:
        if _current_recorder is not recorder:
            return False
        _current_recorder = None
        return True


def bind_capture(
    recorder: EventRecorder | None = None,
    writer: Any | None = None,
) -> str:
    """Bind a whole capture — its recorder *and* its writer — to the current task.

    Returns a release handle for :func:`unbind_capture`.

    The handle is deliberately **not** a :class:`contextvars.Token`, which is the
    second constraint ADR-0224 D3 records: ``bedrock_agentcore`` tears down from
    inside a generator that is consumed later, possibly in a different task, and
    a ``Token`` may only be reset in the context that produced it. Releasing with
    one from elsewhere raises ``ValueError`` and would wedge the binding for the
    life of the process.

    The boundary is narrower than it looks: coroutines merely ``await``-ed in
    sequence share one context, so a Token would work there. It takes a separate
    ``Task`` — or a thread — to copy the context and break the reset. Both cases
    are asserted in ``tests/capture/test_task_scoped_recorder.py`` and
    ``tests/capture/test_task_scoped_writer.py``.

    Binding both halves together is the point: a capture whose recorder is
    task-scoped but whose writer is not files its network events correctly and
    its model calls into somebody else's capsule, which is precisely the defect
    this function was added to close.
    """
    handle = secrets.token_urlsafe(16)
    with _binding_lock:
        _bindings[handle] = (recorder, writer)
    if recorder is not None:
        _recorder_var.set(recorder)
    if writer is not None:
        _writer_var.set(writer)
    return handle


def unbind_capture(handle: str) -> bool:
    """Release a binding made by :func:`bind_capture`.

    Returns True if *handle* was live. Safe to call from any task, more than
    once, and after the binding task has finished: an unknown handle is a no-op
    rather than an error, because a teardown path must never raise into the
    workload it is capturing.

    Each slot is cleared only if it still holds *this* binding's object, so a
    nested capture that bound after this one cannot be blanked by this release.
    Clearing a context variable only has an effect in the task that holds it;
    other tasks' contexts are discarded with the task, so nothing leaks.
    """
    with _binding_lock:
        entry = _bindings.pop(handle, None)
    if entry is None:
        return False
    recorder, writer = entry
    if recorder is not None and _recorder_var.get() is recorder:
        _recorder_var.set(None)
    if writer is not None and _writer_var.get() is writer:
        _writer_var.set(None)
    return True


def get_current_writer(default: Any = None) -> Any:
    """Return the capsule writer active for the current capture, else *default*.

    The hooks pass their own ``self._writer`` as *default*, so a process running
    a single capture — and any thread, which inherits no context — resolves
    exactly the writer it did before this function existed. Only a task that
    explicitly bound a capture scope is redirected.

    Typed ``Any`` rather than ``CapsuleWriter`` on purpose: this module must not
    import :mod:`novafabric.capture.capsule`, and the return is only ever used to
    call the same ``append_*`` methods the caller's own default supports. The
    narrowing that matters — *never None when the caller passed a writer* — is
    guaranteed by the fallback, not by the annotation.
    """
    bound = _writer_var.get()
    return bound if bound is not None else default


def bind_recorder(recorder: EventRecorder) -> str:
    """Bind *recorder* to the current task and return a release handle.

    Kept as the narrow form of :func:`bind_capture` for callers that have no
    writer to bind. Prefer :func:`bind_capture` for a real capture: binding the
    recorder alone leaves model calls filing into the hook owner's capsule.
    """
    return bind_capture(recorder=recorder)


def unbind_recorder(handle: str) -> bool:
    """Release a binding made by :func:`bind_recorder` or :func:`bind_capture`."""
    return unbind_capture(handle)


# ---------------------------------------------------------------------------
# EventRecorder
# ---------------------------------------------------------------------------


class EventRecorder:
    """Thread-safe recorder for capsule event JSONL files.

    Each public ``record_*`` method is fail-open: any exception is silently
    swallowed so a bookkeeping failure can never block the agent workflow.
    """

    def __init__(self, capsule_dir: Path, run_id: str, capsule_id: str) -> None:
        self._capsule_dir = capsule_dir
        self._run_id = run_id
        self._capsule_id = capsule_id
        self._lock = threading.Lock()
        # Fail-open observability: swallowed failures are counted per stream
        # (bounded: one key per JSONL stream) so capture loss is visible in
        # logs and, via finalize_health(), in the capsule itself.
        self._drops: dict[str, int] = {}
        self._warned: set[str] = set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _note_drop(self, stream: str) -> None:
        """Count one dropped event for *stream*; warn once per stream.

        Must itself never raise — it runs inside fail-open except blocks.
        """
        try:
            with self._lock:
                self._drops[stream] = self._drops.get(stream, 0) + 1
                first = stream not in self._warned
                if first:
                    self._warned.add(stream)
            if first:
                logger.warning(
                    "capture fail-open: dropping event(s) for %s "
                    "(run %s); further drops for this stream are counted "
                    "silently — see capture-health.json",
                    stream,
                    self._run_id,
                )
        except Exception:  # pragma: no cover — last-resort guard
            pass

    def _append(self, filename: str, data: dict) -> None:  # type: ignore[type-arg]
        """Append a JSON record to a JSONL file. Never raises."""
        try:
            path = self._capsule_dir / filename
            line = json.dumps(data, separators=(",", ":")) + "\n"
            with self._lock:
                with path.open("a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            # fail-open: never block the agent workflow — but never lose
            # the fact that an event was lost, either.
            self._note_drop(filename)

    @property
    def drop_counts(self) -> dict[str, int]:
        """Events dropped by fail-open handling, keyed by JSONL stream."""
        with self._lock:
            return dict(self._drops)

    def finalize_health(self, capsule_dir: Path | None = None) -> None:
        """Write ``capture-health.json`` if any events were dropped.

        Clean runs write nothing, so capsules without capture loss are
        byte-identical to earlier versions. Fail-open like everything else.
        """
        try:
            drops = self.drop_counts
            if not drops:
                return
            target = (capsule_dir or self._capsule_dir) / "capture-health.json"
            report = {
                "run_id": self._run_id,
                "capsule_id": self._capsule_id,
                "generated_at": self._utc_now(),
                "dropped_events": drops,
                "note": (
                    "Counts of events the fail-open capture layer could not "
                    "persist; the workload was never blocked (ADR-0021)."
                ),
            }
            target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass  # fail-open

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_file_event(
        self,
        operation: str,
        path: str,
        size_bytes: int | None = None,
        success: bool = True,
        error: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Append a FileEvent to ``file_events.jsonl``. Fail-open."""
        try:
            event = FileEvent(
                run_id=self._run_id,
                capsule_id=self._capsule_id,
                timestamp_utc=self._utc_now(),
                operation=operation,  # type: ignore[arg-type]
                path=path,
                size_bytes=size_bytes,
                success=success,
                error=error,
                agent_id=agent_id,
            )
            self._append("file_events.jsonl", event.model_dump())
        except Exception:
            pass  # fail-open

    def record_network_event(
        self,
        method: str,
        url: str,
        host: str,
        status_code: int | None = None,
        response_size_bytes: int | None = None,
        duration_ms: float | None = None,
        library: str = "unknown",
        is_ai_api: bool = False,
        port: int | None = None,
    ) -> None:
        """Append a NetworkEvent to ``network_events.jsonl``. Fail-open."""
        try:
            event = NetworkEvent(
                run_id=self._run_id,
                capsule_id=self._capsule_id,
                timestamp_utc=self._utc_now(),
                method=method,
                url=url,
                host=host,
                port=port,
                status_code=status_code,
                response_size_bytes=response_size_bytes,
                duration_ms=duration_ms,
                library=library,
                is_ai_api=is_ai_api,
            )
            self._append("network_events.jsonl", event.model_dump())
        except Exception:
            pass  # fail-open

    def record_human_approval(
        self,
        approver_id: str,
        action: str,
        target_run_id: str,
        rationale: str | None = None,
        policy_version: str | None = None,
        seal_bundle_path: str | None = None,
    ) -> None:
        """Append a HumanApprovalEvent to ``human_approvals.jsonl``. Fail-open."""
        try:
            event = HumanApprovalEvent(
                run_id=self._run_id,
                capsule_id=self._capsule_id,
                timestamp_utc=self._utc_now(),
                approver_id=approver_id,
                action=action,  # type: ignore[arg-type]
                target_run_id=target_run_id,
                rationale=rationale,
                policy_version=policy_version,
                seal_bundle_path=seal_bundle_path,
            )
            self._append("human_approvals.jsonl", event.model_dump())
        except Exception:
            pass  # fail-open

    # ------------------------------------------------------------------
    # Extended span taxonomy (ADR-0082, gap-011) — capture-side wiring.
    #
    # Each method maps to one of the 8 new CapsuleEventType members and writes
    # to a dedicated JSONL stream, tagging every record with an ``event_type``
    # discriminator so a single reader can distinguish them. Same fail-open
    # contract as the streams above.
    # ------------------------------------------------------------------

    def _append_typed(self, filename: str, event_type: str, data: dict) -> None:  # type: ignore[type-arg]
        data = {"event_type": event_type, **data}
        self._append(filename, data)

    def record_state_transition(
        self,
        step_index: int,
        state_digest_before: str,
        state_digest_after: str,
        agent_id: str | None = None,
        state_before: dict[str, Any] | None = None,
        state_after: dict[str, Any] | None = None,
    ) -> None:
        """Append a StateTransition to ``state_transitions.jsonl``. Fail-open."""
        try:
            event = StateTransitionEvent(
                run_id=self._run_id,
                capsule_id=self._capsule_id,
                timestamp_utc=self._utc_now(),
                step_index=step_index,
                state_digest_before=state_digest_before,
                state_digest_after=state_digest_after,
                agent_id=agent_id,
                state_before=state_before,
                state_after=state_after,
            )
            self._append_typed("state_transitions.jsonl", "StateTransition", event.model_dump())
        except Exception:
            pass  # fail-open

    def record_memory_operation(
        self,
        operation: str,
        memory_key: str,
        relevance_score: float | None = None,
        freshness_seconds: float | None = None,
        agent_id: str | None = None,
        value: object | None = None,
        origin_run_id: str | None = None,
        origin_memory_key: str | None = None,
        origin_timestamp_utc: str | None = None,
    ) -> None:
        """Append a MemoryOperation to ``memory_operations.jsonl``. Fail-open.

        The ``origin_*`` arguments (ADR-0143 P1) record what the caller
        believed it was reading. They are recorded as a claim, not as fact —
        see ``novafabric.lineage.memory``.
        """
        try:
            event = MemoryOperationEvent(
                run_id=self._run_id,
                capsule_id=self._capsule_id,
                timestamp_utc=self._utc_now(),
                operation=operation,  # type: ignore[arg-type]
                memory_key=memory_key,
                relevance_score=relevance_score,
                freshness_seconds=freshness_seconds,
                agent_id=agent_id,
                value=value,
                origin_run_id=origin_run_id,
                origin_memory_key=origin_memory_key,
                origin_timestamp_utc=origin_timestamp_utc,
            )
            self._append_typed("memory_operations.jsonl", "MemoryOperation", event.model_dump())
        except Exception:
            pass  # fail-open

    def record_guardrail(
        self,
        guardrail_name: str,
        outcome: str,
        category: str | None = None,
        score: float | None = None,
        agent_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append a GuardrailEvaluated to ``guardrail_events.jsonl``. Fail-open."""
        try:
            event = GuardrailEvent(
                run_id=self._run_id,
                capsule_id=self._capsule_id,
                timestamp_utc=self._utc_now(),
                guardrail_name=guardrail_name,
                outcome=outcome,  # type: ignore[arg-type]
                category=category,
                score=score,
                agent_id=agent_id,
                details=details,
            )
            self._append_typed("guardrail_events.jsonl", "GuardrailEvaluated", event.model_dump())
        except Exception:
            pass  # fail-open

    def record_evaluator(
        self,
        evaluator_name: str,
        score: float | None = None,
        label: str | None = None,
        passed: bool | None = None,
        dataset_id: str | None = None,
        agent_id: str | None = None,
        rationale: str | None = None,
    ) -> None:
        """Append an EvaluatorScored to ``evaluator_events.jsonl``. Fail-open."""
        try:
            event = EvaluatorEvent(
                run_id=self._run_id,
                capsule_id=self._capsule_id,
                timestamp_utc=self._utc_now(),
                evaluator_name=evaluator_name,
                score=score,
                label=label,
                passed=passed,
                dataset_id=dataset_id,
                agent_id=agent_id,
                rationale=rationale,
            )
            self._append_typed("evaluator_events.jsonl", "EvaluatorScored", event.model_dump())
        except Exception:
            pass  # fail-open

    def record_reranker(
        self,
        reranker_model: str,
        input_count: int | None = None,
        output_count: int | None = None,
        documents: list[Any] | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Append a RerankerApplied to ``reranker_events.jsonl``. Fail-open."""
        try:
            event = RerankerEvent(
                run_id=self._run_id,
                capsule_id=self._capsule_id,
                timestamp_utc=self._utc_now(),
                reranker_model=reranker_model,
                input_count=input_count,
                output_count=output_count,
                documents=documents or [],
                agent_id=agent_id,
            )
            self._append_typed("reranker_events.jsonl", "RerankerApplied", event.model_dump())
        except Exception:
            pass  # fail-open

    def record_vector_retrieval(
        self,
        vector_store: str,
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
        """Append a VectorRetrieval{Started,Completed,Failed} to
        ``vector_retrievals.jsonl``. ``phase`` selects the CapsuleEventType
        member. Fail-open."""
        try:
            phase_to_type = {
                "started": "VectorRetrievalStarted",
                "completed": "VectorRetrievalCompleted",
                "failed": "VectorRetrievalFailed",
            }
            event_type = phase_to_type.get(phase, "VectorRetrievalCompleted")
            event = VectorRetrievalEvent(
                run_id=self._run_id,
                capsule_id=self._capsule_id,
                timestamp_utc=self._utc_now(),
                vector_store=vector_store,
                operation=operation,  # type: ignore[arg-type]
                collection=collection,
                top_k=top_k,
                returned_count=returned_count,
                duration_ms=duration_ms,
                status="error" if phase == "failed" else "ok",
                error=error,
                documents=documents or [],
                agent_id=agent_id,
            )
            self._append_typed("vector_retrievals.jsonl", event_type, event.model_dump())
        except Exception:
            pass  # fail-open
