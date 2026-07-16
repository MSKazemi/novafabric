"""Capsules resource — /v0/capsules.

Implements:
  GET  /v0/capsules                  list (cursor pagination)
  POST /v0/capsules                  upload capsule ZIP (multipart/form-data)
  GET  /v0/capsules/{run_id}         get capsule detail
  POST /v0/capsules/{run_id}/scores  submit an external score (ADR-0119, experimental)
"""

from __future__ import annotations

import time
import zipfile
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Depends, Query, UploadFile

from novafabric.server.auth import AuthContext
from novafabric.server.deps import get_capsule_dir, get_db_path, get_metadata_store_dep
from novafabric.server.errors import BadRequestError, ConflictError, NotFoundError
from novafabric.server.pagination import clamp_limit, decode_cursor, paginate
from novafabric.server.rbac import Role, require_role

router = APIRouter(prefix="/capsules", tags=["capsules"])

# Matches OrphanManager.pending_parent_timeout_s default (FR-14).
_ORPHAN_TIMEOUT_S: float = 86_400.0


def _is_valid_uuid(s: str) -> bool:
    """Return True if *s* is a parseable UUID string."""
    try:
        import uuid as _u
        _u.UUID(s)
        return True
    except (ValueError, AttributeError):
        return False


# ---------- list ----------


