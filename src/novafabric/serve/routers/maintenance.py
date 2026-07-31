"""Maintenance safe-mutation routes (ADR-0201 P8, ADR-0183 router pattern).

Confirm-gated, **idempotent, lossless** recompute actions for the dashboard's
safe-mutations surface. Lands as an APIRouter module (not an inline serve/app
route) per the ADR-0183 route freeze.

- ``POST /api/admin/reindex-runs`` — rebuild the ``runs_cache`` index from the
  capsule filesystem. The cache is always regenerable from capsules, so this is
  a full INSERT-OR-REPLACE rebuild that never deletes a capsule or a row that
  still has a capsule; re-running is a no-op on the data.
  Optional ``prune: true`` additionally drops rows whose capsule directory no
  longer exists — dangling index entries the additive rebuild cannot clear,
  which otherwise list in the dashboard and 404 on every drill-in. A capsule
  is never touched; only the derived index row is removed.

Topology re-seed and KG rebuild (the other two P8-P10 actions) already have
their own idempotent endpoints; only the runs reindex was missing one.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException


def build_maintenance_router(
    verify_token: Callable[..., Any],
    *,
    capsule_dir: Path,
    db_path: Path | None,
) -> APIRouter:
    """Build the maintenance safe-mutation router."""
    router = APIRouter(dependencies=[Depends(verify_token)], tags=["maintenance"])

    @router.post("/api/admin/reindex-runs")
    async def reindex_runs(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        if not body.get("confirmed"):
            raise HTTPException(status_code=400, detail="reindex requires confirmed=true")
        if db_path is None:
            return {"ok": False, "error": "no database configured"}
        from novafabric.registry.runs_cache import (
            build_runs_index,
            count_cached_runs,
            ensure_runs_cache,
            prune_orphaned_runs,
        )
        from novafabric.registry.store import get_connection, init_schema

        # Opt-in orphan pruning: a rebuild is additive, so rows whose capsule
        # directory has disappeared survive forever and 404 on every drill-in.
        # Off by default — deleting index rows is the caller's decision.
        prune = bool(body.get("prune"))

        conn = get_connection(db_path)
        try:
            init_schema(conn)
            ensure_runs_cache(conn)
            reindexed = build_runs_index(capsule_dir, conn, incremental=False)
            pruned = prune_orphaned_runs(conn) if prune else 0
            total = count_cached_runs(conn)
        finally:
            conn.close()
        return {"ok": True, "reindexed": reindexed, "pruned": pruned, "total": total}

    return router
