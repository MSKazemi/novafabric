"""Avro serialization for Evidence Fabric events using fastavro.

Provides schema-based serialization and deserialization of evidence events.
The schema is loaded from ``schemas/evidence_event.avsc`` at the same package
level.

cap-001 / cap-002 / ADR-0066 (Evidence Fabric v1.0)
"""

from __future__ import annotations

import json
import logging
from io import BytesIO
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Schema file location relative to this module.
_SCHEMA_PATH = Path(__file__).parent / "schemas" / "evidence_event.avsc"

try:
    from fastavro import parse_schema, reader, writer

    _FASTAVRO_AVAILABLE = True
except ImportError:  # pragma: no cover
    _FASTAVRO_AVAILABLE = False


def _require_fastavro() -> None:
    if not _FASTAVRO_AVAILABLE:
        raise ImportError(
            "fastavro is required for Avro serialization. "
            "Install it with: pip install novafabric[avro]"
        )


class AvroSerializer:
    """Avro serializer/deserializer for EvidenceEvent records.

    Uses ``fastavro`` for schema-based binary encoding.  The schema is read
    from ``schemas/evidence_event.avsc`` (same package directory).

    Raises:
        ImportError: if ``fastavro`` is not installed (gate on first use).
    """

    def __init__(self) -> None:
        _require_fastavro()
        schema_text = _SCHEMA_PATH.read_text()
        self._schema = parse_schema(json.loads(schema_text))

    # ------------------------------------------------------------------
    # Single-record API
    # ------------------------------------------------------------------

    def serialize(self, event: dict[str, Any]) -> bytes:
        """Serialize a single event dict to Avro binary.

        Args:
            event: Dict with keys matching the EvidenceEvent schema fields.

        Returns:
            Avro-encoded bytes (single-object encoding via fastavro writer
            with a BytesIO buffer containing exactly one record).
        """
        buf = BytesIO()
        writer(buf, self._schema, [event])
        return buf.getvalue()

    def deserialize(self, data: bytes) -> dict[str, Any]:
        """Deserialize Avro binary to a single event dict.

        Args:
            data: Avro-encoded bytes produced by :meth:`serialize`.

        Returns:
            The decoded event dict.

        Raises:
            ValueError: if the data decodes to zero or multiple records.
        """
        buf = BytesIO(data)
        records = list(reader(buf))
        if len(records) != 1:
            raise ValueError(
                f"Expected exactly 1 Avro record, got {len(records)}"
            )
        record = records[0]
        if not isinstance(record, dict):
            raise ValueError(f"Expected Avro record mapping, got {type(record).__name__}")
        return dict(record)

    # ------------------------------------------------------------------
    # Batch API
    # ------------------------------------------------------------------

    def serialize_batch(self, events: list[dict[str, Any]]) -> bytes:
        """Serialize a list of event dicts to a single Avro binary blob.

        Args:
            events: List of event dicts, each matching the EvidenceEvent schema.

        Returns:
            Avro-encoded bytes for all records (Avro container format).
        """
        buf = BytesIO()
        writer(buf, self._schema, events)
        return buf.getvalue()

    def deserialize_batch(self, data: bytes) -> list[dict[str, Any]]:
        """Deserialize Avro binary blob to a list of event dicts.

        Args:
            data: Avro-encoded bytes produced by :meth:`serialize_batch`.

        Returns:
            List of decoded event dicts, in the same order they were written.
        """
        buf = BytesIO(data)
        out: list[dict[str, Any]] = []
        for record in reader(buf):
            if not isinstance(record, dict):
                raise ValueError(
                    f"Expected Avro record mapping, got {type(record).__name__}"
                )
            out.append(dict(record))
        return out
