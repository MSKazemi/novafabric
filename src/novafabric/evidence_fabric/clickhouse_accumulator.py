"""ClickHouse accumulator for Evidence Fabric scale tier.

Provides the same ``ingest_edges`` / ``ingest_capsule_events`` interface as
:class:`DuckDBAccumulator` but writes to a real ClickHouse cluster via the
``clickhouse-connect`` driver.  Also implements ``aggregate_cost_report`` which
replaces the stub in ``nova cost report`` when a ClickHouse backend is active.

cap-002 / ADR-0066 (Evidence Fabric v1.0 — scale tier)

Environment variables
---------------------
``NOVA_CLICKHOUSE_URL``
    ClickHouse HTTP endpoint (default: ``http://localhost:8123``).
``NOVA_CLICKHOUSE_DB``
    Database / schema name (default: ``nova``).
``NOVA_CLICKHOUSE_USER``
    Username (default: ``default``).
``NOVA_CLICKHOUSE_PASSWORD``
    Password (default: empty string).
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# Optional import gate
# -------------------------------------------------------------------
try:
    import clickhouse_connect

    _CH_AVAILABLE = True
except ImportError:  # pragma: no cover
    _CH_AVAILABLE = False


def _require_clickhouse() -> None:
    if not _CH_AVAILABLE:
        raise ImportError(
            "clickhouse-connect is required for the ClickHouse accumulator. "
            "Install it with: pip install novafabric[clickhouse]"
        )


# DDL executed on first use (CREATE TABLE IF NOT EXISTS — idempotent).
_DDL_LINEAGE_EDGES = """\
CREATE TABLE IF NOT EXISTS {db}.lineage_edges (
    from_ref   String      NOT NULL,
    to_ref     String      NOT NULL,
    edge_type  String      NOT NULL,
    tenant     String      NOT NULL,
    created_at DateTime64(3) DEFAULT now64(),
    meta       Nullable(String)
) ENGINE = MergeTree()
ORDER BY (tenant, from_ref, to_ref)
"""

_DDL_CAPSULE_EVENTS = """\
CREATE TABLE IF NOT EXISTS {db}.capsule_events (
    run_id       String      NOT NULL,
    event_type   String      NOT NULL,
    model_id     Nullable(String),
    tool_name    Nullable(String),
    tenant       String      NOT NULL,
    ts           DateTime64(3) DEFAULT now64(),
    tokens_total Nullable(Int64)
) ENGINE = MergeTree()
ORDER BY (tenant, run_id, ts)
"""


class ClickHouseAccumulator:
    """ClickHouse sink for lineage edges and capsule events.

    Presents the same write interface as :class:`DuckDBAccumulator` so it can
    be used as a drop-in replacement in :class:`NATSJetStreamConsumer`.

    Tables are created on first write if they do not already exist.

    Args:
        client: An existing ``clickhouse_connect`` client.  If ``None``,
                one is created from environment variables on first use.
    """

    def __init__(self, client: Any = None) -> None:
        _require_clickhouse()
        self._client = client
        self._db = os.environ.get("NOVA_CLICKHOUSE_DB", "nova")
        self._schema_created = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _get_client(self) -> Any:
        """Return (or lazily create) the clickhouse-connect client."""
        if self._client is not None:
            return self._client

        url = os.environ.get("NOVA_CLICKHOUSE_URL", "http://localhost:8123")
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 8123
        user = os.environ.get("NOVA_CLICKHOUSE_USER", "default")
        password = os.environ.get("NOVA_CLICKHOUSE_PASSWORD", "")

        self._client = clickhouse_connect.get_client(
            host=host,
            port=port,
            username=user,
            password=password,
            database=self._db,
        )
        return self._client

    def _ensure_schema(self) -> None:
        """Create tables if they don't already exist (idempotent)."""
        if self._schema_created:
            return
        client = self._get_client()
        client.command(_DDL_LINEAGE_EDGES.format(db=self._db))
        client.command(_DDL_CAPSULE_EVENTS.format(db=self._db))
        self._schema_created = True
        logger.debug("ClickHouseAccumulator: schema ensured (db=%s)", self._db)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def ingest_edges(self, edges: list[dict[str, Any]]) -> int:
        """Bulk-insert lineage edges into ClickHouse.

        Each dict must contain ``from_ref``, ``to_ref``, ``edge_type``,
        ``tenant``.  Optional: ``meta`` (str or dict, JSON-serialized if dict).

        Returns:
            Number of rows inserted.
        """
        if not edges:
            return 0

        import json as _json

        self._ensure_schema()
        client = self._get_client()

        rows = []
        for e in edges:
            meta = e.get("meta")
            if isinstance(meta, dict):
                meta = _json.dumps(meta)
            rows.append(
                [
                    e["from_ref"],
                    e["to_ref"],
                    e["edge_type"],
                    e.get("tenant", "default"),
                    meta,
                ]
            )

        client.insert(
            f"{self._db}.lineage_edges",
            rows,
            column_names=["from_ref", "to_ref", "edge_type", "tenant", "meta"],
        )
        logger.debug("ClickHouseAccumulator: inserted %d lineage edges", len(edges))
        return len(edges)

    def ingest_events(self, events: list[dict[str, Any]]) -> int:
        """Bulk-insert capsule events into ClickHouse.

        Each dict must contain ``run_id``, ``event_type``, ``tenant``.
        Optional: ``model_id`` (str), ``tool_name`` (str), ``tokens_total`` (int).

        Returns:
            Number of rows inserted.
        """
        if not events:
            return 0

        self._ensure_schema()
        client = self._get_client()

        rows = []
        for ev in events:
            rows.append(
                [
                    ev["run_id"],
                    ev["event_type"],
                    ev.get("model_id") or ev.get("model"),
                    ev.get("tool_name"),
                    ev.get("tenant", "default"),
                    ev.get("tokens_total"),
                ]
            )

        client.insert(
            f"{self._db}.capsule_events",
            rows,
            column_names=[
                "run_id",
                "event_type",
                "model_id",
                "tool_name",
                "tenant",
                "tokens_total",
            ],
        )
        logger.debug("ClickHouseAccumulator: inserted %d capsule events", len(events))
        return len(events)

    # ------------------------------------------------------------------
    # ingest_capsule_events alias (DuckDBAccumulator interface compatibility)
    # ------------------------------------------------------------------

    def ingest_capsule_events(self, events: list[dict[str, Any]]) -> int:
        """Alias for :meth:`ingest_events` (DuckDB interface compatibility)."""
        return self.ingest_events(events)

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def aggregate_cost_report(self, since_iso: str) -> list[dict[str, Any]]:
        """Return per-model call count and total tokens since a given timestamp.

        Replaces the stub in ``nova cost report`` when ClickHouse is active.

        Args:
            since_iso: ISO-8601 timestamp string (e.g. ``"2024-01-01T00:00:00"``).
                       Rows with ``ts >= since_iso`` are included.

        Returns:
            List of dicts: ``[{"model_id": str, "call_count": int,
            "tokens": int}, ...]`` ordered by ``call_count`` descending.
        """
        self._ensure_schema()
        client = self._get_client()

        query = f"""
            SELECT
                model_id,
                COUNT(*)              AS call_count,
                SUM(tokens_total)     AS tokens
            FROM {self._db}.capsule_events
            WHERE ts >= toDateTime('{since_iso}')
              AND model_id IS NOT NULL
            GROUP BY model_id
            ORDER BY call_count DESC
        """  # noqa: S608
        result = client.query(query)
        rows = []
        for row in result.result_rows:
            rows.append(
                {
                    "model_id": row[0],
                    "call_count": int(row[1]),
                    "tokens": int(row[2] or 0),
                }
            )
        return rows
