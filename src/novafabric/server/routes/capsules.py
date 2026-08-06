"""Capsules resource — /v0/capsules.

Implements:
  GET    /v0/capsules                  list (keyset pagination, ADR-0206 experimental)
  POST   /v0/capsules                  upload capsule ZIP (multipart/form-data)
  GET    /v0/capsules/{run_id}         get capsule detail
  DELETE /v0/capsules/{run_id}         governed delete (ADR-0206, experimental)
  POST   /v0/capsules/bulk-delete      bounded bulk delete (ADR-0206, experimental)
  POST   /v0/capsules/{run_id}/scores  submit an external score (ADR-0119, experimental)
"""

from __future__ import annotations

import os
import secrets
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Depends, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field

# Imported eagerly, unlike the rest of this module's novafabric.eval imports:
# the route decorator needs the model at import time to describe the request
# body in the OpenAPI document (ADR-0227).
from novafabric.eval.score_submission import (
    ScoreSubmissionRequest as _ScoreSubmissionRequest,
)
from novafabric.server import capsule_delete, capsule_index, ingest
from novafabric.server.auth import AuthContext
from novafabric.server.config import ServerConfig
from novafabric.server.deps import (
    get_capsule_dir,
    get_config,
    get_db_path,
    get_metadata_store_dep,
)
from novafabric.server.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from novafabric.server.pagination import (
    InvalidCursorError,
    clamp_limit,
    encode_keyset_cursor,
    paginate,
    parse_cursor,
)
from novafabric.server.quotas import enforce_storage_quota
from novafabric.server.rbac import Role, require_role
from novafabric.server.schemas import (
    CapsuleDetail,
    CapsuleListResponse,
    CapsuleSummary,
    ScoreSubmissionResult,
    error_responses,
    request_body,
)

router = APIRouter(prefix="/capsules", tags=["capsules"])

# Matches OrphanManager.pending_parent_timeout_s default (FR-14).
_ORPHAN_TIMEOUT_S: float = 86_400.0


def _notify_webhooks_capsule_created(request: Request, run_id: str) -> None:
    """ADR-0205: non-blocking `capsule.created` fan-out to the webhook registry.

    Fail-safe by construction (ADR-0137 D4 inherited): no dispatcher (feature
    disabled) → no-op; any failure is swallowed — a webhook problem must never
    delay or fail a capsule upload.
    """
    try:
        dispatcher = getattr(request.app.state, "webhook_dispatcher", None)
        if dispatcher is None:
            return
        from novafabric.events.model import (
            EventType,
            LifecycleEvent,
            Subject,
            SubjectKind,
        )

        dispatcher.enqueue_event(
            LifecycleEvent(
                type=EventType.CAPSULE_CREATED,
                subject=Subject(kind=SubjectKind.CAPSULE, ref=run_id),
                payload={"source": "server.upload"},
            )
        )
    except Exception:  # noqa: BLE001 — never blocks or fails the upload
        pass


def _record_usage_capsule_created(
    request: Request, run_id: str, dest: Path, auth: AuthContext | None
) -> None:
    """ADR-0208: meter one published capsule (count + unpacked bytes).

    Fail-safe by construction (spec: metering failures never fail the upload
    — ledger+counter are atomic with each other, best-effort relative to the
    filesystem publish). Inert unless the ADR-0179 master switch AND
    ``server.usage.metering_enabled`` are on. Runs only AFTER the atomic
    rename published the capsule (spec ordering: a crash between publish and
    count undercounts by ≤ 1, caught by drift/reconciliation).
    """
    try:
        from novafabric.server import usage

        config: ServerConfig = request.app.state.config
        if not usage.metering_active(config):
            return
        db_path = Path(config.db_path) if config.db_path else None
        attribution = usage.resolve_attribution(auth, db_path)
        usage.record_capsule_upload(
            run_id=run_id,
            size_bytes=usage.dir_size_bytes(dest),
            attribution=attribution,
            actor=auth.subject if auth is not None else "unknown",
            db_path=db_path,
            rollup_retention_months=config.usage.rollup_retention_months,
            ledger_retention_months=config.usage.ledger_retention_months,
        )
        ws_checker = getattr(request.app.state, "workspace_quota_checker", None)
        if ws_checker is not None:
            ws_checker.invalidate(attribution.workspace)
    except Exception:  # noqa: BLE001 — metering must never fail the upload
        _audit_metering_failure("capsule_upload_metering_failed", run_id)


