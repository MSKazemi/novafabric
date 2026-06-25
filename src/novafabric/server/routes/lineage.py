"""Lineage resource — /v0/lineage.

Implements:
  GET /v0/lineage/nodes          list nodes (cursor pagination)
  GET /v0/lineage/blast-radius   ?ref=...&kind=...&depth=5
  GET /v0/lineage/provenance     ?ref=...&kind=...&depth=5
  GET /v0/lineage/replay-chain   ?run_id=...
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from novafabric.server.auth import AuthContext
from novafabric.server.deps import get_db_path
from novafabric.server.errors import BadRequestError
from novafabric.server.pagination import clamp_limit, decode_cursor, paginate
from novafabric.server.rbac import Role, require_role

router = APIRouter(prefix="/lineage", tags=["lineage"])


# ---------- nodes ----------


@router.get("/nodes", response_model=None)
async def list_lineage_nodes(
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    _auth: Annotated[AuthContext, Depends(require_role(Role.reader))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    from novafabric.registry.store import get_connection, init_schema

    conn = get_connection(db_path)
    init_schema(conn)
    try:
        # Check lineage tables exist
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "lineage_nodes" not in tables:
            return {"items": [], "next_cursor": None, "total": 0}

        if kind:
            rows = conn.execute(
                "SELECT node_id, kind, ref, payload FROM lineage_nodes WHERE kind = ?",
                (kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT node_id, kind, ref, payload FROM lineage_nodes"
            ).fetchall()
    finally:
        conn.close()

    import json

    nodes = []
    for r in rows:
        payload: Any = {}
        try:
            payload = json.loads(r["payload"] or "{}")
        except (ValueError, TypeError):
            pass
        nodes.append(
            {
                "node_id": r["node_id"],
                "kind": r["kind"],
                "ref": r["ref"],
                "payload": payload,
            }
        )

    offset = decode_cursor(cursor)
    limit = clamp_limit(limit)
    page, next_cursor = paginate(nodes, limit, offset)
    return {"items": page, "next_cursor": next_cursor, "total": len(nodes)}


# ---------- blast-radius ----------


@router.get("/blast-radius", response_model=None)
async def blast_radius(
    ref: str = Query(...),
    kind: str | None = Query(default=None),
    depth: int = Query(default=5, ge=1, le=20),
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    _auth: Annotated[AuthContext, Depends(require_role(Role.reader))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    if not ref:
        raise BadRequestError("ref is required")
    from novafabric.lineage._store import LineageStore

    store = LineageStore(db_path=db_path)
    results = store.blast_radius(ref, kind, depth)
    nodes = _to_nodes(results)
    return {"ref": ref, "kind": kind, "depth": depth, "nodes": nodes}


# ---------- provenance ----------


@router.get("/provenance", response_model=None)
async def provenance(
    ref: str = Query(...),
    kind: str | None = Query(default=None),
    depth: int = Query(default=5, ge=1, le=20),
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    _auth: Annotated[AuthContext, Depends(require_role(Role.reader))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    if not ref:
        raise BadRequestError("ref is required")
    from novafabric.lineage._store import LineageStore

    store = LineageStore(db_path=db_path)
    results = store.provenance(ref, kind, depth)
    nodes = _to_nodes(results)
    return {"ref": ref, "kind": kind, "depth": depth, "nodes": nodes}


# ---------- replay-chain ----------


@router.get("/replay-chain", response_model=None)
async def replay_chain(
    run_id: str = Query(...),
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    _auth: Annotated[AuthContext, Depends(require_role(Role.reader))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    if not run_id:
        raise BadRequestError("run_id is required")
    from novafabric.lineage._store import LineageStore

    store = LineageStore(db_path=db_path)
    results = store.replay_chain(run_id)
    nodes = _to_nodes(results)
    return {"run_id": run_id, "chain": nodes}


# ---------- helpers ----------


def _to_nodes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import json

    nodes = []
    for r in rows:
        payload: Any = {}
        raw = r.get("payload")
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                pass
        elif isinstance(raw, dict):
            payload = raw
        nodes.append(
            {
                "node_id": r.get("node_id"),
                "kind": r.get("kind"),
                "ref": r.get("ref"),
                "payload": payload,
            }
        )
    return nodes
