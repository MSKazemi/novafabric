from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.lineage.consumer import LineageConsumer

_SPOOL_HEADER_SIZE = 16


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture()
def consumer() -> LineageConsumer:
    return LineageConsumer(nats_url="nats://test:4222", kuzu_path="/tmp/test.kuzu")


class TestEdgeExtraction:
    def test_run_started_with_parent_produces_spawned_by(
        self, consumer: LineageConsumer
    ) -> None:
        event = {
            "event_id": "e1",
            "event_type": "RunStarted",
            "run_id": "child-run",
            "parent_run_id": "parent-run",
        }
        edges = _run(consumer.run_once([event]))
        assert len(edges) == 1
        e = edges[0]
        assert e["edge_type"] == "SPAWNED_BY"
        assert e["src"] == "parent-run"
        assert e["dst"] == "child-run"
        assert e["source_event_id"] == "e1"

    def test_run_started_without_parent_produces_no_edges(
        self, consumer: LineageConsumer
    ) -> None:
        event = {
            "event_id": "e2",
            "event_type": "RunStarted",
            "run_id": "root-run",
        }
        edges = _run(consumer.run_once([event]))
        assert edges == []

    def test_artifact_produced_produces_produced_edge(
        self, consumer: LineageConsumer
    ) -> None:
        event = {
            "event_id": "e3",
            "event_type": "ArtifactProduced",
            "run_id": "run-99",
            "artifact_id": "artifact-abc",
        }
        edges = _run(consumer.run_once([event]))
        assert len(edges) == 1
        e = edges[0]
        assert e["edge_type"] == "PRODUCED"
        assert e["src"] == "run-99"
        assert e["dst"] == "artifact-abc"

    def test_artifact_consumed_produces_consumed_by_edge(
        self, consumer: LineageConsumer
    ) -> None:
        event = {
            "event_id": "e4",
            "event_type": "ArtifactConsumed",
            "run_id": "run-88",
            "artifact_id": "artifact-xyz",
        }
        edges = _run(consumer.run_once([event]))
        assert len(edges) == 1
        e = edges[0]
        assert e["edge_type"] == "CONSUMED_BY"
        assert e["src"] == "artifact-xyz"
        assert e["dst"] == "run-88"

    def test_unhandled_event_type_produces_no_edges(
        self, consumer: LineageConsumer
    ) -> None:
        event = {
            "event_id": "e5",
            "event_type": "PIIDetected",
            "run_id": "run-77",
        }
        edges = _run(consumer.run_once([event]))
        assert edges == []


