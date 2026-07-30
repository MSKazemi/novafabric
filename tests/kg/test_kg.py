"""Tests for the Capsule Knowledge Graph (KG) v1.2.

All KGStore / pipeline tests use ``pytest.importorskip("kuzu")`` so they
skip gracefully when kuzu is absent from the environment.  When kuzu IS
installed they must pass without any skips.

CRDT and EntityNormaliser tests have no kuzu dependency and must always pass.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def kg_store(tmp_path: Path) -> Any:
    """KGStore backed by a temp KuzuDB directory.  Skips if kuzu not installed."""
    pytest.importorskip("kuzu")
    from novafabric.kg.store import KGStore

    store = KGStore(tmp_path / "test_kg.kuzu")
    store.init_schema()
    return store


@pytest.fixture()
def pipeline(kg_store: Any) -> Any:
    """KGIngestionPipeline wired to the temp kg_store fixture."""
    from novafabric.kg.pipeline import KGIngestionPipeline

    return KGIngestionPipeline(kg_store)


# ---------------------------------------------------------------------------
# KGStore — schema and node tests
# ---------------------------------------------------------------------------


def test_schema_init_idempotent(tmp_path: Path) -> None:
    """init_schema() can be called twice without error (all DDL is IF NOT EXISTS)."""
    pytest.importorskip("kuzu")
    from novafabric.kg.store import KGStore

    store = KGStore(tmp_path / "test_kg.kuzu")
    store.init_schema()
    store.init_schema()  # must not raise
    status = store.get_status()
    assert status["store_health"] == "ok"


def test_merge_agent_and_model(kg_store: Any) -> None:
    """Can upsert Agent and Model nodes; no CALLS edge created yet."""
    kg_store.merge_agent("agent-1", name="TestAgent")
    kg_store.merge_model("gpt-4o", provider="openai", canonical_name="gpt-4o")
    assert kg_store.get_edge_count() == 0


def test_merge_agent_idempotent(kg_store: Any) -> None:
    """Merging the same Agent node twice is idempotent."""
    kg_store.merge_agent("agent-x", name="Dup")
    kg_store.merge_agent("agent-x", name="Dup")
    assert kg_store.get_status()["store_health"] == "ok"


def test_upsert_calls_edge(kg_store: Any) -> None:
    """CALLS edge is created and call_count accumulates correctly."""
    kg_store.upsert_calls_edge(
        agent_id="agent-1",
        model_id="gpt-4o",
        call_count=3,
        verified_count=2,
        confidence=2.0 / 3,
        capsule_id="cap-1",
    )
    assert kg_store.get_edge_count() == 1
    rows = kg_store.query_agent_models("agent-1")
    assert len(rows) == 1
    assert rows[0]["model_id"] == "gpt-4o"
    assert rows[0]["call_count"] == 3


def test_upsert_calls_edge_accumulates(kg_store: Any) -> None:
    """Second upsert increments call_count rather than replacing it."""
    kg_store.upsert_calls_edge("agent-1", "gpt-4o", 5, 5, 1.0, "cap-1")
    kg_store.upsert_calls_edge("agent-1", "gpt-4o", 3, 0, 0.0, "cap-2")
    rows = kg_store.query_agent_models("agent-1")
    assert rows[0]["call_count"] == 8


def test_upsert_uses_tool_edge(kg_store: Any) -> None:
    """USES_TOOL edge is created; query_agent_tools returns it."""
    kg_store.upsert_uses_tool_edge(
        agent_id="agent-2",
        tool_id="read_file",
        call_count=4,
        verified_count=0,
        confidence=0.0,
        capsule_id="cap-3",
        tool_name="read_file",
    )
    tools = kg_store.query_agent_tools("agent-2")
    assert len(tools) == 1
    assert tools[0]["tool_id"] == "read_file"
    assert tools[0]["call_count"] == 4


def test_query_agent_models_empty(kg_store: Any) -> None:
    """query_agent_models returns [] for an unknown agent."""
    rows = kg_store.query_agent_models("nobody")
    assert rows == []


def test_get_status_ok(kg_store: Any) -> None:
    """get_status() returns store_health=ok dict."""
    status = kg_store.get_status()
    assert status["store_health"] == "ok"
    assert "edge_count" in status
    assert "db_path" in status


def test_kg_store_no_kuzu(tmp_path: Path) -> None:
    """KGStore raises ImportError with helpful message when kuzu is absent."""
    import sys
    import unittest.mock as mock

    # Snapshot the real module so we can restore it after the test.
    original_module = sys.modules.get("novafabric.kg.store")

    with mock.patch.dict(sys.modules, {"kuzu": None}):
        # Force re-import of store module to pick up the mock
        if "novafabric.kg.store" in sys.modules:
            del sys.modules["novafabric.kg.store"]
        from novafabric.kg import store as _store_mod

        with pytest.raises(ImportError, match="kuzu is required"):
            _store_mod.KGStore(tmp_path / "no_kuzu.kuzu")

    # Restore original module so subsequent imports reuse the already-initialised
    # module (including Prometheus metrics already registered in the default REGISTRY).
    if original_module is not None:
        sys.modules["novafabric.kg.store"] = original_module
    elif "novafabric.kg.store" in sys.modules:
        del sys.modules["novafabric.kg.store"]


# ---------------------------------------------------------------------------
# KGStore — thread safety
# ---------------------------------------------------------------------------


def test_concurrent_upsert_calls_edge(kg_store: Any) -> None:
    """Concurrent upserts from 4 threads all land without exception."""
    errors: list[str] = []

    def worker(n: int) -> None:
        try:
            kg_store.upsert_calls_edge(
                "agent-thread",
                "gpt-4o",
                n,
                0,
                0.0,
                f"cap-{n}",
            )
        except Exception as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=worker, args=(i + 1,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    rows = kg_store.query_agent_models("agent-thread")
    assert rows[0]["call_count"] > 0


# ---------------------------------------------------------------------------
# CRDT G-Counter
# ---------------------------------------------------------------------------


def test_gcounter_increment_verified() -> None:
    from novafabric.kg.crdt import GCounter

    c = GCounter()
    c.increment(novaseal_valid=True, capsule_id="cap-1")
    c.increment(novaseal_valid=False, capsule_id="cap-2")
    assert c.call_count == 2
    assert c.verified_count == 1
    assert abs(c.confidence - 0.5) < 1e-9


def test_gcounter_increment_all_unverified() -> None:
    from novafabric.kg.crdt import GCounter

    c = GCounter()
    for i in range(5):
        c.increment(novaseal_valid=False, capsule_id=f"cap-{i}")
    assert c.call_count == 5
    assert c.verified_count == 0
    assert c.confidence == 0.0


def test_gcounter_zero_confidence_when_empty() -> None:
    from novafabric.kg.crdt import GCounter

    c = GCounter()
    assert c.confidence == 0.0


def test_gcounter_merge_elementwise_max() -> None:
    from novafabric.kg.crdt import GCounter

    a = GCounter(call_count=10, verified_count=5)
    b = GCounter(call_count=8, verified_count=7)
    merged = a.merge(b)
    assert merged.call_count == 10   # max(10, 8)
    assert merged.verified_count == 7  # max(5, 7)


def test_gcounter_merge_preserves_representative_ids() -> None:
    from novafabric.kg.crdt import GCounter

    a = GCounter()
    a.increment(novaseal_valid=True, capsule_id="cap-a")
    b = GCounter()
    b.increment(novaseal_valid=True, capsule_id="cap-b")
    merged = a.merge(b)
    ids = list(merged.representative_capsule_ids)
    assert "cap-a" in ids or "cap-b" in ids


def test_gcounter_to_dict_keys() -> None:
    from novafabric.kg.crdt import GCounter

    c = GCounter(call_count=3, verified_count=2)
    d = c.to_dict()
    assert set(d.keys()) == {
        "call_count",
        "verified_count",
        "confidence",
        "representative_capsule_id",
    }


# ---------------------------------------------------------------------------
# CRDT Accumulator
# ---------------------------------------------------------------------------


def test_crdt_accumulator_single_edge() -> None:
    from novafabric.kg.crdt import CRDTAccumulator

    acc = CRDTAccumulator()
    acc.accumulate("agent-1", "gpt-4o", "CALLS", "cap-1", novaseal_valid=True)
    acc.accumulate("agent-1", "gpt-4o", "CALLS", "cap-2", novaseal_valid=False)
    assert len(acc) == 2
    deltas = acc.flush()
    assert len(deltas) == 1
    assert deltas[0]["call_count"] == 2
    assert deltas[0]["verified_count"] == 1
    assert abs(float(deltas[0]["confidence"]) - 0.5) < 1e-9


def test_crdt_accumulator_flush_resets() -> None:
    from novafabric.kg.crdt import CRDTAccumulator

    acc = CRDTAccumulator()
    acc.accumulate("a", "b", "CALLS", "cap-1", novaseal_valid=False)
    acc.flush()
    assert len(acc) == 0
    assert acc.flush() == []


def test_crdt_accumulator_multiple_edges() -> None:
    from novafabric.kg.crdt import CRDTAccumulator

    acc = CRDTAccumulator()
    acc.accumulate("agent-1", "gpt-4o", "CALLS", "c1")
    acc.accumulate("agent-1", "read_file", "USES_TOOL", "c2")
    acc.accumulate("agent-2", "gpt-4o", "CALLS", "c3")
    deltas = acc.flush()
    assert len(deltas) == 3
    edge_keys = {(str(d["src"]), str(d["dst"]), str(d["edge_type"])) for d in deltas}
    assert ("agent-1", "gpt-4o", "CALLS") in edge_keys
    assert ("agent-1", "read_file", "USES_TOOL") in edge_keys
    assert ("agent-2", "gpt-4o", "CALLS") in edge_keys


# ---------------------------------------------------------------------------
# EntityNormaliser
# ---------------------------------------------------------------------------


def test_normalise_model_exact_match() -> None:
    from novafabric.kg.entity_normaliser import EntityNormaliser

    n = EntityNormaliser()
    assert n.normalise("model", "GPT-4o") == "gpt-4o"
    assert n.normalise("model", "  claude-3-opus  ") == "claude-3-opus"


def test_normalise_model_date_stamped_gpt() -> None:
    from novafabric.kg.entity_normaliser import EntityNormaliser

    n = EntityNormaliser()
    assert n.normalise("model", "gpt-4o-2024-08-06") == "gpt-4o"
    assert n.normalise("model", "GPT-4o-2024-11-20") == "gpt-4o"


def test_normalise_model_date_stamped_claude() -> None:
    from novafabric.kg.entity_normaliser import EntityNormaliser

    n = EntityNormaliser()
    assert n.normalise("model", "claude-3-5-sonnet-20241022") == "claude-3-5-sonnet"
    assert n.normalise("model", "claude-3-5-sonnet-20250101") == "claude-3-5-sonnet"


def test_normalise_model_unknown_passthrough() -> None:
    from novafabric.kg.entity_normaliser import EntityNormaliser

    n = EntityNormaliser()
    result = n.normalise("model", "some-unknown-model-v99")
    assert result == "some-unknown-model-v99"  # lowercase passthrough


def test_normalise_endpoint_strips_query_and_upgrades_scheme() -> None:
    from novafabric.kg.entity_normaliser import EntityNormaliser

    n = EntityNormaliser()
    result = n.normalise("endpoint", "http://api.example.com/v1/?key=abc")
    assert result == "https://api.example.com/v1"


def test_normalise_endpoint_strips_trailing_slash() -> None:
    from novafabric.kg.entity_normaliser import EntityNormaliser

    n = EntityNormaliser()
    result = n.normalise("endpoint", "https://api.openai.com/v1/")
    assert result == "https://api.openai.com/v1"


def test_normalise_agent_strips_whitespace() -> None:
    from novafabric.kg.entity_normaliser import EntityNormaliser

    n = EntityNormaliser()
    assert n.normalise("agent", "  MyAgent  ") == "myagent"


def test_normalise_tool_lowercase() -> None:
    from novafabric.kg.entity_normaliser import EntityNormaliser

    n = EntityNormaliser()
    assert n.normalise("tool", "Read_File") == "read_file"


def test_normalise_unknown_entity_type() -> None:
    from novafabric.kg.entity_normaliser import EntityNormaliser

    n = EntityNormaliser()
    result = n.normalise("widget", "  Foo  ")
    assert result == "foo"


# ---------------------------------------------------------------------------
# KGIngestionPipeline — event ingestion
# ---------------------------------------------------------------------------


def test_pipeline_ingest_model_call_completed(pipeline: Any, kg_store: Any) -> None:
    """ModelCallCompleted event creates a CALLS edge in KGStore."""
    pipeline.ingest_event(
        {
            "event_type": "ModelCallCompleted",
            "agent_id": "agent-1",
            "model_id": "gpt-4o",
            "capsule_id": "cap-1",
        },
        novaseal_valid=True,
    )
    written = pipeline.flush_to_store()
    assert written == 1
    rows = kg_store.query_agent_models("agent-1")
    assert any(r["model_id"] == "gpt-4o" for r in rows)


def test_pipeline_ingest_model_call_started(pipeline: Any, kg_store: Any) -> None:
    """ModelCallStarted also accumulates a CALLS edge."""
    pipeline.ingest_event(
        {
            "event_type": "ModelCallStarted",
            "agent_id": "agent-a",
            "model": "claude-3-5-sonnet-20241022",  # canonical form tested via normaliser
            "capsule_id": "cap-a",
        }
    )
    written = pipeline.flush_to_store()
    assert written == 1
    rows = kg_store.query_agent_models("agent-a")
    assert any(r["model_id"] == "claude-3-5-sonnet" for r in rows)


def test_pipeline_ingest_tool_call_completed(pipeline: Any, kg_store: Any) -> None:
    """ToolCallCompleted event creates a USES_TOOL edge in KGStore."""
    pipeline.ingest_event(
        {
            "event_type": "ToolCallCompleted",
            "agent_id": "agent-2",
            "tool_name": "read_file",
            "capsule_id": "cap-2",
        }
    )
    written = pipeline.flush_to_store()
    assert written == 1
    tools = kg_store.query_agent_tools("agent-2")
    assert any(t["tool_id"] == "read_file" for t in tools)


def test_pipeline_ingest_tool_call_started(pipeline: Any, kg_store: Any) -> None:
    """ToolCallStarted with tool_id field also accumulates a USES_TOOL edge."""
    pipeline.ingest_event(
        {
            "event_type": "ToolCallStarted",
            "agent_id": "agent-3",
            "tool_id": "Web_Search",  # should normalise to lowercase
            "capsule_id": "cap-3",
        }
    )
    written = pipeline.flush_to_store()
    assert written == 1
    tools = kg_store.query_agent_tools("agent-3")
    assert any(t["tool_id"] == "web_search" for t in tools)


def test_pipeline_ingest_model_call_from_envelope_payload(pipeline: Any, kg_store: Any) -> None:
    """ADR-0220 follow-up: a real Event Envelope v1 nests model_id under
    "payload" (no dedicated top-level slot for it) rather than at the top
    level test_pipeline_ingest_model_call_completed uses — ingest_event must
    still extract it and produce a CALLS edge."""
    pipeline.ingest_event(
        {
            "event_type": "ModelCallCompleted",
            "agent_id": "agent-envelope",
            "capsule_id": "",
            "payload": {"model_id": "gpt-4o", "status": "success"},
        }
    )
    written = pipeline.flush_to_store()
    assert written == 1
    rows = kg_store.query_agent_models("agent-envelope")
    assert any(r["model_id"] == "gpt-4o" for r in rows)


def test_pipeline_ingest_tool_call_from_envelope_payload(pipeline: Any, kg_store: Any) -> None:
    """Same fallback, for the ToolCall/tool_name case."""
    pipeline.ingest_event(
        {
            "event_type": "ToolCallCompleted",
            "agent_id": "agent-envelope-2",
            "capsule_id": "",
            "payload": {"tool_name": "search", "status": "success"},
        }
    )
    written = pipeline.flush_to_store()
    assert written == 1
    tools = kg_store.query_agent_tools("agent-envelope-2")
    assert any(t["tool_id"] == "search" for t in tools)


def test_pipeline_skips_model_call_with_empty_payload_and_no_top_level_model(
    pipeline: Any,
) -> None:
    """No model_id anywhere (top level or payload) — skip, don't crash."""
    pipeline.ingest_event(
        {
            "event_type": "ModelCallCompleted",
            "agent_id": "agent-x",
            "capsule_id": "",
            "payload": None,
        }
    )
    assert pipeline.flush_to_store() == 0