@router.get("", response_model=None)
async def list_capsules(
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = Query(default=None),
    capsule_dir: Annotated[Path, Depends(get_capsule_dir)] = None,  # type: ignore[assignment]
    _auth: Annotated[AuthContext, Depends(require_role(Role.reader))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    summaries = _collect_summaries(capsule_dir)
    offset = decode_cursor(cursor)
    limit = clamp_limit(limit)
    page, next_cursor = paginate(summaries, limit, offset)
    return {
        "items": page,
        "next_cursor": next_cursor,
        "total": len(summaries),
    }


# ---------- upload ----------


@router.post("", status_code=201, response_model=None)
async def upload_capsule(
    capsule: UploadFile,
    capsule_dir: Annotated[Path, Depends(get_capsule_dir)] = None,  # type: ignore[assignment]
    _auth: Annotated[AuthContext, Depends(require_role(Role.writer))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Accept a capsule ZIP, unpack it under the capsule_dir."""
    content = await capsule.read()
    if not content:
        raise BadRequestError("Capsule file is empty.")

    import io

    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise BadRequestError("Uploaded file is not a valid ZIP archive.")

    # Peek at capsule.yaml to extract run_id
    names = zf.namelist()
    yaml_candidates = [n for n in names if n.endswith("capsule.yaml")]
    if not yaml_candidates:
        raise BadRequestError("ZIP does not contain a capsule.yaml file.")

    manifest_bytes = zf.read(yaml_candidates[0])
    try:
        manifest = yaml.safe_load(manifest_bytes)
    except yaml.YAMLError as exc:
        raise BadRequestError(f"capsule.yaml is invalid YAML: {exc}")

    run_id = manifest.get("run_id")
    if not run_id:
        raise BadRequestError("capsule.yaml is missing run_id.")

    # Security: reject child capsules whose parent doesn't yet exist in the store,
    # provided the orphan timeout window hasn't elapsed.  This prevents lineage
    # injection — an attacker cannot forge a parent_run_id that was never uploaded.
    parent_run_id = manifest.get("parent_run_id")
    if parent_run_id:
        parent_dest = capsule_dir / str(parent_run_id)
        parent_exists = parent_dest.is_dir() and (parent_dest / "capsule.yaml").exists()
        if not parent_exists:
            # Allow the upload only if the orphan timeout has already elapsed
            # (the child is old enough that the parent is presumed lost).
            within_window = True
            created_at_str = manifest.get("created_at", "")
            if created_at_str:
                try:
                    from datetime import datetime
                    created_epoch = datetime.fromisoformat(
                        str(created_at_str).replace("Z", "+00:00")
                    ).timestamp()
                    within_window = (time.time() - created_epoch) < _ORPHAN_TIMEOUT_S
                except Exception:
                    within_window = True  # unparseable timestamp → treat as fresh
            if within_window:
                raise ConflictError(
                    f"Parent capsule '{parent_run_id}' does not exist. "
                    "Upload the parent capsule first, or wait for the orphan "
                    "timeout window to elapse before uploading this child.",
                    code="parent_not_found",
                )

    dest = capsule_dir / run_id
    if dest.exists():
        raise ConflictError(f"Capsule '{run_id}' already exists.")

    dest.mkdir(parents=True)
    # Extract only into the destination directory (path traversal safe)
    for member in zf.infolist():
        # Strip leading path component (the zip might be flat or have a subdir)
        parts = Path(member.filename).parts
        # Skip directory entries
        if member.filename.endswith("/"):
            continue
        # Use only the basename for flat zips, or strip top-level dir
        if len(parts) > 1:
            rel = Path(*parts[1:])
        else:
            rel = Path(parts[0])
        out_path = dest / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(zf.read(member.filename))

    # Index the capsule in MetadataStore (best-effort — never blocks the upload).
    import uuid as _uuid

    _raw_tenant_id = getattr(_auth, "tenant_id", None)
    if _raw_tenant_id is not None:
        try:
            _ms = get_metadata_store_dep()
            _run_uuid = _uuid.UUID(run_id) if _is_valid_uuid(run_id) else _uuid.uuid4()
            _tenant_id = _uuid.UUID(str(_raw_tenant_id))
            with _ms.begin_tenant_context(_tenant_id):
                _ms.register_run(
                    run_id=_run_uuid,
                    tenant_id=_tenant_id,
                    event_type="capsule.upload",
                )
        except Exception:  # noqa: BLE001
            pass  # MetadataStore indexing is best-effort; the capsule store is source of truth

    return _capsule_summary(manifest)


# ---------- get ----------


@router.get("/{run_id}", response_model=None)
async def get_capsule(
    run_id: str,
    capsule_dir: Annotated[Path, Depends(get_capsule_dir)] = None,  # type: ignore[assignment]
    _auth: Annotated[AuthContext, Depends(require_role(Role.reader))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    cdir = _resolve(run_id, capsule_dir)
    manifest = _load_manifest(cdir)
    detail = _capsule_summary(manifest)
    detail["schema_version"] = manifest.get("schema_version")
    detail["novafabric_version"] = manifest.get("novafabric_version")
    detail["model_call_count"] = manifest.get("model_call_count", 0)
    detail["tool_call_count"] = manifest.get("tool_call_count", 0)
    detail["mutating_tool_count"] = manifest.get("mutating_tool_count", 0)
    detail["capture_mode"] = manifest.get("capture_mode")
    return detail


# ---------- external score submission (ADR-0119, experimental) ----------


@router.post("/{run_id}/scores", status_code=201, response_model=None)
async def submit_score(
    run_id: str,
    body: dict[str, Any],
    capsule_dir: Annotated[Path, Depends(get_capsule_dir)] = None,  # type: ignore[assignment]
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    auth: Annotated[AuthContext, Depends(require_role(Role.writer))] = None,  # type: ignore[assignment]
) -> Any:
    """Append one externally-computed score to the capsule's ``scores.jsonl``.

    ADR-0119 D2: the same validation core as the local SDK/CLI, exposed over the
    v0.7 REST API. Append-only and fail-closed (a rejection writes nothing);
    idempotent by ``score_id`` (identical replay → 200, no second line). The
    ``scores:write`` capability maps to the ``writer`` role in the shipped RBAC
    model; the authenticated principal is recorded in the ``submission`` block so
    *who submitted* stays distinguishable from *what evaluator produced the value*.
    """
    from datetime import datetime, timezone

    from fastapi.responses import JSONResponse
    from pydantic import ValidationError as _PydanticValidationError

    from novafabric.eval.score_config import ScoreConfigViolation
    from novafabric.eval.score_submission import (
        CapsuleNotFoundError,
        IdempotencyConflictError,
        ScoreSubmissionRequest,
        SubjectNotFoundError,
        SubmissionInvalidError,
        SupersedesNotFoundError,
        submit_request,
    )
    from novafabric.serve import audit
    from novafabric.server.errors import ValidationError

    def _audit(result_str: str, error: str | None = None) -> None:
        # Durable audit record (THREAT_MODEL R-4): the response body alone is
        # not evidence — mirror the serve-app route and the roles endpoints.
        audit.append(
            action="score_submit",
            args={"run_id": run_id},
            cli_equivalent=f"nova score submit {run_id} …",
            actor_token_fp=auth.subject,
            result=result_str,
            error=error,
        )

    cdir = _resolve(run_id, capsule_dir)
    try:
        request = ScoreSubmissionRequest.model_validate(body)
    except _PydanticValidationError as exc:
        _audit("error", f"malformed: {exc}")
        raise BadRequestError(f"malformed score submission: {exc}")

    try:
        result = submit_request(cdir, request, db_path=db_path)
    except SubmissionInvalidError as exc:
        _audit("error", str(exc))
        raise BadRequestError(str(exc))
    except (CapsuleNotFoundError, SubjectNotFoundError) as exc:
        _audit("error", str(exc))
        raise NotFoundError(str(exc))
    except IdempotencyConflictError as exc:
        _audit("error", str(exc))
        raise ConflictError(str(exc), code="idempotency_conflict")
    except SupersedesNotFoundError as exc:
        _audit("error", str(exc))
        raise ValidationError(str(exc), code="supersedes_not_found")
    except ScoreConfigViolation as exc:
        _audit("error", str(exc))
        raise ValidationError(str(exc), code="score_config_violation")
    _audit("ok")

    import json as _json

    return JSONResponse(
        status_code=200 if result.idempotent_replay else 201,
        content={
            "score": _json.loads(result.score.model_dump_json(exclude_none=True)),
            "idempotent_replay": result.idempotent_replay,
            "config_bound": result.config_bound,
            "submission": {
                "principal": auth.subject,
                "scope": "scores:write",
                "received_at": datetime.now(timezone.utc).isoformat(),
            },
        },
    )


# ---------- helpers ----------


def _load_manifest(cdir: Path) -> dict[str, Any]:
    path = cdir / "capsule.yaml"
    if not path.exists():
        raise NotFoundError(f"capsule.yaml not found in {cdir}")
    raw = yaml.safe_load(path.read_text())
    return dict(raw) if raw else {}


def _capsule_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": manifest.get("run_id"),
        "status": manifest.get("status", "unknown"),
        "created_at": manifest.get("created_at"),
        "finished_at": manifest.get("finished_at"),
        "duration_ms": manifest.get("duration_ms"),
        "command": manifest.get("command"),
        "exit_code": manifest.get("exit_code"),
    }


def _collect_summaries(capsule_dir: Path) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    if not capsule_dir.exists():
        return summaries
    for entry in sorted(capsule_dir.iterdir(), key=lambda p: p.name, reverse=True):
        if not entry.is_dir():
            continue
        yaml_path = entry / "capsule.yaml"
        if not yaml_path.exists():
            continue
        try:
            manifest = yaml.safe_load(yaml_path.read_text()) or {}
            summaries.append(_capsule_summary(manifest))
        except Exception:  # noqa: BLE001
            pass
    return summaries


def _resolve(run_id: str, capsule_dir: Path) -> Path:
    if "/" in run_id or ".." in run_id:
        raise BadRequestError("invalid run_id")
    candidate = capsule_dir / run_id
    if candidate.is_dir() and (candidate / "capsule.yaml").exists():
        return candidate
    raise NotFoundError(f"Capsule '{run_id}' not found.")
