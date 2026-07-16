"""SCIM 2.0 provisioning surface — /scim/v2 (ADR-0139, experimental).

RFC 7643/7644 subset (ADR-0139 P1–P3): discovery endpoints, the ``Users``
resource — create, read, list/filter, PATCH (deactivate), DELETE — and the
``Groups`` resource (create, read, list, PATCH, PUT full-replace, DELETE)
whose membership drives RBAC role grants via the operator-declared
``scim.group_role_map`` (ADR-0190 provenance rules).

Server-mode only and doubly opt-in: the endpoints return 404 unless
``server.scim.enabled`` is true in the server config *and* the dedicated
provisioning bearer token is configured via ``NOVAFABRIC_SCIM_TOKEN``.
The token is a distinct credential class from OIDC user tokens: it grants
provisioning only and is not a JWT, so it cannot authenticate to ``/v0/``.

SCIM defines its own media type (``application/scim+json``) and error
envelope (RFC 7644 §3.12), which this module honors verbatim instead of the
NovaFabric ``/v0/`` error model.

De-provisioning (``active: false`` or DELETE) revokes the subject's role
assignments through :mod:`novafabric.server.rbac_store` — the ADR-0060
last-admin lockout invariant is preserved: a SCIM deprovision that would
remove the last admin is refused with a SCIM 409 and no partial mutation.
Every mutation appends an event to the append-only provisioning audit log
(ADR-0139 D5).
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from novafabric.server import rbac_store, scim_store
from novafabric.server.config import ServerConfig
from novafabric.server.rbac_store import LastAdminError
from novafabric.server.scim_store import DuplicateUserNameError, ScimUser

router = APIRouter(prefix="/scim/v2", tags=["scim"])

SCIM_MEDIA_TYPE = "application/scim+json"

USER_URN = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_URN = "urn:ietf:params:scim:schemas:core:2.0:Group"
PATCH_OP_URN = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
LIST_RESPONSE_URN = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_URN = "urn:ietf:params:scim:api:messages:2.0:Error"

# RFC 7644 §3.4.2.2 — only the `attr eq value` subset is supported in v0.
_FILTER_RE = re.compile(
    r'^\s*(?P<attr>[A-Za-z]\w*)\s+eq\s+(?P<value>"[^"]*"|true|false)\s*$'
)


# ---------------------------------------------------------------------------
# SCIM error envelope + responses (RFC 7644 §3.12)
# ---------------------------------------------------------------------------


class _ScimError(Exception):
    """Raised by handlers; converted to the RFC 7644 error envelope."""

    def __init__(
        self, status_code: int, detail: str, scim_type: str | None = None
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.scim_type = scim_type


def _scim_error_response(exc: _ScimError) -> JSONResponse:
    body: dict[str, Any] = {
        "schemas": [ERROR_URN],
        "status": str(exc.status_code),
        "detail": exc.detail,
    }
    if exc.scim_type:
        body["scimType"] = exc.scim_type
    return JSONResponse(
        status_code=exc.status_code, content=body, media_type=SCIM_MEDIA_TYPE
    )


async def scim_error_handler(request: Request, exc: _ScimError) -> JSONResponse:
    return _scim_error_response(exc)


def _scim_response(content: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=content, media_type=SCIM_MEDIA_TYPE
    )


# ---------------------------------------------------------------------------
# Auth — provisioning-scoped bearer token (ADR-0139 D6)
# ---------------------------------------------------------------------------


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


async def require_scim_token(request: Request) -> str:
    """Gate every /scim/v2 route.

    Returns the actor identity string for the audit log.

    * SCIM not configured (flag off or no token) → 404, as if the feature
      did not exist (spec: *Scope*, normative).
    * Bad or missing bearer token → 401 SCIM error envelope.
    """
    config: ServerConfig = request.app.state.config
    if not config.scim_active:
        # Plain FastAPI 404 (not the SCIM envelope): when disabled the server
        # behaves exactly as if the feature did not exist (spec, normative).
        raise HTTPException(status_code=404, detail="Not Found")

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise _ScimError(401, "Missing or malformed Authorization header")
    presented = auth_header.removeprefix("Bearer ").strip()
    expected = config.scim_token or ""
    if not secrets.compare_digest(presented.encode(), expected.encode()):
        raise _ScimError(401, "Invalid provisioning token")
    return f"scim-token:{_token_fingerprint(expected)}"


_Actor = Annotated[str, Depends(require_scim_token)]


# ---------------------------------------------------------------------------
# Wire → stored-subset parsing (PII minimization: drop, don't reject)
# ---------------------------------------------------------------------------


def _parse_user_payload(payload: Any) -> dict[str, Any]:
    """Extract the stored SCIM User subset from an inbound wire payload.

    Attributes outside the stored subset (enterprise extension, addresses,
    phoneNumbers, ...) are dropped, not rejected — per the spec, PII
    minimization is enforced at the storage layer while the wire stays
    SCIM-conformant.
    """
    if not isinstance(payload, dict):
        raise _ScimError(400, "Request body must be a SCIM User object", "invalidSyntax")
    schemas = payload.get("schemas")
    if not isinstance(schemas, list) or USER_URN not in schemas:
        raise _ScimError(
            400, f"schemas must contain {USER_URN!r}", "invalidValue"
        )
    user_name = payload.get("userName")
    if not isinstance(user_name, str) or not user_name:
        raise _ScimError(400, "userName is required", "invalidValue")
    active = payload.get("active", True)
    if not isinstance(active, bool):
        raise _ScimError(400, "active must be a boolean", "invalidValue")

    external_id = payload.get("externalId")
    if external_id is not None and not isinstance(external_id, str):
        raise _ScimError(400, "externalId must be a string", "invalidValue")
    display_name = payload.get("displayName")
    if display_name is not None and not isinstance(display_name, str):
        raise _ScimError(400, "displayName must be a string", "invalidValue")

    emails: list[dict[str, Any]] = []
    raw_emails = payload.get("emails")
    if raw_emails is not None:
        if not isinstance(raw_emails, list):
            raise _ScimError(400, "emails must be an array", "invalidValue")
        for entry in raw_emails:
            if not isinstance(entry, dict):
                raise _ScimError(400, "emails entries must be objects", "invalidValue")
            kept = {k: entry[k] for k in ("value", "primary") if k in entry}
            if kept:
                emails.append(kept)

    return {
        "user_name": user_name,
        "active": active,
        "external_id": external_id,
        "display_name": display_name,
        "emails": emails,
    }


def _user_resource(user: ScimUser) -> dict[str, Any]:
    """Serialize a stored user as an RFC 7643 User resource."""
    resource: dict[str, Any] = {
        "schemas": [USER_URN],
        "id": user.id,
        "userName": user.user_name,
        "active": user.active,
        "meta": {
            "resourceType": "User",
            "created": user.created,
            "lastModified": user.last_modified,
            "location": f"/scim/v2/Users/{user.id}",
        },
    }
    if user.external_id is not None:
        resource["externalId"] = user.external_id
    if user.display_name is not None:
        resource["displayName"] = user.display_name
    if user.emails:
        resource["emails"] = user.emails
    return resource


# ---------------------------------------------------------------------------
# De-provisioning (ADR-0139 D4) — honest role revocation, last-admin guarded
# ---------------------------------------------------------------------------


def _revoke_all_roles(user_name: str) -> tuple[list[str], list[str]]:
    """Revoke every role assignment for *user_name*.

    Returns ``(roles_before, roles_after)``. Revokes ``admin`` first so the
    ADR-0060 last-admin lockout guard fires *before* any mutation — a refused
    deprovision leaves the role assignments untouched.
    """
    roles_before = sorted(rbac_store.get_roles(user_name))
    ordered = sorted(roles_before, key=lambda r: (r != "admin", r))
    for role in ordered:
        try:
            rbac_store.revoke_role(user_name, role)
        except LastAdminError as exc:
            raise _ScimError(409, str(exc), "mutability") from exc
    return roles_before, sorted(rbac_store.get_roles(user_name))


def _get_user_or_404(user_id: str) -> ScimUser:
    user = scim_store.get_user(user_id)
    if user is None:
        raise _ScimError(404, f"User {user_id} not found", "invalidValue")
    return user


# ---------------------------------------------------------------------------
# Discovery endpoints (RFC 7644 §4)
# ---------------------------------------------------------------------------


@router.get("/ServiceProviderConfig")
async def service_provider_config(_actor: _Actor) -> JSONResponse:
    return _scim_response(
        {
            "schemas": [
                "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
            ],
            "documentationUri": "https://github.com/novafabric/novafabric",
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": 200},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "Provisioning bearer token",
                    "description": (
                        "Dedicated provisioning-scoped bearer token"
                        " (NOVAFABRIC_SCIM_TOKEN); grants no /v0/ access."
                    ),
                }
            ],
            "meta": {
                "resourceType": "ServiceProviderConfig",
                "location": "/scim/v2/ServiceProviderConfig",
            },
        }
    )


@router.get("/ResourceTypes")
async def resource_types(_actor: _Actor) -> JSONResponse:
    return _scim_response(
        {
            "schemas": [LIST_RESPONSE_URN],
            "totalResults": 1,
            "startIndex": 1,
            "itemsPerPage": 1,
            "Resources": [
                {
                    "schemas": [
                        "urn:ietf:params:scim:schemas:core:2.0:ResourceType"
                    ],
                    "id": "User",
                    "name": "User",
                    "endpoint": "/Users",
                    "schema": USER_URN,
                    "meta": {
                        "resourceType": "ResourceType",
                        "location": "/scim/v2/ResourceTypes/User",
                    },
                }
            ],
        }
    )


@router.get("/Schemas")
async def schemas(_actor: _Actor) -> JSONResponse:
    return _scim_response(
        {
            "schemas": [LIST_RESPONSE_URN],
            "totalResults": 1,
            "startIndex": 1,
            "itemsPerPage": 1,
            "Resources": [
                {
                    "id": USER_URN,
                    "name": "User",
                    "description": (
                        "NovaFabric stored User subset (PII-minimal):"
                        " userName, active, externalId, displayName, emails."
                    ),
                    "attributes": [
                        {"name": "userName", "type": "string", "required": True,
                         "uniqueness": "server"},
                        {"name": "active", "type": "boolean", "required": False},
                        {"name": "externalId", "type": "string", "required": False},
                        {"name": "displayName", "type": "string", "required": False},
                        {"name": "emails", "type": "complex", "multiValued": True,
                         "required": False},
                    ],
                    "meta": {
                        "resourceType": "Schema",
                        "location": f"/scim/v2/Schemas/{USER_URN}",
                    },
                }
            ],
        }
    )


# ---------------------------------------------------------------------------
# Users resource (RFC 7644 §3.3–3.6)
# ---------------------------------------------------------------------------


@router.post("/Users")
async def create_user(request: Request, actor: _Actor) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 — malformed JSON body
        raise _ScimError(400, f"Malformed JSON body: {exc}", "invalidSyntax") from exc
    fields = _parse_user_payload(payload)
    try:
        user = scim_store.create_user(
            fields["user_name"],
            active=fields["active"],
            external_id=fields["external_id"],
            display_name=fields["display_name"],
            emails=fields["emails"],
        )
    except DuplicateUserNameError as exc:
        raise _ScimError(409, str(exc), "uniqueness") from exc
    roles = sorted(rbac_store.get_roles(user.user_name))
    scim_store.append_audit_event(
        actor=actor,
        operation="user.create",
        resource_type="User",
        subject=user.user_name,
        roles_before=roles,
        roles_after=roles,
    )
    return _scim_response(_user_resource(user), status_code=201)


@router.get("/Users")
async def list_users(request: Request, _actor: _Actor) -> JSONResponse:
    params = request.query_params
    filter_attr: str | None = None
    filter_value: str | bool | None = None
    raw_filter = params.get("filter")
    if raw_filter is not None:
        match = _FILTER_RE.match(raw_filter)
        if match is None or match.group("attr") not in scim_store.FILTERABLE_ATTRIBUTES:
            raise _ScimError(
                400,
                "Unsupported filter; v0 supports 'eq' on userName, externalId, active",
                "invalidFilter",
            )
        filter_attr = match.group("attr")
        raw_value = match.group("value")
        if raw_value in ("true", "false"):
            filter_value = raw_value == "true"
        else:
            filter_value = raw_value[1:-1]
        if filter_attr == "active" and not isinstance(filter_value, bool):
            raise _ScimError(
                400, "active filter value must be true or false", "invalidFilter"
            )

    try:
        start_index = max(int(params.get("startIndex", "1")), 1)
        count = int(params["count"]) if "count" in params else None
    except ValueError as exc:
        raise _ScimError(
            400, "startIndex and count must be integers", "invalidValue"
        ) from exc

    total, users = scim_store.list_users(
        filter_attr=filter_attr,
        filter_value=filter_value,
        start_index=start_index,
        count=count,
    )
    resources = [_user_resource(u) for u in users]
    return _scim_response(
        {
            "schemas": [LIST_RESPONSE_URN],
            "totalResults": total,
            "startIndex": start_index,
            "itemsPerPage": len(resources),
            "Resources": resources,
        }
    )


@router.get("/Users/{user_id}")
async def get_user(user_id: str, _actor: _Actor) -> JSONResponse:
    return _scim_response(_user_resource(_get_user_or_404(user_id)))


def _coerce_active(value: Any) -> bool:
    """Coerce a PATCH `active` value to bool (Entra ID sends "True"/"False")."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise _ScimError(400, "active must be a boolean", "invalidValue")