def test_real_producer_to_kg_pipeline_end_to_end(pipeline: Any, kg_store: Any, tmp_path: Path) -> None:
    """ADR-0220 follow-up, full chain: the real producer
    (spool_sink.emit_call_events_from_capsule, reading real model-calls.jsonl/
    tool-calls.jsonl exactly as capture/orchestrator.py does) writes real
    Event Envelope v1 records to a real spool; those envelopes — nested
    payload and all — are fed into the real KGIngestionPipeline.ingest_event()
    and must produce real CALLS/USES_TOOL edges. Not hand-built KG-schema
    fixtures — the actual wire shape a real NATS deployment would carry."""
    from novafabric.capture._ulid import new_ulid
    from novafabric.capture.spool_sink import SpoolSink, emit_call_events_from_capsule

    capsule_dir = tmp_path / "capsule"
    capsule_dir.mkdir()
    (capsule_dir / "model-calls.jsonl").write_text(
        json.dumps({"gen_ai.request.model": "gpt-4o", "status": "success"}) + "\n"
    )
    (capsule_dir / "tool-calls.jsonl").write_text(
        json.dumps({"tool_name": "read_file", "status": "success"}) + "\n"
    )

    spool_dir = tmp_path / "spool"
    sink = SpoolSink(spool_dir)
    emit_call_events_from_capsule(
        sink, capsule_dir, run_id=new_ulid(), agent_id="agent-e2e"
    )
    sink.close()

    envelopes: list[dict] = []
    for seg in sorted(spool_dir.glob("*.jsonl")):
        body = seg.read_bytes()[16:]  # 16-byte binary spool segment header
        for line in body.splitlines():
            if line.strip():
                envelopes.append(json.loads(line))
    assert {e["event_type"] for e in envelopes} == {"ModelCallCompleted", "ToolCallCompleted"}

    for envelope in envelopes:
        pipeline.ingest_event(envelope)
    written = pipeline.flush_to_store()
    assert written == 2

    models = kg_store.query_agent_models("agent-e2e")
    assert any(r["model_id"] == "gpt-4o" for r in models)
    tools = kg_store.query_agent_tools("agent-e2e")
    assert any(t["tool_id"] == "read_file" for t in tools)


