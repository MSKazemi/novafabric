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
from novafabric.server.step_up import require_step_up

router = APIRouter(prefix="/admin", tags=["admin"])


def _chained_audit(event_value: str, actor: str, subject: str, details: "dict[str, Any]") -> None:
    """ADR-0246 §12.1 fix: the most privileged mutation joins the HASH-CHAINED
    audit log (the dashboard append-only log alone is not tamper-evident).
    Best-effort like every audit writer here — an audit IO failure must not
    turn a completed grant into a 500, but it is logged, never swallowed."""
    import logging

    from novafabric.audit import AuditEventType, AuditLog
    from novafabric.audit._paths import AUDIT_LOG_PATH

    try:
        AuditLog(AUDIT_LOG_PATH).append(
            event_type=AuditEventType(event_value),
            actor=actor,
            resource_id=subject,
            details=details,
        )
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("chained audit append failed")


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


@router.post(
    "/roles", status_code=201, dependencies=[Depends(require_step_up("role.assign"))]
)
async def assign_role(
    body: AssignRoleRequest,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Assign a role to a subject (idempotent). Admin only."""
    rbac_store.assign_role(body.subject, body.role.value, auth.subject)
    actor_fp = auth.subject[:8] if auth.subject else "anonymous"
    _chained_audit(
        "role.assign",
        actor=auth.subject or "anonymous",
        subject=body.subject,
        details={"role": body.role.value},
    )
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


@router.delete(
    "/roles/{subject}/{role}",
    dependencies=[Depends(require_step_up("role.revoke"))],
)
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

    _chained_audit(
        "role.revoke",
        actor=auth.subject or "anonymous",
        subject=subject,
        details={"role": role},
    )
    audit.append(
        action="revoke_role",
        args={"subject": subject, "role": role},
        cli_equivalent=f"nova server revoke-role {subject} {role}",
        actor_token_fp=actor_fp,
        result="ok",
    )
    return {"ok": True, "subject": subject, "role": role}
