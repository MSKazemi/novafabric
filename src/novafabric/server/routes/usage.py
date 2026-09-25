"""Usage reporting resource — GET /v0/usage (ADR-0208 D2, experimental).

Current-period (or ``?period=YYYY-MM``) per-workspace metric totals + org
rollups from the metering ledger (``server/usage.py``), with:

- **RBAC by filtering, not 403** (spec §RBAC): admin/auditor see every
  workspace plus the ``global`` derived figures and the ``drift`` block; any
  other authenticated principal sees only workspaces it holds an ADR-0178
  membership in (workspace-scoped directly, org-scoped via the org's
  workspaces) — application-enforced, honestly labeled;
- ``quota`` blocks only for workspaces with a configured ADR-0208 budget,
  showing the **all-time** metered consumption (the enforcement figure);
- ``drift`` = global derived (``measure_capsule_store``) minus metered sums —
  pre-metering capsules appear only in the derived figures (spec: stated so
  nobody files the discrepancy as a bug).

The route is always mounted (ADR-0205 precedent: registry reads stay
available); with metering off it reports whatever was ever metered — for a
never-enabled deployment, an empty list.

``GET /v0/usage/export`` (ADR-0208 P3, experimental) serves the same
chargeback rows as ``nova server usage export`` (``server/usage_export.py``)
as a CSV/NDJSON attachment, under the same RBAC-by-filtering rule, opening
the registry strictly read-only.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse

from novafabric.server import usage
from novafabric.server.auth import AuthContext, verify_token
from novafabric.server.config import ServerConfig
from novafabric.server.deps import get_capsule_dir, get_config, get_db_path
from novafabric.server.errors import BadRequestError, error_response
from novafabric.server.pagination import clamp_limit, decode_cursor, paginate
from novafabric.server.quotas import measure_capsule_store
from novafabric.server.rbac import Role, require_role
from novafabric.server.usage_export import (
    ExportFormat,
    InvalidPeriodRangeError,
    UsageStoreUnavailableError,
    chargeback_rows,
    iter_render,
    open_usage_db_read_only,
)

router = APIRouter(prefix="/usage", tags=["usage"])

_PERIOD_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])\Z")

#: Characters the Content-Disposition filename may carry besides the fixed
#: prefix/extension — anything else is dropped, whatever validation ran first
#: (defence in depth against header injection, e.g. a raw ``\n``).
_FILENAME_UNSAFE_RE = re.compile(r"[^0-9_-]")

#: Roles that see all workspaces + the global/drift block (spec §RBAC).
_PRIVILEGED_ROLES = frozenset({"admin", "auditor"})

#: Export media types — CSV per RFC 4180 §3 (``header=present``), NDJSON per
#: the de-facto ``application/x-ndjson`` registration.
_EXPORT_MEDIA_TYPES: dict[str, str] = {
    "csv": "text/csv; charset=utf-8; header=present",
    "ndjson": "application/x-ndjson",
}


def _membership_rows_read_only(
    subject: str, db_path: Path | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(workspaces, memberships of *subject*)`` read from the registry ``mode=ro``.

    Unlike ``workspace_store``'s accessors this never runs DDL, so a GET can
    neither create the registry file nor add tables to it. A missing registry
    or missing ``workspaces``/``memberships`` table reads as "no rows".

    Raises:
        UsageStoreUnavailableError: the registry exists but cannot be opened.
        sqlite3.Error: any read failure other than a missing table.
    """
    conn = open_usage_db_read_only(db_path)
    if conn is None:
        return [], []
    try:
        try:
            ws_rows = [dict(r) for r in conn.execute("SELECT id, org_id, slug FROM workspaces")]
            memberships = [
                dict(r)
                for r in conn.execute(
                    "SELECT scope_type, scope_id FROM memberships WHERE principal = ?",
                    (subject,),
                )
            ]
        except sqlite3.Error as exc:
            if "no such table" not in str(exc):
                raise
            return [], []
    finally:
        conn.close()
    return ws_rows, memberships


