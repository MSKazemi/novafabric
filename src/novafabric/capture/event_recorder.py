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
"""
from __future__ import annotations

import json
import logging
import threading
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


def get_current_recorder() -> EventRecorder | None:
    """Return the recorder active for the current capture run, or None."""
    return _current_recorder


def set_current_recorder(recorder: EventRecorder | None) -> None:
    """Set (or clear) the module-level recorder singleton.

    Called by ``CaptureOrchestrator.run()`` at run start and run end so that
    wire-level hooks can call ``get_current_recorder()`` without holding a
    reference to the orchestrator.
    """
    global _current_recorder
    with _singleton_lock:
        _current_recorder = recorder


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
    ) -> None:
        """Append a MemoryOperation to ``memory_operations.jsonl``. Fail-open."""
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
