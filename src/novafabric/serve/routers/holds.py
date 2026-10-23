"""Legal-holds route group (ADR-0031) — first ADR-0183 strangler-migration wave.

Extracted verbatim from the inline ``@app.*`` decorators in ``serve/app.py``.
Paths, methods, auth, response shapes, and status codes are unchanged:

- ``GET  /api/holds``                    — list active holds per registry
- ``POST /api/holds``                    — place a hold
- ``POST /api/holds/{hold_id}/release``  — release a hold

The router is built by a factory so the caller injects its own auth
dependency: ``serve`` passes its shared-token ``verify_token`` closure;
``server`` can mount the same routes behind OIDC/RBAC (ADR-0183 §3).

Storage: the one legal-hold store shared with ``nova hold``
(:mod:`novafabric.retention.holds`, ``$NOVAFABRIC_HOME/registries/<registry>/
holds.jsonl``). Holds this router wrote before that store existed —
``<capsule_dir>/../registries/<registry>/holds.jsonl`` — are still listed and can
still be released, and every deletion gate keeps honouring them.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from novafabric.retention.holds import (
    HoldNotFoundError,
    InvalidRegistryNameError,
    capsule_base_holds_root,
    place_hold,
    read_holds,
)
from novafabric.retention.holds import (
    release_hold as _store_release_hold,
)

#: Actor recorded in the hash-chained audit log for holds placed/released here.
_API_ACTOR = "serve-api"


class PlaceHoldRequest(BaseModel):
    registry: str
    reason: str
    duration_days: int | None = None


class HoldRecord(BaseModel):
    """One legal hold as stored in ``holds.jsonl`` (the shared hold store)."""

    hold_id: str
    registry: str
    reason: str
    duration_days: int | None = None
    created_at: str
    released_at: str | None = None


class RegistryHolds(BaseModel):
    name: str
    holds: list[HoldRecord]


class HoldsListResponse(BaseModel):
    total_active: int
    registries: list[RegistryHolds]


class HoldCreatedResponse(BaseModel):
    hold_id: str
    registry: str
    reason: str
    duration_days: int | None = None
    created_at: str


class HoldReleasedResponse(BaseModel):
    released: bool
    hold_id: str
    registry: str


def build_holds_router(
    verify_token: Callable[..., Any],
    *,
    capsule_dir: Path,
) -> APIRouter:
    """Build the legal-holds router.

    ``verify_token`` is the auth dependency guarding every route. New holds go
    to the shared store (``$NOVAFABRIC_HOME/registries``); ``capsule_dir`` names
    the pre-unification location ``<capsule_dir>/../registries`` that is still
    read and released from.
    """
    router = APIRouter(dependencies=[Depends(verify_token)], tags=["holds"])
    legacy_roots = (capsule_base_holds_root(capsule_dir),)

    @router.get(
        "/api/holds",
        operation_id="dashboardListHolds",
        responses={200: {"model": HoldsListResponse}},
        response_model=None,
    )
    async def list_holds() -> dict[str, Any]:
        by_registry: dict[str, list[dict[str, Any]]] = {}
        for hold in read_holds(extra_roots=legacy_roots):
            bucket = by_registry.setdefault(hold.registry, [])
            if hold.active:
                bucket.append(hold.as_dict())
        registries = [
            {"name": name, "holds": holds} for name, holds in sorted(by_registry.items())
        ]
        total_active = sum(len(r["holds"]) for r in registries)
        return {"total_active": total_active, "registries": registries}

    @router.post(
        "/api/holds",
        operation_id="dashboardPlaceHold",
        responses={200: {"model": HoldCreatedResponse}},
        response_model=None,
    )
    async def create_hold(body: PlaceHoldRequest = Body(...)) -> dict[str, Any]:
        for param, val in (("registry", body.registry),):
            if "/" in val or ".." in val:
                raise HTTPException(
                    status_code=400,
                    detail=f"invalid {param}: must not contain '/' or '..'",
                )
        if not body.registry.strip():
            raise HTTPException(status_code=422, detail="registry must not be empty")
        if not body.reason.strip():
            raise HTTPException(status_code=422, detail="reason must not be empty")
        try:
            record = place_hold(
                body.registry, body.reason, body.duration_days, actor=_API_ACTOR
            )
        except InvalidRegistryNameError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return {k: v for k, v in record.as_dict().items() if k != "released_at"}

    @router.post(
        "/api/holds/{hold_id}/release",
        operation_id="dashboardReleaseHold",
        responses={200: {"model": HoldReleasedResponse}},
        response_model=None,
    )
    async def release_hold(hold_id: str) -> dict[str, Any]:
        if "/" in hold_id or ".." in hold_id:
            raise HTTPException(status_code=400, detail="invalid hold_id")
        try:
            registries = _store_release_hold(
                hold_id, actor=_API_ACTOR, extra_roots=legacy_roots
            )
        except InvalidRegistryNameError:
            raise HTTPException(status_code=400, detail="invalid hold_id") from None
        except HoldNotFoundError:
            raise HTTPException(
                status_code=404, detail=f"hold not found or already released: {hold_id}"
            ) from None
        return {"released": True, "hold_id": hold_id, "registry": registries[0]}

    return router
