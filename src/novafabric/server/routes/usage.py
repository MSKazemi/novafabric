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
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from novafabric.server import usage
from novafabric.server.auth import AuthContext
from novafabric.server.config import ServerConfig
from novafabric.server.deps import get_capsule_dir, get_config, get_db_path
from novafabric.server.errors import BadRequestError
from novafabric.server.pagination import clamp_limit, decode_cursor, paginate
from novafabric.server.quotas import measure_capsule_store
from novafabric.server.rbac import Role, require_role

router = APIRouter(prefix="/usage", tags=["usage"])

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

#: Roles that see all workspaces + the global/drift block (spec §RBAC).
_PRIVILEGED_ROLES = frozenset({"admin", "auditor"})


def _member_workspace_slugs(subject: str, db_path: Path | None) -> set[str]:
    """Workspace slugs the principal holds an ADR-0178 membership in.

    Workspace-scoped memberships map directly; org-scoped memberships expand
    to every workspace of that org. Store errors yield the empty set (fail
    closed — the caller then filters everything out).
    """
    try:
        from novafabric.server import workspace_store

        ws_rows = workspace_store.list_workspaces(db_path=db_path)
        by_id = {w["id"]: w for w in ws_rows}
        slugs: set[str] = set()
        for m in workspace_store.list_memberships(
            principal=subject, db_path=db_path
        ):
            if m["scope_type"] == "workspace":
                w = by_id.get(m["scope_id"])
                if w is not None:
                    slugs.add(w["slug"])
            else:  # org scope — every workspace of the org
                slugs.update(
                    w["slug"] for w in ws_rows if w["org_id"] == m["scope_id"]
                )
        return slugs
    except Exception:  # noqa: BLE001 — visibility filter must fail closed
        return set()


@router.get("", response_model=None)
async def get_usage(
    period: str | None = Query(default=None),
    workspace: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = Query(default=None),
    capsule_dir: Annotated[Path, Depends(get_capsule_dir)] = None,  # type: ignore[assignment]
    db_path: Annotated[Path | None, Depends(get_db_path)] = None,
    config: Annotated[ServerConfig, Depends(get_config)] = None,  # type: ignore[assignment]
    auth: Annotated[AuthContext, Depends(require_role(Role.reader))] = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Per-workspace usage for one period (default: current UTC period)."""
    if period is not None and not _PERIOD_RE.match(period):
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
        all_time = usage.all_time_totals(db_path=db_path)
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
