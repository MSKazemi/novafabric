"""Query execution — filter, group, aggregate, and canonical JSON (ADR-0129 P3).

The engine filters (parameterized SQL over the derived in-memory index); the
aggregation itself runs here in pure Python so both backends (DuckDB and
stdlib SQLite) produce byte-identical results — including percentiles, which
use linear interpolation on the sorted values.

Aggregation semantics (deterministic, spec-aligned):

- ``count()`` — number of **distinct capsules** (run ids) matched.
- ``sum/avg/min/max/pXX(metric)`` — over the non-null metric values of the
  matched model-call rows (standard SQL null semantics: a capsule without
  the metric is excluded from that aggregate but still counted).
- ``FUNC(score[name])`` — over matched score rows with that name.
- Groups are the union of group keys seen in call rows and score rows; an
  aggregate with no contributing values renders as ``null``.
- Group cardinality is capped (:data:`~novafabric.query.model.MAX_GROUPS`);
  exceeding it refuses the query with guidance, never an unbounded scan.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novafabric.query.cache import scan_capsule_dir_cached
from novafabric.query.engine import QueryIndex
from novafabric.query.errors import QueryExecutionError
from novafabric.query.indexer import scan_capsule_dir
from novafabric.query.model import MAX_GROUPS, QUERY_SCHEMA_VERSION, Aggregate, QueryPlan
from novafabric.query.parser import parse_duration, parse_timestamp

_EPOCH_ISO = "1970-01-01T00:00:00Z"


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_time_window(
    since: str | None, until: str | None, now: datetime
) -> tuple[float | None, float, str, str]:
    """Resolve the raw since/until clause values against *now*.

    Returns ``(since_epoch, until_epoch, since_iso, until_iso)``; an absent
    ``since`` means an unbounded start (rendered as the Unix epoch).
    """
    until_dt = parse_timestamp(until, clause="until") if until is not None else now
    since_epoch: float | None = None
    since_iso = _EPOCH_ISO
    if since is not None:
        duration = parse_duration(since)
        since_dt = now - duration if duration is not None else parse_timestamp(
            since, clause="since"
        )
        since_epoch = since_dt.timestamp()
        since_iso = _iso(since_dt)
    return since_epoch, until_dt.timestamp(), since_iso, _iso(until_dt)


def _percentile(values: list[float], p: int) -> float:
    """Linear-interpolation percentile over sorted values (numpy 'linear')."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (p / 100.0) * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _aggregate_values(func: str, values: list[float]) -> float | None:
    if not values:
        return None
    if func == "sum":
        return sum(values)
    if func == "avg":
        return sum(values) / len(values)
    if func == "min":
        return min(values)
    if func == "max":
        return max(values)
    return _percentile(values, int(func[1:]))  # pXX — validated at parse time