def _parse_patch_operations(payload: Any) -> dict[str, Any]:
    """Reduce a PatchOp body to the supported change set.

    v0 supports add/replace on ``active``, ``displayName`` and ``externalId``
    — including the no-path Entra ID dialect (``value`` object). Anything
    else is rejected with 400 rather than silently ignored.
    """
    if not isinstance(payload, dict):
        raise _ScimError(400, "Request body must be a PatchOp object", "invalidSyntax")
    schemas = payload.get("schemas")
    if not isinstance(schemas, list) or PATCH_OP_URN not in schemas:
        raise _ScimError(400, f"schemas must contain {PATCH_OP_URN!r}", "invalidValue")
    operations = payload.get("Operations")
    if not isinstance(operations, list) or not operations:
        raise _ScimError(400, "Operations must be a non-empty array", "invalidValue")

    supported = {"active", "displayName", "externalId"}
    changes: dict[str, Any] = {}
    for op in operations:
        if not isinstance(op, dict):
            raise _ScimError(400, "Each Operation must be an object", "invalidSyntax")
        op_name = str(op.get("op", "")).lower()
        if op_name not in ("add", "replace"):
            raise _ScimError(
                400, f"Unsupported PATCH op {op.get('op')!r}", "invalidValue"
            )
        path = op.get("path")
        value = op.get("value")
        if path is None:
            if not isinstance(value, dict):
                raise _ScimError(
                    400, "PATCH without path requires an object value", "invalidValue"
                )
            for key, val in value.items():
                if key not in supported:
                    raise _ScimError(
                        400, f"Unsupported PATCH attribute {key!r}", "invalidPath"
                    )
                changes[key] = val
        else:
            if path not in supported:
                raise _ScimError(400, f"Unsupported PATCH path {path!r}", "invalidPath")
            changes[path] = value

    if "active" in changes:
        changes["active"] = _coerce_active(changes["active"])
    for key in ("displayName", "externalId"):
        if key in changes and not isinstance(changes[key], str):
            raise _ScimError(400, f"{key} must be a string", "invalidValue")
    return changes


