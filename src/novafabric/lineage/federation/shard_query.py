"""Shard-local query router — FR-10 sovereignty enforcement point (C-2)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel

from novafabric.lineage.store import AbstractLineageStore


class ShardLocalRequest(BaseModel):
    query_type: Literal["provenance", "blast_radius", "replay_chain"]
    root_run_id: str
    max_depth: int = 5
    site_id: str


class ShardLocalResponse(BaseModel):
    site_id: str
    rows: list[dict[str, Any]]
    partial: bool = False


def create_shard_app(store: AbstractLineageStore, site_id: str) -> FastAPI:
    """Return a FastAPI app wired to the given store and site_id."""
    app = FastAPI(title="NovaFabric Shard-Local Query")
    router = _build_router(store=store, site_id=site_id)
    app.include_router(router)
    return app


def _build_router(store: AbstractLineageStore, site_id: str) -> APIRouter:
    router = APIRouter()

    # Capture store/site_id in closure so endpoint does not need Depends injection.
    _store = store
    _site_id = site_id

    @router.post("/lineage/shard-local-query", response_model=ShardLocalResponse)
    def shard_local_query(req: ShardLocalRequest) -> ShardLocalResponse:
        """Execute a lineage query locally and return NodeRow dicts only."""
        partial = False
        rows: list[dict[str, Any]] = []
        try:
            if req.query_type == "provenance":
                raw = _store.provenance(run_id=req.root_run_id, depth=req.max_depth)
            elif req.query_type == "blast_radius":
                raw = _store.blast_radius(run_id=req.root_run_id, max_depth=req.max_depth)
            else:
                raw = _store.replay_chain(run_id=req.root_run_id)
            rows = [_to_node_row(r) for r in raw]
        except Exception:  # noqa: BLE001
            partial = True

        return ShardLocalResponse(site_id=_site_id, rows=rows, partial=partial)

    return router


def _to_node_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Project a raw store result to a NodeRow dict (node_id, kind, ref only)."""
    return {
        "node_id": str(raw.get("node_id", "")),
        "kind": str(raw.get("kind", "")),
        "ref": str(raw.get("ref", "")),
    }
