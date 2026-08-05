"""E-1: Read lineage edges from JSONL and convert to Parquet via PyArrow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

# Canonical schema for the lineage edge Parquet table.
_EDGE_SCHEMA = pa.schema(
    [
        pa.field("edge_id", pa.string()),
        pa.field("edge_type", pa.string()),
        pa.field("source_run_id", pa.string()),
        pa.field("target_run_id", pa.string()),
        pa.field("confidence", pa.string()),
        pa.field("capsule_run_id", pa.string()),
        pa.field("schema_version", pa.string()),
        pa.field("created_at", pa.string()),
    ]
)


def _extract_run_id(node: dict[str, Any]) -> str:
    """Extract the run-id string from a source/target node dict."""
    kind = node.get("kind", "")
    if kind == "run":
        return str(node.get("run_id", ""))
    return str(node.get("ref", str(node)))


def edges_from_jsonl(path: Path) -> pa.Table:
    """Read LineageEdge records from a .jsonl file, return as PyArrow table."""
    rows: dict[str, list[Any]] = {field.name: [] for field in _EDGE_SCHEMA}

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj: dict[str, Any] = json.loads(line)
            rows["edge_id"].append(str(obj.get("edge_id", "")))
            rows["edge_type"].append(str(obj.get("edge_type", "")))
            rows["source_run_id"].append(_extract_run_id(obj.get("source", {})))
            rows["target_run_id"].append(_extract_run_id(obj.get("target", {})))
            rows["confidence"].append(str(obj.get("confidence", "")))
            rows["capsule_run_id"].append(str(obj.get("capsule_run_id", "")))
            rows["schema_version"].append(str(obj.get("schema_version", "0.1.0")))
            rows["created_at"].append(str(obj.get("created_at", "")))

    arrays = [pa.array(rows[field.name], type=field.type) for field in _EDGE_SCHEMA]
    return pa.table(arrays, schema=_EDGE_SCHEMA)


def edges_to_parquet(table: pa.Table, output_path: Path) -> Path:
    """Write a PyArrow edges table to a Parquet file. Returns output_path."""
    pq.write_table(table, str(output_path))
    return output_path


def jsonl_to_parquet(jsonl_path: Path, output_path: Path) -> Path:
    """Convert a lineage JSONL file to Parquet. Returns output_path."""
    table = edges_from_jsonl(jsonl_path)
    return edges_to_parquet(table, output_path)
