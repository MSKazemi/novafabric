"""Tests for ADS v1 Arrow schema encoder (FR-01..FR-04)."""

from __future__ import annotations

import io
import json

import pyarrow as pa
import pyarrow.ipc as pa_ipc

from novafabric.serve.topology.ads_encoder import (
    ADS_CLUSTER_LAYER_SCHEMA,
    ADS_METRIC_FRAME_SCHEMA,
    ADS_SUBGRAPH_PAGE_EDGES_SCHEMA,
    ADS_SUBGRAPH_PAGE_NODES_SCHEMA,
    SCHEMA_IDS,
    AdsValidator,
    encode_cluster_layer,
    encode_delta_event_arrow,
    encode_subgraph_edges,
    encode_subgraph_nodes,
)

# ---------- FR-01: metric_frame schema ----------

def test_metric_frame_schema_column_count() -> None:
    assert len(ADS_METRIC_FRAME_SCHEMA) == 11


def test_metric_frame_schema_agent_id_is_dict() -> None:
    f = ADS_METRIC_FRAME_SCHEMA.field("agent_id")
    assert pa.types.is_dictionary(f.type)


def test_metric_frame_schema_latency_is_float64() -> None:
    for col in ("p50_latency_ms", "p95_latency_ms", "p99_latency_ms"):
        assert ADS_METRIC_FRAME_SCHEMA.field(col).type == pa.float64(), col


def test_metric_frame_schema_token_counts_int64() -> None:
    for col in ("token_count_in", "token_count_out"):
        assert ADS_METRIC_FRAME_SCHEMA.field(col).type == pa.int64(), col


def test_metric_frame_schema_timestamps_utc() -> None:
    for col in ("ts_start", "ts_end"):
        t = ADS_METRIC_FRAME_SCHEMA.field(col).type
        assert pa.types.is_timestamp(t)
        assert t.tz == "UTC", col


# ---------- FR-02: cluster_layer schema ----------

def test_cluster_layer_schema_column_count() -> None:
    assert len(ADS_CLUSTER_LAYER_SCHEMA) == 5


def test_cluster_layer_schema_types() -> None:
    assert ADS_CLUSTER_LAYER_SCHEMA.field("cluster_id").type == pa.int64()
    assert ADS_CLUSTER_LAYER_SCHEMA.field("centroid_x").type == pa.float64()
    assert ADS_CLUSTER_LAYER_SCHEMA.field("centroid_y").type == pa.float64()
    assert ADS_CLUSTER_LAYER_SCHEMA.field("agent_count").type == pa.int64()
    assert ADS_CLUSTER_LAYER_SCHEMA.field("inter_cluster_edges").type == pa.int64()


# ---------- FR-03: subgraph_page schemas ----------

def test_subgraph_nodes_schema_column_count() -> None:
    assert len(ADS_SUBGRAPH_PAGE_NODES_SCHEMA) == 6


def test_subgraph_nodes_schema_types() -> None:
    s = ADS_SUBGRAPH_PAGE_NODES_SCHEMA
    assert s.field("node_id").type == pa.utf8()
    assert pa.types.is_dictionary(s.field("agent_type").type)
    assert pa.types.is_dictionary(s.field("status").type)
    assert s.field("cluster_id").type == pa.int64()
    assert s.field("pos_x").type == pa.float64()
    assert s.field("pos_y").type == pa.float64()


def test_subgraph_edges_schema_column_count() -> None:
    assert len(ADS_SUBGRAPH_PAGE_EDGES_SCHEMA) == 4


def test_subgraph_edges_schema_types() -> None:
    s = ADS_SUBGRAPH_PAGE_EDGES_SCHEMA
    assert s.field("source_id").type == pa.utf8()
    assert s.field("target_id").type == pa.utf8()
    assert pa.types.is_dictionary(s.field("edge_type").type)
    assert s.field("cluster_id").type == pa.int64()


# ---------- FR-04: schema_id in metadata ----------

def test_encode_cluster_layer_has_schema_id() -> None:
    rows = [{"cluster_id": 1, "centroid_x": 0.5, "centroid_y": 0.5, "agent_count": 10, "inter_cluster_edges": 2}]
    ipc = encode_cluster_layer(rows)
    reader = pa_ipc.open_stream(ipc)
    schema_id = (reader.schema.metadata or {}).get(b"schema_id", b"").decode()
    assert schema_id == SCHEMA_IDS["cluster_layer"]


