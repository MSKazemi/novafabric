"""Federation summary endpoint — periodic in-memory refresh (C-5)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Literal

from fastapi import APIRouter
from pydantic import BaseModel

log = logging.getLogger(__name__)


class SiteSummaryEntry(BaseModel):
    source_site_id: str
    target_site_id: str
    edge_type: str
    count: int


class FederationSummaryResponse(BaseModel):
    entries: list[SiteSummaryEntry]
    source: Literal["summary"] = "summary"
    refreshed_at: str


class SummaryCache:
    """In-memory cache of SiteSummaryEntry records with periodic refresh."""

    def __init__(
        self,
        fetch_fn: Callable[[], list[SiteSummaryEntry]],
        refresh_interval: float = 60.0,
    ) -> None:
        self._fetch_fn = fetch_fn
        self._refresh_interval = refresh_interval
        self._entries: list[SiteSummaryEntry] = []
        self._refreshed_at: str = _now_iso()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the background refresh task in the running event loop."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_event_loop().create_task(self._refresh_loop())

    def stop(self) -> None:
        """Cancel the background refresh task."""
        if self._task and not self._task.done():
            self._task.cancel()

    def snapshot(self) -> FederationSummaryResponse:
        """Return the latest cached summary."""
        return FederationSummaryResponse(
            entries=list(self._entries),
            refreshed_at=self._refreshed_at,
        )

    def _refresh_now(self) -> None:
        try:
            self._entries = self._fetch_fn()
            self._refreshed_at = _now_iso()
        except Exception as exc:  # noqa: BLE001
            log.warning("Summary refresh failed: %s", exc)

    async def _refresh_loop(self) -> None:
        self._refresh_now()
        while True:
            await asyncio.sleep(self._refresh_interval)
            self._refresh_now()


def build_summary_router(cache: SummaryCache) -> APIRouter:
    """Return an APIRouter with GET /federation/summary wired to *cache*."""
    router = APIRouter()

    @router.get("/federation/summary", response_model=FederationSummaryResponse)
    def get_summary() -> FederationSummaryResponse:
        """Return the latest cached cross-site edge-type summary."""
        return cache.snapshot()

    return router


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
