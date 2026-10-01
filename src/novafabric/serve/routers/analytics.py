"""Analytics summary route group (dashboard analytics slice, ADR-0183 pattern).

Serves pre-aggregated, time-bucketed run metrics computed from the
``runs_cache`` index — run volume, failure counts, duration percentiles,
model/tool-call volume — so dashboard charts consume day buckets, never raw
rows. No capsule scans; one indexed SQL pass per request bounded by the
requested window.

Built by a factory so the caller injects its own auth dependency
(ADR-0183 §3): ``serve`` passes its shared-token ``verify_token`` closure;
``server`` can mount the same routes behind OIDC/RBAC.

**ADR-0234 D2 — every response carries an ``aggregate`` verdict.** It refuses,
and ``totals`` is ``null`` (never zeros), when:

* the runs index does not exist (``source_unavailable``);
* the index holds fewer runs than there are capsules on disk
  (``truncated_source``) — a total over a partial index is not a total;
* the caller's view narrows by something this endpoint cannot push into the
  aggregation — a filter-bar expression, a status chip, free-text search
  (``unpushable_filter``) — so the numbers would describe a different
  population than the table beside them.

A measured zero (an empty window over a complete index) stays a computable
``0`` (spec I2). Percentiles are *flagged*, not refused, on a small sample:
each bucket carries ``duration_samples``, and ``notes.small_sample_buckets``
lists buckets under :data:`MIN_PERCENTILE_SAMPLES`. A sample-size condition is
not in ADR-0234's closed set (spec I5), so it is disclosed rather than turned
into a refusal.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from novafabric.serve.aggregates import (
    AggregateCondition,
    AggregateVerdict,
    computable,
    refuse,
)
from novafabric.serve.http_cache import conditional_json

_FAILED_STATUSES_SQL = "status IS NOT NULL AND status != 'success'"

#: Below this many durations a bucket's p95 is flagged as resting on a small
#: sample. It is still the exact percentile of what was measured.
MIN_PERCENTILE_SAMPLES = 20


def _percentile(sorted_values: list[float], q: float) -> float | None:
    """Linear-interpolation percentile on an already-sorted list; None when empty."""
    if not sorted_values:
        return None
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def unpushable_view_refusal(
    *, f: str | None, status: str | None, q: str | None
) -> AggregateVerdict | None:
    """Refuse when the caller's view narrows by something a date-only aggregate
    cannot apply (ADR-0234 D2 first bullet); ``None`` when nothing is unpushable."""
    unpushable = [
        name
        for name, value in (("filter", f), ("status", status), ("search", q))
        if value and value.strip() and not (name == "status" and value == "all")
    ]
    if not unpushable:
        return None
    return refuse(
        AggregateCondition.UNPUSHABLE_FILTER,
        reason=(
            "the run aggregate can only be narrowed by date; the view is also "
            f"narrowed by {', '.join(unpushable)}, so a total here would describe a "
            "different set of runs than the list"
        ),
        remedy=(
            f"clear the {' / '.join(unpushable)} to see aggregates, or read the count "
            "from the list header; for a filtered aggregate use "
            "`nova query --select 'count()' --where …`"
        ),
        unpushable=unpushable,
    )


def build_analytics_router(
    verify_token: Callable[..., Any],
    *,
    db_path: Path | None,
    capsule_dir: Path | None = None,
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(verify_token)], tags=["analytics"])

    # Watermark cache (ADR-0199 rule 3): the aggregate pass reads every row's
    # (bucket, duration) pair — ~O(rows) per request. The dashboard polls the
    # same window every 30s against data that rarely changed, so key the
    # computed payload by a cheap (COUNT(*), MAX(created_at)) watermark and
    # skip the heavy pass when it matches. Bounded size; per-app closure.
    _cache: dict[tuple[str | None, str | None], tuple[tuple[int, str | None], dict[str, Any]]] = {}
    _CACHE_MAX = 32

    def _on_disk() -> int | None:
        """Capsules on disk — a directory listing, no manifest parsing."""
        if capsule_dir is None:
            return None
        from novafabric.serve.capsule_loader import discover_capsule_dirs  # noqa: PLC0415

        return len(discover_capsule_dirs(capsule_dir))

    @router.get("/api/analytics/summary")
    async def analytics_summary(
        request: Request,
        since: str | None = Query(
            default=None, description="ISO date lower bound on created_at"
        ),
        until: str | None = Query(
            default=None, description="ISO date upper bound on created_at"
        ),
        f: str | None = Query(
            default=None,
            description=(
                "The caller's filter-bar text. Not pushable into this aggregate: when "
                "non-empty the response refuses (ADR-0234 D2) rather than summarize a "
                "different population than the caller's table."
            ),
        ),
        status: str | None = Query(
            default=None, description="The caller's status chip; not pushable (see f)."
        ),
        q: str | None = Query(
            default=None, description="The caller's free-text search; not pushable (see f)."
        ),
    ) -> Response:
        from novafabric.registry.runs_cache import ensure_runs_cache  # noqa: PLC0415
        from novafabric.registry.store import get_connection, init_schema  # noqa: PLC0415

        def _refused(verdict: AggregateVerdict) -> Response:
            return conditional_json(
                request,
                {
                    "buckets": [],
                    "totals": None,
                    "since": since,
                    "until": until,
                    "aggregate": verdict.as_dict(),
                },
                max_age=10,
            )

        view_refusal = unpushable_view_refusal(f=f, status=status, q=q)
        if view_refusal is not None:
            return _refused(view_refusal)

        if db_path is None or not Path(db_path).exists():
            if capsule_dir is not None and await asyncio.to_thread(_on_disk) == 0:
                # No index, and nothing on disk to index: zero runs is a measured
                # fact here, not an unavailable one (spec I2).
                empty = {
                    "run_count": 0,
                    "failed_count": 0,
                    "model_call_count": 0,
                    "tool_call_count": 0,
                }
                return conditional_json(
                    request,
                    {
                        "buckets": [],
                        "totals": empty,
                        "since": since,
                        "until": until,
                        "aggregate": computable(empty, source="capsule_dir").as_dict(),
                    },
                    max_age=30,
                )
            return _refused(
                refuse(
                    AggregateCondition.SOURCE_UNAVAILABLE,
                    reason="the runs index (registry database) does not exist yet",
                    remedy=(
                        "start `nova serve` against a capsule directory so the index is "
                        "built, or run Infra → Maintenance → reindex runs"
                    ),
                )
            )

        from novafabric.registry.runs_cache import (  # noqa: PLC0415
            aggregate_runs_daily,
            durations_by_bucket,
        )

        def _compute() -> tuple[dict[str, Any], int]:
            # Whole sqlite lifecycle inside the worker thread (B4).
            conn = get_connection(db_path)
            try:
                init_schema(conn)
                ensure_runs_cache(conn)
                indexed_total = int(conn.execute("SELECT COUNT(*) FROM runs_cache").fetchone()[0])

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
                    return cached[1], indexed_total

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
                    "duration_samples": len(bucket_durations),
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
            return result, indexed_total

        (result, indexed_total), on_disk = await asyncio.gather(
            asyncio.to_thread(_compute), asyncio.to_thread(_on_disk)
        )
        if on_disk is not None and indexed_total < on_disk:
            return _refused(
                refuse(
                    AggregateCondition.TRUNCATED_SOURCE,
                    reason=(
                        f"the runs index holds {indexed_total} of the {on_disk} capsules "
                        "on disk, so any total from it would undercount"
                    ),
                    remedy=(
                        "wait a few seconds for the index to catch up after a capture, "
                        "or rebuild it: Infra → Maintenance → reindex runs "
                        "(POST /api/admin/reindex-runs)"
                    ),
                    indexed=indexed_total,
                    on_disk=on_disk,
                )
            )

        notes: dict[str, Any] = {"source": "runs_cache"}
        small = [
            b["bucket"]
            for b in result["buckets"]
            if 0 < b["duration_samples"] < MIN_PERCENTILE_SAMPLES
        ]
        if small:
            notes["small_sample_buckets"] = small
            notes["min_percentile_samples"] = MIN_PERCENTILE_SAMPLES
        missing_duration = sum(b["run_count"] - b["duration_samples"] for b in result["buckets"])
        if missing_duration:
            # Absent durations are skipped by the percentiles, never counted as 0.
            notes["runs_without_duration"] = missing_duration
        payload = {**result, "aggregate": computable(result["totals"], **notes).as_dict()}
        return conditional_json(request, payload, max_age=30)

    return router