@router.patch("/Users/{user_id}")
async def patch_user(user_id: str, request: Request, actor: _Actor) -> JSONResponse:
    user = _get_user_or_404(user_id)
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 — malformed JSON body
        raise _ScimError(400, f"Malformed JSON body: {exc}", "invalidSyntax") from exc
    changes = _parse_patch_operations(payload)

    deactivating = changes.get("active") is False and user.active
    operation = "user.update"
    if deactivating:
        # Revoke first: if the last-admin guard refuses, nothing has mutated
        # (neither role assignments nor the stored user).
        roles_before, roles_after = _revoke_all_roles(user.user_name)
        operation = "user.deactivate"
    else:
        roles_before = sorted(rbac_store.get_roles(user.user_name))
        roles_after = roles_before

    store_changes: dict[str, Any] = {}
    if "active" in changes:
        store_changes["active"] = changes["active"]
    if "displayName" in changes:
        store_changes["display_name"] = changes["displayName"]
    if "externalId" in changes:
        store_changes["external_id"] = changes["externalId"]
    updated = scim_store.update_user(user_id, changes=store_changes)
    if updated is None:  # pragma: no cover — row deleted between read and write
        raise _ScimError(404, f"User {user_id} not found", "invalidValue")

    scim_store.append_audit_event(
        actor=actor,
        operation=operation,
        resource_type="User",
        subject=updated.user_name,
        roles_before=roles_before,
        roles_after=roles_after,
    )
    return _scim_response(_user_resource(updated))


