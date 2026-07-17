"""API-key resource — /v0/api-keys (ADR-0193 Track 1 slice 2, experimental).

Role-gated CRUD over first-class API keys, mirroring the service-accounts
resource. All mutating routes require the ``admin`` role (``require_role``);
the store (``server/api_keys.py``) holds hash-only secrets and the
hash-chained audit trail (ADR-0193 D5).

- ``POST   /v0/api-keys``               — create (returns the key ONCE)
- ``GET    /v0/api-keys``               — list metadata (never secrets/hashes)
- ``DELETE /v0/api-keys/{key_id}``      — revoke (immediate)
- ``POST   /v0/api-keys/{key_id}/rotate`` — rotate (returns successor ONCE)

Errors use the standard ErrorEnvelope via the shared named exceptions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from novafabric.server import api_keys
from novafabric.server.auth import AuthContext
from novafabric.server.errors import BadRequestError, NotFoundError
from novafabric.server.rbac import Role, require_role

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


class CreateApiKeyRequest(BaseModel):
    owner: str = Field(..., min_length=1, max_length=256)
    roles: list[str] = Field(default_factory=lambda: ["reader"], min_length=1)
    workspace: str | None = Field(default=None, max_length=256)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


def _db_path(request: Request) -> Path | None:
    """Resolve the server's configured registry DB path (None → registry default)."""
    configured = getattr(request.app.state.config, "db_path", None)
    return Path(configured) if configured else None


def _with_status(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row["status"] = api_keys.key_status(row)
    return row


@router.post("", status_code=201)
async def create_api_key(
    body: CreateApiKeyRequest,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Create an API key (admin only). The key is returned ONCE and never stored."""
    try:
        key, record = api_keys.create_key(
            body.owner,
            body.roles,
            actor=auth.subject,
            workspace=body.workspace,
            expires_in_days=body.expires_in_days,
            db_path=_db_path(request),
        )
    except api_keys.InvalidRoleError as exc:
        raise BadRequestError(str(exc)) from exc

    return {
        "key": key,
        "api_key": record,
        "note": "Store this key now — it is shown only once and never persisted.",
    }


@router.get("")
async def list_api_keys(
    request: Request,
    _auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """List API keys (admin only) — metadata only, never secrets or hashes."""
    rows = [_with_status(r) for r in api_keys.list_keys(db_path=_db_path(request))]
    return {"api_keys": rows, "total": len(rows)}


@router.delete("/{key_id}")
async def revoke_api_key(
    key_id: str,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Revoke an API key by key_id (admin only) — effective on the next request."""
    try:
        api_keys.revoke_key(key_id, actor=auth.subject, db_path=_db_path(request))
    except api_keys.UnknownKeyError as exc:
        raise NotFoundError(f"API key {key_id!r} not found") from exc
    return {"ok": True, "key_id": key_id, "revoked": True}


@router.post("/{key_id}/rotate", status_code=201)
async def rotate_api_key(
    key_id: str,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Rotate an API key (admin only). The successor is returned ONCE.

    Both keys stay valid for the bounded overlap window; the predecessor
    auto-revokes at verify time once the window elapses (ADR-0193 D3).
    """
    try:
        key, record = api_keys.rotate_key(
            key_id, actor=auth.subject, db_path=_db_path(request)
        )
    except api_keys.UnknownKeyError as exc:
        raise NotFoundError(f"API key {key_id!r} not found") from exc
    except api_keys.RevokedKeyError as exc:
        raise BadRequestError(str(exc)) from exc

    return {
        "key": key,
        "api_key": record,
        "overlap_seconds": record["rotate_overlap_seconds"],
        "rotate_expires_at": record["rotate_expires_at"],
        "note": "Store this successor key now — it is shown only once.",
    }