def test_pipeline_ingest_endpoint_routed(pipeline: Any) -> None:
    """EndpointRouted event creates a ROUTES_TO edge (no assertion on tools/models)."""
    pipeline.ingest_event(
        {
            "event_type": "EndpointRouted",
            "agent_id": "agent-4",
            "url": "https://api.openai.com/v1/",
            "capsule_id": "cap-4",
        }
    )
    written = pipeline.flush_to_store()
    assert written == 1


def test_pipeline_skips_event_with_no_agent_id(pipeline: Any) -> None:
    """Events missing agent_id are silently skipped."""
    pipeline.ingest_event(
        {"event_type": "ModelCallCompleted", "model_id": "gpt-4o", "capsule_id": "cap-5"}
    )
    assert pipeline.pending_count == 0


def test_pipeline_skips_model_event_with_no_model(pipeline: Any) -> None:
    """ModelCallCompleted with no model_id/model is silently skipped."""
    pipeline.ingest_event(
        {"event_type": "ModelCallCompleted", "agent_id": "agent-5", "capsule_id": "cap-6"}
    )
    assert pipeline.pending_count == 0


def test_pipeline_skips_unknown_event_type(pipeline: Any) -> None:
    """Unknown event types do not create edges."""
    pipeline.ingest_event(
        {"event_type": "SomeRandomEvent", "agent_id": "agent-6", "capsule_id": "cap-7"}
    )
    written = pipeline.flush_to_store()
    assert written == 0


