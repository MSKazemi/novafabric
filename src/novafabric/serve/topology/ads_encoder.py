"""ADS v1 — Arrow Dashboard Schema encoder and validator.

Produces Arrow IPC batches with schema_id custom metadata for all four
ADS v1 schema types. See cap-007 in the topology dashboard build prompt.
"""

from __future__ import annotations

import io
import json as _json
from typing import Any

import pyarrow as pa
import pyarrow.ipc as pa_ipc

SCHEMA_IDS: dict[str, str] = {
    "metric_frame": "ads.v1.metric_frame",
    "cluster_layer": "ads.v1.cluster_layer",
    "subgraph_page_nodes": "ads.v1.subgraph_page.nodes",
    "subgraph_page_edges": "ads.v1.subgraph_page.edges",
    "delta_event": "ads.v1.delta_event",
}

# ---------- ADS v1 schemas ----------

ADS_METRIC_FRAME_SCHEMA = pa.schema(
    [
        pa.field("agent_id", pa.dictionary(pa.int32(), pa.utf8())),
        pa.field("agent_type", pa.dictionary(pa.int32(), pa.utf8())),
        pa.field("status", pa.dictionary(pa.int32(), pa.utf8())),
        pa.field("p50_latency_ms", pa.float64()),
        pa.field("p95_latency_ms", pa.float64()),
        pa.field("p99_latency_ms", pa.float64()),
        pa.field("error_rate", pa.float64()),
        pa.field("token_count_in", pa.int64()),
        pa.field("token_count_out", pa.int64()),
        pa.field("ts_start", pa.timestamp("ms", tz="UTC")),
        pa.field("ts_end", pa.timestamp("ms", tz="UTC")),
    ],
    metadata={"schema_id": SCHEMA_IDS["metric_frame"]},
)

ADS_CLUSTER_LAYER_SCHEMA = pa.schema(
    [
        pa.field("cluster_id", pa.int64()),
        pa.field("centroid_x", pa.float64()),
        pa.field("centroid_y", pa.float64()),
        pa.field("agent_count", pa.int64()),
        pa.field("inter_cluster_edges", pa.int64()),
    ],
    metadata={"schema_id": SCHEMA_IDS["cluster_layer"]},
)

ADS_SUBGRAPH_PAGE_NODES_SCHEMA = pa.schema(
    [
        pa.field("node_id", pa.utf8()),
        pa.field("agent_type", pa.dictionary(pa.int32(), pa.utf8())),
        pa.field("status", pa.dictionary(pa.int32(), pa.utf8())),
        pa.field("cluster_id", pa.int64()),
        pa.field("pos_x", pa.float64()),
        pa.field("pos_y", pa.float64()),
    ],
    metadata={"schema_id": SCHEMA_IDS["subgraph_page_nodes"]},
)

ADS_SUBGRAPH_PAGE_EDGES_SCHEMA = pa.schema(
    [
        pa.field("source_id", pa.utf8()),
        pa.field("target_id", pa.utf8()),
        pa.field("edge_type", pa.dictionary(pa.int32(), pa.utf8())),
        pa.field("cluster_id", pa.int64()),
    ],
    metadata={"schema_id": SCHEMA_IDS["subgraph_page_edges"]},
)


def _to_ipc(table: pa.Table) -> bytes:
    buf = io.BytesIO()
    with pa_ipc.new_stream(buf, table.schema) as writer:  # type: ignore[no-untyped-call]
        writer.write_table(table)
    return buf.getvalue()


def encode_cluster_layer(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        table = pa.table(
            {
                "cluster_id": pa.array([], type=pa.int64()),
                "centroid_x": pa.array([], type=pa.float64()),
                "centroid_y": pa.array([], type=pa.float64()),
                "agent_count": pa.array([], type=pa.int64()),
                "inter_cluster_edges": pa.array([], type=pa.int64()),
            },
            schema=ADS_CLUSTER_LAYER_SCHEMA,
        )
    else:
        table = pa.Table.from_pylist(rows, schema=ADS_CLUSTER_LAYER_SCHEMA)
    return _to_ipc(table)


def encode_subgraph_nodes(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        table = pa.table(
            {
                "node_id": pa.array([], type=pa.utf8()),
                "agent_type": pa.array([], type=pa.dictionary(pa.int32(), pa.utf8())),
                "status": pa.array([], type=pa.dictionary(pa.int32(), pa.utf8())),
                "cluster_id": pa.array([], type=pa.int64()),
                "pos_x": pa.array([], type=pa.float64()),
                "pos_y": pa.array([], type=pa.float64()),
            },
            schema=ADS_SUBGRAPH_PAGE_NODES_SCHEMA,
        )
    else:
        table = pa.Table.from_pylist(rows, schema=ADS_SUBGRAPH_PAGE_NODES_SCHEMA)
    return _to_ipc(table)


def encode_subgraph_edges(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        table = pa.table(
            {
                "source_id": pa.array([], type=pa.utf8()),
                "target_id": pa.array([], type=pa.utf8()),
                "edge_type": pa.array([], type=pa.dictionary(pa.int32(), pa.utf8())),
                "cluster_id": pa.array([], type=pa.int64()),
            },
            schema=ADS_SUBGRAPH_PAGE_EDGES_SCHEMA,
        )
    else:
        table = pa.Table.from_pylist(rows, schema=ADS_SUBGRAPH_PAGE_EDGES_SCHEMA)
    return _to_ipc(table)


# ADS v1 delta event schema — one row per event, payload is JSON-encoded remainder
ADS_DELTA_EVENT_SCHEMA = pa.schema(
    [
        pa.field("event_type", pa.utf8()),
        pa.field("payload", pa.binary()),
    ],
    metadata={"schema_id": SCHEMA_IDS["delta_event"]},
)


def encode_delta_event_arrow(event: dict[str, Any]) -> bytes:
    """Encode a TDP delta event dict as a single-row Arrow IPC binary frame.

    Schema: event_type (utf8) + payload (binary / JSON-encoded remaining fields).
    The ``event_type`` column carries ``event["type"]``; ``payload`` carries the
    full event dict re-serialised as UTF-8 JSON bytes so that the client can
    reconstruct any event without schema changes for new field additions.

    Returns the Arrow IPC stream bytes (same transport format as subgraph frames).
    """
    event_type = str(event.get("type", "unknown"))
    payload_bytes = _json.dumps(event).encode("utf-8")
    table = pa.table(
        {
            "event_type": pa.array([event_type], type=pa.utf8()),
            "payload": pa.array([payload_bytes], type=pa.binary()),
        },
        schema=ADS_DELTA_EVENT_SCHEMA,
    )
    return _to_ipc(table)


class AdsValidator:
    """Validates Arrow IPC batches for ADS v1 compliance."""

    def validate(self, ipc_bytes: bytes) -> dict[str, Any]:
        errors: list[str] = []
        try:
            reader = pa_ipc.open_stream(ipc_bytes)  # type: ignore[no-untyped-call]
            schema = reader.schema
            schema_id = (schema.metadata or {}).get(b"schema_id", b"").decode("utf-8")
            if not schema_id:
                errors.append("missing schema_id in Arrow IPC custom metadata")
            _ = reader.read_all()  # consume to validate record batches
        except Exception as exc:
            errors.append(f"Arrow IPC parse error: {exc}")
        return {"valid": len(errors) == 0, "errors": errors}