class TestDuplicateWindowConfig:
    def test_default_duplicate_window_is_120s(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_NATS_DUPLICATE_WINDOW_S", raising=False)
        c = LineageConsumer(nats_url="nats://test:4222")
        assert c.duplicate_window_s == 120.0

    def test_duplicate_window_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVA_NATS_DUPLICATE_WINDOW_S", "300")
        c = LineageConsumer(nats_url="nats://test:4222")
        assert c.duplicate_window_s == 300.0

    def test_duplicate_window_constructor_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_NATS_DUPLICATE_WINDOW_S", "300")
        c = LineageConsumer(nats_url="nats://test:4222", duplicate_window_s=60.0)
        assert c.duplicate_window_s == 60.0


class TestDeduplication:
    def test_duplicate_event_id_ignored(self, consumer: LineageConsumer) -> None:
        event = {
            "event_id": "dup-id",
            "event_type": "ArtifactProduced",
            "run_id": "run-1",
            "artifact_id": "art-1",
        }
        # Send same event twice
        edges = _run(consumer.run_once([event, event]))
        # Should only produce one edge (second is deduplicated)
        assert len(edges) == 1

    def test_events_without_id_are_not_deduplicated(
        self, consumer: LineageConsumer
    ) -> None:
        event = {
            # No event_id
            "event_type": "ArtifactProduced",
            "run_id": "run-2",
            "artifact_id": "art-2",
        }
        edges = _run(consumer.run_once([event, event]))
        # Both processed since no deduplication key
        assert len(edges) == 2

    def test_redelivery_across_separate_batches_is_deduplicated(
        self, consumer: LineageConsumer
    ) -> None:
        """SCALE-ADR-001: at-least-once NATS delivery can redeliver a message
        in a *later* fetch() batch, not just within one — dedup must persist
        across separate run_once() calls on the same consumer instance."""
        event = {
            "event_id": "redelivered-id",
            "event_type": "ArtifactProduced",
            "run_id": "run-3",
            "artifact_id": "art-3",
        }
        first_batch = _run(consumer.run_once([event]))
        second_batch = _run(consumer.run_once([event]))  # simulates redelivery
        assert len(first_batch) == 1
        assert len(second_batch) == 0

    def test_dedup_cache_is_bounded(self) -> None:
        """The dedup cache must evict oldest entries rather than grow
        unboundedly over a long-running consumer's lifetime."""
        small_consumer = LineageConsumer(
            nats_url="nats://test:4222",
            kuzu_path="/tmp/test.kuzu",
            dedup_cache_size=3,
        )
        for i in range(5):
            event = {
                "event_id": f"id-{i}",
                "event_type": "ArtifactProduced",
                "run_id": f"run-{i}",
                "artifact_id": f"art-{i}",
            }
            _run(small_consumer.run_once([event]))

        assert len(small_consumer._seen_event_ids) == 3
        # Oldest entries (id-0, id-1) were evicted; newest (id-4) survives.
        assert "id-0" not in small_consumer._seen_event_ids
        assert "id-4" in small_consumer._seen_event_ids

        # A re-delivery of the evicted id-0 is (correctly) no longer caught —
        # documents the bounded-cache tradeoff rather than hiding it.
        replay_edges = _run(
            small_consumer.run_once(
                [
                    {
                        "event_id": "id-0",
                        "event_type": "ArtifactProduced",
                        "run_id": "run-0",
                        "artifact_id": "art-0",
                    }
                ]
            )
        )
        assert len(replay_edges) == 1


def _read_spool_envelopes(spool_dir: Path) -> list[dict[str, Any]]:
    envelopes: list[dict[str, Any]] = []
    for seg in sorted(spool_dir.glob("*.jsonl")):
        body = seg.read_bytes()[_SPOOL_HEADER_SIZE:]
        for line in body.splitlines():
            if line.strip():
                envelopes.append(json.loads(line))
    return envelopes


class TestRealProducerEndToEnd:
    """ADR-0220: the real production producer (CaptureOrchestrator ->
    SpoolSink) must emit events LineageConsumer can actually extract edges
    from — not just hand-built dict fixtures. Before ADR-0220, the
    orchestrator emitted 'run.start'/'capsule.finalize' while this consumer
    only recognized 'RunStarted'/'ArtifactProduced'/'ArtifactConsumed' — a
    real producer/consumer mismatch that silently produced zero edges.
    """

    def test_orchestrator_run_boundary_events_produce_spawned_by_edge(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        spool_dir = tmp_path / "spool"

        # Parent capsule: no NOVAFABRIC_PARENT_RUN_ID set.
        monkeypatch.delenv("NOVAFABRIC_PARENT_RUN_ID", raising=False)
        monkeypatch.delenv("NOVAFABRIC_GLOBAL_RUN_ID", raising=False)
        script = tmp_path / "agent.py"
        script.write_text("pass\n")
        parent_orch = CaptureOrchestrator(
            base_dir=tmp_path / "runs", emit_spool=True, spool_dir=spool_dir
        )
        parent_result = parent_orch.run(command=[sys.executable, str(script)])

        # Child capsule: NOVAFABRIC_PARENT_RUN_ID points back at the parent.
        monkeypatch.setenv("NOVAFABRIC_PARENT_RUN_ID", parent_result.run_id)
        child_orch = CaptureOrchestrator(
            base_dir=tmp_path / "runs", emit_spool=True, spool_dir=spool_dir
        )
        child_result = child_orch.run(command=[sys.executable, str(script)])

        envelopes = _read_spool_envelopes(spool_dir)
        assert {"RunStarted", "RunCompleted"} <= {e["event_type"] for e in envelopes}

        consumer = LineageConsumer(nats_url="nats://test:4222", kuzu_path="/tmp/test.kuzu")
        edges = asyncio.run(consumer.run_once(envelopes))

        spawned_by = [e for e in edges if e["edge_type"] == "SPAWNED_BY"]
        assert len(spawned_by) == 1
        assert spawned_by[0]["src"] == parent_result.run_id
        assert spawned_by[0]["dst"] == child_result.run_id