def test_pipeline_flush_resets_accumulator(pipeline: Any) -> None:
    """After flush_to_store(), pending_count returns to 0."""
    pipeline.ingest_event(
        {
            "event_type": "ModelCallCompleted",
            "agent_id": "agent-7",
            "model_id": "gpt-4o",
            "capsule_id": "cap-8",
        }
    )
    assert pipeline.pending_count == 1
    pipeline.flush_to_store()
    assert pipeline.pending_count == 0


def test_pipeline_multiple_events_same_edge(pipeline: Any, kg_store: Any) -> None:
    """Multiple events for the same (agent, model) pair accumulate call_count."""
    for i in range(5):
        pipeline.ingest_event(
            {
                "event_type": "ModelCallCompleted",
                "agent_id": "agent-8",
                "model_id": "gpt-4o",
                "capsule_id": f"cap-{i}",
            }
        )
    pipeline.flush_to_store()
    rows = kg_store.query_agent_models("agent-8")
    assert rows[0]["call_count"] == 5


# ---------------------------------------------------------------------------
# CLI — smoke tests
# ---------------------------------------------------------------------------


def test_kg_init_cli(tmp_path: Path) -> None:
    """nova kg init creates the KuzuDB schema without error."""
    pytest.importorskip("kuzu")
    from typer.testing import CliRunner

    from novafabric.cli.kg import kg_app

    runner = CliRunner()
    kg_path = str(tmp_path / "test.kuzu")
    result = runner.invoke(kg_app, ["init", "--path", kg_path])
    assert result.exit_code == 0, result.output
    assert "initialised" in result.output