def _record_usage_capsule_deleted(request: Request, run_id: str, actor: str) -> None:
    """ADR-0208 D3: append negative ledger rows for one deleted capsule.

    Budgets then reflect reclaimed storage. Mirrors the capsule's original
    metered rows; a pre-metering capsule records nothing (attribution would
    be guesswork — spec refuses it; the delete shows up via drift instead).
    Fail-safe: an accounting failure never fails the delete.
    """
    try:
        from novafabric.server import usage

        config: ServerConfig = request.app.state.config
        if not usage.metering_active(config):
            return
        db_path = Path(config.db_path) if config.db_path else None
        workspace = usage.record_capsule_delete(
            run_id=run_id,
            actor=actor,
            db_path=db_path,
            rollup_retention_months=config.usage.rollup_retention_months,
            ledger_retention_months=config.usage.ledger_retention_months,
        )
        if workspace is not None:
            ws_checker = getattr(
                request.app.state, "workspace_quota_checker", None
            )
            if ws_checker is not None:
                ws_checker.invalidate(workspace)
    except Exception:  # noqa: BLE001 — metering must never fail the delete
        _audit_metering_failure("capsule_delete_metering_failed", run_id)


def _audit_metering_failure(action: str, run_id: str) -> None:
    """Spec: metering failures are logged + audited, never raised."""
    import logging

    logging.getLogger(__name__).warning(
        "%s for run_id=%s", action, run_id, exc_info=True
    )
    try:
        _audit_append(
            action=action,
            args={"run_id": run_id},
            cli_equivalent="n/a (server usage metering, ADR-0208)",
            actor_token_fp="usage",
            result="error",
        )
    except Exception:  # noqa: BLE001 — the audit fallback itself never raises
        pass


def _is_valid_uuid(s: str) -> bool:
    """Return True if *s* is a parseable UUID string."""
    try:
        import uuid as _u
        _u.UUID(s)
        return True
    except (ValueError, AttributeError):
        return False


# ---------- list ----------


