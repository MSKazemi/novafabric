"""Local Parquet PII event table for Evidence Fabric self-contained mode.

Writes PII detection records to a partitioned Parquet file and generates an
Iceberg-compatible metadata sidecar (no Iceberg SDK required).

cap-001 / ADR-0066 / ADR-0069 (Evidence Fabric v1.0)
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Parquet file name within the base directory.
_PARQUET_FILENAME = "pii_events.parquet"

# Iceberg-compatible sidecar path relative to base directory.
_ICEBERG_META_DIR = "metadata"
_ICEBERG_META_FILE = "iceberg_v1.json"

# PyArrow schema — mirrors PIIEvent fields.
_ARROW_SCHEMA = pa.schema(
    [
        pa.field("run_id", pa.string()),
        pa.field("tenant", pa.string()),
        pa.field("detector", pa.string()),
        pa.field("pii_type", pa.string()),
        pa.field("redacted", pa.bool_()),
        pa.field("pepper_hash", pa.string()),
        pa.field("detected_at", pa.timestamp("us", tz="UTC")),
    ]
)


class PIIEvent(BaseModel):
    """A PII detection record (cap-001, ADR-0066)."""

    run_id: str
    tenant: str
    detector: str
    pii_type: str
    redacted: bool
    pepper_hash: str
    detected_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = {"frozen": True}


class LocalPIITable:
    """Parquet-backed PII event log with an Iceberg-compatible metadata sidecar.

    All writes are append-only.  On ``append()``, the existing Parquet file
    (if any) is read, the new record appended, and the file rewritten.  For
    high-volume use, prefer batched writes via ``append_many()``.

    Thread-safe via an internal ``threading.Lock``.

    Args:
        base_dir: Directory where ``pii_events.parquet`` and
                  ``metadata/iceberg_v1.json`` will be written.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._lock = threading.Lock()
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / _ICEBERG_META_DIR).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def _parquet_path(self) -> Path:
        return self._base_dir / _PARQUET_FILENAME

    @property
    def _iceberg_path(self) -> Path:
        return self._base_dir / _ICEBERG_META_DIR / _ICEBERG_META_FILE

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _read_existing(self) -> pa.Table:
        """Read the existing Parquet file or return an empty table."""
        if self._parquet_path.exists():
            try:
                return pq.read_table(str(self._parquet_path), schema=_ARROW_SCHEMA)  # type: ignore[no-untyped-call]
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "LocalPIITable: could not read existing file %s: %s",
                    self._parquet_path,
                    exc,
                )
        return pa.table(
            {field.name: pa.array([], type=field.type) for field in _ARROW_SCHEMA},
            schema=_ARROW_SCHEMA,
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def append(self, event: PIIEvent) -> None:
        """Append a single PII detection record."""
        self.append_many([event])

    def append_many(self, events: list[PIIEvent]) -> None:
        """Append a batch of PII detection records (more efficient than individual append)."""
        if not events:
            return
        new_rows = pa.table(
            {
                "run_id": pa.array([e.run_id for e in events], type=pa.string()),
                "tenant": pa.array([e.tenant for e in events], type=pa.string()),
                "detector": pa.array([e.detector for e in events], type=pa.string()),
                "pii_type": pa.array([e.pii_type for e in events], type=pa.string()),
                "redacted": pa.array([e.redacted for e in events], type=pa.bool_()),
                "pepper_hash": pa.array(
                    [e.pepper_hash for e in events], type=pa.string()
                ),
                "detected_at": pa.array(
                    [
                        e.detected_at.replace(tzinfo=None)
                        if e.detected_at.tzinfo is not None
                        else e.detected_at
                        for e in events
                    ],
                    type=pa.timestamp("us"),
                ),
            },
            schema=_ARROW_SCHEMA,
        )
        with self._lock:
            existing = self._read_existing()
            combined = pa.concat_tables([existing, new_rows])
            pq.write_table(combined, str(self._parquet_path), compression="snappy")  # type: ignore[no-untyped-call]
            self._write_iceberg_sidecar(combined)
        logger.debug("LocalPIITable: appended %d PII events", len(events))

    def _write_iceberg_sidecar(self, table: pa.Table) -> None:
        """Write an Iceberg-compatible metadata sidecar JSON.

        This is a lightweight JSON dict with schema info and a snapshot
        entry — no Iceberg SDK is required.  It is sufficient for tooling
        that understands basic Iceberg metadata format v1.
        """
        schema_fields = [
            {
                "id": i,
                "name": field.name,
                "type": str(field.type),
                "required": not field.nullable,
            }
            for i, field in enumerate(_ARROW_SCHEMA)
        ]
        snapshot_id = int(datetime.utcnow().timestamp() * 1000)
        meta: dict[str, Any] = {
            "format-version": 1,
            "table-uuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, str(self._parquet_path))),
            "location": str(self._base_dir),
            "schema": {
                "type": "struct",
                "schema-id": 0,
                "fields": schema_fields,
            },
            "current-snapshot-id": snapshot_id,
            "snapshots": [
                {
                    "snapshot-id": snapshot_id,
                    "timestamp-ms": snapshot_id,
                    "summary": {
                        "operation": "append",
                        "total-records": str(len(table)),
                    },
                    "manifest-list": str(self._parquet_path),
                }
            ],
        }
        self._iceberg_path.write_text(json.dumps(meta, indent=2))

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(
        self,
        tenant: str,
        start: datetime,
        end: datetime,
    ) -> list[PIIEvent]:
        """Return PII events for a tenant within a time window.

        Args:
            tenant: Tenant identifier to filter by.
            start:  Inclusive lower bound on ``detected_at``.
            end:    Inclusive upper bound on ``detected_at``.

        Returns:
            List of :class:`PIIEvent` ordered by ``detected_at`` ascending.
        """
        with self._lock:
            table = self._read_existing()

        if len(table) == 0:
            return []

        # Convert to Python dicts for filtering (avoids a DuckDB dependency).
        results: list[PIIEvent] = []
        start_naive = start.replace(tzinfo=None) if start.tzinfo else start
        end_naive = end.replace(tzinfo=None) if end.tzinfo else end

        for batch in table.to_batches():
            for i in range(batch.num_rows):
                row_tenant = batch.column("tenant")[i].as_py()
                if row_tenant != tenant:
                    continue
                detected_at_raw = batch.column("detected_at")[i].as_py()
                # PyArrow may return a timezone-aware datetime; normalise.
                if detected_at_raw is None:
                    continue
                if hasattr(detected_at_raw, "tzinfo") and detected_at_raw.tzinfo:
                    detected_at = detected_at_raw.replace(tzinfo=None)
                else:
                    detected_at = detected_at_raw
                if not (start_naive <= detected_at <= end_naive):
                    continue
                results.append(
                    PIIEvent(
                        run_id=batch.column("run_id")[i].as_py(),
                        tenant=row_tenant,
                        detector=batch.column("detector")[i].as_py(),
                        pii_type=batch.column("pii_type")[i].as_py(),
                        redacted=bool(batch.column("redacted")[i].as_py()),
                        pepper_hash=batch.column("pepper_hash")[i].as_py(),
                        detected_at=detected_at,
                    )
                )

        results.sort(key=lambda e: e.detected_at)
        return results

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_parquet(self, output_path: Path) -> int:
        """Export the full PII event table to an external Parquet file.

        Args:
            output_path: Destination file path.

        Returns:
            Number of rows exported.
        """
        with self._lock:
            table = self._read_existing()
        pq.write_table(table, str(output_path), compression="snappy")  # type: ignore[no-untyped-call]
        logger.debug(
            "LocalPIITable: exported %d rows to %s", len(table), output_path
        )
        return len(table)

    # ------------------------------------------------------------------
    # Iceberg sidecar read
    # ------------------------------------------------------------------

    def read_iceberg_meta(self) -> dict[str, Any]:
        """Return the current Iceberg-compatible metadata sidecar dict.

        Returns an empty dict if the file does not exist yet.
        """
        if not self._iceberg_path.exists():
            return {}
        result: dict[str, Any] = json.loads(self._iceberg_path.read_text())
        return result
