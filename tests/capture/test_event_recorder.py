"""Tests for EventRecorder, event models, and hook integration.

Coverage targets:
- EventRecorder.record_file_event → file_events.jsonl
- EventRecorder.record_network_event → network_events.jsonl
- EventRecorder.record_human_approval → human_approvals.jsonl
- Fail-open on permission error (read-only directory)
- Thread safety (concurrent writes don't corrupt JSONL)
- _is_ai_api() detection helper
- Module-level singleton (get/set_current_recorder)
- requests hook → network_events.jsonl integration
- httpx hook → network_events.jsonl integration
- Model field validation (Pydantic)
"""
from __future__ import annotations

import json
import stat
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from novafabric.capture.event_recorder import (
    EventRecorder,
    _is_ai_api,
    get_current_recorder,
    set_current_recorder,
)
from novafabric.capture.events import FileEvent, HumanApprovalEvent, NetworkEvent

RUN_ID = "01HXTEST000000000000000000"
CAPSULE_ID = "01HXTEST000000000000000001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_recorder(tmp_path: Path) -> EventRecorder:
    return EventRecorder(
        capsule_dir=tmp_path,
        run_id=RUN_ID,
        capsule_id=CAPSULE_ID,
    )


def read_jsonl(path: Path) -> list[dict]:  # type: ignore[type-arg]
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# FileEvent tests
# ---------------------------------------------------------------------------


def test_record_file_event_creates_jsonl(tmp_path: Path) -> None:
    recorder = make_recorder(tmp_path)
    recorder.record_file_event("read", "/tmp/data.csv", size_bytes=1024)
    records = read_jsonl(tmp_path / "file_events.jsonl")
    assert len(records) == 1
    event = records[0]
    assert event["operation"] == "read"
    assert event["path"] == "/tmp/data.csv"
    assert event["size_bytes"] == 1024
    assert event["run_id"] == RUN_ID
    assert event["capsule_id"] == CAPSULE_ID
    assert event["success"] is True
    assert "event_id" in event
    assert "timestamp_utc" in event


def test_record_file_event_multiple_appends(tmp_path: Path) -> None:
    recorder = make_recorder(tmp_path)
    recorder.record_file_event("read", "/tmp/a.txt")
    recorder.record_file_event("write", "/tmp/b.txt", size_bytes=512)
    recorder.record_file_event("delete", "/tmp/c.txt", success=False, error="EPERM")
    records = read_jsonl(tmp_path / "file_events.jsonl")
    assert len(records) == 3
    assert records[0]["operation"] == "read"
    assert records[1]["operation"] == "write"
    assert records[2]["operation"] == "delete"
    assert records[2]["success"] is False
    assert records[2]["error"] == "EPERM"


def test_record_file_event_all_operations(tmp_path: Path) -> None:
    recorder = make_recorder(tmp_path)
    for op in ("read", "write", "append", "delete", "mkdir", "listdir", "stat"):
        recorder.record_file_event(op, f"/tmp/{op}_test")
    records = read_jsonl(tmp_path / "file_events.jsonl")
    assert len(records) == 7
    ops = {r["operation"] for r in records}
    assert ops == {"read", "write", "append", "delete", "mkdir", "listdir", "stat"}


def test_record_file_event_with_agent_id(tmp_path: Path) -> None:
    recorder = make_recorder(tmp_path)
    recorder.record_file_event("read", "/tmp/x.bin", agent_id="agent-42")
    event = read_jsonl(tmp_path / "file_events.jsonl")[0]
    assert event["agent_id"] == "agent-42"


# ---------------------------------------------------------------------------
# NetworkEvent tests
# ---------------------------------------------------------------------------


def test_record_network_event_creates_jsonl(tmp_path: Path) -> None:
    recorder = make_recorder(tmp_path)
    recorder.record_network_event(
        method="POST",
        url="https://api.openai.com/v1/chat/completions",
        host="api.openai.com",
        status_code=200,
        response_size_bytes=4096,
        duration_ms=312.5,
        library="requests",
        is_ai_api=True,
    )
    records = read_jsonl(tmp_path / "network_events.jsonl")
    assert len(records) == 1
    event = records[0]
    assert event["method"] == "POST"
    assert event["host"] == "api.openai.com"
    assert event["status_code"] == 200
    assert event["response_size_bytes"] == 4096
    assert event["duration_ms"] == pytest.approx(312.5)
    assert event["library"] == "requests"
    assert event["is_ai_api"] is True
    assert event["run_id"] == RUN_ID


