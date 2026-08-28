"""Webhook subscription resource — /v0/webhooks (ADR-0205 P1, experimental).

Role-gated CRUD + test ping + delivery log + redeliver, mirroring the
ADR-0193 ``routes/api_keys.py`` shape. RBAC per the spec matrix
(``the private design/spec/webhook-registry-v0.md``): mutations are admin-only; list/get
and the delivery log are admin **or** auditor; the signing secret is returned
exactly once by create and by nothing else.

- ``POST   /v0/webhooks``                         — create (secret shown ONCE)
- ``GET    /v0/webhooks``                         — list (never secrets)
- ``GET    /v0/webhooks/{hook_id}``               — get one
- ``PATCH  /v0/webhooks/{hook_id}``               — update (secret NOT updatable)
- ``DELETE /v0/webhooks/{hook_id}``               — delete
- ``POST   /v0/webhooks/{hook_id}/ping``          — synthetic webhook.ping (202)
- ``GET    /v0/webhooks/{hook_id}/deliveries``    — cursor-paginated log
- ``POST   /v0/webhooks/{hook_id}/deliveries/{delivery_id}/redeliver`` — 202

Ping and redeliver ride the in-process dispatcher (``app.state.
webhook_dispatcher``); while ``server.webhooks.enabled`` is false there is no
dispatcher and both answer 409 (the CRUD registry itself still works).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from novafabric.server import webhooks as store
from novafabric.server.auth import AuthContext
from novafabric.server.errors import BadRequestError, ConflictError, NotFoundError
from novafabric.server.pagination import clamp_limit, decode_cursor, paginate
from novafabric.server.rbac import Role, require_role

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_SECRET_NOTE = (
    "Store this signing secret now — it is shown only once and is never "
    "retrievable over the API."
)


class CreateWebhookRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)
    description: str = Field(default="", max_length=1024)
    event_types: list[str] | None = None
    workspace: str | None = Field(default=None, max_length=256)
    disabled: bool = False


class UpdateWebhookRequest(BaseModel):
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    description: str | None = Field(default=None, max_length=1024)
    event_types: list[str] | None = None
    workspace: str | None = Field(default=None, max_length=256)
    disabled: bool | None = None
    # Deliberately accepted so the attempt gets the spec's 400 (not a 422):
    # the secret is not updatable — rotation is a P2 slice (ADR-0205 D2).
    secret: str | None = None


def _db_path(request: Request) -> Path | None:
    """Resolve the server's configured registry DB path (None → registry default)."""
    configured = getattr(request.app.state.config, "db_path", None)
    return Path(configured) if configured else None


def _allow_insecure(request: Request) -> bool:
    webhooks_cfg = getattr(request.app.state.config, "webhooks", None)
    return bool(getattr(webhooks_cfg, "allow_insecure_url", False))


def _allow_internal(request: Request) -> bool:
    webhooks_cfg = getattr(request.app.state.config, "webhooks", None)
    return bool(getattr(webhooks_cfg, "allow_internal_targets", False))


def _dispatcher(request: Request) -> Any:
    dispatcher = getattr(request.app.state, "webhook_dispatcher", None)
    if dispatcher is None:
        raise ConflictError(
            "webhook dispatch is disabled (server.webhooks.enabled=false); "
            "the subscription registry is writable but nothing is delivered",
            code="webhooks_disabled",
        )
    return dispatcher