@router.delete("/Users/{user_id}", status_code=204)
async def delete_user(user_id: str, actor: _Actor) -> Response:
    user = _get_user_or_404(user_id)
    # De-provision before deleting: the last-admin guard can still refuse,
    # in which case the user row is untouched.
    roles_before, roles_after = _revoke_all_roles(user.user_name)
    scim_store.delete_user(user_id)
    scim_store.append_audit_event(
        actor=actor,
        operation="user.delete",
        resource_type="User",
        subject=user.user_name,
        roles_before=roles_before,
        roles_after=roles_after,
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Groups (ADR-0139 D3 / ADR-0190) — group→role mapping wired to RBAC
# ---------------------------------------------------------------------------

# RFC 7644 §3.5.2 remove path: members[value eq "<id>"]
_MEMBER_PATH_RE = re.compile(r'^members\[value eq "(?P<value>[^"]+)"\]$')


def _parse_group_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise _ScimError(400, "Request body must be a SCIM Group object", "invalidSyntax")
    schemas = payload.get("schemas")
    if not isinstance(schemas, list) or GROUP_URN not in schemas:
        raise _ScimError(400, f"schemas must contain {GROUP_URN!r}", "invalidValue")
    display_name = payload.get("displayName")
    if not isinstance(display_name, str) or not display_name:
        raise _ScimError(400, "displayName is required", "invalidValue")
    external_id = payload.get("externalId")
    if external_id is not None and not isinstance(external_id, str):
        raise _ScimError(400, "externalId must be a string", "invalidValue")
    member_ids: list[str] = []
    raw_members = payload.get("members")
    if raw_members is not None:
        if not isinstance(raw_members, list):
            raise _ScimError(400, "members must be an array", "invalidValue")
        for entry in raw_members:
            if not isinstance(entry, dict) or not isinstance(entry.get("value"), str):
                raise _ScimError(400, "each member needs a string 'value'", "invalidValue")
            member_ids.append(entry["value"])
    return {"display_name": display_name, "external_id": external_id, "member_ids": member_ids}


def _group_resource(group: scim_store.ScimGroup) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "schemas": [GROUP_URN],
        "id": group.id,
        "displayName": group.display_name,
        "members": [{"value": uid} for uid in group.members],
        "meta": {
            "resourceType": "Group",
            "created": group.created,
            "lastModified": group.last_modified,
            "location": f"/scim/v2/Groups/{group.id}",
        },
    }
    if group.external_id is not None:
        resource["externalId"] = group.external_id
    return resource


