"""Evidence Fabric — self-contained and scale-tier accumulator stack.

Implements the Evidence Fabric v1.0 primitives (cap-001..cap-006/009,
ADR-0066).

Self-contained backends (no optional deps required):
  DuckDBAccumulator  — in-process DuckDB lineage-edge + capsule-event store
  EventQueueConsumer — asyncio.Queue consumer with NATS-compatible interface
  LocalPIITable      — Parquet-backed PII event log with Iceberg-compat sidecar
  PIIEvent           — Pydantic model for a PII detection record

Scale-tier backends (optional deps gated with import guards):
  NATSJetStreamConsumer  — NATS JetStream pull consumer (pip install novafabric[nats])
  ClickHouseAccumulator  — ClickHouse sink for lineage + cost (pip install novafabric[clickhouse])
  AvroSerializer         — Avro binary serialization for evidence events
                           (pip install novafabric[avro])

Scale-tier classes raise ``ImportError`` with a ``pip install`` hint when the
corresponding optional dependency is not installed.  They are always importable
from this package; the error is only raised on instantiation.
"""

from novafabric.evidence_fabric.avro_serializer import AvroSerializer
from novafabric.evidence_fabric.clickhouse_accumulator import ClickHouseAccumulator
from novafabric.evidence_fabric.duckdb_accumulator import DuckDBAccumulator
from novafabric.evidence_fabric.nats_consumer import NATSJetStreamConsumer
from novafabric.evidence_fabric.pii_table import LocalPIITable, PIIEvent
from novafabric.evidence_fabric.queue_consumer import EventQueueConsumer

__all__ = [
    "AvroSerializer",
    "ClickHouseAccumulator",
    "DuckDBAccumulator",
    "EventQueueConsumer",
    "LocalPIITable",
    "NATSJetStreamConsumer",
    "PIIEvent",
]