def _member_workspace_slugs(subject: str, db_path: Path | None) -> set[str]:
    """Workspace slugs the principal holds an ADR-0178 membership in.

    Workspace-scoped memberships map directly; org-scoped memberships expand
    to every workspace of that org. Read strictly read-only (a GET never
    creates or modifies the registry). Store errors yield the empty set (fail
    closed — the caller then filters everything out).
    """
    try:
        ws_rows, memberships = _membership_rows_read_only(subject, db_path)
        by_id = {w["id"]: w for w in ws_rows}
        slugs: set[str] = set()
        for m in memberships:
            if m["scope_type"] == "workspace":
                w = by_id.get(m["scope_id"])
                if w is not None:
                    slugs.add(w["slug"])
            else:  # org scope — every workspace of the org
                slugs.update(w["slug"] for w in ws_rows if w["org_id"] == m["scope_id"])
        return slugs
    except Exception:  # noqa: BLE001 — visibility filter must fail closed
        return set()


def _export_filename(start: str, end: str, fmt: ExportFormat) -> str:
    """``nova-usage-<start>_<end>.<ext>`` with only ``[0-9_-]`` from the inputs."""
    span = _FILENAME_UNSAFE_RE.sub("", f"{start}_{end}")
    return f"nova-usage-{span}.{fmt}"


async def _require_usage_viewer(
    auth: Annotated[AuthContext, Depends(verify_token)],
) -> AuthContext:
    """Reader-or-auditor gate for the usage resources (ADR-0208 D2).

    D2 lets auditors see every workspace, but ``auditor`` is orthogonal to the
    reader < writer < admin hierarchy (ADR-0018), so a plain
    ``require_role(Role.reader)`` would 403 a pure-auditor token before the
    handler's privileged view is reached. Unauthenticated requests still get
    ``verify_token``'s 401; everyone else goes through the unchanged reader
    check (incl. the ADR-0178 scoped-membership fallback).
    """
    if _PRIVILEGED_ROLES & set(auth.roles):
        return auth
    # require_role's factory is typed as returning a sync callable, but the
    # dependency it builds is ``async`` — await it through a cast.
    reader_check = cast(
        "Callable[[AuthContext], Awaitable[AuthContext]]", require_role(Role.reader)
    )
    return await reader_check(auth)


