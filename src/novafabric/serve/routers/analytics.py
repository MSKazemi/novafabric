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

from fastapi import APIRouter, Depends, Query

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
        since: str | None = Query(
            default=None, description="ISO date lower bound on created_at"
        ),
        until: str | None = Query(
            default=None, description="ISO date upper bound on created_at"
        ),
    ) -> dict[str, Any]:
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
            return empty

        conn = get_connection(db_path)
        try:
            init_schema(conn)
            ensure_runs_cache(conn)
            where = ["created_at IS NOT NULL"]
            params: list[Any] = []
            if since:
                where.append("created_at >= ?")
                params.append(since)
            if until:
                # Inclusive day upper bound: a bare date must cover the
                # whole day, so compare on the date prefix.
                where.append("substr(created_at, 1, 10) <= ?")
                params.append(until[:10])
            where_sql = " AND ".join(where)

            agg_rows = conn.execute(
                f"""
                SELECT substr(created_at, 1, 10) AS bucket,
                       COUNT(*) AS run_count,
                       SUM(CASE WHEN {_FAILED_STATUSES_SQL} THEN 1 ELSE 0 END)
                           AS failed_count,
                       SUM(COALESCE(model_call_count, 0)) AS model_call_count,
                       SUM(COALESCE(tool_call_count, 0)) AS tool_call_count,
                       MAX(duration_ms) AS duration_ms_max
                FROM runs_cache
                WHERE {where_sql}
                GROUP BY bucket
                ORDER BY bucket
                """,
                params,
            ).fetchall()

            # Durations per bucket for percentiles: one ordered pass, grouped
            # in Python. Bounded by the requested window.
            duration_rows = conn.execute(
                f"""
                SELECT substr(created_at, 1, 10) AS bucket, duration_ms
                FROM runs_cache
                WHERE {where_sql} AND duration_ms IS NOT NULL
                ORDER BY bucket, duration_ms
                """,
                params,
            ).fetchall()
        finally:
            conn.close()

        durations: dict[str, list[float]] = {}
        for row in duration_rows:
            durations.setdefault(row["bucket"], []).append(float(row["duration_ms"]))

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

        return {"buckets": buckets, "totals": totals, "since": since, "until": until}

    return router
