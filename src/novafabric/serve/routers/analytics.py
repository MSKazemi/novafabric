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

    # Watermark cache (ADR-0199 rule 3): the aggregate pass reads every row's
    # (bucket, duration) pair — ~O(rows) per request. The dashboard polls the
    # same window every 30s against data that rarely changed, so key the
    # computed payload by a cheap (COUNT(*), MAX(created_at)) watermark and
    # skip the heavy pass when it matches. Bounded size; per-app closure.
    _cache: dict[tuple[str | None, str | None], tuple[tuple[int, str | None], dict[str, Any]]] = {}
    _CACHE_MAX = 32

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

        def _compute() -> dict[str, Any]:
            # Whole sqlite lifecycle inside the worker thread (B4).
            conn = get_connection(db_path)
            try:
                init_schema(conn)
                ensure_runs_cache(conn)

                # Cheap indexed watermark; on a hit, skip the O(rows) pass.
                where = []
                params: list[str] = []
                if since:
                    where.append("created_at >= ?")
                    params.append(since)
                if until:
                    where.append("created_at <= ?")
                    params.append(until)
                where_sql = f"WHERE {' AND '.join(where)}" if where else ""
                count, max_created = conn.execute(
                    f"SELECT COUNT(*), MAX(created_at) FROM runs_cache {where_sql}",
                    params,
                ).fetchone()
                watermark = (int(count), max_created)
                cached = _cache.get((since, until))
                if cached is not None and cached[0] == watermark:
                    return cached[1]

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

            result = {"buckets": buckets, "totals": totals, "since": since, "until": until}
            if len(_cache) >= _CACHE_MAX:
                _cache.pop(next(iter(_cache)))
            _cache[(since, until)] = (watermark, result)
            return result

        import asyncio  # noqa: PLC0415

        payload = await asyncio.to_thread(_compute)
        return conditional_json(request, payload, max_age=30)

    return router
