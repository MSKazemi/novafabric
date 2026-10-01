"""Assets resource — /v0/assets.

Implements:
  GET  /v0/assets           list (keyset cursor pagination, ADR-0206 P2)
  POST /v0/assets           register from YAML spec
  GET  /v0/assets/{id}      get by asset UUID
  PUT  /v0/assets/{id}/promote
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response

from novafabric.server.auth import AuthContext
from novafabric.server.config import ServerConfig
from novafabric.server.deps import get_config, get_db_path
from novafabric.server.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    PreconditionFailedError,
    ValidationError,
)
from novafabric.server.pagination import (
    InvalidCursorError,
    clamp_limit,
    encode_keyset_cursor,
    paginate,
    parse_cursor,
)
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
        200: {
            "model": AssetListResponse,
            "description": (
                "A page of assets. `total` is present on the first page only — "
                "keyset pages omit it by design (ADR-0206)."
            ),
        },
        **error_responses(400, 401, 403),
    },
)
async def list_assets(
    response: Response,
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = Query(default=None),
    asset_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    config: Annotated[ServerConfig, Depends(get_config)] = None,  # type: ignore[assignment]
    _auth: Annotated[AuthContext, Depends(require_role(Role.reader))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """List assets — keyset (seek) pagination, ADR-0206 P2 (experimental).

    Order is pinned to ``created_at DESC, id DESC``; ``next_cursor`` is an
    opaque v1 keyset cursor naming the page's last asset, so registrations
    between pages neither repeat nor skip a surviving asset. A non-empty
    cursor that fails strict decoding is a 400 ``invalid_cursor`` (it used to
    restart silently at page one). Legacy ``{"offset": N}`` cursors are served
    by the old path for one deprecation cycle (ADR-0188) with a
    ``Deprecation: true`` header. Cursors are not bound to the filters: reuse a
    cursor only with the ``asset_type``/``status`` it was issued under.
    """
    from novafabric.registry.service import list_assets_keyset

    limit = clamp_limit(limit)
    try:
        parsed = parse_cursor(cursor)
    except InvalidCursorError as exc:
        raise BadRequestError(str(exc), code="invalid_cursor")

    if parsed.kind == "offset":
        if not config.pagination.legacy_offset_cursors:
            raise BadRequestError(
                "legacy offset cursors are sunset (ADR-0188); restart the "
                "listing without a cursor to receive keyset cursors",
                code="invalid_cursor",
            )
        from novafabric.registry.service import list_assets as _list_assets

        summaries = [_to_summary(r) for r in _list_assets(asset_type, status, db_path=db_path)]
        page, next_cursor = paginate(summaries, limit, parsed.offset)
        response.headers["Deprecation"] = "true"
        return {"items": page, "next_cursor": next_cursor, "total": len(summaries)}

    first = parsed.kind == "first"
    rows, total = list_assets_keyset(
        asset_type,
        status,
        limit=limit + 1,
        after=parsed.key,
        with_total=first,
        db_path=db_path,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    body: dict[str, Any] = {
        "items": [_to_summary(r) for r in rows],
        "next_cursor": (
            encode_keyset_cursor(rows[-1].get("created_at"), str(rows[-1]["id"]))
            if has_more
            else None
        ),
    }
    if first:
        body["total"] = total
    return body


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