def test_record_network_event_with_port(tmp_path: Path) -> None:
    recorder = make_recorder(tmp_path)
    recorder.record_network_event(
        method="GET",
        url="http://localhost:11434/api/generate",
        host="localhost",
        port=11434,
        status_code=200,
        library="httpx",
        is_ai_api=True,
    )
    event = read_jsonl(tmp_path / "network_events.jsonl")[0]
    assert event["port"] == 11434
    assert event["is_ai_api"] is True


def test_record_network_event_non_ai(tmp_path: Path) -> None:
    recorder = make_recorder(tmp_path)
    recorder.record_network_event(
        method="GET",
        url="https://example.com/data",
        host="example.com",
        status_code=200,
        library="urllib3",
        is_ai_api=False,
    )
    event = read_jsonl(tmp_path / "network_events.jsonl")[0]
    assert event["is_ai_api"] is False


# ---------------------------------------------------------------------------
# HumanApprovalEvent tests
# ---------------------------------------------------------------------------


def test_record_human_approval_creates_jsonl(tmp_path: Path) -> None:
    recorder = make_recorder(tmp_path)
    recorder.record_human_approval(
        approver_id="alice@example.com",
        action="approved",
        target_run_id="01HXRUN0000000000000000001",
        rationale="All acceptance criteria met.",
        policy_version="42",
        seal_bundle_path="/data/nova/bundles/approval_abc.json",
    )
    records = read_jsonl(tmp_path / "human_approvals.jsonl")
    assert len(records) == 1
    event = records[0]
    assert event["approver_id"] == "alice@example.com"
    assert event["action"] == "approved"
    assert event["target_run_id"] == "01HXRUN0000000000000000001"
    assert event["rationale"] == "All acceptance criteria met."
    assert event["policy_version"] == "42"
    assert event["seal_bundle_path"] == "/data/nova/bundles/approval_abc.json"
    assert event["run_id"] == RUN_ID
    assert "event_id" in event
    assert "timestamp_utc" in event


def test_record_human_approval_all_actions(tmp_path: Path) -> None:
    recorder = make_recorder(tmp_path)
    for action in ("proposed", "approved", "rejected", "bypassed"):
        recorder.record_human_approval(
            approver_id="bob@example.com",
            action=action,
            target_run_id="01HXRUN0000000000000000002",
        )
    records = read_jsonl(tmp_path / "human_approvals.jsonl")
    assert len(records) == 4
    actions = {r["action"] for r in records}
    assert actions == {"proposed", "approved", "rejected", "bypassed"}


def test_record_human_approval_minimal(tmp_path: Path) -> None:
    """Optional fields are allowed to be None."""
    recorder = make_recorder(tmp_path)
    recorder.record_human_approval(
        approver_id="charlie@example.com",
        action="rejected",
        target_run_id="01HXRUN0000000000000000003",
    )
    event = read_jsonl(tmp_path / "human_approvals.jsonl")[0]
    assert event["rationale"] is None
    assert event["policy_version"] is None
    assert event["seal_bundle_path"] is None


# ---------------------------------------------------------------------------
# Fail-open tests
# ---------------------------------------------------------------------------


def test_fail_open_on_permission_error(tmp_path: Path) -> None:
    """A read-only capsule directory must not cause any exception."""
    recorder = make_recorder(tmp_path)
    # Make the directory read-only
    tmp_path.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        # None of these should raise
        recorder.record_file_event("read", "/tmp/x.txt")
        recorder.record_network_event("GET", "https://example.com", "example.com")
        recorder.record_human_approval("alice", "approved", "run-1")
    finally:
        tmp_path.chmod(stat.S_IRWXU)  # restore so pytest can clean up


def test_fail_open_on_bad_operation(tmp_path: Path) -> None:
    """An invalid operation literal must not raise (Pydantic validation swallowed)."""
    recorder = make_recorder(tmp_path)
    # "explode" is not a valid Literal for operation — Pydantic will raise
    # internally but _append swallows it.
    recorder.record_file_event("explode", "/tmp/x.txt")  # type: ignore[arg-type]
    # File may or may not exist depending on where the exception occurs;
    # the important thing is no exception propagated.


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_thread_safety_file_events(tmp_path: Path) -> None:
    recorder = make_recorder(tmp_path)
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            for _ in range(20):
                recorder.record_file_event("read", f"/tmp/worker_{n}.txt", size_bytes=n)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    records = read_jsonl(tmp_path / "file_events.jsonl")
    assert len(records) == 100  # 5 workers × 20 records each
    # Every line must be valid JSON (no corruption)
    for r in records:
        assert "operation" in r


def test_thread_safety_network_events(tmp_path: Path) -> None:
    recorder = make_recorder(tmp_path)

    def worker() -> None:
        for _ in range(10):
            recorder.record_network_event("GET", "https://api.openai.com/x", "api.openai.com")

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    records = read_jsonl(tmp_path / "network_events.jsonl")
    assert len(records) == 50


