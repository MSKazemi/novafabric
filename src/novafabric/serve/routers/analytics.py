"""Analytics summary route group (dashboard analytics slice, ADR-0183 pattern).

Serves pre-aggregated, time-bucketed run metrics computed from the
``runs_cache`` index — run volume, failure counts, duration percentiles,
model/tool-call volume — so dashboard charts consume day buckets, never raw
rows. No capsule scans; one indexed SQL pass per request bounded by the
requested window.

Built by a factory so the caller injects its own auth dependency
(ADR-0183 §3): ``serve`` passes its shared-token ``verify_token`` closure;
``server`` can mount the same routes behind OIDC/RBAC.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from novafabric.serve.http_cache import conditional_json

_FAILED_STATUSES_SQL = "status IS NOT NULL AND status != 'success'"


def _percentile(sorted_values: list[float], q: float) -> float | None:
    """Linear-interpolation percentile on an already-sorted list; None when empty."""
    if not sorted_values:
        return None
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def build_analytics_router(
    verify_token: Callable[..., Any],
    *,
    db_path: Path | None,
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(verify_token)], tags=["analytics"])

    @router.get("/api/analytics/summary")
    async def analytics_summary(
        request: Request,
        since: str | None = Query(
            default=None, description="ISO date lower bound on created_at"
        ),
        until: str | None = Query(
            default=None, description="ISO date upper bound on created_at"
        ),
    ) -> Response:
        from novafabric.registry.runs_cache import ensure_runs_cache  # noqa: PLC0415
        from novafabric.registry.store import get_connection, init_schema  # noqa: PLC0415

        empty: dict[str, Any] = {
            "buckets": [],
            "totals": {
                "run_count": 0,
                "failed_count": 0,
                "model_call_count": 0,
                "tool_call_count": 0,
            },
            "since": since,
            "until": until,
        }
        if db_path is None or not Path(db_path).exists():
            return conditional_json(request, empty, max_age=30)

        from novafabric.registry.runs_cache import (  # noqa: PLC0415
            aggregate_runs_daily,
            durations_by_bucket,
        )

        conn = get_connection(db_path)
        try:
            init_schema(conn)
            ensure_runs_cache(conn)
            agg_rows = aggregate_runs_daily(
                conn,
                since=since,
                until=until,
                failed_predicate=_FAILED_STATUSES_SQL,
            )
            duration_pairs = durations_by_bucket(conn, since=since, until=until)
        finally:
            conn.close()

        durations: dict[str, list[float]] = {}
        for bucket, duration_ms in duration_pairs:
            durations.setdefault(bucket, []).append(duration_ms)

        buckets: list[dict[str, Any]] = []
        totals = {
            "run_count": 0,
            "failed_count": 0,
            "model_call_count": 0,
            "tool_call_count": 0,
        }
        for row in agg_rows:
            bucket_durations = durations.get(row["bucket"], [])
            entry = {
                "bucket": row["bucket"],
                "run_count": row["run_count"],
                "failed_count": row["failed_count"] or 0,
                "model_call_count": row["model_call_count"] or 0,
                "tool_call_count": row["tool_call_count"] or 0,
                "duration_ms_p50": _percentile(bucket_durations, 0.50),
                "duration_ms_p95": _percentile(bucket_durations, 0.95),
                "duration_ms_max": row["duration_ms_max"],
            }
            buckets.append(entry)
            totals["run_count"] += entry["run_count"]
            totals["failed_count"] += entry["failed_count"]
            totals["model_call_count"] += entry["model_call_count"]
            totals["tool_call_count"] += entry["tool_call_count"]

        payload = {"buckets": buckets, "totals": totals, "since": since, "until": until}
        return conditional_json(request, payload, max_age=30)

    return router