@router.get("", response_model=None)
async def get_usage(
    period: str | None = Query(default=None),
    workspace: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = Query(default=None),
    capsule_dir: Annotated[Path, Depends(get_capsule_dir)] = None,  # type: ignore[assignment]
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    config: Annotated[ServerConfig, Depends(get_config)] = None,  # type: ignore[assignment]
    auth: Annotated[AuthContext, Depends(_require_usage_viewer)] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Per-workspace usage for one period (default: current UTC period)."""
    if period is not None and not _PERIOD_RE.fullmatch(period):
        raise BadRequestError(
            f"invalid period {period!r}: expected YYYY-MM", code="invalid_period"
        )
    period = period or usage.period_for()

    rows = usage.usage_for_period(period, db_path=db_path)
    privileged = bool(_PRIVILEGED_ROLES & set(auth.roles))
    if not privileged:
        visible = _member_workspace_slugs(auth.subject, db_path)
        rows = [r for r in rows if r["workspace"] in visible]
    if workspace is not None:
        rows = [r for r in rows if r["workspace"] == workspace]

    # Quota blocks — only for workspaces with a configured ADR-0208 budget,
    # only while the master switch is on; usage shown is the all-time
    # enforcement figure, not the period figure (spec §Endpoint contract).
    rl = config.rate_limits
    budgets = (
        rl.quota.workspaces if (rl.enabled and rl.quota is not None) else {}
    )
    if budgets:
        all_time = usage.all_time_totals(db_path=db_path)
        for r in rows:
            budget = budgets.get(r["workspace"])
            if budget is None or not budget.any_limit:
                continue
            totals = all_time.get(r["workspace"], {})
            r["quota"] = {
                "capsules": {
                    "usage": int(totals.get(usage.METRIC_CAPSULES, 0)),
                    "soft": budget.max_capsules_soft,
                    "hard": budget.max_capsules_hard,
                },
                "bytes": {
                    "usage": int(totals.get(usage.METRIC_BYTES, 0)),
                    "soft": budget.max_bytes_soft,
                    "hard": budget.max_bytes_hard,
                },
            }

    # Org rollups — sums over the *visible* workspace rows (a filtered view
    # must not leak other teams' totals through its org aggregate).
    orgs: dict[str, dict[str, int]] = {}
    for r in rows:
        agg = orgs.setdefault(r["org"], dict.fromkeys(usage.REPORTED_METRICS, 0))
        for metric, value in r["metrics"].items():
            agg[metric] += value

    limit = clamp_limit(limit)
    offset = decode_cursor(cursor)
    page, next_cursor = paginate(rows, limit, offset)

    body: dict[str, Any] = {
        "period": period,
        "workspaces": page,
        "orgs": [
            {"org": org, "metrics": metrics}
            for org, metrics in sorted(orgs.items())
        ],
        "next_cursor": next_cursor,
    }
    if privileged:
        derived = measure_capsule_store(capsule_dir)
        # Lifetime (pruned-carry included), not the rolling enforcement window:
        # the store side counts every capsule ever kept, so the metered side
        # must too, or each rollup prune would surface as spurious drift.
        all_time = usage.lifetime_totals(db_path=db_path)
        metered_capsules = sum(
            t.get(usage.METRIC_CAPSULES, 0) for t in all_time.values()
        )
        metered_bytes = sum(
            t.get(usage.METRIC_BYTES, 0) for t in all_time.values()
        )
        body["global"] = {
            "capsules": derived.capsules,
            "total_bytes": derived.total_bytes,
            "source": "measure_capsule_store",
        }
        body["drift"] = {
            "capsules": derived.capsules - metered_capsules,
            "bytes": derived.total_bytes - metered_bytes,
            "note": (
                "global derived minus metered; includes pre-metering capsules"
            ),
        }
    return body


@router.get(
    "/export",
    response_model=None,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Chargeback rows as an attachment (RFC 4180 CSV or NDJSON).",
            "content": {
                "text/csv": {"schema": {"type": "string"}},
                "application/x-ndjson": {"schema": {"type": "string"}},
            },
        },
        400: {"description": "Invalid period, period range or format (error envelope)."},
        503: {
            "description": "Registry exists but could not be read, e.g. locked (error envelope)."
        },
    },
)
async def export_usage(
    period_from: str | None = Query(
        default=None,
        alias="from",
        description="First period YYYY-MM (default: current UTC period).",
    ),
    period_to: str | None = Query(
        default=None,
        alias="to",
        description="Last period YYYY-MM, inclusive (default: `from`). At most 120 periods.",
    ),
    fmt: str = Query(default="csv", alias="format", description="`csv` or `ndjson`."),
    workspace: str | None = Query(default=None, description="Only this workspace slug."),
    org: str | None = Query(default=None, description="Only this org slug."),
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    auth: Annotated[AuthContext, Depends(_require_usage_viewer)] = None,  # type: ignore[assignment]
) -> StreamingResponse | JSONResponse:
    """Chargeback export of per-workspace usage as CSV or NDJSON (ADR-0208 P3).

    Same rows, ordering, formula-injection-safe cells and 120-period bound as
    ``nova server usage export``: finalized periods from rollups (``final``),
    others from live counters (``provisional``). Admin/auditor see every
    workspace; any other principal only its ADR-0178 membership workspaces
    (filtering, not 403). Read-only: the registry is opened ``mode=ro``.
    """
    if fmt not in _EXPORT_MEDIA_TYPES:
        raise BadRequestError(
            f"invalid format {fmt!r}: expected csv or ndjson", code="invalid_format"
        )
    for value in (period_from, period_to):
        if value is not None and not _PERIOD_RE.fullmatch(value):
            raise BadRequestError(
                f"invalid period {value!r}: expected YYYY-MM", code="invalid_period"
            )
    start = period_from or usage.period_for()
    end = period_to or start
    try:
        rows = chargeback_rows(
            start, end, db_path=db_path, workspace=workspace, org=org, read_only=True
        )
    except InvalidPeriodRangeError as exc:  # inverted or over-wide range
        raise BadRequestError(str(exc), code="invalid_period_range") from exc
    except UsageStoreUnavailableError:
        # Never an empty 200: an empty billing file would read as "no usage".
        return error_response(
            503,
            "usage_store_unavailable",
            "the usage registry could not be read; retry later",
        )

    if not (_PRIVILEGED_ROLES & set(auth.roles)):
        visible = _member_workspace_slugs(auth.subject, db_path)
        rows = [r for r in rows if r.workspace in visible]

    export_fmt: ExportFormat = "csv" if fmt == "csv" else "ndjson"
    # Periods are validated above; the filename is still reduced to [0-9_-]
    # so no future validation gap can inject into the header.
    filename = _export_filename(start, end, export_fmt)
    return StreamingResponse(
        (chunk.encode("utf-8") for chunk in iter_render(rows, export_fmt)),
        media_type=_EXPORT_MEDIA_TYPES[export_fmt],
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
