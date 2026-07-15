"""Tests for Evidence Fabric self-contained DuckDB stack.

Covers:
- DuckDBAccumulator: round-trip ingest, query_cost_report, query_lineage_summary,
  export_arrow, empty/edge cases.
- EventQueueConsumer: publish/batch/dead-letter/stats, stop flush.
- LocalPIITable: append/query/export/iceberg-sidecar.
- /api/cost/report: returns real DuckDB data when NOVA_CLICKHOUSE_URL unset.

cap-002 / ADR-0066 (Evidence Fabric v1.0)
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from novafabric.evidence_fabric import (
    DuckDBAccumulator,
    EventQueueConsumer,
    LocalPIITable,
    PIIEvent,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_accumulator(tmp_path: Path) -> DuckDBAccumulator:
    """Return a DuckDBAccumulator backed by a temp in-memory-ish file."""
    return DuckDBAccumulator(tmp_path / "test_evidence.duckdb")


def _make_edges(n: int = 3, tenant: str = "acme") -> list[dict[str, Any]]:
    return [
        {
            "from_ref": f"cap:run-{i}",
            "to_ref": f"cap:run-{i + 1}",
            "edge_type": "contains",
            "tenant": tenant,
        }
        for i in range(n)
    ]


def _make_model_events(
    n: int = 5,
    tenant: str = "acme",
    model: str = "gpt-4o",
    ts: datetime | None = None,
) -> list[dict[str, Any]]:
    base_ts = ts or datetime.utcnow()
    return [
        {
            "run_id": f"run-{i:04d}",
            "event_type": "model_call",
            "model": model,
            "tenant": tenant,
            "ts": base_ts - timedelta(hours=i),
            "tokens_in": 100 * (i + 1),
            "tokens_out": 50 * (i + 1),
        }
        for i in range(n)
    ]


def _make_pii_event(
    run_id: str = "run-001",
    tenant: str = "acme",
    detected_at: datetime | None = None,
) -> PIIEvent:
    return PIIEvent(
        run_id=run_id,
        tenant=tenant,
        detector="regex",
        pii_type="EMAIL",
        redacted=True,
        pepper_hash="deadbeef0000",
        detected_at=detected_at or datetime.utcnow(),
    )


# ===========================================================================
# DuckDBAccumulator tests
# ===========================================================================


class TestDuckDBAccumulatorRoundTrip:
    """Basic ingest and read-back."""

    def test_ingest_edges_returns_count(self, tmp_path: Path) -> None:
        acc = _make_accumulator(tmp_path)
        n = acc.ingest_edges(_make_edges(5))
        assert n == 5
        acc.close()

    def test_ingest_edges_empty_list_returns_zero(self, tmp_path: Path) -> None:
        acc = _make_accumulator(tmp_path)
        assert acc.ingest_edges([]) == 0
        acc.close()

    def test_ingest_capsule_events_returns_count(self, tmp_path: Path) -> None:
        acc = _make_accumulator(tmp_path)
        n = acc.ingest_capsule_events(_make_model_events(4))
        assert n == 4
        acc.close()

    def test_ingest_capsule_events_empty_returns_zero(self, tmp_path: Path) -> None:
        acc = _make_accumulator(tmp_path)
        assert acc.ingest_capsule_events([]) == 0
        acc.close()

    def test_export_arrow_lineage_edges(self, tmp_path: Path) -> None:
        acc = _make_accumulator(tmp_path)
        acc.ingest_edges(_make_edges(3))
        tbl = acc.export_arrow("lineage_edges")
        assert isinstance(tbl, pa.Table)
        assert len(tbl) == 3
        assert "from_ref" in tbl.schema.names
        acc.close()

    def test_export_arrow_capsule_events(self, tmp_path: Path) -> None:
        acc = _make_accumulator(tmp_path)
        acc.ingest_capsule_events(_make_model_events(2))
        tbl = acc.export_arrow("capsule_events")
        assert isinstance(tbl, pa.Table)
        assert len(tbl) == 2
        assert "run_id" in tbl.schema.names
        acc.close()

    def test_export_arrow_unknown_table_raises(self, tmp_path: Path) -> None:
        acc = _make_accumulator(tmp_path)
        with pytest.raises(ValueError, match="Unknown table"):
            acc.export_arrow("nonexistent_table")
        acc.close()

    def test_context_manager(self, tmp_path: Path) -> None:
        with _make_accumulator(tmp_path) as acc:
            n = acc.ingest_edges(_make_edges(2))
        assert n == 2

    def test_edges_persist_across_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "persist.duckdb"
        with DuckDBAccumulator(db_path) as acc:
            acc.ingest_edges(_make_edges(3))
        with DuckDBAccumulator(db_path) as acc2:
            tbl = acc2.export_arrow("lineage_edges")
        assert len(tbl) == 3

    def test_ingest_edges_with_meta_dict(self, tmp_path: Path) -> None:
        acc = _make_accumulator(tmp_path)
        edges = [
            {
                "from_ref": "a",
                "to_ref": "b",
                "edge_type": "spawned",
                "tenant": "t1",
                "meta": {"source": "test", "weight": 1},
            }
        ]
        assert acc.ingest_edges(edges) == 1
        tbl = acc.export_arrow("lineage_edges")
        meta_val = tbl.column("meta")[0].as_py()
        assert json.loads(meta_val)["source"] == "test"
        acc.close()


class TestDuckDBAccumulatorQueryCostReport:
    """query_cost_report aggregation logic."""

    def test_returns_per_model_breakdown(self, tmp_path: Path) -> None:
        acc = _make_accumulator(tmp_path)
        now = datetime.utcnow()
        acc.ingest_capsule_events(_make_model_events(3, model="gpt-4o", tenant="t1"))
        acc.ingest_capsule_events(
            _make_model_events(2, model="claude-sonnet-4-6", tenant="t1")
        )
        report = acc.query_cost_report(
            tenant="t1",
            start=now - timedelta(hours=10),
            end=now + timedelta(hours=1),
        )
        assert "gpt-4o" in report
        assert "claude-sonnet-4-6" in report
        assert report["gpt-4o"]["calls"] == 3
        assert report["claude-sonnet-4-6"]["calls"] == 2
        acc.close()

    def test_token_counts_aggregated_correctly(self, tmp_path: Path) -> None:
        acc = _make_accumulator(tmp_path)
        now = datetime.utcnow()
        events = [
            {
                "run_id": "r1",
                "event_type": "model_call",
                "model": "gpt-4o",
                "tenant": "t1",
                "ts": now,
                "tokens_in": 200,
                "tokens_out": 100,
            },
            {
                "run_id": "r2",
                "event_type": "model_call",
                "model": "gpt-4o",
                "tenant": "t1",
                "ts": now,
                "tokens_in": 300,
                "tokens_out": 150,
            },
        ]
        acc.ingest_capsule_events(events)
        report = acc.query_cost_report(
            tenant="t1",
            start=now - timedelta(minutes=1),
            end=now + timedelta(minutes=1),
        )
        assert report["gpt-4o"]["tokens_in"] == 500
        assert report["gpt-4o"]["tokens_out"] == 250
        assert report["gpt-4o"]["calls"] == 2
        acc.close()

    def test_cost_usd_nonzero_for_known_model(self, tmp_path: Path) -> None:
        acc = _make_accumulator(tmp_path)
        now = datetime.utcnow()
        events = [
            {
                "run_id": "r1",
                "event_type": "model_call",
                "model": "gpt-4o",
                "tenant": "t1",
                "ts": now,
                "tokens_in": 1000,
                "tokens_out": 1000,
            }
        ]
        acc.ingest_capsule_events(events)
        report = acc.query_cost_report(
            tenant="t1",
            start=now - timedelta(minutes=1),
            end=now + timedelta(minutes=1),
        )
        assert report["gpt-4o"]["cost_usd"] > 0.0
        acc.close()

    def test_empty_result_for_unknown_tenant(self, tmp_path: Path) -> None:
        acc = _make_accumulator(tmp_path)
        now = datetime.utcnow()
        acc.ingest_capsule_events(_make_model_events(2, tenant="t1"))
        report = acc.query_cost_report(
            tenant="nobody",
            start=now - timedelta(hours=1),
            end=now + timedelta(hours=1),
        )
        assert report == {}
        acc.close()

    def test_time_range_filters_correctly(self, tmp_path: Path) -> None:
        acc = _make_accumulator(tmp_path)
        now = datetime.utcnow()
        # Insert old events (5 days ago)
        old_ts = now - timedelta(days=5)
        old_events = [
            {
                "run_id": "old",
                "event_type": "model_call",
                "model": "gpt-4o",
                "tenant": "t1",
                "ts": old_ts,
                "tokens_in": 999,
                "tokens_out": 999,
            }
        ]
        acc.ingest_capsule_events(old_events)
        # Query only the last hour — should return nothing
        report = acc.query_cost_report(
            tenant="t1",
            start=now - timedelta(hours=1),
            end=now + timedelta(hours=1),
        )
        assert report == {}
        acc.close()

    def test_tool_call_events_excluded_from_cost_report(
        self, tmp_path: Path
    ) -> None:
        acc = _make_accumulator(tmp_path)
        now = datetime.utcnow()
        events = [
            {
                "run_id": "r1",
                "event_type": "tool_call",
                "tool_name": "bash",
                "tenant": "t1",
                "ts": now,
            }
        ]
        acc.ingest_capsule_events(events)
        report = acc.query_cost_report(
            tenant="t1",
            start=now - timedelta(minutes=1),
            end=now + timedelta(minutes=1),
        )
        assert report == {}
        acc.close()


class TestDuckDBAccumulatorLineageSummary:
    """query_lineage_summary tests."""

    def test_blast_radius_counts(self, tmp_path: Path) -> None:
        acc = _make_accumulator(tmp_path)
        # from_ref "root" has 3 distinct to_ref values
        edges = [
            {"from_ref": "root", "to_ref": f"child-{i}", "edge_type": "contains", "tenant": "t1"}
            for i in range(3)
        ]
        # from_ref "node-1" has 1 to_ref
        edges.append(
            {"from_ref": "node-1", "to_ref": "leaf", "edge_type": "spawned", "tenant": "t1"}
        )
        acc.ingest_edges(edges)
        summary = acc.query_lineage_summary(tenant="t1")
        assert summary["root"] == 3
        assert summary["node-1"] == 1
        acc.close()

    def test_returns_top_20_at_most(self, tmp_path: Path) -> None:
        acc = _make_accumulator(tmp_path)
        edges = [
            {"from_ref": f"src-{i}", "to_ref": f"dst-{i}", "edge_type": "contains", "tenant": "t1"}
            for i in range(25)
        ]
        acc.ingest_edges(edges)
        summary = acc.query_lineage_summary(tenant="t1")
        assert len(summary) <= 20
        acc.close()

    def test_empty_result_for_no_data(self, tmp_path: Path) -> None:
        acc = _make_accumulator(tmp_path)
        assert acc.query_lineage_summary(tenant="nobody") == {}
        acc.close()


# ===========================================================================
# EventQueueConsumer tests
# ===========================================================================


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class TestEventQueueConsumer:
    """asyncio EventQueueConsumer — publish, batch processing, dead-letter, stats."""

    def test_publish_and_process_model_calls(self, tmp_path: Path) -> None:
        async def _go() -> None:
            acc = _make_accumulator(tmp_path)
            consumer = EventQueueConsumer(acc, batch_size=10)
            await consumer.start()

            events = _make_model_events(5)
            for ev in events:
                await consumer.publish(ev)

            # Give the drain loop time to process
            await asyncio.sleep(0.2)
            await consumer.stop()

            tbl = acc.export_arrow("capsule_events")
            assert len(tbl) == 5
            acc.close()

        _run(_go())

    def test_publish_lineage_edges_routed_correctly(self, tmp_path: Path) -> None:
        async def _go() -> None:
            acc = _make_accumulator(tmp_path)
            consumer = EventQueueConsumer(acc, batch_size=10)
            await consumer.start()

            edge_event = {
                "event_type": "lineage_edge",
                "from_ref": "a",
                "to_ref": "b",
                "edge_type": "contains",
                "tenant": "t1",
            }
            await consumer.publish(edge_event)
            await asyncio.sleep(0.2)
            await consumer.stop()

            tbl = acc.export_arrow("lineage_edges")
            assert len(tbl) == 1
            acc.close()

        _run(_go())

    def test_stats_reflect_processed_count(self, tmp_path: Path) -> None:
        async def _go() -> None:
            acc = _make_accumulator(tmp_path)
            consumer = EventQueueConsumer(acc, batch_size=20)
            await consumer.start()

            for ev in _make_model_events(10):
                await consumer.publish(ev)

            await asyncio.sleep(0.3)
            await consumer.stop()

            stats = consumer.stats
            assert stats["processed"] == 10
            assert stats["errors"] == 0
            assert stats["dead_letter"] == 0
            acc.close()

        _run(_go())

    def test_stop_flushes_remaining_events(self, tmp_path: Path) -> None:
        async def _go() -> None:
            acc = _make_accumulator(tmp_path)
            consumer = EventQueueConsumer(acc, batch_size=100)
            await consumer.start()

            for ev in _make_model_events(7):
                await consumer.publish(ev)

            # Stop immediately without waiting for the drain loop
            await consumer.stop()

            tbl = acc.export_arrow("capsule_events")
            assert len(tbl) == 7
            acc.close()

        _run(_go())

    def test_dead_letter_populated_on_failure(self, tmp_path: Path) -> None:
        async def _go() -> None:
            acc = _make_accumulator(tmp_path)
            consumer = EventQueueConsumer(acc, batch_size=10)
            await consumer.start()

            # Patch ingest_capsule_events to raise on the first call
            original = acc.ingest_capsule_events
            call_count = 0

            def _fail_once(events: list) -> int:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise RuntimeError("injected failure")
                return original(events)

            acc.ingest_capsule_events = _fail_once  # type: ignore[method-assign]

            for ev in _make_model_events(3):
                await consumer.publish(ev)

            await asyncio.sleep(0.3)
            await consumer.stop()

            stats = consumer.stats
            # First batch of 3 failed → dead_letter=3
            assert stats["errors"] == 3
            assert stats["dead_letter"] == 3
            acc.close()

        _run(_go())

    def test_stats_initial_state(self, tmp_path: Path) -> None:
        acc = _make_accumulator(tmp_path)
        consumer = EventQueueConsumer(acc, batch_size=10)
        stats = consumer.stats
        assert stats == {"queued": 0, "processed": 0, "errors": 0, "dead_letter": 0}
        acc.close()

    def test_double_start_is_safe(self, tmp_path: Path) -> None:
        async def _go() -> None:
            acc = _make_accumulator(tmp_path)
            consumer = EventQueueConsumer(acc, batch_size=10)
            await consumer.start()
            await consumer.start()  # should not raise
            await consumer.stop()
            acc.close()

        _run(_go())

    def test_dead_letter_list_bounded(self, tmp_path: Path) -> None:
        """Dead-letter list caps at _DEAD_LETTER_MAX (1000) and logs overflow."""
        from novafabric.evidence_fabric.queue_consumer import _DEAD_LETTER_MAX  # noqa: PLC0415

        async def _go() -> None:
            acc = _make_accumulator(tmp_path)
            consumer = EventQueueConsumer(acc, batch_size=200)

            # Force all batches to fail by replacing the method
            def _always_fail(evs: list) -> int:
                raise RuntimeError("always fail")

            acc.ingest_capsule_events = _always_fail  # type: ignore[method-assign]

            # Publish more than the dead-letter cap
            total = _DEAD_LETTER_MAX + 200
            for i in range(total):
                await consumer.publish(
                    {"run_id": f"r{i}", "event_type": "model_call", "tenant": "t1"}
                )

            # Manually drain without the loop
            for _ in range((total // 200) + 2):
                await consumer._process_batch()

            assert len(consumer.dead_letter) <= _DEAD_LETTER_MAX
            acc.close()

        _run(_go())


# ===========================================================================
# LocalPIITable tests
# ===========================================================================


class TestLocalPIITable:
    """LocalPIITable append / query / export / Iceberg sidecar."""

    def test_append_single_event(self, tmp_path: Path) -> None:
        table = LocalPIITable(tmp_path)
        ev = _make_pii_event()
        table.append(ev)
        assert (tmp_path / "pii_events.parquet").exists()

    def test_query_returns_correct_events(self, tmp_path: Path) -> None:
        table = LocalPIITable(tmp_path)
        now = datetime.utcnow()
        ev = _make_pii_event(run_id="run-q1", tenant="corp", detected_at=now)
        table.append(ev)

        results = table.query(
            tenant="corp",
            start=now - timedelta(minutes=1),
            end=now + timedelta(minutes=1),
        )
        assert len(results) == 1
        assert results[0].run_id == "run-q1"

    def test_query_filters_by_tenant(self, tmp_path: Path) -> None:
        table = LocalPIITable(tmp_path)
        now = datetime.utcnow()
        table.append(_make_pii_event(tenant="corp", detected_at=now))
        table.append(_make_pii_event(tenant="other", detected_at=now))

        corp = table.query(
            tenant="corp",
            start=now - timedelta(minutes=1),
            end=now + timedelta(minutes=1),
        )
        assert len(corp) == 1
        assert corp[0].tenant == "corp"

    def test_query_filters_by_time_range(self, tmp_path: Path) -> None:
        table = LocalPIITable(tmp_path)
        now = datetime.utcnow()
        old_ts = now - timedelta(hours=5)
        table.append(_make_pii_event(run_id="old", tenant="t", detected_at=old_ts))
        table.append(_make_pii_event(run_id="new", tenant="t", detected_at=now))

        results = table.query(
            tenant="t",
            start=now - timedelta(minutes=1),
            end=now + timedelta(minutes=1),
        )
        assert len(results) == 1
        assert results[0].run_id == "new"

    def test_query_empty_table_returns_empty_list(self, tmp_path: Path) -> None:
        table = LocalPIITable(tmp_path)
        now = datetime.utcnow()
        results = table.query(
            tenant="t",
            start=now - timedelta(hours=1),
            end=now + timedelta(hours=1),
        )
        assert results == []

    def test_append_many_batch(self, tmp_path: Path) -> None:
        table = LocalPIITable(tmp_path)
        now = datetime.utcnow()
        events = [
            _make_pii_event(run_id=f"run-{i}", detected_at=now) for i in range(10)
        ]
        table.append_many(events)
        results = table.query(
            tenant="acme",
            start=now - timedelta(minutes=1),
            end=now + timedelta(minutes=1),
        )
        assert len(results) == 10

    def test_export_parquet_returns_row_count(self, tmp_path: Path) -> None:
        table = LocalPIITable(tmp_path)
        now = datetime.utcnow()
        for i in range(5):
            table.append(_make_pii_event(run_id=f"run-{i}", detected_at=now))
        out_path = tmp_path / "export.parquet"
        count = table.export_parquet(out_path)
        assert count == 5
        assert out_path.exists()

    def test_export_parquet_file_is_readable(self, tmp_path: Path) -> None:
        import pyarrow.parquet as _pq

        table = LocalPIITable(tmp_path)
        now = datetime.utcnow()
        table.append(_make_pii_event(detected_at=now))
        out_path = tmp_path / "export.parquet"
        table.export_parquet(out_path)
        read_back = _pq.read_table(str(out_path))
        assert len(read_back) == 1

    def test_iceberg_sidecar_created(self, tmp_path: Path) -> None:
        table = LocalPIITable(tmp_path)
        table.append(_make_pii_event())
        sidecar_path = tmp_path / "metadata" / "iceberg_v1.json"
        assert sidecar_path.exists()

    def test_iceberg_sidecar_valid_json(self, tmp_path: Path) -> None:
        table = LocalPIITable(tmp_path)
        table.append(_make_pii_event())
        meta = table.read_iceberg_meta()
        assert meta["format-version"] == 1
        assert "table-uuid" in meta
        assert "schema" in meta
        assert len(meta["snapshots"]) == 1

    def test_iceberg_sidecar_row_count_reflects_data(self, tmp_path: Path) -> None:
        table = LocalPIITable(tmp_path)
        now = datetime.utcnow()
        for i in range(7):
            table.append(_make_pii_event(run_id=f"r{i}", detected_at=now))
        meta = table.read_iceberg_meta()
        assert meta["snapshots"][0]["summary"]["total-records"] == "7"

    def test_iceberg_meta_empty_before_first_write(self, tmp_path: Path) -> None:
        table = LocalPIITable(tmp_path)
        assert table.read_iceberg_meta() == {}

    def test_results_sorted_by_detected_at(self, tmp_path: Path) -> None:
        table = LocalPIITable(tmp_path)
        now = datetime.utcnow()
        # Insert out-of-order
        times = [
            now - timedelta(seconds=30),
            now,
            now - timedelta(seconds=60),
        ]
        for i, ts in enumerate(times):
            table.append(_make_pii_event(run_id=f"r{i}", detected_at=ts))
        results = table.query(
            tenant="acme",
            start=now - timedelta(minutes=2),
            end=now + timedelta(minutes=1),
        )
        dts = [r.detected_at for r in results]
        assert dts == sorted(dts)


# ===========================================================================
# /api/cost/report endpoint — DuckDB fallback
# ===========================================================================

import yaml  # noqa: E402  (conditional import at module level is fine here)

pytest.importorskip("fastapi")
pytest.importorskip("starlette")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

_SERVE_TOKEN = "test-serve-token-abcdef1234"
_SERVE_HEADERS = {"host": "127.0.0.1:4321"}
_SERVE_TOKEN_Q = f"token={_SERVE_TOKEN}"


def _make_capsule_dir(base: Path) -> Path:
    """Create a minimal capsule directory tree for create_app."""
    base.mkdir(parents=True, exist_ok=True)
    run_dir = base / "01TEST00000000000000000001"
    run_dir.mkdir()
    (run_dir / "capsule.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0.0",
                "novafabric_version": "0.26.0",
                "run_id": "01TEST00000000000000000001",
                "created_at": "2026-05-19T00:00:00+00:00",
                "finished_at": "2026-05-19T00:00:01+00:00",
                "duration_ms": 1000,
                "command": ["python", "-c", "pass"],
                "exit_code": 0,
                "status": "success",
                "capture_mode": "cli-wrapper",
                "model_call_count": 0,
                "tool_call_count": 0,
                "mutating_tool_count": 0,
            }
        )
    )
    (run_dir / "trace.jsonl").write_text("")
    (run_dir / "model-calls.jsonl").write_text("")
    (run_dir / "tool-calls.jsonl").write_text("")
    return base


class TestCostReportEndpointDuckDB:
    """/api/cost/report uses DuckDB accumulator when ClickHouse is not configured."""

    def test_cost_report_duckdb_backend_empty_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Endpoint returns ok=True with empty by_model when DuckDB has no events."""
        capsule_dir = _make_capsule_dir(tmp_path / "capsules")
        ev_db = tmp_path / "evidence.duckdb"

        monkeypatch.delenv("NOVA_CLICKHOUSE_URL", raising=False)
        monkeypatch.setenv("NOVA_EVIDENCE_DUCKDB_PATH", str(ev_db))

        app = create_app(
            token=_SERVE_TOKEN,
            capsule_dir=capsule_dir,
            db_path=tmp_path / "registry.db",
            static_dir=None,
        )
        with TestClient(app) as client:
            resp = client.get(
                f"/api/cost/report?days=7&{_SERVE_TOKEN_Q}", headers=_SERVE_HEADERS
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["backend"] == "duckdb"
        assert data["ok"] is True
        assert isinstance(data["by_model"], list)
        assert data["totals"]["cost_usd"] == 0.0

    def test_cost_report_duckdb_backend_with_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Endpoint returns real aggregated data from DuckDB."""
        capsule_dir = _make_capsule_dir(tmp_path / "capsules")
        ev_db = tmp_path / "evidence.duckdb"

        # Pre-populate the DuckDB
        now = datetime.utcnow()
        with DuckDBAccumulator(ev_db) as acc:
            acc.ingest_capsule_events(
                [
                    {
                        "run_id": "r1",
                        "event_type": "model_call",
                        "model": "gpt-4o",
                        "tenant": "default",
                        "ts": now,
                        "tokens_in": 500,
                        "tokens_out": 250,
                    }
                ]
            )

        monkeypatch.delenv("NOVA_CLICKHOUSE_URL", raising=False)
        monkeypatch.setenv("NOVA_EVIDENCE_DUCKDB_PATH", str(ev_db))

        app = create_app(
            token=_SERVE_TOKEN,
            capsule_dir=capsule_dir,
            db_path=tmp_path / "registry.db",
            static_dir=None,
        )
        with TestClient(app) as client:
            resp = client.get(
                f"/api/cost/report?days=7&{_SERVE_TOKEN_Q}", headers=_SERVE_HEADERS
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["backend"] == "duckdb"
        models = {m["model_id"] for m in data["by_model"]}
        assert "gpt-4o" in models
        totals = data["totals"]
        assert totals["input_tokens"] == 500
        assert totals["completion_tokens"] == 250
        assert totals["cost_usd"] > 0.0
