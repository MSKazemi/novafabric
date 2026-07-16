"""Service-account resource — /v0/service-accounts (ADR-0178 first slice, experimental).

First-class non-human principals. POST creates the account AND mints an offline
ed25519 token (ADR-0018, ``offline_tokens.issue_token``) bound to subject
``svc:<name>`` — the token is returned exactly once and never stored. Disabling
revokes the token via the existing ``token_audit`` revocation path (spec I4).

Requires ``NOVAFABRIC_OFFLINE_KEY_PATH`` (the existing offline signing key
secret); without it, token minting is refused with a clear 400.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import jwt
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from novafabric.serve import audit
from novafabric.server import workspace_store
from novafabric.server.auth import AuthContext
from novafabric.server.errors import BadRequestError, ConflictError, NotFoundError
from novafabric.server.rbac import Role, require_role

router = APIRouter(prefix="/service-accounts", tags=["service-accounts"])

_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"


class CreateServiceAccountRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=_NAME_PATTERN)
    description: str = Field(default="", max_length=1024)
    expires_in_days: int = Field(default=90, ge=1, le=3650)


def _actor_fp(auth: AuthContext) -> str:
    return auth.subject[:8] if auth.subject else "anonymous"


def _offline_key_path(request: Request) -> Path:
    key_path = request.app.state.config.offline_key_path
    if not key_path:
        raise BadRequestError(
            "service-account tokens require the offline signing key: set "
            "NOVAFABRIC_OFFLINE_KEY_PATH (ADR-0018) before creating service accounts"
        )
    return Path(key_path)


@router.post("", status_code=201)
async def create_service_account(
    body: CreateServiceAccountRequest,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Create a service account and mint its offline token (admin only).

    The token is returned ONCE in this response and is not retrievable later.
    It carries no roles of its own — effective roles come from the account's
    workspace/org memberships or global role assignments (ADR-0178 I3).
    """
    from novafabric.server.offline_tokens import generate_keypair, issue_token

    key_path = _offline_key_path(request)
    if not key_path.exists():
        generate_keypair(key_path)

    try:
        account = workspace_store.create_service_account(
            body.name, body.description, auth.subject
        )
    except workspace_store.SlugConflictError as exc:
        raise ConflictError(str(exc)) from exc

    subject = f"{workspace_store.SERVICE_ACCOUNT_SUBJECT_PREFIX}{body.name}"
    token = issue_token(subject, [], body.expires_in_days, key_path)
    token_id: str = jwt.decode(token, options={"verify_signature": False})["jti"]
    workspace_store.set_service_account_token(account["id"], token_id)
    account["token_jti"] = token_id

    audit.append(
        action="create_service_account",
        args={"name": body.name, "subject": subject, "token_id": token_id},
        cli_equivalent=f"POST /v0/service-accounts {body.name}",
        actor_token_fp=_actor_fp(auth),
        result="ok",
    )
    return {
        "service_account": account,
        "subject": subject,
        "token": token,
        "token_id": token_id,
        "note": "Store this token now — it is shown only once and never persisted.",
    }


@router.get("")
async def list_service_accounts(
    _auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """List service accounts (admin only). Tokens are never included."""
    return {"service_accounts": workspace_store.list_service_accounts()}


@router.post("/{account_id}/disable")
async def disable_service_account(
    account_id: str,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Disable a service account and revoke its outstanding token (admin only).

    Revocation reuses the existing ``token_audit`` path; independently, the
    disabled flag is checked at token verification time (spec I4).
    """
    from novafabric.server.offline_tokens import revoke_token

    account = workspace_store.get_service_account(account_id)
    if account is None:
        raise NotFoundError(f"service account {account_id!r} not found")

    account = workspace_store.disable_service_account(account_id)

    token_revoked = False
    key_path = request.app.state.config.offline_key_path
    if account["token_jti"] and key_path:
        try:
            revoke_token(account["token_jti"], Path(key_path))
            token_revoked = True
        except KeyError:
            token_revoked = False  # no audit row (e.g. audit DB moved) — flag stays authoritative

    audit.append(
        action="disable_service_account",
        args={"account_id": account_id, "token_revoked": token_revoked},
        cli_equivalent=f"POST /v0/service-accounts/{account_id}/disable",
        actor_token_fp=_actor_fp(auth),
        result="ok",
    )
    return {
        "ok": True,
        "id": account_id,
        "disabled": True,
        "token_revoked": token_revoked,
    }
