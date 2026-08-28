"""Offline score/cost/latency trend reports over local capsules (ADR-0131).

Computes a time- or asset-bucketed series of **one** metric (``cost``,
``score:<name>``, or ``latency``) over a local capsule directory and shapes
it into the canonical ``TrendReport`` JSON (``schemas/trend-report.schema.json``,
spec ``the private design/spec/trend-report-v0.md``). Status: **experimental**.

Design invariants (ADR-0131 D1–D6):

- **Runs on ADR-0129, not a new engine** — capsule extraction is the ADR-0129
  indexer (single definition of each metric), filtering is the ADR-0129
  derived in-memory index with bound parameters, and a saved view (ADR-0130)
  supplies only the ``where`` capsule selector.
- **Gaps are evidence** — a time bucket with no matching capsules is emitted
  with ``value: null`` / ``n: 0``, never dropped.
- **Non-blocking, read-only, offline** — an unreadable capsule, a capsule
  missing the metric, or an unresolvable cost currency is counted in
  ``skipped_count`` (with a warning) and never aborts the report. Nothing is
  written to the capsule directory; no server, no network.
- **Snapshot, not a monitor** — no thresholds, no polling, no notifications
  (that concern is the ADR-0136 budget gate).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from pathlib import Path
from typing import Any

from novafabric.query.engine import QueryIndex
from novafabric.query.errors import QueryIndexError, QueryParseError
from novafabric.query.executor import _aggregate_values, resolve_time_window
from novafabric.query.indexer import CallRow, IndexRows, ScoreRow, scan_capsule
from novafabric.query.model import Predicate
from novafabric.query.parser import validate_query_object
from novafabric.trend.errors import TrendError, TrendUsageError
from novafabric.views.errors import ViewError
from novafabric.views.store import load_view

TREND_SCHEMA_VERSION = "0.1.0"

GROUP_BYS: tuple[str, ...] = ("day", "week", "asset")
LATENCY_STATS: tuple[str, ...] = ("p50", "p95", "p99", "mean")
DEFAULT_SINCE = "30d"
DEFAULT_STAT = "p95"
#: v0 report currency — recorded costs in any other currency are skipped
#: (warned), never silently converted (spec §Edge cases; ADR-0133 has no FX).
REPORT_CURRENCY = "USD"
#: Hard cap on emitted buckets — a pathological window is refused, never an
#: unbounded series (mirrors ADR-0129's MAX_GROUPS stance).
MAX_BUCKETS = 5000
#: Categorical bucket label for capsules that carry no asset dimension.
NO_ASSET_BUCKET = "(none)"

#: Mirrors the ``metric`` pattern in ``schemas/trend-report.schema.json``.
_METRIC_RE = re.compile(r"^(cost|latency|score:[A-Za-z0-9_.\-]+)$")
_STAT_FUNCS = {"p50": "p50", "p95": "p95", "p99": "p99", "mean": "avg"}


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _generator() -> str:
    try:
        return f"nova-trend/{_dist_version('novafabric')}"
    except PackageNotFoundError:  # pragma: no cover - packaging edge
        return "nova-trend/0.0.0"


def _day_start(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)


def _week_start(dt: datetime) -> datetime:
    monday = dt.date() - timedelta(days=dt.isocalendar()[2] - 1)
    return datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)


def _bucket_label(group_by: str, dt: datetime) -> str:
    if group_by == "day":
        return dt.date().isoformat()
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _time_buckets(
    group_by: str, since_epoch: float, until_epoch: float
) -> list[tuple[str, datetime]]:
    """All UTC calendar buckets covering ``[since, until)``, in order."""
    since_dt = datetime.fromtimestamp(since_epoch, tz=timezone.utc)
    cursor = _day_start(since_dt) if group_by == "day" else _week_start(since_dt)
    step = timedelta(days=1) if group_by == "day" else timedelta(days=7)
    buckets: list[tuple[str, datetime]] = []
    while cursor.timestamp() < until_epoch:
        if len(buckets) >= MAX_BUCKETS:
            raise TrendUsageError(
                f"the window spans more than {MAX_BUCKETS} {group_by} buckets; "
                "narrow --since (or bucket by week)"
            )
        buckets.append((_bucket_label(group_by, cursor), cursor))
        cursor += step
    return buckets


def _scan_tolerant(
    base: Path,
    *,
    guard_currency: bool,
    since_epoch: float | None,
    until_epoch: float,
    warnings: list[str],
) -> tuple[IndexRows, int]:
    """Scan every capsule under ``base``, skipping (never aborting on) bad ones.

    Returns ``(rows, skipped)`` where ``skipped`` counts unreadable capsules
    plus — when ``guard_currency`` — in-window capsules whose recorded cost
    currency cannot be normalized to :data:`REPORT_CURRENCY` (ADR-0131 D6).
    """
    if not base.is_dir():
        raise TrendError(f"capsule directory not found: {base}")
    calls: list[CallRow] = []
    scores: list[ScoreRow] = []
    capsule_count = 0
    skipped = 0
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        try:
            scanned = scan_capsule(child)
        except QueryIndexError as exc:
            skipped += 1
            warnings.append(f"skipped unreadable capsule '{child.name}': {exc}")
            continue
        if scanned is None:
            continue
        capsule_count += 1
        capsule_calls, capsule_scores = scanned
        if guard_currency:
            foreign = {
                row.cost_currency or REPORT_CURRENCY
                for row in capsule_calls
                if row.cost is not None
            } - {REPORT_CURRENCY}
            if foreign:
                created = capsule_calls[0].created_at
                if (since_epoch is None or created >= since_epoch) and created < until_epoch:
                    skipped += 1
                    warnings.append(
                        f"skipped capsule '{child.name}': unresolvable cost currency "
                        f"{', '.join(sorted(foreign))} (v0 reports {REPORT_CURRENCY} "
                        "only; values are never silently converted)"
                    )
                continue
        calls.extend(capsule_calls)
        scores.extend(capsule_scores)
    return IndexRows(calls=calls, scores=scores, capsule_count=capsule_count), skipped


def _view_predicates(
    view: str, views_dir: Path | None
) -> tuple[str, tuple[Predicate, ...]]:
    """Resolve a saved view (ADR-0130) into its capsule-selection predicates.

    Only the view's ``where`` clause participates — ``--metric`` /
    ``--group-by`` / ``--since`` still parameterize the aggregation on top of
    that selection (ADR-0131 D3).
    """
    try:
        saved = load_view(view, views_dir)
    except ViewError as exc:
        raise TrendError(str(exc)) from exc
    try:
        plan = validate_query_object(saved.query, source=f"saved view {saved.view_id!r}")
    except QueryParseError as exc:
        raise TrendError(f"saved view {saved.view_id!r} has an invalid query: {exc}") from exc
    return saved.view_id, plan.where


def _reduce(metric: str, stat: str | None, values: list[float]) -> float | None:
    if metric == "cost":
        return _aggregate_values("sum", values)
    if metric == "latency":
        return _aggregate_values(_STAT_FUNCS[stat or DEFAULT_STAT], values)
    return _aggregate_values("avg", values)  # score:<name>


def build_trend_report(
    capsule_dir: str | Path,
    *,
    metric: str,
    group_by: str = "day",
    since: str | None = None,
    until: str | None = None,
    stat: str | None = None,
    view: str | None = None,
    views_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute a ``TrendReport`` over a local capsule directory, offline.

    Read-only end to end. Raises :class:`TrendUsageError` for an invalid
    request (before touching any capsule) and :class:`TrendError` for runtime
    failures (missing directory, unresolvable saved view). Per-capsule
    problems never raise — they are tallied in ``skipped_count`` with a
    warning (ADR-0131 D6).
    """
    if not _METRIC_RE.match(metric):
        raise TrendUsageError(
            f"unknown metric {metric!r}; expected cost, latency, or score:<name>"
        )
    if group_by not in GROUP_BYS:
        raise TrendUsageError(
            f"unknown group-by {group_by!r}; expected one of {', '.join(GROUP_BYS)}"
        )
    if stat is not None and metric != "latency":
        raise TrendUsageError("--stat is only valid with --metric latency")
    if stat is not None and stat not in LATENCY_STATS:
        raise TrendUsageError(
            f"unknown stat {stat!r}; expected one of {', '.join(LATENCY_STATS)}"
        )
    resolved_stat = (stat or DEFAULT_STAT) if metric == "latency" else None

    view_id: str | None = None
    predicates: tuple[Predicate, ...] = ()
    if view is not None:
        view_id, predicates = _view_predicates(view, views_dir)

    now = now or datetime.now(timezone.utc)
    try:
        since_epoch, until_epoch, since_iso, until_iso = resolve_time_window(
            since or DEFAULT_SINCE, until, now
        )
    except QueryParseError as exc:
        raise TrendUsageError(str(exc)) from exc
    time_buckets: list[tuple[str, datetime]] = []
    if group_by != "asset":
        # ``since`` always resolves (DEFAULT_SINCE applies when omitted);
        # a pathological window is refused here, before any capsule is read.
        assert since_epoch is not None
        time_buckets = _time_buckets(group_by, since_epoch, until_epoch)

    warnings: list[str] = []
    rows, skipped = _scan_tolerant(
        Path(capsule_dir),
        guard_currency=metric == "cost",
        since_epoch=since_epoch,
        until_epoch=until_epoch,
        warnings=warnings,
    )
    index = QueryIndex.build(rows)
    try:
        call_rows = index.fetch_calls(predicates, since_epoch, until_epoch)
        score_rows = (
            index.fetch_scores(predicates, since_epoch, until_epoch)
            if metric.startswith("score:")
            else []
        )
    finally:
        index.close()

    # One data point per contributing row: (run_id, created_at, asset, value).
    if metric.startswith("score:"):
        score_name = metric.split(":", 1)[1]
        data = [
            (str(r["run_id"]), float(r["created_at"]), r["asset"], float(r["value"]))
            for r in score_rows
            if r["name"] == score_name
        ]
        missing_label = f"no score {score_name!r} recorded"
    else:
        data = [
            (str(r["run_id"]), float(r["created_at"]), r["asset"], float(r[metric]))
            for r in call_rows
            if r[metric] is not None
        ]
        missing_label = f"no {metric} recorded"

    candidates = {str(r["run_id"]) for r in call_rows}
    contributing = {run_id for run_id, _, _, _ in data}
    missing = candidates - contributing
    if missing:
        skipped += len(missing)
        warnings.append(f"{len(missing)} capsule(s) skipped: {missing_label}")

    series: list[dict[str, Any]] = []
    if group_by == "asset":
        grouped: dict[str, tuple[list[float], set[str]]] = {}
        for run_id, _, asset, value in data:
            label = asset if isinstance(asset, str) and asset else NO_ASSET_BUCKET
            values, run_ids = grouped.setdefault(label, ([], set()))
            values.append(value)
            run_ids.add(run_id)
        for label in sorted(grouped):
            values, run_ids = grouped[label]
            series.append(
                {
                    "bucket": label,
                    "value": _reduce(metric, resolved_stat, values),
                    "n": len(run_ids),
                    "bucket_start": None,
                }
            )
    elif candidates:
        # Spec §Edge cases: with no matching capsules at all the series is
        # empty; otherwise every calendar bucket in the window is emitted,
        # gaps included (absence is evidence).
        bucketed: dict[str, tuple[list[float], set[str]]] = {}
        for run_id, created_at, _, value in data:
            created = datetime.fromtimestamp(created_at, tz=timezone.utc)
            values, run_ids = bucketed.setdefault(
                _bucket_label(group_by, created), ([], set())
            )
            values.append(value)
            run_ids.add(run_id)
        gaps = 0
        for label, start in time_buckets:
            values, run_ids = bucketed.get(label, ([], set()))
            bucket_value = _reduce(metric, resolved_stat, values) if values else None
            if bucket_value is None:
                gaps += 1
            series.append(
                {
                    "bucket": label,
                    "value": bucket_value,
                    "n": len(run_ids),
                    "bucket_start": _iso(start),
                }
            )
        if gaps:
            warnings.append(f"{gaps} gap bucket(s) in window (no matching capsules)")
    if not candidates:
        warnings.append("no capsules matched the window")

    unit: dict[str, Any]
    if metric == "cost":
        unit = {"kind": "currency", "currency": REPORT_CURRENCY}
    elif metric == "latency":
        unit = {"kind": "duration_ms", "stat": resolved_stat}
    else:
        unit = {"kind": "score", "name": metric.split(":", 1)[1]}

    report: dict[str, Any] = {
        "schema_version": TREND_SCHEMA_VERSION,
        "generated_at": _iso(now),
        "generator": _generator(),
        "metric": metric,
        "group_by": group_by,
        "window": {"since": since_iso, "until": until_iso},
        "unit": unit,
        "capsule_count": len(contributing),
        "skipped_count": skipped,
        "series": series,
    }
    if resolved_stat is not None:
        report["stat"] = resolved_stat
    if view_id is not None:
        report["view"] = view_id
    if predicates:
        report["filters"] = {"where": [pred.normalized() for pred in predicates]}
    if warnings:
        report["warnings"] = warnings
    return report
