"""Admin API-keys read endpoint for the dashboard console (ADR-0193 slice 2).

Serves a read-only, secret-free projection of the API-key store for the
Layer A/B dashboard admin console. Create/revoke/rotate are deliberately NOT
here — those mutating flows live behind the RBAC-gated ``/v0/api-keys`` server
resource. This router only reads.

Built by a factory so the caller injects its own auth dependency
(ADR-0183 pattern): ``serve`` passes its shared-token ``verify_token`` closure.

Response shape (exact):

    {
      "keys": [
        {
          "key_id": str, "owner": str, "roles": [str],
          "workspace": str|null, "created_at": str,
          "expires_at": str|null, "last_used_at": str|null,
          "revoked_at": str|null, "status": "active"|"revoked"|"expired"
        }
      ],
      "total": int,       # true COUNT(*) of all keys, not len(keys)
      "truncated": bool   # true when total > len(keys) (ADR-0199 bound)
    }

``limit`` (default 500, max 2000) bounds the page at the SQL layer.
Never hashes, never secrets. Empty or missing DB →
``{"keys": [], "total": 0, "truncated": false}`` (never a 500).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query

_ROW_FIELDS = (
    "key_id",
    "owner",
    "roles",
    "workspace",
    "created_at",
    "expires_at",
    "last_used_at",
    "revoked_at",
)


def api_key_rows(
    db_path: Path | None, limit: int | None = None
) -> list[dict[str, Any]]:
    """Secret-free projection of the API-key store (shared read helper).

    Each row carries only the metadata fields in ``_ROW_FIELDS`` plus a
    derived ``status`` (``active``/``revoked``/``expired`` via
    :func:`novafabric.server.api_keys.key_status`). ``limit`` (optional)
    is pushed into the store's SQL read (ADR-0199). Never hashes, never
    secrets. Missing/unreadable store → ``[]``, never an exception.
    """
    if db_path is None or not Path(db_path).exists():
        return []

    from novafabric.server.api_keys import key_status, list_keys  # noqa: PLC0415

    try:
        rows = list_keys(db_path=Path(db_path), limit=limit)
    except Exception:  # noqa: BLE001 — a read view must never 500 the dashboard
        return []

    keys: list[dict[str, Any]] = []
    for row in rows:
        projected = {field: row.get(field) for field in _ROW_FIELDS}
        projected["status"] = key_status(row)
        keys.append(projected)
    return keys


def _count_keys(db_path: Path | None) -> int:
    """Total keys in the store; missing/unreadable store → 0, never a 500."""
    if db_path is None or not Path(db_path).exists():
        return 0

    from novafabric.server.api_keys import count_keys  # noqa: PLC0415

    try:
        return count_keys(db_path=Path(db_path))
    except Exception:  # noqa: BLE001 — a read view must never 500 the dashboard
        return 0


def build_admin_keys_router(
    verify_token: Callable[..., Any],
    *,
    db_path: Path | None,
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(verify_token)], tags=["admin-keys"])

    @router.get("/api/admin/api-keys")
    async def admin_api_keys(
        limit: int = Query(default=500, ge=1, le=2000),
    ) -> dict[str, Any]:
        keys = api_key_rows(db_path, limit=limit)
        total = _count_keys(db_path)
        return {"keys": keys, "total": total, "truncated": total > len(keys)}

    return router
