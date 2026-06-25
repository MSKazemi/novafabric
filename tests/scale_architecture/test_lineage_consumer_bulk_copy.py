"""Tests for LineageConsumer DuckDB Arrow → KuzuDB bulk COPY (cap-006, ADR-0066)."""

from __future__ import annotations

import sys
import types
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


class TestBulkInsertEdgesSuccess:
    def test_calls_kuzu_copy(self) -> None:
        """bulk_insert_edges calls kuzu_conn.execute with COPY statement."""
        pa, pq, written_files = _make_fake_pyarrow()

        consumer = _make_consumer()
        mock_conn = MagicMock()
        edges = [
            {"src": "run-1", "dst": "art-1", "edge_type": "PRODUCED", "source_event_id": "e1"},
            {"src": "run-2", "dst": "art-2", "edge_type": "CONSUMED_BY"},
        ]

        with (
            patch.dict(sys.modules, {"pyarrow": pa, "pyarrow.parquet": pq}),
        ):
            result = consumer.bulk_insert_edges(edges, mock_conn)

        assert result == 2
        mock_conn.execute.assert_called_once()
        call_sql: str = mock_conn.execute.call_args.args[0]
        assert "COPY LineageEdge FROM" in call_sql
        assert "IGNORE_ERRORS=true" in call_sql

    def test_writes_parquet_file(self) -> None:
        """A temporary Parquet file is written during bulk insertion."""
        pa, pq, written_files = _make_fake_pyarrow()

        consumer = _make_consumer()
        mock_conn = MagicMock()
        edges = [{"src": "r", "dst": "a", "edge_type": "PRODUCED"}]

        with patch.dict(sys.modules, {"pyarrow": pa, "pyarrow.parquet": pq}):
            consumer.bulk_insert_edges(edges, mock_conn)

        assert len(written_files) == 1
        assert written_files[0].endswith(".parquet")

    def test_temp_file_cleaned_up_after_success(self) -> None:
        """The temporary Parquet file is deleted after a successful COPY."""
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

        # File should be deleted after successful COPY
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

        assert len(captured_tables) == 1
        col_dict = captured_tables[0]
        # event_id column should exist and have empty string
        event_id_col = col_dict["event_id"]
        assert event_id_col.data == [""]

    def test_temp_file_cleaned_up_after_kuzu_error(self) -> None:
        """The temporary Parquet file is deleted even when KuzuDB raises."""
        import os

        pa, pq, _ = _make_fake_pyarrow()

        consumer = _make_consumer()
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = RuntimeError("kuzu error")
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

        # File should still be cleaned up
        for p in created_paths:
            assert not os.path.exists(p), f"Temp file not cleaned up on error: {p}"