# ---------------------------------------------------------------------------
# _is_ai_api() detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("https://api.openai.com/v1/chat/completions", True),
    ("https://api.anthropic.com/v1/messages", True),
    ("https://api.cohere.ai/v1/generate", True),
    ("https://api.together.xyz/inference", True),
    ("https://api.mistral.ai/v1/chat", True),
    ("https://api.replicate.com/v1/predictions", True),
    ("https://bedrock-runtime.us-east-1.amazonaws.com/model/invoke", True),
    ("https://bedrock.us-east-1.amazonaws.com/foundation-models", True),
    ("https://localhost:11434/api/generate", True),
    ("https://127.0.0.1:11434/api/generate", True),
    ("https://generativelanguage.googleapis.com/v1/generate", True),
    ("https://api.groq.com/openai/v1/chat/completions", True),
    ("https://openrouter.ai/api/v1/chat/completions", True),
    ("https://example.com/api/data", False),
    ("https://google.com/search", False),
    ("https://github.com/api", False),
    ("https://s3.amazonaws.com/bucket/object", False),
    ("http://myservice.internal/health", False),
])
def test_is_ai_api_detection(url: str, expected: bool) -> None:
    assert _is_ai_api(url) is expected


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


def test_current_recorder_singleton_get_set(tmp_path: Path) -> None:
    # Initially may be None (or whatever previous test left — reset it)
    set_current_recorder(None)
    assert get_current_recorder() is None

    recorder = make_recorder(tmp_path)
    set_current_recorder(recorder)
    assert get_current_recorder() is recorder

    set_current_recorder(None)
    assert get_current_recorder() is None


def test_current_recorder_singleton_replace(tmp_path: Path) -> None:
    r1 = make_recorder(tmp_path / "r1")
    r2 = make_recorder(tmp_path / "r2")
    (tmp_path / "r1").mkdir()
    (tmp_path / "r2").mkdir()

    set_current_recorder(r1)
    assert get_current_recorder() is r1

    set_current_recorder(r2)
    assert get_current_recorder() is r2

    set_current_recorder(None)


# ---------------------------------------------------------------------------
# requests hook integration
# ---------------------------------------------------------------------------


def test_requests_hook_records_network_event(tmp_path: Path) -> None:
    """Verify that the requests hook writes a NetworkEvent when a recorder is active."""
    from novafabric.capture.capsule import CapsuleWriter
    from novafabric.capture.hooks._requests import RequestsHook
    from novafabric.capture.hooks._url_registry import UrlRegistry, UrlRegistryEntry

    writer = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    writer.open()
    capsule_dir = writer.capsule_dir

    recorder = EventRecorder(
        capsule_dir=capsule_dir,
        run_id=RUN_ID,
        capsule_id=RUN_ID,
    )
    set_current_recorder(recorder)

    registry = UrlRegistry(patterns=[
        UrlRegistryEntry(match="api.openai.com", gen_ai_system="openai"),
    ])
    hook = RequestsHook(writer=writer, parent_span_id="0" * 16, registry=registry)

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.content = b'{"id": "chatcmpl-abc"}'

    fake_prepared = MagicMock()
    fake_prepared.url = "https://api.openai.com/v1/chat/completions"
    fake_prepared.method = "POST"
    fake_prepared.body = b'{"model":"gpt-4o","messages":[]}'

    hook._wrapped_send(fake_prepared, original=MagicMock(return_value=fake_response))

    set_current_recorder(None)

    net_events = read_jsonl(capsule_dir / "network_events.jsonl")
    assert len(net_events) == 1
    ev = net_events[0]
    assert ev["method"] == "POST"
    assert "openai.com" in ev["host"]
    assert ev["is_ai_api"] is True
    assert ev["library"] == "requests"
    assert ev["status_code"] == 200


def test_requests_hook_no_recorder_no_error(tmp_path: Path) -> None:
    """When no recorder is active, the hook must still work without error."""
    from novafabric.capture.capsule import CapsuleWriter
    from novafabric.capture.hooks._requests import RequestsHook
    from novafabric.capture.hooks._url_registry import UrlRegistry, UrlRegistryEntry

    set_current_recorder(None)

    writer = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    writer.open()

    registry = UrlRegistry(patterns=[
        UrlRegistryEntry(match="api.openai.com", gen_ai_system="openai"),
    ])
    hook = RequestsHook(writer=writer, parent_span_id="0" * 16, registry=registry)

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.content = b"{}"

    fake_prepared = MagicMock()
    fake_prepared.url = "https://api.openai.com/v1/chat/completions"
    fake_prepared.method = "POST"
    fake_prepared.body = b"{}"

    # Must not raise
    hook._wrapped_send(fake_prepared, original=MagicMock(return_value=fake_response))

    # No network_events.jsonl should be created
    assert not (writer.capsule_dir / "network_events.jsonl").exists()


