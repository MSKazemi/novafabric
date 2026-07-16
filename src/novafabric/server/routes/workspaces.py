"""Workspace resource — /v0/workspaces (ADR-0178 first slice, experimental).

CRUD-lite over the ``workspaces`` registry table plus workspace-scoped
membership grant/revoke. Admin-gated like routes/roles.py. Workspace access
control is application-enforced scoping only — ``tenant_id`` remains the sole
database-enforced (RLS) isolation key (spec I1/I2).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from novafabric.serve import audit
from novafabric.server import workspace_store
from novafabric.server.auth import AuthContext
from novafabric.server.errors import ConflictError, NotFoundError
from novafabric.server.rbac import Role, require_role

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

_SLUG_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"


class CreateWorkspaceRequest(BaseModel):
    org_id: str = Field(..., min_length=1, max_length=64)
    slug: str = Field(..., min_length=1, max_length=64, pattern=_SLUG_PATTERN)
    name: str = Field(..., min_length=1, max_length=256)


class MembershipRequest(BaseModel):
    principal: str = Field(..., min_length=1, max_length=256)
    role: str = Field(..., min_length=1, max_length=32)

    @field_validator("role")
    @classmethod
    def _role_in_vocabulary(cls, value: str) -> str:
        if value not in workspace_store.VALID_ROLES:
            raise ValueError(
                f"role {value!r} is not one of the six roles: "
                f"{sorted(workspace_store.VALID_ROLES)}"
            )
        return value


def _actor_fp(auth: AuthContext) -> str:
    return auth.subject[:8] if auth.subject else "anonymous"


@router.post("", status_code=201)
async def create_workspace(
    body: CreateWorkspaceRequest,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Create a workspace in an organization (admin only)."""
    try:
        ws = workspace_store.create_workspace(
            body.org_id, body.slug, body.name, auth.subject
        )
    except workspace_store.UnknownScopeError as exc:
        raise NotFoundError(str(exc.args[0])) from exc
    except workspace_store.SlugConflictError as exc:
        raise ConflictError(str(exc)) from exc
    audit.append(
        action="create_workspace",
        args={"org_id": body.org_id, "slug": body.slug},
        cli_equivalent=f"POST /v0/workspaces {body.slug}",
        actor_token_fp=_actor_fp(auth),
        result="ok",
    )
    return {"workspace": ws}


@router.get("")
async def list_workspaces(
    _auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
    org_id: str | None = None,
) -> dict[str, Any]:
    """List workspaces, optionally filtered by org (admin only)."""
    return {"workspaces": workspace_store.list_workspaces(org_id=org_id)}


@router.get("/{ws_id}")
async def get_workspace(
    ws_id: str,
    _auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Fetch one workspace with its memberships (admin only)."""
    ws = workspace_store.get_workspace(ws_id)
    if ws is None:
        raise NotFoundError(f"workspace {ws_id!r} not found")
    memberships = workspace_store.list_memberships(
        scope_type="workspace", scope_id=ws_id
    )
    return {"workspace": ws, "memberships": memberships}


@router.delete("/{ws_id}")
async def delete_workspace(
    ws_id: str,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Delete an empty workspace (admin only). 409 if it has memberships."""
    try:
        workspace_store.delete_workspace(ws_id)
    except workspace_store.UnknownScopeError as exc:
        raise NotFoundError(str(exc.args[0])) from exc
    except workspace_store.ScopeNotEmptyError as exc:
        raise ConflictError(str(exc)) from exc
    audit.append(
        action="delete_workspace",
        args={"ws_id": ws_id},
        cli_equivalent=f"DELETE /v0/workspaces/{ws_id}",
        actor_token_fp=_actor_fp(auth),
        result="ok",
    )
    return {"ok": True, "id": ws_id}


@router.post("/{ws_id}/memberships", status_code=201)
async def grant_membership(
    ws_id: str,
    body: MembershipRequest,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Grant a workspace-scoped role binding (admin only). Idempotent."""
    try:
        added = workspace_store.add_membership(
            body.principal, "workspace", ws_id, body.role, auth.subject
        )
    except workspace_store.UnknownScopeError as exc:
        raise NotFoundError(str(exc.args[0])) from exc
    audit.append(
        action="grant_membership",
        args={"ws_id": ws_id, "principal": body.principal, "role": body.role},
        cli_equivalent=f"POST /v0/workspaces/{ws_id}/memberships",
        actor_token_fp=_actor_fp(auth),
        result="ok",
    )
    return {
        "ok": True,
        "principal": body.principal,
        "scope_type": "workspace",
        "scope_id": ws_id,
        "role": body.role,
        "created": added,
    }


@router.delete("/{ws_id}/memberships/{principal}/{role}")
async def revoke_membership(
    ws_id: str,
    principal: str,
    role: str,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Revoke a workspace-scoped role binding (admin only). 404 if absent."""
    removed = workspace_store.remove_membership(principal, "workspace", ws_id, role)
    if not removed:
        raise NotFoundError(
            f"no {role!r} membership for {principal!r} in workspace {ws_id!r}"
        )
    audit.append(
        action="revoke_membership",
        args={"ws_id": ws_id, "principal": principal, "role": role},
        cli_equivalent=f"DELETE /v0/workspaces/{ws_id}/memberships/{principal}/{role}",
        actor_token_fp=_actor_fp(auth),
        result="ok",
    )
    return {"ok": True, "principal": principal, "scope_id": ws_id, "role": role}
