"""Tests for LineageConsumer DuckDB Arrow → KuzuDB bulk COPY (cap-006, ADR-0066, ADR-0219)."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from novafabric.lineage.consumer import LineageConsumer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_consumer() -> LineageConsumer:
    return LineageConsumer(nats_url="nats://test:4222", kuzu_path="/tmp/test.kuzu")


def _make_fake_pyarrow():
    """Build a minimal pyarrow stub that tracks calls."""
    pa = types.ModuleType("pyarrow")
    pq = types.ModuleType("pyarrow.parquet")

    class FakeArray:
        def __init__(self, data, type=None):
            self.data = data

    class FakeTable:
        def __init__(self, columns):
            self.columns = columns

    pa.array = FakeArray
    pa.string = MagicMock(return_value="string")
    pa.table = lambda col_dict: FakeTable(col_dict)

    written_files: list[str] = []

    def fake_write_table(table, path):
        written_files.append(path)

    pq.write_table = fake_write_table

    return pa, pq, written_files


def _execute_calls(mock_conn: MagicMock) -> list[str]:
    return [c.args[0] for c in mock_conn.execute.call_args_list]


class TestBulkInsertEdgesEmpty:
    def test_returns_zero_for_empty_list(self) -> None:
        consumer = _make_consumer()
        mock_conn = MagicMock()
        result = consumer.bulk_insert_edges([], mock_conn)
        assert result == 0
        mock_conn.execute.assert_not_called()


class TestBulkInsertEdgesNoPyarrow:
    def test_raises_import_error_when_pyarrow_missing(self) -> None:
        consumer = _make_consumer()
        mock_conn = MagicMock()
        edges = [{"src": "a", "dst": "b", "edge_type": "PRODUCED"}]

        with patch.dict(sys.modules, {"pyarrow": None, "pyarrow.parquet": None}):
            with pytest.raises(ImportError, match="pyarrow required"):
                consumer.bulk_insert_edges(edges, mock_conn)


class TestBulkInsertEdgesDdl:
    """ADR-0219: DDL must be idempotent (IF NOT EXISTS) and run before COPY."""

    def test_creates_node_and_edge_tables_idempotently(self) -> None:
        pa, pq, _ = _make_fake_pyarrow()
        consumer = _make_consumer()
        mock_conn = MagicMock()
        edges = [{"src": "run-1", "dst": "art-1", "edge_type": "PRODUCED"}]

        with patch.dict(sys.modules, {"pyarrow": pa, "pyarrow.parquet": pq}):
            consumer.bulk_insert_edges(edges, mock_conn)

        calls = _execute_calls(mock_conn)
        assert any(
            "CREATE NODE TABLE IF NOT EXISTS LineageNode" in c for c in calls
        )
        assert any("CREATE REL TABLE IF NOT EXISTS LineageEdge" in c for c in calls)


class TestBulkInsertEdgesSuccess:
    def test_calls_kuzu_copy_for_nodes_then_edges(self) -> None:
        """bulk_insert_edges COPYs into LineageNode before LineageEdge (FK order)."""
        pa, pq, written_files = _make_fake_pyarrow()

        consumer = _make_consumer()
        mock_conn = MagicMock()
        edges = [
            {"src": "run-1", "dst": "art-1", "edge_type": "PRODUCED", "source_event_id": "e1"},
            {"src": "art-2", "dst": "run-2", "edge_type": "CONSUMED_BY"},
        ]

        with (
            patch.dict(sys.modules, {"pyarrow": pa, "pyarrow.parquet": pq}),
        ):
            result = consumer.bulk_insert_edges(edges, mock_conn)

        assert result == 2
        calls = _execute_calls(mock_conn)
        node_copy_idx = next(i for i, c in enumerate(calls) if "COPY LineageNode FROM" in c)
        edge_copy_idx = next(i for i, c in enumerate(calls) if "COPY LineageEdge FROM" in c)
        assert node_copy_idx < edge_copy_idx, "nodes must be COPYed before edges"
        assert "IGNORE_ERRORS=true" in calls[node_copy_idx]
        assert "IGNORE_ERRORS=true" in calls[edge_copy_idx]

    def test_writes_node_and_edge_parquet_files(self) -> None:
        """Both a node Parquet file and an edge Parquet file are written."""
        pa, pq, written_files = _make_fake_pyarrow()

        consumer = _make_consumer()
        mock_conn = MagicMock()
        edges = [{"src": "r", "dst": "a", "edge_type": "PRODUCED"}]

        with patch.dict(sys.modules, {"pyarrow": pa, "pyarrow.parquet": pq}):
            consumer.bulk_insert_edges(edges, mock_conn)

        assert len(written_files) == 2
        assert all(p.endswith(".parquet") for p in written_files)
        assert any("nodes" in p for p in written_files)
        assert any("edges" in p for p in written_files)

    def test_temp_files_cleaned_up_after_success(self) -> None:
        """Both temporary Parquet files are deleted after a successful COPY."""
        import os

        pa, pq, _ = _make_fake_pyarrow()

        consumer = _make_consumer()
        mock_conn = MagicMock()
        edges = [{"src": "r", "dst": "a", "edge_type": "PRODUCED"}]

        created_paths: list[str] = []

        def recording_write(table, path):
            created_paths.append(path)
            # Simulate file creation
            with open(path, "w") as f:
                f.write("")

        pq.write_table = recording_write

        with patch.dict(sys.modules, {"pyarrow": pa, "pyarrow.parquet": pq}):
            consumer.bulk_insert_edges(edges, mock_conn)

        assert len(created_paths) == 2
        for p in created_paths:
            assert not os.path.exists(p), f"Temp file not cleaned up: {p}"

    def test_source_event_id_defaults_to_empty_string(self) -> None:
        """Edges without source_event_id get an empty string."""
        pa, pq, _ = _make_fake_pyarrow()

        consumer = _make_consumer()
        mock_conn = MagicMock()
        edges = [{"src": "r", "dst": "a", "edge_type": "PRODUCED"}]  # no source_event_id

        captured_tables: list = []
        original_table_fn = pa.table

        def capturing_table(col_dict):
            captured_tables.append(col_dict)
            return original_table_fn(col_dict)

        pa.table = capturing_table

        with patch.dict(sys.modules, {"pyarrow": pa, "pyarrow.parquet": pq}):
            consumer.bulk_insert_edges(edges, mock_conn)

        # First table built is the node table, second is the edge table.
        assert len(captured_tables) == 2
        edge_col_dict = captured_tables[1]
        event_id_col = edge_col_dict["event_id"]
        assert event_id_col.data == [""]

    def test_node_kinds_derived_from_edge_type(self) -> None:
        """ADR-0219: PRODUCED/CONSUMED_BY split run vs. artifact endpoints."""
        pa, pq, _ = _make_fake_pyarrow()

        consumer = _make_consumer()
        mock_conn = MagicMock()
        edges = [
            {"src": "run-1", "dst": "art-1", "edge_type": "PRODUCED"},
            {"src": "art-2", "dst": "run-2", "edge_type": "CONSUMED_BY"},
            {"src": "run-3", "dst": "run-4", "edge_type": "SPAWNED_BY"},
        ]

        captured_tables: list = []
        original_table_fn = pa.table

        def capturing_table(col_dict):
            captured_tables.append(col_dict)
            return original_table_fn(col_dict)

        pa.table = capturing_table

        with patch.dict(sys.modules, {"pyarrow": pa, "pyarrow.parquet": pq}):
            consumer.bulk_insert_edges(edges, mock_conn)

        node_col_dict = captured_tables[0]
        by_id = dict(zip(node_col_dict["id"].data, node_col_dict["kind"].data))
        assert by_id["run-1"] == "run"
        assert by_id["art-1"] == "artifact"
        assert by_id["art-2"] == "artifact"
        assert by_id["run-2"] == "run"
        assert by_id["run-3"] == "run"
        assert by_id["run-4"] == "run"

    def test_node_ids_deduplicated_across_edges(self) -> None:
        """A node id recurring across multiple edges appears once in the node table."""
        pa, pq, _ = _make_fake_pyarrow()

        consumer = _make_consumer()
        mock_conn = MagicMock()
        edges = [
            {"src": "run-1", "dst": "art-1", "edge_type": "PRODUCED"},
            {"src": "run-1", "dst": "art-2", "edge_type": "PRODUCED"},  # run-1 recurs
        ]

        captured_tables: list = []
        original_table_fn = pa.table

        def capturing_table(col_dict):
            captured_tables.append(col_dict)
            return original_table_fn(col_dict)

        pa.table = capturing_table

        with patch.dict(sys.modules, {"pyarrow": pa, "pyarrow.parquet": pq}):
            consumer.bulk_insert_edges(edges, mock_conn)

        node_col_dict = captured_tables[0]
        assert node_col_dict["id"].data.count("run-1") == 1

    def test_unknown_edge_type_defaults_to_unknown_kind(self) -> None:
        """An edge_type not in the mapping degrades to 'unknown', not a crash."""
        pa, pq, _ = _make_fake_pyarrow()

        consumer = _make_consumer()
        mock_conn = MagicMock()
        edges = [{"src": "x", "dst": "y", "edge_type": "SOME_FUTURE_TYPE"}]

        captured_tables: list = []
        original_table_fn = pa.table

        def capturing_table(col_dict):
            captured_tables.append(col_dict)
            return original_table_fn(col_dict)

        pa.table = capturing_table

        with patch.dict(sys.modules, {"pyarrow": pa, "pyarrow.parquet": pq}):
            result = consumer.bulk_insert_edges(edges, mock_conn)

        assert result == 1
        node_col_dict = captured_tables[0]
        by_id = dict(zip(node_col_dict["id"].data, node_col_dict["kind"].data))
        assert by_id["x"] == "unknown"
        assert by_id["y"] == "unknown"

    def test_temp_files_cleaned_up_after_kuzu_error(self) -> None:
        """Both temporary Parquet files are deleted even when a COPY raises."""
        import os

        pa, pq, _ = _make_fake_pyarrow()

        consumer = _make_consumer()
        mock_conn = MagicMock()

        def execute_side_effect(sql, *args, **kwargs):
            if "COPY LineageEdge FROM" in sql:
                raise RuntimeError("kuzu error")

        mock_conn.execute.side_effect = execute_side_effect
        edges = [{"src": "r", "dst": "a", "edge_type": "PRODUCED"}]

        created_paths: list[str] = []

        def recording_write(table, path):
            created_paths.append(path)
            with open(path, "w") as f:
                f.write("")

        pq.write_table = recording_write

        with patch.dict(sys.modules, {"pyarrow": pa, "pyarrow.parquet": pq}):
            with pytest.raises(RuntimeError, match="kuzu error"):
                consumer.bulk_insert_edges(edges, mock_conn)

        assert len(created_paths) == 2
        for p in created_paths:
            assert not os.path.exists(p), f"Temp file not cleaned up on error: {p}"


# ---------------------------------------------------------------------------
# ADR-0219: live end-to-end verification against a real KuzuDB instance —
# every test above mocks kuzu_conn, so this is the first coverage that
# actually exercises the COPY statements against the real engine.
# ---------------------------------------------------------------------------


class TestBulkInsertEdgesLiveKuzu:
    def test_two_phase_copy_against_real_kuzudb(self, tmp_path: Path) -> None:
        kuzu = pytest.importorskip("kuzu")
        pytest.importorskip("pyarrow")

        db = kuzu.Database(str(tmp_path / "db"))
        conn = kuzu.Connection(db)

        consumer = _make_consumer()
        edges = [
            {
                "src": "run-A",
                "dst": "art-X",
                "edge_type": "PRODUCED",
                "source_event_id": "e1",
            },
            {"src": "art-X", "dst": "run-B", "edge_type": "CONSUMED_BY", "source_event_id": "e2"},
            {"src": "run-C", "dst": "run-A", "edge_type": "SPAWNED_BY", "source_event_id": "e3"},
        ]

        result = consumer.bulk_insert_edges(edges, conn)
        assert result == 3

        nodes = {
            row[0]: row[1]
            for row in conn.execute("MATCH (n:LineageNode) RETURN n.id, n.kind")
        }
        assert nodes == {
            "run-A": "run",
            "art-X": "artifact",
            "run-B": "run",
            "run-C": "run",
        }

        edge_rows = sorted(
            tuple(row)
            for row in conn.execute(
                "MATCH (a:LineageNode)-[e:LineageEdge]->(b:LineageNode) "
                "RETURN e.event_id, a.id, e.edge_type, b.id"
            )
        )
        assert edge_rows == [
            ("e1", "run-A", "PRODUCED", "art-X"),
            ("e2", "art-X", "CONSUMED_BY", "run-B"),
            ("e3", "run-C", "SPAWNED_BY", "run-A"),
        ]

    def test_recurring_node_id_across_batches_does_not_crash(self, tmp_path: Path) -> None:
        """ADR-0219's core idempotency claim: a node id recurring in a LATER
        bulk_insert_edges() call (a later NATS fetch batch) must not raise a
        duplicate-primary-key error."""
        kuzu = pytest.importorskip("kuzu")
        pytest.importorskip("pyarrow")

        db = kuzu.Database(str(tmp_path / "db"))
        conn = kuzu.Connection(db)
        consumer = _make_consumer()

        batch_1 = [{"src": "run-1", "dst": "art-1", "edge_type": "PRODUCED", "source_event_id": "e1"}]
        batch_2 = [
            {"src": "run-1", "dst": "art-2", "edge_type": "PRODUCED", "source_event_id": "e2"}
        ]  # run-1 recurs

        assert consumer.bulk_insert_edges(batch_1, conn) == 1
        assert consumer.bulk_insert_edges(batch_2, conn) == 1  # must not raise

        node_count = list(conn.execute("MATCH (n:LineageNode) RETURN count(n)"))[0][0]
        assert node_count == 3  # run-1, art-1, art-2 — run-1 not duplicated

        edge_count = list(conn.execute("MATCH ()-[e:LineageEdge]->() RETURN count(e)"))[0][0]
        assert edge_count == 2