def test_kg_status_cli(tmp_path: Path) -> None:
    """nova kg status shows health and node counts in Rich text output."""
    pytest.importorskip("kuzu")
    from typer.testing import CliRunner

    from novafabric.cli.kg import kg_app

    runner = CliRunner()
    kg_path = str(tmp_path / "test.kuzu")
    # init first
    runner.invoke(kg_app, ["init", "--path", kg_path])
    result = runner.invoke(kg_app, ["status", "--path", kg_path])
    assert result.exit_code == 0, result.output
    output = result.output
    assert "ok" in output
    assert "edge_count" in output


def test_kg_ingest_cli(tmp_path: Path) -> None:
    """nova kg ingest reads a model-calls.jsonl and writes KG edges."""
    pytest.importorskip("kuzu")
    from typer.testing import CliRunner

    from novafabric.cli.kg import kg_app

    # Create a minimal capsule directory
    capsule_dir = tmp_path / "capsule"
    capsule_dir.mkdir()
    events = [
        {"event_type": "ModelCallCompleted", "agent_id": "cli-agent", "model_id": "gpt-4o", "capsule_id": "cap-cli"},
        {"event_type": "ToolCallCompleted", "agent_id": "cli-agent", "tool_name": "read_file", "capsule_id": "cap-cli"},
    ]
    (capsule_dir / "model-calls.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events)
    )

    kg_path = str(tmp_path / "cli_test.kuzu")
    runner = CliRunner()
    result = runner.invoke(kg_app, ["ingest", str(capsule_dir), "--path", kg_path])
    assert result.exit_code == 0, result.output
    assert "edges" in result.output.lower() or "wrote" in result.output.lower()


def test_kg_query_cli(tmp_path: Path) -> None:
    """nova kg query returns JSON with models/tools for an agent."""
    pytest.importorskip("kuzu")
    from typer.testing import CliRunner

    from novafabric.cli.kg import kg_app

    capsule_dir = tmp_path / "capsule"
    capsule_dir.mkdir()
    (capsule_dir / "model-calls.jsonl").write_text(
        json.dumps(
            {
                "event_type": "ModelCallCompleted",
                "agent_id": "query-agent",
                "model_id": "gpt-4o",
                "capsule_id": "cap-q",
            }
        )
    )
    kg_path = str(tmp_path / "query_test.kuzu")
    runner = CliRunner()
    runner.invoke(kg_app, ["ingest", str(capsule_dir), "--path", kg_path])
    result = runner.invoke(kg_app, ["query", "query-agent", "--path", kg_path])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["agent_id"] == "query-agent"
    assert any(m["model_id"] == "gpt-4o" for m in data["models"])


# ---------------------------------------------------------------------------
# OTel GenAI semconv normaliser (no kuzu required)
# ---------------------------------------------------------------------------

def test_normalise_otel_semconv_model_call() -> None:
    """OTel semconv record → event_type + agent_id + model_id injected."""
    from novafabric.kg.pipeline import _normalise_otel_semconv

    event = {
        "schema_version": "0.1.0",
        "model_call_id": "01ABC",
        "parent_span_id": "abc123",
        "gen_ai.system": "openai",
        "gen_ai.request.model": "gpt-4o-2024-08-06",
        "gen_ai.response.model": "gpt-4o-2024-08-06",
        "endpoint": "https://api.openai.com/v1/chat/completions",
    }
    out = _normalise_otel_semconv(event)
    assert out["event_type"] == "ModelCallCompleted"
    assert out["agent_id"] == "abc123"           # from parent_span_id
    assert out["model_id"] == "gpt-4o-2024-08-06"  # from gen_ai.request.model
    # Original dict is not mutated
    assert "event_type" not in event