def _reconcile_member(request: Request, user_id: str, actor: str) -> None:
    """Recompute and apply the RBAC roles a SCIM member should have from all groups.

    Unknown member ids are ignored (SCIM allows references the server doesn't hold).
    A LastAdminError from the reconcile surfaces as a SCIM 409 with no mutation.
    """
    user = scim_store.get_user(user_id)
    if user is None:
        return
    config: ServerConfig = request.app.state.config
    from novafabric.server.scim_group_mapping import resolve_roles

    display_names = scim_store.group_display_names_for_user(user_id)
    desired = resolve_roles(display_names, config.scim.role_mapping())
    try:
        scim_store.reconcile_subject_roles(user.user_name, desired, actor)
    except rbac_store.LastAdminError as exc:
        raise _ScimError(409, str(exc), "mutability") from exc


@router.post("/Groups")
async def create_group(request: Request, actor: _Actor) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 — malformed JSON body
        raise _ScimError(400, f"Malformed JSON body: {exc}", "invalidSyntax") from exc
    fields = _parse_group_payload(payload)
    group = scim_store.create_group(
        fields["display_name"],
        external_id=fields["external_id"],
        member_ids=fields["member_ids"],
    )
    for uid in fields["member_ids"]:
        _reconcile_member(request, uid, actor)
    return _scim_response(_group_resource(group), status_code=201)


@router.get("/Groups")
async def list_groups(_actor: _Actor) -> JSONResponse:
    groups = scim_store.list_groups()
    return _scim_response(
        {
            "schemas": [LIST_RESPONSE_URN],
            "totalResults": len(groups),
            "startIndex": 1,
            "itemsPerPage": len(groups),
            "Resources": [_group_resource(g) for g in groups],
        }
    )


