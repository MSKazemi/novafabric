"""Cluster Collector FastAPI application for Evidence Fabric scale tier.

Provides a minimal HTTP ingestion endpoint (``POST /capsule``) that accepts
capsule events and routes them to the configured accumulator backend.

Local mode (default): :class:`DuckDBAccumulator`
Scale mode: :class:`ClickHouseAccumulator` (activated when ``NOVA_CLICKHOUSE_URL``
is set in the environment)

cap-002 / ADR-0066 (Evidence Fabric v1.0)
"""

from novafabric.collector_app.app import create_collector_app

__all__ = ["create_collector_app"]