@router.get(
    "",
    response_model=None,
    operation_id="listCapsules",
    summary="List run capsules",
    responses={
        200: {
            "model": CapsuleListResponse,
            "description": (
                "A page of run capsules. `total` is present on the first page "
                "only — keyset pages omit it by design (ADR-0206)."
            ),
        },
        **error_responses(400, 401, 403),
    },
)
async def list_capsules(
    response: Response,
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = Query(default=None),
    capsule_dir: Annotated[Path, Depends(get_capsule_dir)] = None,  # type: ignore[assignment]
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    config: Annotated[ServerConfig, Depends(get_config)] = None,  # type: ignore[assignment]
    _auth: Annotated[AuthContext, Depends(require_role(Role.reader))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """List capsules — keyset (seek) pagination, ADR-0206 P1 (experimental).

    Sort order is pinned to ``created_at DESC, run_id DESC``; ``next_cursor``
    is an opaque v1 keyset cursor. Legacy ``{"offset": N}`` cursors are still
    served by the old materialized path for one deprecation cycle (ADR-0188)
    and carry a ``Deprecation: true`` response header. A non-empty cursor
    that fails strict decoding is a 400 ``invalid_cursor``.
    """
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
        # Legacy path, unchanged behavior + deprecation signal (ADR-0188).
        summaries = _collect_summaries(capsule_dir)
        page, next_cursor = paginate(summaries, limit, parsed.offset)
        response.headers["Deprecation"] = "true"
        return {"items": page, "next_cursor": next_cursor, "total": len(summaries)}

    from novafabric.registry.runs_cache import query_runs

    conn = capsule_index.open_index(db_path)
    try:
        capsule_index.sync_index(conn, capsule_dir)
        rows, total = query_runs(conn, limit=limit + 1, after=parsed.key)
    finally:
        conn.close()

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        {
            "run_id": r.get("run_id"),
            "status": r.get("status") or "unknown",
            "created_at": r.get("created_at"),
            "finished_at": r.get("finished_at"),
            "duration_ms": r.get("duration_ms"),
            "command": r.get("command"),
            "exit_code": r.get("exit_code"),
        }
        for r in rows
    ]
    next_cursor = (
        encode_keyset_cursor(rows[-1].get("created_at"), str(rows[-1]["run_id"]))
        if has_more
        else None
    )
    body: dict[str, Any] = {"items": items, "next_cursor": next_cursor}
    if parsed.kind == "first":
        # First page keeps `total` (cheap COUNT on the index); v1-cursor
        # pages omit it — an exact total is the O(N) scan keyset eliminates.
        body["total"] = total
    return body


# ---------- upload ----------


@router.post(
    "",
    status_code=201,
    response_model=None,
    operation_id="uploadCapsule",
    summary="Upload a run capsule archive",
    responses={
        201: {"model": CapsuleSummary, "description": "The stored capsule."},
        **error_responses(400, 401, 403, 409, 413),
    },
)
async def upload_capsule(
    request: Request,
    capsule: UploadFile,
    capsule_dir: Annotated[Path, Depends(get_capsule_dir)] = None,  # type: ignore[assignment]
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    config: Annotated[ServerConfig, Depends(get_config)] = None,  # type: ignore[assignment]
    _auth: Annotated[AuthContext, Depends(require_role(Role.writer))] = None,  # type: ignore[assignment]
    # ADR-0179 second slice: storage quota (warn-then-reject) at capsule ingest.
    _quota: Annotated[None, Depends(enforce_storage_quota)] = None,
) -> dict[str, Any]:
    """Accept a capsule ZIP, spool it to disk, unpack it under the capsule_dir.

    ADR-0203 P1 (experimental): the body is size-capped (413
    ``payload_too_large``), streamed to a disk spool in bounded chunks,
    guarded against zip bombs and ``..``-traversal members (422
    ``zip_guard_violation``), extracted member-by-member into a temp
    directory, and published atomically via rename — a failed request never
    leaves a partial ``capsule_dir/<run_id>``.
    """
    limits = config.ingest

    # Step 1 — Content-Length fast path (no body read on rejection).
    ingest.check_content_length(request.headers.get("content-length"), limits)

    # Step 2 — chunked spool to disk; authoritative size enforcement.
    spool_dir = capsule_dir / ingest.SPOOL_DIR_NAME
    spool_path, received = await ingest.spool_upload(capsule, spool_dir, limits)

    tmp_root: Path | None = None
    try:
        if received == 0:
            raise BadRequestError("Capsule file is empty.")

        # Step 3 — open from disk; central-directory prechecks.
        try:
            zf = zipfile.ZipFile(spool_path)
        except zipfile.BadZipFile:
            raise BadRequestError("Uploaded file is not a valid ZIP archive.")
        with zf:
            ingest.precheck_central_directory(zf, limits)

            # Step 4 — manifest peek (guarded read) + unchanged checks.
            names = zf.namelist()
            yaml_candidates = [n for n in names if n.endswith("capsule.yaml")]
            if not yaml_candidates:
                raise BadRequestError("ZIP does not contain a capsule.yaml file.")

            manifest_bytes = ingest.read_member_guarded(
                zf, yaml_candidates[0], limits
            )
            try:
                manifest = yaml.safe_load(manifest_bytes)
            except yaml.YAMLError as exc:
                raise BadRequestError(f"capsule.yaml is invalid YAML: {exc}")

            run_id = manifest.get("run_id")
            if not run_id:
                raise BadRequestError("capsule.yaml is missing run_id.")
            run_id = str(run_id)
            if "/" in run_id or "\\" in run_id or ".." in run_id:
                # The run_id becomes a path component of the capsule store —
                # never let it traverse (mirrors the read-route guard).
                raise BadRequestError("capsule.yaml has an invalid run_id.")

            _check_parent_exists(manifest, capsule_dir)

            dest = capsule_dir / run_id
            if dest.exists():
                raise ConflictError(f"Capsule '{run_id}' already exists.")

            # Step 5 — streamed, guarded extraction into a temp dir.
            tmp_root = spool_dir / f"{run_id}.{secrets.token_hex(8)}"
            tmp_root.mkdir(parents=True)
            ingest.extract_archive(zf, tmp_root, limits)

        # Step 6 — atomic publish (re-check the duplicate right before the
        # rename; TOCTOU narrowing).
        if dest.exists():
            raise ConflictError(f"Capsule '{run_id}' already exists.")
        os.rename(tmp_root, dest)
        tmp_root = None
    finally:
        # Step 7 — cleanup invariant: spool + temp dir go away on every exit
        # path; a crashed request never leaves a partial capsule_dir/<run_id>.
        spool_path.unlink(missing_ok=True)
        if tmp_root is not None:
            shutil.rmtree(tmp_root, ignore_errors=True)

    # Step 8 — indexing and the success response are unchanged.
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

    # ADR-0206 P1: upsert the runs-cache row (keyset-pagination index) at
    # upload time. Best-effort — the index is derived and rebuildable; the
    # lazy list-time backfill repairs any miss.
    try:
        _conn = capsule_index.open_index(db_path)
        try:
            capsule_index.upsert_capsule(_conn, capsule_dir, run_id, manifest)
        finally:
            _conn.close()
    except Exception:  # noqa: BLE001 — never blocks the upload
        pass

    # ADR-0208 (experimental): usage metering — count + unpacked bytes,
    # attributed at ingest. Fail-safe; inert unless rate_limits.enabled AND
    # usage.metering_enabled. Duplicate-safe: the 409 above plus the ledger's
    # (metric, ref) unique index make a retried upload count exactly once.
    _record_usage_capsule_created(request, str(run_id), dest, _auth)

    # ADR-0205 (experimental): notify registered webhooks — non-blocking,
    # fail-safe, inert unless server.webhooks.enabled.
    _notify_webhooks_capsule_created(request, str(run_id))

    return _capsule_summary(manifest)


# ---------- get ----------


@router.get(
    "/{run_id}",
    response_model=None,
    operation_id="getCapsule",
    summary="Get a run capsule by run_id",
    responses={
        200: {"model": CapsuleDetail, "description": "The run capsule."},
        **error_responses(401, 403, 404),
    },
)
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


# ---------- delete + bulk delete (ADR-0206, experimental) ----------


def _audit_append(**kwargs: Any) -> None:
    """Durable audit record (deletion is evidence — ADR-0134)."""
    from novafabric.serve import audit

    audit.append(**kwargs)


@router.delete("/{run_id}", response_model=None)
async def delete_capsule(
    request: Request,
    run_id: str,
    capsule_dir: Annotated[Path, Depends(get_capsule_dir)] = None,  # type: ignore[assignment]
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Delete one capsule — admin-only, hold-refusing, audited (ADR-0206 D2).

    Legal holds always win: any unreleased hold in any registry refuses with
    409 ``legal_hold_active`` (holds are registry-global today — documented
    limit), and there is no force override. An unexpired WORM lock refuses
    with 409 ``worm_hold``. Deleting an already-deleted id is a 404 (delete
    is evidenced, not idempotent-silent).
    """
    if not capsule_delete.is_valid_run_id(run_id):
        raise BadRequestError("invalid run_id")
    if not capsule_delete.capsule_exists(capsule_dir, run_id):
        raise NotFoundError(f"Capsule '{run_id}' not found.")
    try:
        capsule_delete.check_deletable(capsule_dir, run_id)
    except capsule_delete.DeleteBlockedError as exc:
        _audit_append(
            action="capsule_delete_refused",
            args={"run_id": run_id, "code": exc.code, **exc.details},
            cli_equivalent=f"nova capsule delete {run_id}",
            actor_token_fp=auth.subject,
            result="refused",
            error=str(exc),
        )
        raise ConflictError(str(exc), code=exc.code, details=exc.details)

    conn = capsule_index.open_index(db_path)
    try:
        capsule_delete.execute_delete(capsule_dir, run_id, conn)
    finally:
        conn.close()
    # ADR-0208 D3 (experimental): negative usage-ledger rows so workspace
    # budgets reflect the reclaimed storage. Fail-safe, never fails the delete.
    _record_usage_capsule_deleted(request, run_id, auth.subject)
    _audit_append(
        action="capsule_delete",
        args={"run_id": run_id, "via": "api"},
        cli_equivalent=f"nova capsule delete {run_id}",
        actor_token_fp=auth.subject,
    )
    return {"ok": True, "run_id": run_id, "audit": {"action": "capsule_delete"}}


class BulkDeleteRequest(BaseModel):
    """Body of ``POST /v0/capsules/bulk-delete`` (spec bulk-ops-pagination-v0)."""

    run_ids: list[str] = Field(default_factory=list)
    dry_run: bool = False


@router.post("/bulk-delete", response_model=None)
async def bulk_delete_capsules(
    request: Request,
    body: BulkDeleteRequest,
    capsule_dir: Annotated[Path, Depends(get_capsule_dir)] = None,  # type: ignore[assignment]
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    config: Annotated[ServerConfig, Depends(get_config)] = None,  # type: ignore[assignment]
    auth: Annotated[AuthContext, Depends(require_role(Role.admin))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Bounded bulk delete with per-item outcomes (ADR-0206 D3, experimental).

    Batch size is capped by ``server.bulk.max_items`` (default 100, ceiling
    1000) — an oversized batch is a 422 before any work. Each id runs the
    full single-delete pipeline; outcomes are reported per item
    (``deleted | held | not_found | invalid_id | duplicate | error``), with
    no transactional rollback — partial progress is visible, not undone.
    ``dry_run`` returns the identical report while deleting nothing.
    """
    max_items = config.bulk.max_items
    if not body.run_ids:
        raise BadRequestError("run_ids must be a non-empty list")
    if len(body.run_ids) > max_items:
        raise ValidationError(
            f"batch of {len(body.run_ids)} exceeds server.bulk.max_items "
            f"({max_items})",
            code="bulk_batch_too_large",
            details={"limit": max_items, "received": len(body.run_ids)},
        )

    bulk_id = str(uuid.uuid4())
    results: list[dict[str, Any]] = []
    counts = {"deleted": 0, "held": 0, "not_found": 0, "errors": 0,
              "invalid_id": 0, "duplicate": 0}
    seen: set[str] = set()

    conn = capsule_index.open_index(db_path)
    try:
        for run_id in body.run_ids:
            if run_id in seen:
                results.append({"run_id": run_id, "outcome": "duplicate"})
                counts["duplicate"] += 1
                continue
            seen.add(run_id)
            if not capsule_delete.is_valid_run_id(run_id):
                results.append({"run_id": run_id, "outcome": "invalid_id"})
                counts["invalid_id"] += 1
                continue
            if not capsule_delete.capsule_exists(capsule_dir, run_id):
                results.append({"run_id": run_id, "outcome": "not_found"})
                counts["not_found"] += 1
                continue
            try:
                capsule_delete.check_deletable(capsule_dir, run_id)
            except capsule_delete.DeleteBlockedError as exc:
                # Held items are recorded in the summary counts, not one
                # refused audit entry each (spec: keep the log bounded).
                results.append(
                    {"run_id": run_id, "outcome": "held", "code": exc.code}
                )
                counts["held"] += 1
                continue
            if body.dry_run:
                results.append({"run_id": run_id, "outcome": "deleted"})
                counts["deleted"] += 1
                continue
            try:
                capsule_delete.execute_delete(capsule_dir, run_id, conn)
            except Exception as exc:  # noqa: BLE001 — per-item report, no rollback
                results.append({
                    "run_id": run_id, "outcome": "error",
                    "code": "delete_failed", "message": str(exc),
                })
                counts["errors"] += 1
                continue
            results.append({"run_id": run_id, "outcome": "deleted"})
            counts["deleted"] += 1
            # ADR-0208 D3: per-item negative usage rows (fail-safe).
            _record_usage_capsule_deleted(request, run_id, auth.subject)
            _audit_append(
                action="capsule_delete",
                args={"run_id": run_id, "via": "bulk", "bulk_id": bulk_id},
                cli_equivalent=f"nova capsule delete {run_id}",
                actor_token_fp=auth.subject,
            )
    finally:
        conn.close()

    summary = {
        "requested": len(body.run_ids),
        "deleted": counts["deleted"],
        "held": counts["held"],
        "not_found": counts["not_found"],
        "errors": counts["errors"],
        # Additive beyond the spec's six keys so requested == sum(counts).
        "invalid_id": counts["invalid_id"],
        "duplicate": counts["duplicate"],
        "dry_run": body.dry_run,
    }
    _audit_append(
        action="capsule_bulk_delete",
        args={"bulk_id": bulk_id, **summary},
        cli_equivalent="nova capsule delete … (bulk)",
        actor_token_fp=auth.subject,
    )
    return {"results": results, "summary": summary}


# ---------- external score submission (ADR-0119, experimental) ----------


@router.post(
    "/{run_id}/scores",
    status_code=201,
    response_model=None,
    operation_id="submitCapsuleScore",
    summary="Append an externally-computed score to a capsule",
    openapi_extra=request_body(
        _ScoreSubmissionRequest,
        description="The externally-computed evaluation record to append (ADR-0119).",
    ),
    responses={
        200: {
            "model": ScoreSubmissionResult,
            "description": (
                "Idempotent replay — `score_id` was already present with an "
                "identical body, so no second line was appended. The body is "
                "the same as the 201; only the status differs."
            ),
        },
        201: {
            "model": ScoreSubmissionResult,
            "description": "Score appended.",
        },
        **error_responses(400, 401, 403, 404, 409),
    },
)
async def submit_score(
    run_id: str,
    body: dict[str, Any],
    capsule_dir: Annotated[Path, Depends(get_capsule_dir)] = None,  # type: ignore[assignment]
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    auth: Annotated[AuthContext, Depends(require_role(Role.writer))] = None,  # type: ignore[assignment]
    # ADR-0179 second slice: scores append bytes into the capsule store.
    _quota: Annotated[None, Depends(enforce_storage_quota)] = None,
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


def _check_parent_exists(manifest: dict[str, Any], capsule_dir: Path) -> None:
    """Reject child capsules whose parent doesn't yet exist in the store.

    Unchanged ADR-era semantics (lineage-injection guard): the upload is
    allowed only once the orphan timeout window has elapsed (the child is old
    enough that the parent is presumed lost); otherwise 409
    ``parent_not_found``.
    """
    parent_run_id = manifest.get("parent_run_id")
    if not parent_run_id:
        return
    parent_dest = capsule_dir / str(parent_run_id)
    parent_exists = parent_dest.is_dir() and (parent_dest / "capsule.yaml").exists()
    if parent_exists:
        return
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
