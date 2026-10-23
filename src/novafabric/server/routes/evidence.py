"""Evidence resource — /v0/evidence.

Implements:
  POST /v0/evidence                      export (build evidence bundle from capsule)
  GET  /v0/evidence/{bundle_id}          get bundle metadata
  GET  /v0/evidence/{bundle_id}/download download ZIP
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from novafabric import _paths as _nf_paths
from novafabric._safe_path import (
    PathEscapeError,
    UnsafePathSegmentError,
    require_path_segment,
    safe_child,
)
from novafabric.server.auth import AuthContext
from novafabric.server.deps import get_capsule_dir, get_db_path
from novafabric.server.errors import BadRequestError, ConflictError, NotFoundError
from novafabric.server.rbac import Role, require_role
from novafabric.server.schemas import (
    BundleSummary,
    EvidenceExportRequest,
    error_responses,
    request_body,
)

router = APIRouter(prefix="/evidence", tags=["evidence"])

# In-process bundle registry — maps bundle_id → metadata
_BUNDLES: dict[str, dict[str, Any]] = {}
_BUNDLES_LOCK = threading.Lock()


def _evidence_dir() -> Path:
    return _nf_paths.evidence_dir()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- export ----------


@router.post(
    "",
    status_code=202,
    response_model=None,
    operation_id="exportEvidence",
    summary="Export a capsule as an Evidence Bundle",
    openapi_extra=request_body(
        EvidenceExportRequest,
        description="The capsule to seal into an Evidence Bundle.",
    ),
    responses={
        202: {
            "model": BundleSummary,
            "description": "Bundle accepted; metadata for polling and download.",
        },
        **error_responses(400, 401, 403, 404, 409),
    },
)
async def export_evidence(
    body: dict[str, Any],
    capsule_dir: Annotated[Path, Depends(get_capsule_dir)] = None,  # type: ignore[assignment]
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    _auth: Annotated[AuthContext, Depends(require_role(Role.writer))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    run_id, capsule_path, out_path = _export_paths(body, capsule_dir)
    allow_unsafe_skips = bool(body.get("allow_unsafe_skips", False))
    bundle_id = str(uuid.uuid4())

    # Check for an existing bundle for this run_id
    with _BUNDLES_LOCK:
        for existing in _BUNDLES.values():
            if existing["run_id"] == run_id and existing.get("bundle_path"):  # pragma: no cover
                return _bundle_summary(existing)

    bundle_meta: dict[str, Any] = {
        "bundle_id": bundle_id,
        "run_id": run_id,
        "created_at": _now(),
        "size_bytes": 0,
        "bundle_path": None,
        "status": "pending",
        "error": None,
    }
    with _BUNDLES_LOCK:
        _BUNDLES[bundle_id] = bundle_meta

    # Build synchronously (evidence bundle creation is fast, not async-safe)
    try:
        from novafabric.evidence.bundle import (
            CapsuleValidationError,
            EvidenceBundleBuilder,
            UnsafeSkipsError,
        )
        from novafabric.evidence.signing import LocalSigner, ensure_signing_key

        key_pem = ensure_signing_key(_nf_paths.local_key_path())
        out_path.parent.mkdir(parents=True, exist_ok=True)
        signer = LocalSigner(key_pem)
        builder = EvidenceBundleBuilder(
            capsule_dir=capsule_path,
            signer=signer,
            output_path=out_path,
            allow_unsafe_skips=allow_unsafe_skips,
        )
        out = builder.build()
        size = out.stat().st_size if out.exists() else 0

        with _BUNDLES_LOCK:
            _BUNDLES[bundle_id]["bundle_path"] = str(out)
            _BUNDLES[bundle_id]["size_bytes"] = size
            _BUNDLES[bundle_id]["status"] = "completed"

    except UnsafeSkipsError as exc:
        with _BUNDLES_LOCK:
            _BUNDLES[bundle_id]["status"] = "failed"
            _BUNDLES[bundle_id]["error"] = str(exc)
        raise ConflictError(str(exc))
    except CapsuleValidationError as exc:
        with _BUNDLES_LOCK:
            _BUNDLES[bundle_id]["status"] = "failed"
            _BUNDLES[bundle_id]["error"] = str(exc)
        from novafabric.server.errors import ValidationError

        raise ValidationError(str(exc))
    except Exception as exc:  # noqa: BLE001
        with _BUNDLES_LOCK:
            _BUNDLES[bundle_id]["status"] = "failed"
            _BUNDLES[bundle_id]["error"] = str(exc)
        raise

    with _BUNDLES_LOCK:
        meta = dict(_BUNDLES[bundle_id])

    return _bundle_summary(meta)


# ---------- get bundle metadata ----------


@router.get(
    "/{bundle_id}",
    response_model=None,
    operation_id="getEvidenceBundle",
    summary="Get evidence-bundle metadata",
    responses={
        200: {"model": BundleSummary, "description": "The bundle metadata."},
        **error_responses(401, 403, 404),
    },
)
async def get_evidence_bundle(
    bundle_id: str,
    _auth: Annotated[AuthContext, Depends(require_role(Role.auditor))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    with _BUNDLES_LOCK:
        meta = _BUNDLES.get(bundle_id)
    if not meta:
        raise NotFoundError(f"Evidence bundle '{bundle_id}' not found.")
    return _bundle_summary(meta)


# ---------- download ----------


@router.get(
    "/{bundle_id}/download",
    response_model=None,
    operation_id="downloadEvidenceBundle",
    summary="Download an evidence bundle ZIP",
    responses={
        200: {
            "content": {"application/zip": {}},
            "description": "The bundle archive.",
        },
        **error_responses(401, 403, 404),
    },
)
async def download_evidence_bundle(
    bundle_id: str,
    _auth: Annotated[AuthContext, Depends(require_role(Role.auditor))] = None,  # type: ignore[assignment]
) -> FileResponse:
    with _BUNDLES_LOCK:
        meta = _BUNDLES.get(bundle_id)
    if not meta:
        raise NotFoundError(f"Evidence bundle '{bundle_id}' not found.")
    bundle_path = meta.get("bundle_path")
    if not bundle_path or not Path(bundle_path).exists():
        raise NotFoundError(f"Bundle file for '{bundle_id}' not found on disk.")
    return FileResponse(
        path=bundle_path,
        media_type="application/zip",
        filename=f"{meta['run_id']}.zip",
    )


# ---------- helpers ----------


def _export_paths(body: dict[str, Any], capsule_dir: Path) -> tuple[str, Path, Path]:
    """Validate an export request; return ``(run_id, capsule_path, bundle_path)``.

    ``run_id`` must be one safe path segment (no ``/``, ``\\``, ``..``, NUL, not
    absolute), so it can only name a capsule directly inside the server's capsule
    directory, and the server alone picks where the bundle is written: the
    evidence directory, ``<run_id>.zip``. A request that names an output path is
    refused (400): a ``writer`` once used it to overwrite any file the server
    could write (review server-01).
    """
    if body.get("output_path") is not None:
        raise BadRequestError(
            "output_path is not accepted: the server writes the bundle to its own "
            "evidence directory and returns the path as bundle_path"
        )
    raw = body.get("run_id")
    if raw is None or raw == "":
        raise BadRequestError("run_id is required")
    try:
        run_id = require_path_segment(raw, "run_id")
        capsule_path = safe_child(capsule_dir, run_id, field="run_id")
        out_path = safe_child(_evidence_dir(), run_id, field="run_id", suffix=".zip")
    except (UnsafePathSegmentError, PathEscapeError) as exc:
        raise BadRequestError(str(exc)) from exc
    if not capsule_path.is_dir() or not (capsule_path / "capsule.yaml").is_file():
        raise NotFoundError(f"Capsule '{run_id}' not found.")
    return run_id, capsule_path, out_path


def _bundle_summary(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "bundle_id": meta["bundle_id"],
        "run_id": meta["run_id"],
        "created_at": meta["created_at"],
        "size_bytes": meta.get("size_bytes", 0),
        "bundle_path": meta.get("bundle_path"),
    }
