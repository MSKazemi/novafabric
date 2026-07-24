"""Tests for the public extended-event façade ``novafabric.capture.record``.

ADR-0209 P1 acceptance: silent no-op without an active run; correct
stream/payload through a live recorder for every event type; ``active()``
truth table; capture-level interaction for ``wrap_retriever``; redaction of
seeded secrets in the extended streams (byte-checked); thread-safety smoke;
no-op overhead micro-benchmark.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from novafabric.capture import record
from novafabric.capture.event_recorder import EventRecorder, set_current_recorder

RUN_ID = "01HXFACADE0000000000000000"


@pytest.fixture(autouse=True)
def _clean_recorder_singleton() -> Any:
    """Every test starts and ends with no active recorder."""
    set_current_recorder(None)
    yield
    set_current_recorder(None)


@pytest.fixture()
def capsule_dir(tmp_path: Path) -> Path:
    d = tmp_path / RUN_ID
    d.mkdir()
    return d


@pytest.fixture()
def live_recorder(capsule_dir: Path) -> EventRecorder:
    recorder = EventRecorder(
        capsule_dir=capsule_dir, run_id=RUN_ID, capsule_id=RUN_ID
    )
    set_current_recorder(recorder)
    return recorder


def _read_stream(capsule_dir: Path, filename: str) -> list[dict[str, Any]]:
    path = capsule_dir / filename
    assert path.exists(), f"{filename} must be written"
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


# All seven façade functions with minimal valid arguments, keyed by stream.
_CALLS: list[tuple[str, Any]] = [
    ("file_events.jsonl",
     lambda: record.file_event(operation="write", path="/out/report.md",
                               size_bytes=1024)),
    ("state_transitions.jsonl",
     lambda: record.state_transition(3, "sha256:aa", "sha256:bb",
                                     agent_id="planner")),
    ("memory_operations.jsonl",
     lambda: record.memory_operation("read", "user:prefs",
                                     relevance_score=0.7,
                                     origin_run_id="01HXORIGIN000000000000000")),
    ("guardrail_events.jsonl",
     lambda: record.guardrail("pii-filter", "blocked", score=0.99)),
    ("evaluator_events.jsonl",
     lambda: record.evaluator("answer-relevance", score=0.91, passed=True)),
    ("reranker_events.jsonl",
     lambda: record.reranker("cohere-rerank-3", input_count=40,
                             output_count=8)),
    ("vector_retrievals.jsonl",
     lambda: record.vector_retrieval("qdrant", top_k=8, returned_count=8)),
]


# ── no active run: silent no-op ───────────────────────────────────────────


class TestNoActiveRun:
    @pytest.mark.parametrize(
        ("stream", "call"), _CALLS, ids=[s for s, _ in _CALLS]
    )
    def test_each_function_is_safe_noop(
        self, tmp_path: Path, stream: str, call: Any
    ) -> None:
        assert call() is None  # never raises, returns None
        assert not list(tmp_path.rglob("*.jsonl"))  # nothing written anywhere

    def test_active_false(self) -> None:
        assert record.active() is False

    def test_wrap_retriever_passthrough_without_run(self) -> None:
        wrapped = record.wrap_retriever(
            lambda q: [q, "hit"], vector_store="qdrant"
        )
        assert wrapped("needle") == ["needle", "hit"]

    def test_noop_overhead_is_negligible(self) -> None:
        """Spec overhead bound: no-op path is one call + one global read."""
        n = 100_000
        t0 = time.monotonic()
        for _ in range(n):
            record.guardrail("g", "passed")
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"{n} no-op calls took {elapsed:.2f}s"


# ── live recorder: correct stream + payload per event type ────────────────


class TestRecordsThroughLiveRecorder:
    def test_active_true(self, live_recorder: EventRecorder) -> None:
        assert record.active() is True

    @pytest.mark.parametrize(
        ("stream", "call"), _CALLS, ids=[s for s, _ in _CALLS]
    )
    def test_each_function_writes_its_stream(
        self, live_recorder: EventRecorder, capsule_dir: Path,
        stream: str, call: Any,
    ) -> None:
        call()
        events = _read_stream(capsule_dir, stream)
        assert len(events) == 1
        # run identity is injected by the active run, not the caller.
        assert events[0]["run_id"] == RUN_ID
        assert events[0]["capsule_id"] == RUN_ID

    def test_file_event_payload(
        self, live_recorder: EventRecorder, capsule_dir: Path
    ) -> None:
        record.file_event(operation="write", path="/out/report.md",
                          size_bytes=1024, agent_id="writer")
        (event,) = _read_stream(capsule_dir, "file_events.jsonl")
        assert event["operation"] == "write"
        assert event["path"] == "/out/report.md"
        assert event["size_bytes"] == 1024
        assert event["agent_id"] == "writer"
        assert event["success"] is True

    def test_state_transition_payload(
        self, live_recorder: EventRecorder, capsule_dir: Path
    ) -> None:
        record.state_transition(3, "sha256:aa", "sha256:bb",
                                agent_id="planner",
                                state_after={"messages": ["hi"]})
        (event,) = _read_stream(capsule_dir, "state_transitions.jsonl")
        assert event["event_type"] == "StateTransition"
        assert event["step_index"] == 3
        assert event["state_digest_before"] == "sha256:aa"
        assert event["state_digest_after"] == "sha256:bb"
        # Façade passes caller-supplied payloads through at every level —
        # the caller owns their data (spec §"Capture-level interaction").
        assert event["state_after"] == {"messages": ["hi"]}

    def test_memory_operation_provenance_fields(
        self, live_recorder: EventRecorder, capsule_dir: Path
    ) -> None:
        record.memory_operation(
            "read", "user:prefs", relevance_score=0.7,
            origin_run_id="01HXORIGIN000000000000000",
            origin_memory_key="user:prefs",
            origin_timestamp_utc="2026-07-01T00:00:00+00:00",
        )
        (event,) = _read_stream(capsule_dir, "memory_operations.jsonl")
        assert event["event_type"] == "MemoryOperation"
        assert event["operation"] == "read"
        assert event["origin_run_id"] == "01HXORIGIN000000000000000"

    def test_guardrail_payload(
        self, live_recorder: EventRecorder, capsule_dir: Path
    ) -> None:
        record.guardrail("pii-filter", "blocked", category="pii", score=0.99,
                         details={"rule": "email"})
        (event,) = _read_stream(capsule_dir, "guardrail_events.jsonl")
        assert event["event_type"] == "GuardrailEvaluated"
        assert event["guardrail_name"] == "pii-filter"
        assert event["outcome"] == "blocked"
        assert event["details"] == {"rule": "email"}

    def test_evaluator_payload(
        self, live_recorder: EventRecorder, capsule_dir: Path
    ) -> None:
        record.evaluator("answer-relevance", score=0.91, passed=True,
                         rationale="grounded in retrieved context")
        (event,) = _read_stream(capsule_dir, "evaluator_events.jsonl")
        assert event["event_type"] == "EvaluatorScored"
        assert event["evaluator_name"] == "answer-relevance"
        assert event["score"] == 0.91
        assert event["rationale"] == "grounded in retrieved context"

    def test_reranker_payload(
        self, live_recorder: EventRecorder, capsule_dir: Path
    ) -> None:
        record.reranker("cohere-rerank-3", input_count=40, output_count=8,
                        documents=[{"document_id": "d1", "rank": 1}])
        (event,) = _read_stream(capsule_dir, "reranker_events.jsonl")
        assert event["event_type"] == "RerankerApplied"
        assert event["reranker_model"] == "cohere-rerank-3"
        assert event["documents"][0]["document_id"] == "d1"

    def test_vector_retrieval_phase_selects_event_type(
        self, live_recorder: EventRecorder, capsule_dir: Path
    ) -> None:
        record.vector_retrieval("qdrant", phase="started", top_k=8)
        record.vector_retrieval("qdrant", phase="completed", returned_count=8)
        record.vector_retrieval("qdrant", phase="failed", error="timeout")
        events = _read_stream(capsule_dir, "vector_retrievals.jsonl")
        assert [e["event_type"] for e in events] == [
            "VectorRetrievalStarted",
            "VectorRetrievalCompleted",
            "VectorRetrievalFailed",
        ]
        assert events[2]["status"] == "error"
        assert events[2]["error"] == "timeout"

    def test_thread_safety_smoke(
        self, live_recorder: EventRecorder, capsule_dir: Path
    ) -> None:
        n_threads, per_thread = 8, 50

        def emit() -> None:
            for i in range(per_thread):
                record.guardrail(f"g-{i}", "passed")

        threads = [threading.Thread(target=emit) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        events = _read_stream(capsule_dir, "guardrail_events.jsonl")
        assert len(events) == n_threads * per_thread  # no torn/lost lines
        assert live_recorder.drop_counts == {}


# ── wrap_retriever ────────────────────────────────────────────────────────


class TestWrapRetriever:
    @pytest.fixture(autouse=True)
    def _standard_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOVA_CAPTURE_LEVEL", raising=False)

    def test_success_emits_started_and_completed(
        self, live_recorder: EventRecorder, capsule_dir: Path
    ) -> None:
        wrapped = record.wrap_retriever(
            lambda q, top_k=3: ["a", "b", "c"],
            vector_store="qdrant", collection="docs",
        )
        assert wrapped("query") == ["a", "b", "c"]
        started, completed = _read_stream(capsule_dir, "vector_retrievals.jsonl")
        assert started["event_type"] == "VectorRetrievalStarted"
        assert started["collection"] == "docs"
        assert completed["event_type"] == "VectorRetrievalCompleted"
        assert completed["returned_count"] == 3
        assert completed["duration_ms"] >= 0.0
        # standard level: no document payloads from the default-path wiring.
        assert completed["documents"] == []

    def test_exception_emits_failed_and_propagates(
        self, live_recorder: EventRecorder, capsule_dir: Path
    ) -> None:
        def boom(q: str) -> list[str]:
            raise ConnectionError("vector store unreachable")

        wrapped = record.wrap_retriever(boom, vector_store="qdrant")
        with pytest.raises(ConnectionError, match="unreachable"):
            wrapped("query")
        started, failed = _read_stream(capsule_dir, "vector_retrievals.jsonl")
        assert started["event_type"] == "VectorRetrievalStarted"
        assert failed["event_type"] == "VectorRetrievalFailed"
        assert failed["status"] == "error"
        assert "unreachable" in failed["error"]

    def test_forensic_level_records_documents(
        self, live_recorder: EventRecorder, capsule_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")

        class FakeDoc:
            id = "doc-lc-1"
            page_content = "retrieved text body"

        wrapped = record.wrap_retriever(
            lambda q: [
                FakeDoc(),
                {"id": "doc-d2", "text": "dict-shaped hit"},
                "bare string hit",
                object(),
            ],
            vector_store="chroma",
        )
        wrapped("q")
        _, completed = _read_stream(capsule_dir, "vector_retrievals.jsonl")
        docs = completed["documents"]
        assert [d["document_id"] for d in docs] == [
            "doc-lc-1", "doc-d2", "doc-2", "doc-3",
        ]
        assert docs[0]["content"] == "retrieved text body"
        assert docs[1]["content"] == "dict-shaped hit"
        assert docs[2]["content"] == "bare string hit"
        assert docs[3]["content"] is None

    def test_forensic_document_content_is_bounded(
        self, live_recorder: EventRecorder, capsule_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
        wrapped = record.wrap_retriever(
            lambda q: ["x" * 100_000], vector_store="chroma"
        )
        wrapped("q")
        _, completed = _read_stream(capsule_dir, "vector_retrievals.jsonl")
        assert len(completed["documents"][0]["content"]) == 4096

    def test_non_sized_result_records_none_count(
        self, live_recorder: EventRecorder, capsule_dir: Path
    ) -> None:
        wrapped = record.wrap_retriever(
            lambda q: iter(["a"]),  # type: ignore[arg-type]
            vector_store="qdrant",
        )
        wrapped("q")
        _, completed = _read_stream(capsule_dir, "vector_retrievals.jsonl")
        assert completed["returned_count"] is None

    def test_documents_from_hostile_result_yields_no_docs(self) -> None:
        """A result whose iteration raises degrades to no documents."""

        class Hostile:
            def __len__(self) -> int:
                return 1

            def __iter__(self) -> Any:
                raise RuntimeError("no iteration for you")

        assert record._documents_from(Hostile()) == []

    def test_wraps_preserves_metadata(self) -> None:
        def my_retriever(q: str) -> list[str]:
            """Docstring survives."""
            return []

        wrapped = record.wrap_retriever(my_retriever, vector_store="qdrant")
        assert wrapped.__name__ == "my_retriever"
        assert wrapped.__doc__ == "Docstring survives."


# ── redaction (ADR-0209 D5.1) — byte-checked against the written streams ──


SECRET = "sk-ant-" + "a1b2c3d4e5" * 3


class TestExtendedStreamRedaction:
    @pytest.mark.parametrize(
        ("stream", "emit"),
        [
            ("guardrail_events.jsonl",
             lambda: record.guardrail(
                 "leaky", "error", details={"note": f"used key {SECRET}"})),
            ("evaluator_events.jsonl",
             lambda: record.evaluator(
                 "leaky-judge", rationale=f"model echoed {SECRET}")),
            ("state_transitions.jsonl",
             lambda: record.state_transition(
                 0, "sha256:aa", "sha256:bb",
                 state_after={"scratch": f"token={SECRET}"})),
            ("vector_retrievals.jsonl",
             lambda: record.vector_retrieval(
                 "qdrant",
                 documents=[{"document_id": "d1",
                             "content": f"body with {SECRET}"}])),
            ("memory_operations.jsonl",
             lambda: record.memory_operation(
                 "write", "notes", value=f"remember {SECRET}")),
        ],
        ids=["guardrail", "evaluator", "state-transition",
             "vector-retrieval", "memory-operation"],
    )
    def test_seeded_secret_redacted_at_finalize(
        self, live_recorder: EventRecorder, capsule_dir: Path,
        stream: str, emit: Any,
    ) -> None:
        from novafabric.capture.secrets import SecretScannerV0

        emit()
        assert SECRET.encode() in (capsule_dir / stream).read_bytes()

        proof = SecretScannerV0(capsule_dir, RUN_ID).scan_and_redact()

        raw = (capsule_dir / stream).read_bytes()
        assert SECRET.encode() not in raw, f"secret survived in {stream}"
        assert b"[REDACTED:anthropic-api-key]" in raw
        assert any(
            f["target_ref"] == stream and f["rule_id"] == "anthropic-api-key"
            for f in proof["findings"]
        ), "finding must appear in redaction-proof"
        target = next(t for t in proof["targets"] if t["ref"] == stream)
        assert target["findings_count"] >= 1

    def test_proof_with_extended_targets_validates_against_schema(
        self, live_recorder: EventRecorder, capsule_dir: Path
    ) -> None:
        import jsonschema

        from novafabric.capture.secrets import SecretScannerV0

        record.guardrail("leaky", "error",
                         details={"note": f"used key {SECRET}"})
        proof = SecretScannerV0(capsule_dir, RUN_ID).scan_and_redact()
        schema = json.loads(
            (Path(__file__).parents[2]
             / "src/novafabric/schemas/secret-redaction.schema.json"
             ).read_text()
        )
        jsonschema.validate(proof, schema)