@router.get("/Groups/{group_id}")
async def get_group(group_id: str, _actor: _Actor) -> JSONResponse:
    group = scim_store.get_group(group_id)
    if group is None:
        raise _ScimError(404, f"Group {group_id} not found", "invalidValue")
    return _scim_response(_group_resource(group))


@router.patch("/Groups/{group_id}")
async def patch_group(group_id: str, request: Request, actor: _Actor) -> JSONResponse:
    group = scim_store.get_group(group_id)
    if group is None:
        raise _ScimError(404, f"Group {group_id} not found", "invalidValue")
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 — malformed JSON body
        raise _ScimError(400, f"Malformed JSON body: {exc}", "invalidSyntax") from exc
    if not isinstance(payload, dict) or PATCH_OP_URN not in (payload.get("schemas") or []):
        raise _ScimError(400, f"schemas must contain {PATCH_OP_URN!r}", "invalidValue")
    operations = payload.get("Operations")
    if not isinstance(operations, list) or not operations:
        raise _ScimError(400, "Operations must be a non-empty array", "invalidValue")

    affected: list[str] = []
    for op in operations:
        if not isinstance(op, dict):
            raise _ScimError(400, "each Operation must be an object", "invalidSyntax")
        verb = str(op.get("op", "")).lower()
        path = op.get("path")
        if verb == "add" and path == "members":
            for entry in op.get("value") or []:
                if isinstance(entry, dict) and isinstance(entry.get("value"), str):
                    scim_store.add_group_member(group_id, entry["value"])
                    affected.append(entry["value"])
        elif verb == "remove" and isinstance(path, str):
            match = _MEMBER_PATH_RE.match(path)
            if match is None:
                raise _ScimError(400, f"unsupported patch path {path!r}", "invalidPath")
            uid = match.group("value")
            scim_store.remove_group_member(group_id, uid)
            affected.append(uid)
        else:
            raise _ScimError(400, f"unsupported patch op {verb!r}", "invalidValue")

    for uid in affected:
        _reconcile_member(request, uid, actor)
    updated = scim_store.get_group(group_id)
    assert updated is not None  # just fetched/patched
    return _scim_response(_group_resource(updated))


@router.put("/Groups/{group_id}")
async def put_group(group_id: str, request: Request, actor: _Actor) -> JSONResponse:
    """RFC 7644 §3.5.1 full replace of a Group.

    ``displayName`` and ``members`` are replaced atomically (one transaction);
    role reconciliation then runs for every member added, removed, or kept —
    exactly as PATCH does: new members of a mapped group gain the role, removed
    members lose only SCIM-owned grants (ADR-0190), and a revoke that would
    remove the last admin surfaces as a SCIM 409 (ADR-0060).
    """
    group = scim_store.get_group(group_id)
    if group is None:
        raise _ScimError(404, f"Group {group_id} not found", "invalidValue")
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 — malformed JSON body
        raise _ScimError(400, f"Malformed JSON body: {exc}", "invalidSyntax") from exc
    fields = _parse_group_payload(payload)

    old_members = set(group.members)
    updated = scim_store.replace_group(
        group_id,
        display_name=fields["display_name"],
        external_id=fields["external_id"],
        member_ids=fields["member_ids"],
    )
    if updated is None:  # pragma: no cover — row deleted between read and write
        raise _ScimError(404, f"Group {group_id} not found", "invalidValue")

    # Reconcile the union: removed members lose SCIM-owned grants, added
    # members gain them, and kept members are remapped on a displayName rename.
    for uid in sorted(old_members | set(fields["member_ids"])):
        _reconcile_member(request, uid, actor)
    return _scim_response(_group_resource(updated))


@router.delete("/Groups/{group_id}", status_code=204)
async def delete_group(group_id: str, request: Request, actor: _Actor) -> Response:
    group = scim_store.get_group(group_id)
    if group is None:
        raise _ScimError(404, f"Group {group_id} not found", "invalidValue")
    members = list(group.members)
    scim_store.delete_group(group_id)
    # After the group is gone, each member's role set is recomputed from the
    # groups that remain — dropping the role this group granted (if unique).
    for uid in members:
        _reconcile_member(request, uid, actor)
    return Response(status_code=204)
