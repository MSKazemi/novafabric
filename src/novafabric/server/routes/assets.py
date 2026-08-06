"""Assets resource — /v0/assets.

Implements:
  GET  /v0/assets           list (cursor pagination)
  POST /v0/assets           register from YAML spec
  GET  /v0/assets/{id}      get by asset UUID
  PUT  /v0/assets/{id}/promote
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from novafabric.server.auth import AuthContext
from novafabric.server.deps import get_db_path
from novafabric.server.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    PreconditionFailedError,
    ValidationError,
)
from novafabric.server.pagination import clamp_limit, decode_cursor, paginate
from novafabric.server.rbac import Role, require_role
from novafabric.server.schemas import (
    AssetDetail,
    AssetListResponse,
    error_responses,
)

router = APIRouter(prefix="/assets", tags=["assets"])


# ---------- list ----------


@router.get(
    "",
    response_model=None,
    operation_id="listAssets",
    summary="List assets",
    responses={
        200: {"model": AssetListResponse, "description": "A page of assets."},
        **error_responses(400, 401, 403),
    },
)
async def list_assets(
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = Query(default=None),
    asset_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    _auth: Annotated[AuthContext, Depends(require_role(Role.reader))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    from novafabric.registry.service import list_assets as _list_assets

    rows = _list_assets(asset_type, status, db_path=db_path)
    summaries = [_to_summary(r) for r in rows]

    offset = decode_cursor(cursor)
    limit = clamp_limit(limit)
    page, next_cursor = paginate(summaries, limit, offset)

    return {
        "items": page,
        "next_cursor": next_cursor,
        "total": len(summaries),
    }


# ---------- create ----------


@router.post(
    "",
    status_code=201,
    response_model=None,
    operation_id="createAsset",
    summary="Register an asset from a YAML spec",
    responses={
        201: {"model": AssetDetail, "description": "The registered asset."},
        **error_responses(400, 401, 403, 409),
    },
)
async def create_asset(
    body: dict[str, Any],
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    _auth: Annotated[AuthContext, Depends(require_role(Role.writer))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    spec_yaml = body.get("spec_yaml", "")
    if not spec_yaml:
        raise BadRequestError("spec_yaml is required")

    from novafabric.registry.service import DuplicateAssetError, register_asset
    from novafabric.spec.validator import SpecValidationError, validate_spec

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
        tmp.write(spec_yaml)
        tmp_path = Path(tmp.name)

    try:
        try:
            spec = validate_spec(tmp_path)
        except SpecValidationError as exc:
            raise ValidationError(f"spec validation failed: {exc}")

        try:
            result = register_asset(spec, tmp_path, db_path=db_path)
        except DuplicateAssetError as exc:
            raise ConflictError(str(exc))
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    return _to_detail(result)


# ---------- get by id ----------


@router.get(
    "/{asset_id}",
    response_model=None,
    operation_id="getAsset",
    summary="Get an asset by UUID",
    responses={
        200: {"model": AssetDetail, "description": "The asset."},
        **error_responses(401, 403, 404),
    },
)
async def get_asset(
    asset_id: str,
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    _auth: Annotated[AuthContext, Depends(require_role(Role.reader))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    from novafabric.registry.store import get_connection, init_schema

    conn = get_connection(db_path)
    init_schema(conn)
    try:
        row = conn.execute(
            "SELECT * FROM assets WHERE id = ?", (asset_id,)
        ).fetchone()
        if not row:
            raise NotFoundError(f"Asset '{asset_id}' not found.")
        return _to_detail(dict(row))
    finally:
        conn.close()


# ---------- promote ----------


@router.put(
    "/{asset_id}/promote",
    response_model=None,
    operation_id="promoteAsset",
    summary="Promote an asset to a new lifecycle status",
    responses={
        200: {"model": AssetDetail, "description": "The promoted asset."},
        **error_responses(400, 401, 403, 404, 409, 412),
    },
)
async def promote_asset(
    asset_id: str,
    body: dict[str, Any],
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    _auth: Annotated[AuthContext, Depends(require_role(Role.writer))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    to_status_raw = body.get("to_status")
    if not to_status_raw:
        raise BadRequestError("to_status is required")
    actor = str(body.get("actor", "api"))
    force = bool(body.get("force", False))

    from novafabric.registry.service import (
        AssetNotFoundError,
        InvalidLifecycleTransitionError,
        PromotionBlockedError,
    )
    from novafabric.registry.service import promote_asset as _promote_asset
    from novafabric.registry.store import get_connection, init_schema
    from novafabric.spec.models import AssetStatus

    # Resolve asset_id → name + version
    conn = get_connection(db_path)
    init_schema(conn)
    try:
        row = conn.execute(
            "SELECT name, version FROM assets WHERE id = ?", (asset_id,)
        ).fetchone()
        if not row:
            raise NotFoundError(f"Asset '{asset_id}' not found.")
        name = row["name"]
        version = row["version"]
    finally:
        conn.close()

    try:
        target = AssetStatus(to_status_raw)
    except ValueError:
        raise BadRequestError(f"invalid to_status: {to_status_raw!r}")

    try:
        result = _promote_asset(
            name=name,
            version=version,
            to_status=target,
            actor=actor,
            force=force,
            db_path=db_path,
        )
    except AssetNotFoundError as exc:
        raise NotFoundError(str(exc))
    except InvalidLifecycleTransitionError as exc:
        raise ConflictError(str(exc))
    except PromotionBlockedError as exc:
        raise PreconditionFailedError(str(exc))

    return _to_detail(result)


# ---------- helpers ----------


def _to_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "version": row.get("version"),
        "asset_type": row.get("asset_type"),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
        "promoted_at": row.get("promoted_at"),
        "git_commit_sha": row.get("git_commit_sha"),
    }


def _to_detail(row: dict[str, Any]) -> dict[str, Any]:
    detail = _to_summary(row)
    detail["spec_json"] = row.get("spec_json")
    detail["promoted_by"] = row.get("promoted_by")
    detail["forced_promotion"] = bool(row.get("forced_promotion"))
    return detail