class _Groups:
    """Bounded accumulator of group-key → per-aggregate value lists."""

    def __init__(self, plan: QueryPlan) -> None:
        self._plan = plan
        self._data: dict[tuple[Any, ...], dict[str, list[float]]] = {}
        self._run_ids: dict[tuple[Any, ...], set[str]] = {}

    def _key(self, row: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(row[dim] for dim in self._plan.group_by)

    def _bucket(self, key: tuple[Any, ...]) -> dict[str, list[float]]:
        if key not in self._data:
            if len(self._data) >= MAX_GROUPS:
                raise QueryExecutionError(
                    f"group_by would produce more than {MAX_GROUPS} groups; "
                    "narrow the group_by or add a where filter"
                )
            self._data[key] = {}
            self._run_ids[key] = set()
        return self._data[key]

    def add_call_row(self, row: dict[str, Any], call_aggs: list[Aggregate]) -> None:
        key = self._key(row)
        bucket = self._bucket(key)
        self._run_ids[key].add(str(row["run_id"]))
        for agg in call_aggs:
            value = row.get(agg.metric or "")
            if value is not None:
                bucket.setdefault(agg.alias, []).append(float(value))

    def add_score_row(self, row: dict[str, Any], score_aggs: list[Aggregate]) -> None:
        matching = [agg for agg in score_aggs if agg.score_name == row["name"]]
        if not matching:
            return
        key = self._key(row)
        bucket = self._bucket(key)
        for agg in matching:
            bucket.setdefault(agg.alias, []).append(float(row["value"]))

    def rows(self) -> list[dict[str, Any]]:
        plan = self._plan
        out: list[dict[str, Any]] = []
        for key in sorted(self._data, key=lambda k: tuple(str(v) for v in k)):
            row: dict[str, Any] = dict(zip(plan.group_by, key))
            bucket = self._data[key]
            for agg in plan.selects:
                if agg.func == "count":
                    row[agg.alias] = len(self._run_ids[key])
                else:
                    row[agg.alias] = _aggregate_values(agg.func, bucket.get(agg.alias, []))
            out.append(row)
        return out


def _order_rows(rows: list[dict[str, Any]], plan: QueryPlan) -> list[dict[str, Any]]:
    by = plan.order_by.by
    reverse = plan.order_by.direction == "desc"

    def sort_key(row: dict[str, Any]) -> tuple[int, Any]:
        value = row.get(by)
        if value is None:
            # None sorts after every real value regardless of direction.
            return (1, 0) if not reverse else (-1, 0)
        return (0, value)

    return sorted(rows, key=sort_key, reverse=reverse)


def run_query(
    plan: QueryPlan,
    capsule_dir: str | Path,
    *,
    engine: str | None = None,
    now: datetime | None = None,
    use_cache: bool = True,
    rebuild_cache: bool = False,
) -> dict[str, Any]:
    """Execute a parsed query over a local capsule directory, offline.

    The **capsules** are read-only: they are signed evidence and nothing here
    writes to them. Since ADR-0225 the parsed rows are cached under
    ``NOVAFABRIC_HOME`` so an unchanged capsule is not re-parsed on every query,
    which is where ~92% of the scan time went. Pass ``use_cache=False`` for the
    authoritative full scan, or ``rebuild_cache=True`` to discard and rewrite
    the stored rows. A cache that is missing, stale or damaged costs time, never
    correctness (ADR-0225 D3).

    No server, no network.
    """
    now = now or datetime.now(timezone.utc)
    since_epoch, until_epoch, since_iso, until_iso = resolve_time_window(
        plan.since, plan.until, now
    )
    rows = (
        scan_capsule_dir_cached(capsule_dir, rebuild=rebuild_cache)
        if use_cache
        else scan_capsule_dir(capsule_dir)
    )
    index = QueryIndex.build(rows, engine=engine)
    try:
        call_rows = index.fetch_calls(plan.where, since_epoch, until_epoch)
        score_aggs = [agg for agg in plan.selects if agg.metric == "score"]
        score_rows = (
            index.fetch_scores(plan.where, since_epoch, until_epoch) if score_aggs else []
        )
    finally:
        index.close()

    call_aggs = [agg for agg in plan.selects if agg.metric and agg.metric != "score"]
    groups = _Groups(plan)
    for row in call_rows:
        groups.add_call_row(row, call_aggs)
    for row in score_rows:
        groups.add_score_row(row, score_aggs)

    result_rows = _order_rows(groups.rows(), plan)
    truncated = len(result_rows) > plan.limit
    result_rows = result_rows[: plan.limit]

    return {
        "schema_version": QUERY_SCHEMA_VERSION,
        "generated_at": _iso(now),
        "query": plan.to_query_object(),
        "time_window": {"since": since_iso, "until": until_iso},
        "columns": list(plan.group_by) + [agg.alias for agg in plan.selects],
        "rows": result_rows,
        "row_count": len(result_rows),
        "truncated": truncated,
        "index": {
            "engine": index.info.engine,
            "built_at": index.info.built_at,
            "capsule_count": index.info.capsule_count,
        },
    }
