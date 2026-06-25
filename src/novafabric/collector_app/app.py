"""FastAPI cluster collector app for Evidence Fabric (cap-002, ADR-0066).

Provides:
  POST /capsule  — ingest capsule events via the accumulator backend
  GET  /health   — liveness probe

Auth
----
If the ``NOVA_COLLECTOR_TOKEN`` environment variable is set, every request
must include the header ``X-Nova-Token: <value>``.  If the env var is unset,
auth is skipped (development / local mode).

Accumulator selection
---------------------
If ``NOVA_CLICKHOUSE_URL`` is set, a :class:`ClickHouseAccumulator` is used.
Otherwise a :class:`DuckDBAccumulator` backed by ``:memory:`` is used.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic request model
# ---------------------------------------------------------------------------


class CapsuleIngestionRequest(BaseModel):
    """Request body for ``POST /capsule``."""

    capsule_id: str
    tenant_id: str
    events: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


def _make_auth_dependency(token: str | None) -> Callable[[Request], Awaitable[None]]:
    """Return a FastAPI dependency that checks X-Nova-Token if token is set."""

    async def _check_token(request: Request) -> None:
        if token is None:
            return  # auth disabled
        provided = request.headers.get("X-Nova-Token")
        if provided != token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing X-Nova-Token",
            )

    return _check_token


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _default_accumulator() -> Any:
    """Create the default accumulator based on environment."""
    clickhouse_url = os.environ.get("NOVA_CLICKHOUSE_URL")
    if clickhouse_url:
        try:
            from novafabric.evidence_fabric.clickhouse_accumulator import (
                ClickHouseAccumulator,
            )

            logger.info(
                "collector_app: using ClickHouseAccumulator (%s)", clickhouse_url
            )
            return ClickHouseAccumulator()
        except ImportError:
            logger.warning(
                "collector_app: NOVA_CLICKHOUSE_URL is set but clickhouse-connect "
                "is not installed; falling back to DuckDBAccumulator"
            )

    from pathlib import Path

    from novafabric.evidence_fabric.duckdb_accumulator import DuckDBAccumulator

    logger.info("collector_app: using DuckDBAccumulator (:memory:)")
    return DuckDBAccumulator(Path(":memory:"))


def create_collector_app(accumulator: Any = None) -> FastAPI:
    """Create and return the cluster collector FastAPI application.

    Args:
        accumulator: An accumulator instance with ``ingest_events`` /
                     ``ingest_capsule_events`` methods.  If ``None``, the
                     default accumulator is selected based on environment.

    Returns:
        A configured :class:`fastapi.FastAPI` instance.
    """
    if accumulator is None:
        accumulator = _default_accumulator()

    # Resolve the method name: prefer ingest_events (ClickHouse); fall back
    # to ingest_capsule_events (DuckDB).
    if hasattr(accumulator, "ingest_events"):
        _ingest = accumulator.ingest_events
    else:
        _ingest = accumulator.ingest_capsule_events

    collector_token: str | None = os.environ.get("NOVA_COLLECTOR_TOKEN") or None
    auth_dep = _make_auth_dependency(collector_token)

    app = FastAPI(
        title="NovaFabric Cluster Collector",
        description=(
            "Evidence Fabric ingestion endpoint (cap-002, ADR-0066). "
            "Accepts capsule events and routes them to the configured accumulator."
        ),
        version="1.0.0",
    )

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "ok"}

    @app.post("/capsule", dependencies=[Depends(auth_dep)])
    async def ingest_capsule(body: CapsuleIngestionRequest) -> dict[str, Any]:
        """Ingest a batch of capsule events.

        Each event dict is enriched with ``capsule_id``, ``tenant_id`` from
        the request body before being forwarded to the accumulator.

        Returns:
            ``{"ok": True, "ingested": N}``
        """
        enriched = []
        for ev in body.events:
            enriched_ev = dict(ev)
            enriched_ev.setdefault("run_id", body.capsule_id)
            enriched_ev.setdefault("tenant", body.tenant_id)
            enriched.append(enriched_ev)

        count = _ingest(enriched)
        return {"ok": True, "ingested": count}

    return app
