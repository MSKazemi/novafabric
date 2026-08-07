"""Legal-holds route group (ADR-0031) — first ADR-0183 strangler-migration wave.

Extracted verbatim from the inline ``@app.*`` decorators in ``serve/app.py``.
Paths, methods, auth, response shapes, and status codes are unchanged:

- ``GET  /api/holds``                    — list active holds per registry
- ``POST /api/holds``                    — place a hold
- ``POST /api/holds/{hold_id}/release``  — release a hold

The router is built by a factory so the caller injects its own auth
dependency: ``serve`` passes its shared-token ``verify_token`` closure;
``server`` can mount the same routes behind OIDC/RBAC (ADR-0183 §3).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel


class PlaceHoldRequest(BaseModel):
    registry: str
    reason: str
    duration_days: int | None = None


class HoldRecord(BaseModel):
    """One legal hold as stored in ``holds.jsonl``."""

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

    ``verify_token`` is the auth dependency guarding every route;
    ``capsule_dir`` anchors the on-disk holds registries at
    ``<capsule_dir>/../registries/<registry>/holds.jsonl``.
    """
    router = APIRouter(dependencies=[Depends(verify_token)], tags=["holds"])

    def _holds_base() -> Path:
        return capsule_dir.parent / "registries"

    def _read_holds_file(registry: str) -> list[dict[str, Any]]:
        path = _holds_base() / registry / "holds.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text().splitlines()
            if line.strip()
        ]

    def _active_holds(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [h for h in records if h.get("released_at") is None]

    @router.get(
        "/api/holds",
        operation_id="dashboardListHolds",
        responses={200: {"model": HoldsListResponse}},
        response_model=None,
    )
    async def list_holds() -> dict[str, Any]:
        base = _holds_base()
        registries: list[dict[str, Any]] = []
        total_active = 0
        if base.exists():
            for reg_dir in sorted(base.iterdir()):
                if not reg_dir.is_dir():
                    continue
                all_records = _read_holds_file(reg_dir.name)
                active = _active_holds(all_records)
                total_active += len(active)
                registries.append({"name": reg_dir.name, "holds": active})
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

        hold_id = f"hold-{uuid.uuid4().hex[:8]}"
        path = _holds_base() / body.registry / "holds.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "hold_id": hold_id,
            "registry": body.registry,
            "reason": body.reason,
            "duration_days": body.duration_days,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "released_at": None,
        }
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")
        return {k: v for k, v in record.items() if k != "released_at"}

    @router.post(
        "/api/holds/{hold_id}/release",
        operation_id="dashboardReleaseHold",
        responses={200: {"model": HoldReleasedResponse}},
        response_model=None,
    )
    async def release_hold(hold_id: str) -> dict[str, Any]:
        if "/" in hold_id or ".." in hold_id:
            raise HTTPException(status_code=400, detail="invalid hold_id")

        base = _holds_base()
        if not base.exists():
            raise HTTPException(status_code=404, detail=f"hold not found: {hold_id}")

        for reg_dir in base.iterdir():
            if not reg_dir.is_dir():
                continue
            holds_file = reg_dir / "holds.jsonl"
            if not holds_file.exists():
                continue
            lines = holds_file.read_text().splitlines()
            updated: list[str] = []
            registry = reg_dir.name
            released = False
            for line in lines:
                if not line.strip():
                    continue
                h = json.loads(line)
                if h["hold_id"] == hold_id and h["released_at"] is None:
                    h["released_at"] = datetime.now(tz=timezone.utc).isoformat()
                    released = True
                updated.append(json.dumps(h))
            if released:
                tmp = holds_file.with_suffix(".tmp")
                tmp.write_text("\n".join(updated) + "\n")
                os.replace(tmp, holds_file)
                return {"released": True, "hold_id": hold_id, "registry": registry}

        raise HTTPException(
            status_code=404, detail=f"hold not found or already released: {hold_id}"
        )

    return router