def test_normalise_otel_semconv_fallback_model_call_id() -> None:
    """Falls back to model_call_id when parent_span_id is absent."""
    from novafabric.kg.pipeline import _normalise_otel_semconv

    event = {
        "model_call_id": "call-xyz",
        "gen_ai.request.model": "claude-3-5-sonnet-20241022",
    }
    out = _normalise_otel_semconv(event)
    assert out["agent_id"] == "call-xyz"
    assert out["model_id"] == "claude-3-5-sonnet-20241022"


def test_normalise_otel_semconv_existing_agent_id_preserved() -> None:
    """Explicit agent_id in the event is not overwritten."""
    from novafabric.kg.pipeline import _normalise_otel_semconv

    event = {
        "agent_id": "my-explicit-agent",
        "gen_ai.request.model": "gpt-4o",
        "parent_span_id": "should-be-ignored",
    }
    out = _normalise_otel_semconv(event)
    assert out["agent_id"] == "my-explicit-agent"


def test_ingest_event_otel_semconv_produces_calls_edge() -> None:
    """ingest_event() handles OTel semconv format and accumulates a CALLS edge."""
    from unittest.mock import MagicMock

    from novafabric.kg.pipeline import KGIngestionPipeline

    mock_store = MagicMock()
    mock_store.upsert_calls_edge = MagicMock()
    pipeline = KGIngestionPipeline(mock_store)

    otel_event = {
        "schema_version": "0.1.0",
        "parent_span_id": "span-abc",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.system": "openai",
        "endpoint": "https://api.openai.com/v1/chat/completions",
    }
    pipeline.ingest_event(otel_event)
    written = pipeline.flush_to_store()

    assert written == 1
    mock_store.upsert_calls_edge.assert_called_once()
    call_kwargs = mock_store.upsert_calls_edge.call_args.kwargs
    assert call_kwargs["agent_id"] == "span-abc"
    assert "gpt-4o" in call_kwargs["model_id"]


def test_ingest_event_otel_semconv_skipped_when_no_model() -> None:
    """OTel record without gen_ai.request.model is silently skipped."""
    from unittest.mock import MagicMock

    from novafabric.kg.pipeline import KGIngestionPipeline

    mock_store = MagicMock()
    pipeline = KGIngestionPipeline(mock_store)

    # No gen_ai.request.model → not detected as OTel semconv → skipped
    pipeline.ingest_event({"gen_ai.system": "openai", "parent_span_id": "x"})
    written = pipeline.flush_to_store()
    assert written == 0


def test_ingest_event_otel_real_capsule_format() -> None:
    """End-to-end: exact record format from nova capture model-calls.jsonl → 1 edge."""
    from unittest.mock import MagicMock

    from novafabric.kg.pipeline import KGIngestionPipeline

    mock_store = MagicMock()
    pipeline = KGIngestionPipeline(mock_store)

    # Verbatim record from agentic-examples capsule
    real_event = {
        "schema_version": "0.1.0",
        "semconv_version": "1.30.0",
        "model_call_id": "01KR8DE5ZPTY6VXWPSHEVKSP3J",
        "parent_span_id": "52f39e1989b06e51",
        "started_at": "2026-05-10T07:43:06.998311Z",
        "finished_at": "2026-05-10T07:43:12.374949Z",
        "duration_ms": 5376,
        "status": "success",
        "gen_ai.system": "ollama",
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": "qwen3.6:35b-a3b",
        "gen_ai.response.model": "qwen3.6:35b-a3b",
        "gen_ai.usage.input_tokens": 0,
        "gen_ai.usage.output_tokens": 0,
        "endpoint": "http://localhost:11434/api/chat",
    }
    pipeline.ingest_event(real_event)
    written = pipeline.flush_to_store()
    assert written == 1  # was 0 before the OTel semconv fix


# ---------------------------------------------------------------------------
# MCP server extraction (v0.27.0)
# ---------------------------------------------------------------------------


def test_mcp_server_extracted_from_tool_name_prefix() -> None:
    """Tool name 'filesystem:read_file' → MCP server 'filesystem' + tool 'read_file'."""
    from unittest.mock import MagicMock

    from novafabric.kg.pipeline import KGIngestionPipeline

    mock_store = MagicMock()
    pipeline = KGIngestionPipeline(mock_store)

    event = {
        "event_type": "ToolCallCompleted",
        "agent_id": "test-agent",
        "tool_name": "filesystem:read_file",
        "capsule_id": "cap-mcp-01",
    }
    pipeline.ingest_event(event)
    written = pipeline.flush_to_store()

    # Two edges: USES_TOOL (agent→tool) + SERVED_BY (tool→mcpserver)
    assert written == 2
    call_args = list(mock_store.method_calls)
    assert any("upsert_uses_tool_edge" in str(c) for c in call_args)
    assert any("upsert_served_by_edge" in str(c) for c in call_args)