@router.post("", status_code=201)
async def create_webhook(
    body: CreateWebhookRequest,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Create a subscription (admin only). The signing secret is returned ONCE."""
    try:
        secret, record = store.create_webhook(
            body.url,
            actor=auth.subject,
            description=body.description,
            event_types=body.event_types,
            workspace=body.workspace,
            disabled=body.disabled,
            allow_insecure_url=_allow_insecure(request),
            allow_internal_targets=_allow_internal(request),
            db_path=_db_path(request),
            wrapping_backend=store.resolve_wrapping_backend(),
        )
    except (
        store.InvalidWebhookUrlError,
        store.InvalidEventTypeError,
        store.UnknownWorkspaceError,
    ) as exc:
        raise BadRequestError(str(exc)) from exc

    return {"webhook": record, "secret": secret, "note": _SECRET_NOTE}


@router.get("")
async def list_webhooks(
    request: Request,
    _auth: Annotated[AuthContext, Depends(require_role(Role.auditor))],
) -> dict[str, Any]:
    """List subscriptions (admin, auditor) — metadata only, never secrets."""
    rows = store.list_webhooks(db_path=_db_path(request))
    return {"webhooks": rows, "total": len(rows)}


@router.get("/{hook_id}")
async def get_webhook(
    hook_id: str,
    request: Request,
    _auth: Annotated[AuthContext, Depends(require_role(Role.auditor))],
) -> dict[str, Any]:
    """Get one subscription (admin, auditor); unknown → 404."""
    try:
        return {"webhook": store.get_webhook(hook_id, db_path=_db_path(request))}
    except store.UnknownWebhookError as exc:
        raise NotFoundError(f"webhook {hook_id!r} not found") from exc


@router.patch("/{hook_id}")
async def update_webhook(
    hook_id: str,
    body: UpdateWebhookRequest,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Update url/description/event_types/workspace/disabled (admin only).

    The signing secret is NOT updatable (400) — rotate is P2 (ADR-0205 D2).
    """
    if "secret" in body.model_fields_set:
        raise BadRequestError(
            "the signing secret is not updatable; secret rotation with an "
            "overlap window is a planned P2 slice (ADR-0205 D2)"
        )
    fields = body.model_fields_set
    kwargs: dict[str, Any] = {}
    if "url" in fields and body.url is not None:
        kwargs["url"] = body.url
    if "description" in fields and body.description is not None:
        kwargs["description"] = body.description
    if "event_types" in fields:
        kwargs["event_types"] = body.event_types
    if "workspace" in fields:
        kwargs["workspace"] = body.workspace
    if "disabled" in fields and body.disabled is not None:
        kwargs["disabled"] = body.disabled
    try:
        record = store.update_webhook(
            hook_id,
            actor=auth.subject,
            allow_insecure_url=_allow_insecure(request),
            allow_internal_targets=_allow_internal(request),
            db_path=_db_path(request),
            **kwargs,
        )
    except store.UnknownWebhookError as exc:
        raise NotFoundError(f"webhook {hook_id!r} not found") from exc
    except (
        store.InvalidWebhookUrlError,
        store.InvalidEventTypeError,
        store.UnknownWorkspaceError,
    ) as exc:
        raise BadRequestError(str(exc)) from exc
    return {"webhook": record}


@router.delete("/{hook_id}")
async def delete_webhook(
    hook_id: str,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Delete a subscription (admin only); delivery rows remain until pruned."""
    try:
        store.delete_webhook(hook_id, actor=auth.subject, db_path=_db_path(request))
    except store.UnknownWebhookError as exc:
        raise NotFoundError(f"webhook {hook_id!r} not found") from exc
    return {"ok": True, "hook_id": hook_id, "deleted": True}


@router.post("/{hook_id}/ping", status_code=202)
async def ping_webhook(
    hook_id: str,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Send a synthetic ``webhook.ping`` through the full delivery path (admin)."""
    dispatcher = _dispatcher(request)
    try:
        delivery_id = dispatcher.ping(hook_id, requested_by=auth.subject)
    except store.UnknownWebhookError as exc:
        raise NotFoundError(f"webhook {hook_id!r} not found") from exc
    return {"delivery_id": delivery_id}


@router.get("/{hook_id}/deliveries")
async def list_deliveries(
    hook_id: str,
    request: Request,
    _auth: Annotated[AuthContext, Depends(require_role(Role.auditor))],
    status: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Cursor-paginated delivery-attempt log, newest first (admin, auditor)."""
    try:
        store.get_webhook(hook_id, db_path=_db_path(request))
    except store.UnknownWebhookError as exc:
        raise NotFoundError(f"webhook {hook_id!r} not found") from exc
    rows = store.list_deliveries(hook_id, status=status, db_path=_db_path(request))
    offset = decode_cursor(cursor)
    page, next_cursor = paginate(rows, clamp_limit(limit), offset)
    return {"deliveries": page, "next_cursor": next_cursor, "total": len(rows)}


@router.post("/{hook_id}/deliveries/{delivery_id}/redeliver", status_code=202)
async def redeliver(
    hook_id: str,
    delivery_id: str,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))],
) -> dict[str, Any]:
    """Re-enqueue a terminal-failed delivery (admin). Non-terminal → 409."""
    dispatcher = _dispatcher(request)
    try:
        store.get_webhook(hook_id, db_path=_db_path(request))
        dispatcher.redeliver(hook_id, delivery_id, actor=auth.subject)
    except store.UnknownWebhookError as exc:
        raise NotFoundError(f"webhook {hook_id!r} not found") from exc
    except store.UnknownDeliveryError as exc:
        raise NotFoundError(f"delivery {delivery_id!r} not found") from exc
    except store.NotRedeliverableError as exc:
        raise ConflictError(str(exc), code="not_redeliverable") from exc
    return {"ok": True, "delivery_id": delivery_id, "redelivery": True}