# ---------------------------------------------------------------------------
# Pydantic model field validation
# ---------------------------------------------------------------------------


def test_file_event_model_defaults() -> None:
    event = FileEvent(
        run_id="run-1",
        capsule_id="cap-1",
        timestamp_utc="2026-01-01T00:00:00+00:00",
        operation="read",
        path="/tmp/data.csv",
    )
    assert event.success is True
    assert event.size_bytes is None
    assert event.error is None
    assert event.agent_id is None
    assert len(event.event_id) > 0


def test_network_event_model_defaults() -> None:
    event = NetworkEvent(
        run_id="run-1",
        capsule_id="cap-1",
        timestamp_utc="2026-01-01T00:00:00+00:00",
        method="GET",
        url="https://example.com",
        host="example.com",
        library="requests",
    )
    assert event.is_ai_api is False
    assert event.port is None
    assert event.status_code is None
    assert len(event.event_id) > 0


def test_human_approval_event_model_defaults() -> None:
    event = HumanApprovalEvent(
        run_id="run-1",
        capsule_id="cap-1",
        timestamp_utc="2026-01-01T00:00:00+00:00",
        approver_id="alice@example.com",
        action="approved",
        target_run_id="run-2",
    )
    assert event.rationale is None
    assert event.policy_version is None
    assert event.seal_bundle_path is None
    assert len(event.event_id) > 0


# ---------------------------------------------------------------------------
# Extended span taxonomy (ADR-0082, gap-011) — capture-side wiring
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_record_state_transition(tmp_path):
    rec = make_recorder(tmp_path)
    rec.record_state_transition(step_index=0, state_digest_before="sha256:a",
                                state_digest_after="sha256:b", agent_id="commander")
    rows = _read_jsonl(tmp_path / "state_transitions.jsonl")
    assert len(rows) == 1
    assert rows[0]["event_type"] == "StateTransition"
    assert rows[0]["step_index"] == 0
    assert rows[0]["run_id"] == RUN_ID


def test_record_guardrail_and_evaluator(tmp_path):
    rec = make_recorder(tmp_path)
    rec.record_guardrail(guardrail_name="secret_scan", outcome="blocked", score=1.0)
    rec.record_evaluator(evaluator_name="accuracy", score=0.42, passed=False)
    g = _read_jsonl(tmp_path / "guardrail_events.jsonl")[0]
    e = _read_jsonl(tmp_path / "evaluator_events.jsonl")[0]
    assert g["event_type"] == "GuardrailEvaluated" and g["outcome"] == "blocked"
    assert e["event_type"] == "EvaluatorScored" and e["passed"] is False


def test_record_memory_and_reranker(tmp_path):
    rec = make_recorder(tmp_path)
    rec.record_memory_operation(operation="write", memory_key="ruled_out",
                                relevance_score=0.9)
    rec.record_reranker(reranker_model="bge-reranker", input_count=10, output_count=3)
    m = _read_jsonl(tmp_path / "memory_operations.jsonl")[0]
    r = _read_jsonl(tmp_path / "reranker_events.jsonl")[0]
    assert m["event_type"] == "MemoryOperation" and m["operation"] == "write"
    assert r["event_type"] == "RerankerApplied" and r["output_count"] == 3


def test_record_vector_retrieval_phases(tmp_path):
    rec = make_recorder(tmp_path)
    rec.record_vector_retrieval(vector_store="chroma", phase="started", top_k=5)
    rec.record_vector_retrieval(vector_store="chroma", phase="completed",
                                top_k=5, returned_count=5)
    rec.record_vector_retrieval(vector_store="chroma", phase="failed",
                                error="connection refused")
    rows = _read_jsonl(tmp_path / "vector_retrievals.jsonl")
    types = [r["event_type"] for r in rows]
    assert types == ["VectorRetrievalStarted", "VectorRetrievalCompleted",
                     "VectorRetrievalFailed"]
    assert rows[2]["status"] == "error"


def test_new_recorders_are_fail_open_on_bad_dir(tmp_path):
    # A recorder pointed at a non-writable path must never raise.
    bad = tmp_path / "nonexistent" / "deep"
    rec = EventRecorder(capsule_dir=bad, run_id=RUN_ID, capsule_id=CAPSULE_ID)
    rec.record_state_transition(step_index=0, state_digest_before="a", state_digest_after="b")
    rec.record_evaluator(evaluator_name="x")  # must not raise