def test_plain_tool_name_no_mcp_server() -> None:
    """Tool name without ':' does not produce a SERVED_BY edge."""
    from unittest.mock import MagicMock

    from novafabric.kg.pipeline import KGIngestionPipeline

    mock_store = MagicMock()
    pipeline = KGIngestionPipeline(mock_store)

    event = {
        "event_type": "ToolCallCompleted",
        "agent_id": "test-agent",
        "tool_name": "calculator",
        "capsule_id": "cap-plain-01",
    }
    pipeline.ingest_event(event)
    written = pipeline.flush_to_store()

    assert written == 1
    assert not any("upsert_served_by_edge" in str(c) for c in mock_store.method_calls)


def test_served_by_edge_in_kg_store(tmp_path: Path) -> None:
    """KGStore upsert_served_by_edge creates Tool + MCPServer nodes and SERVED_BY edge."""
    pytest.importorskip("kuzu")
    from novafabric.kg.store import KGStore

    store = KGStore(tmp_path / "test_served_by.kuzu")
    store.init_schema()
    store.upsert_served_by_edge(
        tool_id="read_file",
        server_id="filesystem",
        call_count=3,
        verified_count=0,
        confidence=0.0,
        capsule_id="cap-sv-01",
        server_name="filesystem",
    )
    audit = store.get_audit()
    assert audit["node_mcpserver_count"] == 1
    assert audit["node_tool_count"] == 1
    assert audit["edge_served_by_count"] == 1  # one SERVED_BY edge row (call_count=3 inside it)


def test_get_topology_graph_returns_all_types(tmp_path: Path) -> None:
    """get_topology_graph() returns nodes for all 5 node types and edges for all 4 rel types."""
    pytest.importorskip("kuzu")
    from novafabric.kg.pipeline import KGIngestionPipeline
    from novafabric.kg.store import KGStore

    store = KGStore(tmp_path / "test_topo.kuzu")
    store.init_schema()
    pipeline = KGIngestionPipeline(store)

    # Agent → Model (CALLS)
    pipeline.ingest_event({"event_type": "ModelCallCompleted", "agent_id": "agent-a", "model_id": "gpt-4o", "capsule_id": "c1"})
    # Agent → Tool → MCPServer (USES_TOOL + SERVED_BY)
    pipeline.ingest_event({"event_type": "ToolCallCompleted", "agent_id": "agent-a", "tool_name": "filesystem:read_file", "capsule_id": "c1"})
    # Agent → Endpoint (ROUTES_TO)
    pipeline.ingest_event({"event_type": "EndpointRouted", "agent_id": "agent-a", "url": "https://api.openai.com/v1", "capsule_id": "c1"})
    pipeline.flush_to_store()

    graph = store.get_topology_graph()
    assert graph["node_counts"]["Agent"] == 1
    assert graph["node_counts"]["Model"] >= 1
    assert graph["node_counts"]["Tool"] >= 1
    assert graph["node_counts"]["MCPServer"] >= 1
    assert graph["node_counts"]["InferenceEndpoint"] >= 1
    edge_types = {e["edge_type"] for e in graph["edges"]}
    assert "CALLS" in edge_types
    assert "USES_TOOL" in edge_types
    assert "SERVED_BY" in edge_types
    assert "ROUTES_TO" in edge_types

# ---------------------------------------------------------------------------
# IngestTracker tests
# ---------------------------------------------------------------------------


def test_ingest_tracker_mark_and_contains(tmp_path: Path) -> None:
    """IngestTracker persists mark() and reports contains() correctly."""
    from novafabric.kg.ingest_tracker import IngestTracker

    db = tmp_path / "tracker.db"
    tracker = IngestTracker(db)

    assert not tracker.contains("/some/path")
    tracker.mark("/some/path")
    assert tracker.contains("/some/path")
    assert not tracker.contains("/other/path")
    assert tracker.count() == 1
    tracker.close()

    # Re-open to verify persistence.
    tracker2 = IngestTracker(db)
    assert tracker2.contains("/some/path")
    assert tracker2.count() == 1
    tracker2.close()


def test_ingest_tracker_idempotent_mark(tmp_path: Path) -> None:
    """Marking the same path twice does not raise or duplicate."""
    from novafabric.kg.ingest_tracker import IngestTracker

    tracker = IngestTracker(tmp_path / "tracker.db")
    tracker.mark("/dup/path")
    tracker.mark("/dup/path")
    assert tracker.count() == 1
    tracker.close()


def test_ingest_tracker_all_paths(tmp_path: Path) -> None:
    """all_paths() returns all tracked paths."""
    from novafabric.kg.ingest_tracker import IngestTracker

    tracker = IngestTracker(tmp_path / "tracker.db")
    for i in range(5):
        tracker.mark(f"/capsule/{i}")
    paths = tracker.all_paths()
    assert len(paths) == 5
    tracker.close()


# ---------------------------------------------------------------------------
# nova kg query includes mcp_servers
# ---------------------------------------------------------------------------