def test_encode_subgraph_nodes_has_schema_id() -> None:
    rows = [{"node_id": "a1", "agent_type": "llm", "status": "running", "cluster_id": 0, "pos_x": 1.0, "pos_y": 2.0}]
    ipc = encode_subgraph_nodes(rows)
    reader = pa_ipc.open_stream(ipc)
    schema_id = (reader.schema.metadata or {}).get(b"schema_id", b"").decode()
    assert schema_id == SCHEMA_IDS["subgraph_page_nodes"]


def test_encode_subgraph_edges_has_schema_id() -> None:
    rows = [{"source_id": "a1", "target_id": "a2", "edge_type": "calls", "cluster_id": 0}]
    ipc = encode_subgraph_edges(rows)
    reader = pa_ipc.open_stream(ipc)
    schema_id = (reader.schema.metadata or {}).get(b"schema_id", b"").decode()
    assert schema_id == SCHEMA_IDS["subgraph_page_edges"]


# ---------- A-2: encode_delta_event_arrow ----------

def test_encode_delta_event_arrow_has_delta_event_schema_id() -> None:
    event = {"type": "add_node", "seq": 1, "node_id": "n1", "agent_type": "llm",
             "status": "running", "cluster_id": 0, "pos_x": 0.0, "pos_y": 0.0}
    ipc = encode_delta_event_arrow(event)
    reader = pa_ipc.open_stream(ipc)
    schema_id = (reader.schema.metadata or {}).get(b"schema_id", b"").decode()
    assert schema_id == "ads.v1.delta_event"


def test_encode_delta_event_arrow_single_row() -> None:
    event = {"type": "remove_node", "seq": 2, "node_id": "n2"}
    ipc = encode_delta_event_arrow(event)
    reader = pa_ipc.open_stream(ipc)
    table = reader.read_all()
    assert table.num_rows == 1


def test_encode_delta_event_arrow_event_type_column() -> None:
    event = {"type": "add_edge", "seq": 3, "source_id": "a", "target_id": "b",
             "edge_type": "calls", "cluster_id": 0}
    ipc = encode_delta_event_arrow(event)
    reader = pa_ipc.open_stream(ipc)
    table = reader.read_all()
    assert table.column("event_type")[0].as_py() == "add_edge"


def test_encode_delta_event_arrow_payload_is_full_event_json() -> None:
    event = {"type": "batch_checkpoint", "seq": 4, "ts_ms": 1234567890}
    ipc = encode_delta_event_arrow(event)
    reader = pa_ipc.open_stream(ipc)
    table = reader.read_all()
    payload_bytes = table.column("payload")[0].as_py()
    payload = json.loads(payload_bytes)
    assert payload["type"] == "batch_checkpoint"
    assert payload["seq"] == 4
    assert payload["ts_ms"] == 1234567890


def test_encode_delta_event_arrow_unknown_type_defaults_to_unknown() -> None:
    event = {"no_type_key": True}
    ipc = encode_delta_event_arrow(event)
    reader = pa_ipc.open_stream(ipc)
    table = reader.read_all()
    assert table.column("event_type")[0].as_py() == "unknown"


def test_encode_delta_event_arrow_validator_accepts() -> None:
    event = {"type": "topology_reset", "seq": 5}
    ipc = encode_delta_event_arrow(event)
    result = AdsValidator().validate(ipc)
    assert result["valid"] is True


# ---------- AdsValidator ----------

def test_validator_accepts_valid_cluster_layer() -> None:
    ipc = encode_cluster_layer([])
    result = AdsValidator().validate(ipc)
    assert result["valid"] is True
    assert result["errors"] == []


def test_validator_rejects_missing_schema_id() -> None:
    # Build an IPC batch without schema_id in metadata
    schema = pa.schema([pa.field("x", pa.int64())])
    table = pa.table({"x": pa.array([1, 2], type=pa.int64())}, schema=schema)
    buf = io.BytesIO()
    with pa_ipc.new_stream(buf, schema) as w:
        w.write_table(table)
    result = AdsValidator().validate(buf.getvalue())
    assert result["valid"] is False
    assert any("schema_id" in e for e in result["errors"])


def test_validator_rejects_malformed_bytes() -> None:
    result = AdsValidator().validate(b"not arrow ipc at all")
    assert result["valid"] is False
