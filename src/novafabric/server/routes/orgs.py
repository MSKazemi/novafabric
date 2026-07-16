"""Organization resource — /v0/orgs (ADR-0178 first slice, experimental).

CRUD-lite over the ``organizations`` registry table: POST, GET list, GET one,
DELETE (only when empty). Admin-gated like routes/roles.py. Additive — no
existing route changes; the Postgres metadata-store / RLS layer is untouched
(spec I1/I7).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from novafabric.serve import audit
from novafabric.server import workspace_store
from novafabric.server.auth import AuthContext
from novafabric.server.errors import ConflictError, NotFoundError
from novafabric.server.rbac import Role, require_role

router = APIRouter(prefix="/orgs", tags=["orgs"])

_SLUG_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"


class CreateOrgRequest(BaseModel):
    slug: str = Field(..., min_length=1, max_length=64, pattern=_SLUG_PATTERN)
    name: str = Field(..., min_length=1, max_length=256)


def _actor_fp(auth: AuthContext) -> str:
    return auth.subject[:8] if auth.subject else "anonymous"


@router.post("", status_code=201)
async def create_org(
    body: CreateOrgRequest,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Create an organization (admin only)."""
    try:
        org = workspace_store.create_org(body.slug, body.name, auth.subject)
    except workspace_store.SlugConflictError as exc:
        raise ConflictError(str(exc)) from exc
    audit.append(
        action="create_org",
        args={"slug": body.slug, "name": body.name},
        cli_equivalent=f"POST /v0/orgs {body.slug}",
        actor_token_fp=_actor_fp(auth),
        result="ok",
    )
    return {"org": org}


@router.get("")
async def list_orgs(
    _auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """List all organizations (admin only)."""
    return {"orgs": workspace_store.list_orgs()}


@router.get("/{org_id}")
async def get_org(
    org_id: str,
    _auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Fetch one organization (admin only)."""
    org = workspace_store.get_org(org_id)
    if org is None:
        raise NotFoundError(f"organization {org_id!r} not found")
    return {"org": org}


@router.delete("/{org_id}")
async def delete_org(
    org_id: str,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Delete an empty organization (admin only).

    404 if unknown; 409 if it still contains workspaces or memberships.
    """
    try:
        workspace_store.delete_org(org_id)
    except workspace_store.UnknownScopeError as exc:
        raise NotFoundError(str(exc.args[0])) from exc
    except workspace_store.ScopeNotEmptyError as exc:
        raise ConflictError(str(exc)) from exc
    audit.append(
        action="delete_org",
        args={"org_id": org_id},
        cli_equivalent=f"DELETE /v0/orgs/{org_id}",
        actor_token_fp=_actor_fp(auth),
        result="ok",
    )
    return {"ok": True, "id": org_id}
