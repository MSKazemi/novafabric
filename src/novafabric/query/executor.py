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
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from novafabric.query.cache import scan_capsule_dir_cached
from novafabric.query.engine import QueryIndex
from novafabric.query.errors import QueryExecutionError
from novafabric.query.indexer import scan_capsule_dir
from novafabric.query.model import (
    MAX_GROUPS,
    MAX_SCOPE_EXPANSION,
    QUERY_SCHEMA_VERSION,
    Aggregate,
    QueryPlan,
    Scope,
)
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


def _derived_value(agg: Aggregate, row: dict[str, Any]) -> float | None:
    """Evaluate a derived aggregate over an already-computed row (ADR-0236 D4).

    **A ratio with a zero or absent denominator is undefined, and undefined is
    not zero.** "0 errors out of 0 runs" is not a 0% error rate — it is no
    information, and rendering it as 0% invents a reassuring data point that
    never existed. ``None`` is what the rest of this executor already uses for
    "no value" (:func:`_aggregate_values` returns it for an empty group), so a
    consumer that already handles a missing average handles this too, and
    ADR-0234 D2's rule governs how it is displayed.

    The same applies to an absent *numerator*: a group with no cost rows has no
    cost, not zero cost, and dividing an absent thing yields an absent thing.
    """
    numerator = row.get(agg.numerator or "")
    denominator = row.get(agg.denominator or "")
    if numerator is None or denominator is None:
        return None
    try:
        denominator = float(denominator)
    except (TypeError, ValueError):
        return None
    if denominator == 0:
        return None
    try:
        return float(numerator) / denominator
    except (TypeError, ValueError):
        return None


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
                if agg.is_derived:
                    continue  # second pass — operands must exist first
                if agg.func == "count":
                    row[agg.alias] = len(self._run_ids[key])
                else:
                    row[agg.alias] = _aggregate_values(agg.func, bucket.get(agg.alias, []))
            for agg in plan.selects:
                if agg.is_derived:
                    row[agg.alias] = _derived_value(agg, row)
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


# ---------------------------------------------------------------------------
# ADR-0233 — filter scope over the capsule tree
# ---------------------------------------------------------------------------

#: Depth bound for the walk up the parent chain. `CapsuleTreeAssembler` enforces
#: its own depth limit when it assembles a tree, but the *index* is a flat row
#: set that can contain a malformed or partially-written chain, so the walk here
#: needs its own guard: an unbounded walk over a cycle hangs the query.
_MAX_PARENT_WALK: Final[int] = 64


@dataclass(frozen=True)
class ScopeExpansion:
    """The capsules a scope selects, and everything that was not certain about it."""

    run_ids: frozenset[str]
    truncated: bool
    #: ADR-0233 D4 — why the set may be missing capsules that exist. A `tree`
    #: result that looks complete while children are still in flight is a wrong
    #: answer presented confidently, so the reasons travel with the result.
    incomplete_reasons: tuple[str, ...]


#: ``(children_expected, children_arrived, orphan_reason)`` per capsule — the
#: three fields ADR-0233 D4 needs to decide whether a tree is complete.
_Completeness = tuple[int | None, int | None, str | None]


def _tree_maps(
    rows: Iterable[Any],
) -> tuple[dict[str, str | None], dict[str, set[str]], dict[str, _Completeness]]:
    """Build parent/child maps from indexed rows. One pass, not per matching row.

    Built from the *whole* row set rather than from the matches, because `tree`
    scope must return capsules that do **not** match the filter — the siblings
    are the point of asking for the tree.
    """
    parent_of: dict[str, str | None] = {}
    children_of: dict[str, set[str]] = {}
    completeness: dict[str, _Completeness] = {}
    for row in rows:
        run_id = row.run_id
        parent = getattr(row, "parent_run_id", None)
        parent_of.setdefault(run_id, parent)
        completeness.setdefault(
            run_id,
            (
                getattr(row, "children_expected", None),
                getattr(row, "children_arrived", None),
                getattr(row, "orphan_reason", None),
            ),
        )
        if parent:
            children_of.setdefault(parent, set()).add(run_id)
    return parent_of, children_of, completeness


def _root_of(run_id: str, parent_of: dict[str, str | None]) -> str:
    """Walk to the root. A cycle or a chain longer than the bound stops where it is.

    Stopping rather than raising is deliberate: a malformed chain is a property
    of one capsule's recorded metadata, and it should degrade that capsule's
    scope answer, not fail an otherwise valid query over thousands of others.
    """
    seen: set[str] = {run_id}
    current = run_id
    for _ in range(_MAX_PARENT_WALK):
        parent = parent_of.get(current)
        if not parent or parent in seen or parent not in parent_of:
            return current
        seen.add(parent)
        current = parent
    return current


