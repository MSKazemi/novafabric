"""Role-management resource — /v0/admin/roles.

ADR-0060: REST CRUD over role_assignments. Production (OIDC) server mount.

The router is mounted in src/novafabric/server/app.py with the /v0 prefix and
guarded by require_role(Role.admin). The local experimental server
(src/novafabric/serve/app.py) exposes parallel /api/admin/roles endpoints with
verify_token, calling into the same rbac_store helpers below.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from novafabric.serve import audit
from novafabric.server import rbac_store
from novafabric.server.auth import AuthContext
from novafabric.server.rbac import Role, require_role
from novafabric.server.rbac_store import LastAdminError

router = APIRouter(prefix="/admin", tags=["admin"])


class AssignRoleRequest(BaseModel):
    subject: str = Field(..., min_length=1, max_length=256)
    role: Role


def _oidc_configured() -> bool:
    return bool(
        os.environ.get("NOVA_OIDC_ISSUER") or os.environ.get("NOVA_OIDC_CLIENT_ID")
    )


def _list_roles_response() -> dict[str, Any]:
    """Build the GET /admin/roles response.

    Adds an `effective_now` flag per assignment so callers can see the v0.14
    gap documented in ADR-0060: assignments don't yet alter live OIDC auth.
    """
    oidc = _oidc_configured()
    assignments = rbac_store.list_assignments()
    enriched = [
        {**row, "effective_now": not oidc}
        for row in assignments
    ]
    return {
        "server_mode": oidc,
        "roles": enriched,
        "message": (
            ""
            if oidc
            else (
                "Role management requires server mode with OIDC configured"
                " (NOVA_OIDC_ISSUER, NOVA_OIDC_CLIENT_ID) for assignments to affect"
                " live authorization. In local mode the assignments are stored but"
                " do not change the shared-token admin behavior."
            )
        ),
    }


@router.get("/roles")
async def list_roles(
    _auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """List all role assignments (admin only)."""
    return _list_roles_response()


@router.post("/roles", status_code=201)
async def assign_role(
    body: AssignRoleRequest,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Assign a role to a subject (idempotent). Admin only."""
    rbac_store.assign_role(body.subject, body.role.value, auth.subject)
    actor_fp = auth.subject[:8] if auth.subject else "anonymous"
    audit.append(
        action="assign_role",
        args={"subject": body.subject, "role": body.role.value},
        cli_equivalent=f"nova server assign-role {body.subject} {body.role.value}",
        actor_token_fp=actor_fp,
        result="ok",
    )
    return {
        "ok": True,
        "subject": body.subject,
        "role": body.role.value,
        "assigned_by": auth.subject,
    }


@router.delete("/roles/{subject}/{role}")
async def revoke_role(
    subject: str,
    role: str,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Revoke a role from a subject.

    Returns 404 if the assignment did not exist.
    Returns 409 if revoking would leave the system with no admin path (lockout guard).
    """
    try:
        deleted = rbac_store.revoke_role(subject, role)
    except LastAdminError as e:
        actor_fp = auth.subject[:8] if auth.subject else "anonymous"
        audit.append(
            action="revoke_role",
            args={"subject": subject, "role": role},
            cli_equivalent=f"nova server revoke-role {subject} {role}",
            actor_token_fp=actor_fp,
            result="error",
            error=str(e),
        )
        raise HTTPException(status_code=409, detail=str(e)) from e

    actor_fp = auth.subject[:8] if auth.subject else "anonymous"
    if not deleted:
        audit.append(
            action="revoke_role",
            args={"subject": subject, "role": role},
            cli_equivalent=f"nova server revoke-role {subject} {role}",
            actor_token_fp=actor_fp,
            result="error",
            error="not found",
        )
        raise HTTPException(
            status_code=404, detail=f"no assignment of role {role!r} to {subject!r}"
        )

    audit.append(
        action="revoke_role",
        args={"subject": subject, "role": role},
        cli_equivalent=f"nova server revoke-role {subject} {role}",
        actor_token_fp=actor_fp,
        result="ok",
    )
    return {"ok": True, "subject": subject, "role": role}
