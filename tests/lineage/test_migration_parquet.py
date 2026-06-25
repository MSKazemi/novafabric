"""E-1 tests: parquet_replay module — edges_from_jsonl, edges_to_parquet, jsonl_to_parquet."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from novafabric.lineage.migration.parquet_replay import (
    _EDGE_SCHEMA,
    edges_from_jsonl,
    edges_to_parquet,
    jsonl_to_parquet,
)


def _make_edge_dict(i: int) -> dict:
    return {
        "edge_id": f"edge-{i:04d}",
        "edge_type": "contains",
        "source": {"kind": "run", "run_id": f"run-src-{i}"},
        "target": {"kind": "run", "run_id": f"run-tgt-{i}"},
        "confidence": "high",
        "capsule_run_id": f"capsule-{i}",
        "schema_version": "0.1.0",
        "created_at": "2026-01-01T00:00:00.000000Z",
    }


def _write_jsonl(path: Path, count: int) -> None:
    with path.open("w") as fh:
        for i in range(count):
            fh.write(json.dumps(_make_edge_dict(i)) + "\n")


class TestEdgesFromJsonl:
    def test_row_count(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "edges.jsonl"
        _write_jsonl(jsonl, 10)
        table = edges_from_jsonl(jsonl)
        assert table.num_rows == 10

    def test_schema_column_names(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "edges.jsonl"
        _write_jsonl(jsonl, 3)
        table = edges_from_jsonl(jsonl)
        expected = {f.name for f in _EDGE_SCHEMA}
        assert set(table.schema.names) == expected

    def test_all_eight_columns_present(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "edges.jsonl"
        _write_jsonl(jsonl, 1)
        table = edges_from_jsonl(jsonl)
        assert len(table.schema) == 8

    def test_edge_id_values(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "edges.jsonl"
        _write_jsonl(jsonl, 5)
        table = edges_from_jsonl(jsonl)
        ids = table.column("edge_id").to_pylist()
        assert ids[0] == "edge-0000"
        assert ids[4] == "edge-0004"

    def test_source_run_id_extracted(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "edges.jsonl"
        _write_jsonl(jsonl, 2)
        table = edges_from_jsonl(jsonl)
        assert table.column("source_run_id")[0].as_py() == "run-src-0"

    def test_target_run_id_extracted(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "edges.jsonl"
        _write_jsonl(jsonl, 2)
        table = edges_from_jsonl(jsonl)
        assert table.column("target_run_id")[0].as_py() == "run-tgt-0"

    def test_empty_jsonl_zero_rows(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "empty.jsonl"
        jsonl.write_text("")
        table = edges_from_jsonl(jsonl)
        assert table.num_rows == 0

    def test_empty_jsonl_same_schema(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "empty.jsonl"
        jsonl.write_text("")
        table = edges_from_jsonl(jsonl)
        assert set(table.schema.names) == {f.name for f in _EDGE_SCHEMA}

    def test_column_types_are_string(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "edges.jsonl"
        _write_jsonl(jsonl, 2)
        table = edges_from_jsonl(jsonl)
        for field in table.schema:
            assert pa.types.is_string(field.type), f"Field {field.name} is not string"

    def test_blank_lines_skipped(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "edges.jsonl"
        with jsonl.open("w") as fh:
            fh.write(json.dumps(_make_edge_dict(0)) + "\n")
            fh.write("\n")
            fh.write(json.dumps(_make_edge_dict(1)) + "\n")
        table = edges_from_jsonl(jsonl)
        assert table.num_rows == 2


class TestEdgesToParquet:
    def test_returns_output_path(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "edges.jsonl"
        _write_jsonl(jsonl, 3)
        table = edges_from_jsonl(jsonl)
        out = tmp_path / "edges.parquet"
        result = edges_to_parquet(table, out)
        assert result == out

    def test_file_created(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "edges.jsonl"
        _write_jsonl(jsonl, 3)
        table = edges_from_jsonl(jsonl)
        out = tmp_path / "edges.parquet"
        edges_to_parquet(table, out)
        assert out.exists()

    def test_roundtrip_row_count(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "edges.jsonl"
        _write_jsonl(jsonl, 10)
        table = edges_from_jsonl(jsonl)
        out = tmp_path / "edges.parquet"
        edges_to_parquet(table, out)
        read_back = pq.read_table(str(out))
        assert read_back.num_rows == 10

    def test_roundtrip_schema(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "edges.jsonl"
        _write_jsonl(jsonl, 5)
        table = edges_from_jsonl(jsonl)
        out = tmp_path / "edges.parquet"
        edges_to_parquet(table, out)
        read_back = pq.read_table(str(out))
        assert set(read_back.schema.names) == {f.name for f in _EDGE_SCHEMA}

    def test_roundtrip_values_preserved(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "edges.jsonl"
        _write_jsonl(jsonl, 3)
        table = edges_from_jsonl(jsonl)
        out = tmp_path / "edges.parquet"
        edges_to_parquet(table, out)
        read_back = pq.read_table(str(out))
        assert read_back.column("edge_id")[0].as_py() == "edge-0000"

    def test_empty_table_roundtrip(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "empty.jsonl"
        jsonl.write_text("")
        table = edges_from_jsonl(jsonl)
        out = tmp_path / "empty.parquet"
        edges_to_parquet(table, out)
        read_back = pq.read_table(str(out))
        assert read_back.num_rows == 0
        assert set(read_back.schema.names) == {f.name for f in _EDGE_SCHEMA}


class TestJsonlToParquet:
    def test_convenience_function(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "edges.jsonl"
        _write_jsonl(jsonl, 10)
        out = tmp_path / "edges.parquet"
        result = jsonl_to_parquet(jsonl, out)
        assert result == out
        assert out.exists()

    def test_row_count_matches(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "edges.jsonl"
        _write_jsonl(jsonl, 10)
        out = tmp_path / "edges.parquet"
        jsonl_to_parquet(jsonl, out)
        table = pq.read_table(str(out))
        assert table.num_rows == 10

    def test_empty_jsonl_to_parquet(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "empty.jsonl"
        jsonl.write_text("")
        out = tmp_path / "empty.parquet"
        jsonl_to_parquet(jsonl, out)
        table = pq.read_table(str(out))
        assert table.num_rows == 0
        assert len(table.schema) == 8