def test_kg_query_cli_mcp_servers(tmp_path: Path) -> None:
    """nova kg query includes mcp_servers in JSON output."""
    pytest.importorskip("kuzu")
    from typer.testing import CliRunner

    from novafabric.cli.kg import kg_app

    runner = CliRunner()
    kg_path = str(tmp_path / "test.kuzu")
    runner.invoke(kg_app, ["init", "--path", kg_path])

    # Ingest a capsule with an MCP-namespaced tool so an MCPServer node is created.
    capsule_d = tmp_path / "cap1"
    capsule_d.mkdir()
    (capsule_d / "tool-calls.jsonl").write_text(
        json.dumps({
            "event_type": "ToolCallCompleted",
            "agent_id": "agent-x",
            "tool_name": "fs:read_file",
            "capsule_id": "cap1",
        }) + "\n"
    )
    runner.invoke(kg_app, ["ingest", str(capsule_d), "--path", kg_path])

    result = runner.invoke(kg_app, ["query", "agent-x", "--path", kg_path, "--output", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "mcp_servers" in data
    # MCPServer "fs" should be reachable via tool "read_file"
    server_ids = [s["server_id"] for s in data["mcp_servers"]]
    assert any("fs" in sid for sid in server_ids)


# ---------------------------------------------------------------------------
# Prometheus metrics tests (B-2j)
# ---------------------------------------------------------------------------


def test_kg_prometheus_node_merge_total(kg_store: Any) -> None:
    """novafabric_kg_node_merge_total increments on each node upsert."""
    pytest.importorskip("prometheus_client")
    from novafabric.kg import store as _store

    assert _store._kg_node_merge_total is not None, "Counter should be initialised"
    before = _store._kg_node_merge_total.labels(node_type="Agent")._value.get()
    kg_store.merge_agent("prom-agent-1", "PrometheusTestAgent")
    after = _store._kg_node_merge_total.labels(node_type="Agent")._value.get()
    assert after == before + 1


def test_kg_prometheus_edge_upsert_total(kg_store: Any) -> None:
    """novafabric_kg_edge_upsert_total increments on each edge upsert."""
    pytest.importorskip("prometheus_client")
    from novafabric.kg import store as _store

    assert _store._kg_edge_upsert_total is not None
    before = _store._kg_edge_upsert_total.labels(edge_type="CALLS")._value.get()
    kg_store.upsert_calls_edge(
        agent_id="prom-agent-2",
        model_id="prom-model-2",
        call_count=1,
        verified_count=0,
        confidence=0.5,
        capsule_id="cap-prom-1",
    )
    after = _store._kg_edge_upsert_total.labels(edge_type="CALLS")._value.get()
    assert after == before + 1


def test_kg_prometheus_crdt_merge_total_new_edge(kg_store: Any) -> None:
    """novafabric_kg_crdt_merge_total does NOT increment for a brand-new edge."""
    pytest.importorskip("prometheus_client")
    from novafabric.kg import store as _store

    assert _store._kg_crdt_merge_total is not None
    before = _store._kg_crdt_merge_total.labels(edge_type="CALLS")._value.get()
    kg_store.upsert_calls_edge(
        agent_id="prom-agent-3-new",
        model_id="prom-model-3-new",
        call_count=1,
        verified_count=1,
        confidence=1.0,
        capsule_id="cap-prom-new",
    )
    after = _store._kg_crdt_merge_total.labels(edge_type="CALLS")._value.get()
    assert after == before, "No CRDT merge counter for a brand-new edge"


def test_kg_prometheus_crdt_merge_total_existing_edge(kg_store: Any) -> None:
    """novafabric_kg_crdt_merge_total increments when updating an existing edge."""
    pytest.importorskip("prometheus_client")
    from novafabric.kg import store as _store

    assert _store._kg_crdt_merge_total is not None
    kg_store.upsert_calls_edge(
        agent_id="prom-agent-4",
        model_id="prom-model-4",
        call_count=1,
        verified_count=0,
        confidence=0.5,
        capsule_id="cap-prom-4a",
    )
    before = _store._kg_crdt_merge_total.labels(edge_type="CALLS")._value.get()
    kg_store.upsert_calls_edge(
        agent_id="prom-agent-4",
        model_id="prom-model-4",
        call_count=1,
        verified_count=1,
        confidence=1.0,
        capsule_id="cap-prom-4b",
    )
    after = _store._kg_crdt_merge_total.labels(edge_type="CALLS")._value.get()
    assert after == before + 1, "CRDT merge counter must fire on second write to same edge"


def test_kg_prometheus_node_count_gauge(kg_store: Any) -> None:
    """novafabric_kg_node_count gauge is updated by get_node_counts()."""
    pytest.importorskip("prometheus_client")
    from novafabric.kg import store as _store

    assert _store._kg_node_count is not None
    kg_store.merge_agent("prom-gauge-agent", "GaugeAgent")
    counts = kg_store.get_node_counts()
    gauge_val = _store._kg_node_count._value.get()
    assert gauge_val == sum(counts.values())
    assert gauge_val >= 1
