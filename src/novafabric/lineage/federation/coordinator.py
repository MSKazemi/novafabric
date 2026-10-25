"""Stateless federation coordinator — fan-out across shard sites (C-4)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from typing import Any, AsyncIterator, Callable, Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field

from novafabric.lineage.federation.summary import (
    SiteSummaryEntry,
    SummaryCache,
    build_summary_router,
)

log = logging.getLogger(__name__)

_SHARD_TIMEOUT = 5.0  # seconds per site

#: Upper bound on the configured site list and on a request's subset of it.
MAX_SITES = 64


class FederationRequest(BaseModel):
    query_type: Literal["provenance", "blast_radius", "replay_chain"]
    root_run_id: str
    max_depth: int = 5
    #: Optional subset of the CONFIGURED sites to query (default: all of them). A URL
    #: the operator did not configure is refused: the request never names a host the
    #: coordinator will POST to (review replay-lineage-registry-19).
    sites: list[str] | None = Field(default=None, max_length=MAX_SITES)


class FederationResponse(BaseModel):
    rows: list[dict[str, Any]]
    partial: bool = False
    site_count: int
    dedup_count: int


def _normalise_site(url: str) -> str:
    return url.rstrip("/")


def _validated_sites(sites: Sequence[str]) -> list[str]:
    if len(sites) > MAX_SITES:
        raise ValueError(
            f"at most {MAX_SITES} federation sites may be configured, got {len(sites)}"
        )
    out: list[str] = []
    for url in sites:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"federation site must be an absolute http(s) URL, got {url!r}")
        normalised = _normalise_site(url)
        if normalised not in out:
            out.append(normalised)
    return out


def create_coordinator_app(
    site_id: str,
    summary_fetch_fn: Callable[[], list[SiteSummaryEntry]] | None = None,
    summary_refresh_interval: float = 60.0,
    sites: Sequence[str] = (),
) -> FastAPI:
    """Return a FastAPI app hosting the federation coordinator.

    ``sites`` are the shard sites the coordinator may fan out to: operator
    configuration, never request input. A request may only narrow them.
    """
    configured = _validated_sites(sites)
    fetch_fn = summary_fetch_fn or (lambda: [])
    cache = SummaryCache(fetch_fn=fetch_fn, refresh_interval=summary_refresh_interval)

    @contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        cache.start()
        try:
            yield
        finally:
            cache.stop()

    app = FastAPI(title="NovaFabric Federation Coordinator", lifespan=_lifespan)
    app.include_router(_build_coordinator_router(site_id=site_id, sites=configured))
    app.include_router(build_summary_router(cache))
    return app


def _build_coordinator_router(site_id: str, sites: list[str]) -> APIRouter:
    router = APIRouter()

    @router.post("/federation/query", response_model=FederationResponse)
    async def federation_query(req: FederationRequest) -> FederationResponse:
        """Fan out to the configured shard sites and return merged, deduplicated NodeRows."""
        if req.sites is None:
            targets = list(sites)
        else:
            targets = [_normalise_site(u) for u in req.sites]
            unknown = [u for u in targets if u not in sites]
            if unknown:
                raise HTTPException(
                    status_code=422,
                    detail=f"not configured federation site(s): {sorted(set(unknown))}",
                )
        all_rows, partial = await _fan_out(req, targets)
        pre_dedup = len(all_rows)
        deduped = _dedup_rows(all_rows)
        return FederationResponse(
            rows=deduped,
            partial=partial,
            site_count=len(targets),
            dedup_count=pre_dedup - len(deduped),
        )

    return router


async def _fan_out(
    req: FederationRequest,
    targets: list[str],
) -> tuple[list[dict[str, Any]], bool]:
    """POST to each shard and collect NodeRow lists; mark partial on any failure."""
    shard_payload: dict[str, Any] = {
        "query_type": req.query_type,
        "root_run_id": req.root_run_id,
        "max_depth": req.max_depth,
        "site_id": "coordinator",
    }

    async def _query_one(
        site_url: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        try:
            url = f"{site_url.rstrip('/')}/lineage/shard-local-query"
            async with httpx.AsyncClient(timeout=_SHARD_TIMEOUT) as client:
                _trace_site(site_url)
                resp = await client.post(url, json=shard_payload)
                if resp.status_code >= 500:
                    log.warning("Site %s returned %d", site_url, resp.status_code)
                    return [], True
                data: dict[str, Any] = resp.json()
                rows: list[dict[str, Any]] = data.get("rows", [])
                site_partial: bool = data.get("partial", False)
                return rows, site_partial
        except Exception as exc:  # noqa: BLE001
            log.warning("Site %s unreachable or returned error: %s", site_url, exc)
            return [], True

    results = await asyncio.gather(*[_query_one(s) for s in targets])

    all_rows: list[dict[str, Any]] = []
    partial = False
    for rows, site_partial in results:
        all_rows.extend(rows)
        if site_partial:
            partial = True

    return all_rows, partial


def _dedup_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedup rows on node_id; first occurrence wins."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = str(row.get("node_id", ""))
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def _trace_site(site_url: str) -> None:
    """Add an OTel span for the site query if opentelemetry is available."""
    try:
        from opentelemetry import trace  # noqa: PLC0415

        tracer = trace.get_tracer("novafabric.lineage.federation")
        span = tracer.start_span(f"federation.query_site:{site_url}")
        span.end()
    except Exception:  # noqa: BLE001
        pass
