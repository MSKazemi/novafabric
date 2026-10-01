"""Lineage resource — /v0/lineage.

Implements:
  GET /v0/lineage/nodes          list nodes (keyset pagination, ADR-0206 P2)
  GET /v0/lineage/blast-radius   ?ref=...&kind=...&depth=5
  GET /v0/lineage/provenance     ?ref=...&kind=...&depth=5
  GET /v0/lineage/replay-chain   ?run_id=...
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response

from novafabric.server.auth import AuthContext
from novafabric.server.config import ServerConfig
from novafabric.server.deps import get_config, get_db_path
from novafabric.server.errors import BadRequestError
from novafabric.server.pagination import (
    InvalidCursorError,
    ParsedCursor,
    clamp_limit,
    encode_cursor,
    encode_keyset_cursor,
    parse_cursor,
)
from novafabric.server.rbac import Role, require_role

router = APIRouter(prefix="/lineage", tags=["lineage"])


# ---------- nodes ----------


@router.get("/nodes", response_model=None)
async def list_lineage_nodes(
    response: Response,
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    config: Annotated[ServerConfig, Depends(get_config)] = None,  # type: ignore[assignment]
    _auth: Annotated[AuthContext, Depends(require_role(Role.reader))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """List lineage nodes — keyset (seek) pagination, ADR-0206 P2 (experimental).

    Order is pinned to ``node_id ASC`` (the primary key, so the seek is
    index-satisfiable without a new index). ``next_cursor`` is the shared v1
    keyset cursor (``server/pagination.py``) with ``k = [null, node_id]`` —
    the first slot is unused because the order has one column; a v1 cursor
    whose first slot is non-null (e.g. a capsules cursor) is rejected.
    Legacy ``{"offset": N}`` cursors are served for one deprecation cycle
    (ADR-0188) with a ``Deprecation: true`` header. ``total`` is present on
    the first page and on legacy pages only. A malformed cursor is a 400
    ``invalid_cursor`` (it used to restart silently at offset 0).
    """
    limit = clamp_limit(limit)
    try:
        parsed = parse_cursor(cursor)
    except InvalidCursorError as exc:
        raise BadRequestError(str(exc), code="invalid_cursor")
    if parsed.kind == "keyset" and parsed.key is not None and parsed.key[0] is not None:
        raise BadRequestError(
            "cursor does not belong to this listing (lineage cursors carry a null "
            "first key slot)",
            code="invalid_cursor",
        )
    if parsed.kind == "offset" and not config.pagination.legacy_offset_cursors:
        raise BadRequestError(
            "legacy offset cursors are sunset (ADR-0188); restart the "
            "listing without a cursor to receive keyset cursors",
            code="invalid_cursor",
        )

    from novafabric.registry.store import get_connection, init_schema

    conn = get_connection(db_path)
    init_schema(conn)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "lineage_nodes" not in tables:
            return {"items": [], "next_cursor": None, "total": 0}
        rows, total = _query_nodes(conn, kind=kind, limit=limit, parsed=parsed)
    finally:
        conn.close()

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_node_item(r) for r in rows]

    if parsed.kind == "offset":
        response.headers["Deprecation"] = "true"
        next_cursor = encode_cursor(parsed.offset + limit) if has_more else None
        return {"items": items, "next_cursor": next_cursor, "total": total}

    next_cursor = (
        encode_keyset_cursor(None, str(rows[-1]["node_id"])) if has_more else None
    )
    body: dict[str, Any] = {"items": items, "next_cursor": next_cursor}
    if parsed.kind == "first":
        body["total"] = total
    return body


def _query_nodes(
    conn: Any, *, kind: str | None, limit: int, parsed: ParsedCursor
) -> tuple[list[Any], int | None]:
    """Fetch ``limit + 1`` rows in ``node_id ASC`` order from the cursor position.

    Returns ``(rows, total)``; ``total`` is computed (one ``COUNT``) only for
    the first page and for legacy offset pages, matching the capsules route.
    """
    where: list[str] = []
    params: list[Any] = []
    if kind:
        where.append("kind = ?")
        params.append(kind)
    total: int | None = None
    if parsed.kind in ("first", "offset"):
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM lineage_nodes{clause}",  # noqa: S608
                params,
            ).fetchone()[0]
        )
    seek_params = list(params)
    if parsed.kind == "keyset" and parsed.key is not None:
        where.append("node_id > ?")
        seek_params.append(parsed.key[1])
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    sql = (
        "SELECT node_id, kind, ref, payload FROM lineage_nodes"  # noqa: S608
        f"{clause} ORDER BY node_id ASC LIMIT ?"
    )
    seek_params.append(limit + 1)
    if parsed.kind == "offset":
        sql += " OFFSET ?"
        seek_params.append(parsed.offset)
    return conn.execute(sql, seek_params).fetchall(), total


def _node_item(r: Any) -> dict[str, Any]:
    import json

    payload: Any = {}
    try:
        payload = json.loads(r["payload"] or "{}")
    except (ValueError, TypeError):
        pass
    return {
        "node_id": r["node_id"],
        "kind": r["kind"],
        "ref": r["ref"],
        "payload": payload,
    }


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