def _expand_scope(plan: QueryPlan, all_rows: Iterable[Any], matched: set[str]) -> ScopeExpansion:
    """Widen a set of matching run ids to the plan's scope (ADR-0233 D1/D3/D4)."""
    if plan.scope is Scope.NODE or not matched:
        return ScopeExpansion(frozenset(matched), truncated=False, incomplete_reasons=())

    parent_of, children_of, completeness = _tree_maps(all_rows)
    roots = {_root_of(run_id, parent_of) for run_id in matched}

    if plan.scope is Scope.ROOT:
        selected = set(roots)
    else:
        # Breadth-first from each root, bounded. Expanding per *matching row*
        # would be quadratic on a wide distributed run; expanding per distinct
        # root visits each capsule once.
        selected = set()
        queue = list(roots)
        while queue and len(selected) < MAX_SCOPE_EXPANSION:
            current = queue.pop()
            if current in selected:
                continue
            selected.add(current)
            queue.extend(children_of.get(current, set()) - selected)

    truncated = len(selected) >= MAX_SCOPE_EXPANSION
    if truncated:
        selected = set(sorted(selected)[:MAX_SCOPE_EXPANSION])

    reasons: list[str] = []
    for run_id in sorted(selected):
        expected, arrived, orphan_reason = completeness.get(run_id, (None, None, None))
        if orphan_reason:
            reasons.append(f"{run_id}: orphan placeholder ({orphan_reason})")
        elif expected is not None and arrived is not None and arrived < expected:
            reasons.append(
                f"{run_id}: {arrived} of {expected} children have arrived, so this "
                "tree is still filling"
            )
    return ScopeExpansion(
        run_ids=frozenset(selected),
        truncated=truncated,
        incomplete_reasons=tuple(reasons),
    )


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
    expansion = ScopeExpansion(frozenset(), truncated=False, incomplete_reasons=())
    try:
        call_rows = index.fetch_calls(plan.where, since_epoch, until_epoch)
        score_aggs = [agg for agg in plan.selects if agg.metric == "score"]
        score_rows = (
            index.fetch_scores(plan.where, since_epoch, until_epoch) if score_aggs else []
        )

        # ADR-0233: widen the matched capsules to the plan's scope, then
        # re-fetch **without** the predicates — `tree` scope must return the
        # siblings that did *not* match, which is the whole point of asking for
        # the tree. `node` short-circuits, so the default path is unchanged.
        if plan.scope is not Scope.NODE:
            matched_ids = {str(r["run_id"]) for r in call_rows} | {
                str(r["run_id"]) for r in score_rows
            }
            expansion = _expand_scope(plan, [*rows.calls, *rows.scores], matched_ids)
            unfiltered_calls = index.fetch_calls((), since_epoch, until_epoch)
            call_rows = [r for r in unfiltered_calls if r["run_id"] in expansion.run_ids]
            if score_aggs:
                unfiltered_scores = index.fetch_scores((), since_epoch, until_epoch)
                score_rows = [
                    r for r in unfiltered_scores if r["run_id"] in expansion.run_ids
                ]
            else:
                score_rows = []

            # A capsule can be in the tree and outside the query's time window.
            # Reporting it is D4's whole point: a tree that renders as complete
            # while some of it was filtered out by `--since` is a wrong answer
            # presented confidently.
            present = {str(r["run_id"]) for r in call_rows} | {
                str(r["run_id"]) for r in score_rows
            }
            outside = sorted(expansion.run_ids - present)
            if outside:
                expansion = ScopeExpansion(
                    run_ids=expansion.run_ids,
                    truncated=expansion.truncated,
                    incomplete_reasons=(
                        *expansion.incomplete_reasons,
                        f"{len(outside)} capsule(s) in the tree fall outside the "
                        "query's time window and are not represented in these rows",
                    ),
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
    truncated = len(result_rows) > plan.limit or expansion.truncated
    result_rows = result_rows[: plan.limit]

    # Emitted only when a scope was actually asked for. Under the default
    # `node` the result is byte-identical to the pre-ADR-0233 shape, which is
    # what keeps this additive for every existing consumer and for the spec's
    # exact-key contract. The distinction is meaningful either way: an **absent**
    # block means no expansion was requested, while a present one with an empty
    # `incomplete_reasons` means the tree was checked and found complete.
    scope_block: dict[str, Any] | None = None
    if plan.scope is not Scope.NODE:
        scope_block = {
            "scope": plan.scope.value,
            "capsules_selected": len(expansion.run_ids),
            "expansion_truncated": expansion.truncated,
            "incomplete_reasons": list(expansion.incomplete_reasons),
            "complete": not expansion.incomplete_reasons and not expansion.truncated,
        }

    return {
        "schema_version": QUERY_SCHEMA_VERSION,
        "generated_at": _iso(now),
        "query": plan.to_query_object(),
        "time_window": {"since": since_iso, "until": until_iso},
        "columns": list(plan.group_by) + [agg.alias for agg in plan.selects],
        "rows": result_rows,
        "row_count": len(result_rows),
        "truncated": truncated,
        **({"tree_scope": scope_block} if scope_block is not None else {}),
        "index": {
            "engine": index.info.engine,
            "built_at": index.info.built_at,
            "capsule_count": index.info.capsule_count,
        },
    }
