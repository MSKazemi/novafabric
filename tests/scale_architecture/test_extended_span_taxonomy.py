"""Tests for the extended span taxonomy (gap-011, ADR-0082).

Covers the 8 new ``CapsuleEventType`` members and the 6 new Pydantic event
models. Verifies:

- each new event type validates inside a ``RunEnvelope``;
- each new event model round-trips (model -> json -> model);
- raw content fields are opt-in (default ``None``) — ADR-0021 / ADR-0082;
- absence of the new types/models is backward-compatible (old envelopes and
  old event streams still validate unchanged).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from novafabric.capture.events import (
    EvaluatorEvent,
    FileEvent,
    GuardrailEvent,
    MemoryOperationEvent,
    RerankedDocument,
    RerankerEvent,
    RetrievedDocument,
    StateTransitionEvent,
    VectorRetrievalEvent,
)
from novafabric.schemas.event_schema import CapsuleEventType, RunEnvelope

_NOW = datetime.now(tz=timezone.utc)
_NOW_ISO = _NOW.isoformat()

# ---------------------------------------------------------------------------
# New enum members
# ---------------------------------------------------------------------------

NEW_EVENT_TYPES = [
    "StateTransition",
    "MemoryOperation",
    "GuardrailEvaluated",
    "EvaluatorScored",
    "RerankerApplied",
    "VectorRetrievalStarted",
    "VectorRetrievalCompleted",
    "VectorRetrievalFailed",
]


class TestExtendedEnum:
    def test_total_count(self) -> None:
        # 25 original (ADR-0066) + 8 new (ADR-0082) + 1 EndpointRouted (ADR-0220)
        assert len(CapsuleEventType) == 34

    @pytest.mark.parametrize("name", NEW_EVENT_TYPES)
    def test_new_type_exists(self, name: str) -> None:
        assert CapsuleEventType(name).value == name

    @pytest.mark.parametrize("name", NEW_EVENT_TYPES)
    def test_new_type_validates_in_envelope(self, name: str) -> None:
        env = RunEnvelope(
            run_id="run-001",
            event_type=name,
            tenant_id="tenant-acme",
            capsule_id="cap-001",
            timestamp=_NOW,
        )
        assert env.event_type is CapsuleEventType(name)


# ---------------------------------------------------------------------------
# New event models — round-trip + opt-in content
# ---------------------------------------------------------------------------


class TestStateTransitionEvent:
    def test_minimal_roundtrip(self) -> None:
        ev = StateTransitionEvent(
            run_id="r1",
            capsule_id="c1",
            timestamp_utc=_NOW_ISO,
            step_index=3,
            state_digest_before="sha256:aaa",
            state_digest_after="sha256:bbb",
        )
        # content opt-in
        assert ev.state_before is None
        assert ev.state_after is None
        assert StateTransitionEvent.model_validate_json(ev.model_dump_json()) == ev

    def test_opt_in_content(self) -> None:
        ev = StateTransitionEvent(
            run_id="r1",
            capsule_id="c1",
            timestamp_utc=_NOW_ISO,
            step_index=0,
            state_digest_before="sha256:aaa",
            state_digest_after="sha256:bbb",
            state_before={"goal": "x"},
            state_after={"goal": "x", "done": True},
        )
        assert ev.state_after == {"goal": "x", "done": True}


class TestMemoryOperationEvent:
    def test_roundtrip_and_optin(self) -> None:
        ev = MemoryOperationEvent(
            run_id="r1",
            capsule_id="c1",
            timestamp_utc=_NOW_ISO,
            operation="read",
            memory_key="user_pref",
            relevance_score=0.82,
            freshness_seconds=12.5,
        )
        assert ev.value is None  # opt-in content
        assert MemoryOperationEvent.model_validate_json(ev.model_dump_json()) == ev

    def test_invalid_operation_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MemoryOperationEvent(
                run_id="r1",
                capsule_id="c1",
                timestamp_utc=_NOW_ISO,
                operation="teleport",  # not in Literal set
                memory_key="k",
            )


class TestGuardrailEvent:
    def test_roundtrip(self) -> None:
        ev = GuardrailEvent(
            run_id="r1",
            capsule_id="c1",
            timestamp_utc=_NOW_ISO,
            guardrail_name="pii_filter",
            outcome="blocked",
            category="privacy",
            score=0.97,
        )
        assert ev.details is None  # opt-in
        assert GuardrailEvent.model_validate_json(ev.model_dump_json()) == ev


class TestEvaluatorEvent:
    def test_roundtrip(self) -> None:
        ev = EvaluatorEvent(
            run_id="r1",
            capsule_id="c1",
            timestamp_utc=_NOW_ISO,
            evaluator_name="answer_relevance",
            score=0.74,
            label="pass",
            passed=True,
            dataset_id="ds-42",
        )
        assert ev.rationale is None  # opt-in
        assert EvaluatorEvent.model_validate_json(ev.model_dump_json()) == ev


class TestRerankerEvent:
    def test_roundtrip_with_bounded_docs(self) -> None:
        ev = RerankerEvent(
            run_id="r1",
            capsule_id="c1",
            timestamp_utc=_NOW_ISO,
            reranker_model="bge-reranker",
            input_count=10,
            output_count=2,
            documents=[
                RerankedDocument(document_id="d1", score=0.9, rank=0),
                RerankedDocument(document_id="d2", score=0.8, rank=1),
            ],
        )
        assert all(d.content is None for d in ev.documents)  # opt-in
        assert RerankerEvent.model_validate_json(ev.model_dump_json()) == ev


class TestVectorRetrievalEvent:
    def test_roundtrip(self) -> None:
        ev = VectorRetrievalEvent(
            run_id="r1",
            capsule_id="c1",
            timestamp_utc=_NOW_ISO,
            vector_store="qdrant",
            operation="query",
            collection="docs",
            top_k=5,
            returned_count=2,
            duration_ms=14.2,
            status="ok",
            documents=[
                RetrievedDocument(document_id="d1", score=0.91),
                RetrievedDocument(document_id="d2", score=0.77),
            ],
        )
        assert all(d.content is None for d in ev.documents)  # opt-in
        assert VectorRetrievalEvent.model_validate_json(ev.model_dump_json()) == ev


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatible:
    def test_old_envelope_still_validates(self) -> None:
        env = RunEnvelope(
            run_id="run-legacy",
            event_type="RunStarted",
            tenant_id="t",
            capsule_id="c",
            timestamp=_NOW,
        )
        assert env.event_type is CapsuleEventType.RunStarted

    def test_old_event_model_unchanged(self) -> None:
        # FileEvent predates ADR-0082 and must construct exactly as before.
        ev = FileEvent(
            run_id="r1",
            capsule_id="c1",
            timestamp_utc=_NOW_ISO,
            operation="read",
            path="/tmp/x",
        )
        assert ev.success is True
        assert FileEvent.model_validate_json(ev.model_dump_json()) == ev
